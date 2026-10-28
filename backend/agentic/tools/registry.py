"""Phase 1B — the controlled tool registry.

Controls enforced here:
  * **Closed set** — only ``ToolName`` members can be registered, each once;
    the mapping is read-only after construction (no ``register`` method).
  * **No dynamic dispatch** — a tool is looked up by exact enum value. There
    is no ``eval``/``exec``/``getattr``/import-by-name, so a string such as
    ``"os.system"`` is simply an unknown tool.
  * **Validated input** — payloads are parsed by the tool's strict input
    model (unknown fields rejected) before any handler runs.
  * **Typed output** — a handler must return exactly its declared output model.
  * **Contained failure** — handler exceptions become a ``ToolResult`` with a
    typed error; they never propagate to the caller.
"""
from __future__ import annotations

import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from pydantic import BaseModel, ValidationError

from backend.agentic.common import ToolName
from backend.agentic.tools.base import ToolError, ToolErrorCode, ToolResult, ToolSpec
from backend.core.logging import get_logger

logger = get_logger(__name__)

_MAX_ERROR_CHARS = 300


class UnknownToolError(KeyError):
    """Raised by ``ToolRegistry.get`` for a name that is not registered."""


def _validation_details(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        by_name: dict[ToolName, ToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, ToolSpec):
                raise TypeError("ToolRegistry only accepts ToolSpec instances")
            if spec.name in by_name:
                raise ValueError(f"duplicate tool registration: {spec.name.value}")
            by_name[spec.name] = spec
        self._specs: Mapping[ToolName, ToolSpec] = MappingProxyType(by_name)

    # ── introspection ────────────────────────────────────────────────────────
    def names(self) -> list[ToolName]:
        return list(self._specs)

    def get(self, name: str | ToolName) -> ToolSpec:
        try:
            return self._specs[ToolName(name)]
        except (ValueError, KeyError):
            raise UnknownToolError(str(name)[:80]) from None

    def describe(self) -> list[dict[str, Any]]:
        return [spec.describe() for spec in self._specs.values()]

    # ── controlled execution ─────────────────────────────────────────────────
    def execute(self, name: str | ToolName, payload: Optional[Mapping[str, Any] | BaseModel] = None) -> ToolResult:
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        def fail(label: str, code: ToolErrorCode, message: str, details: Optional[list[str]] = None) -> ToolResult:
            return ToolResult(
                tool=label,
                success=False,
                error=ToolError(code=code, message=message[:_MAX_ERROR_CHARS], details=details or []),
                duration_ms=elapsed_ms(),
            )

        try:
            spec = self.get(name)
        except UnknownToolError:
            return fail(str(name)[:80], ToolErrorCode.UNKNOWN_TOOL, "No such tool is registered.")
        label = spec.name.value

        try:
            if isinstance(payload, spec.input_model):
                tool_input = payload
            elif isinstance(payload, Mapping):
                tool_input = spec.input_model.model_validate(dict(payload))
            elif payload is None:
                tool_input = spec.input_model.model_validate({})
            else:
                return fail(label, ToolErrorCode.INVALID_INPUT, "Input must be a mapping or the tool's input model.")
        except ValidationError as exc:
            return fail(label, ToolErrorCode.INVALID_INPUT, "Input failed validation.", _validation_details(exc))

        try:
            output = spec.handler(tool_input)
        except Exception as exc:  # noqa: BLE001 — contain every handler failure
            logger.error("Tool execution failed", extra={"tool": label, "error": str(exc), "error_type": type(exc).__name__})
            return fail(label, ToolErrorCode.EXECUTION_FAILED, f"{type(exc).__name__}: {exc}")

        if not isinstance(output, spec.output_model):
            logger.error("Tool returned wrong output type", extra={"tool": label, "got": type(output).__name__})
            return fail(label, ToolErrorCode.INVALID_OUTPUT, f"Handler did not return {spec.output_model.__name__}.")

        logger.info("Tool executed", extra={"tool": label, "duration_ms": elapsed_ms()})
        return ToolResult(tool=label, success=True, output=output, duration_ms=elapsed_ms())
