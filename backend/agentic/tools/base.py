"""Phase 1B — tool spec and the execution envelope.

A ``ToolSpec`` binds one ``ToolName`` to an input model, an output model and
a handler that *our own code* supplies. Callers (including a future agent)
only ever provide a tool name and a plain data payload — never code.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, SerializeAsAny, model_validator

from backend.agentic.common import ToolName
from backend.models.common import BaseSchema


class ToolErrorCode(str, Enum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_INPUT = "invalid_input"
    EXECUTION_FAILED = "execution_failed"
    INVALID_OUTPUT = "invalid_output"


class ToolError(BaseSchema):
    code: ToolErrorCode
    message: str
    # Field-level problems for invalid_input: "loc: message". Never echoes the
    # offending input values back.
    details: list[str] = []


class ToolResult(BaseModel):
    """Uniform outcome of ``ToolRegistry.execute``. Expected failures are
    returned as data (``success=False``), not raised."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    success: bool
    output: Optional[SerializeAsAny[BaseModel]] = None
    error: Optional[ToolError] = None
    duration_ms: int = 0

    @model_validator(mode="after")
    def _exactly_one_of_output_or_error(self) -> "ToolResult":
        if self.success and (self.output is None or self.error is not None):
            raise ValueError("a successful result needs an output and no error")
        if not self.success and (self.error is None or self.output is not None):
            raise ValueError("a failed result needs an error and no output")
        return self


@dataclass(frozen=True)
class ToolSpec:
    name: ToolName
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[Any], BaseModel]

    def __post_init__(self) -> None:
        if not isinstance(self.name, ToolName):
            raise TypeError("ToolSpec.name must be a ToolName member")
        if not self.description.strip():
            raise ValueError("ToolSpec.description must not be empty")
        for label, model in (("input_model", self.input_model), ("output_model", self.output_model)):
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                raise TypeError(f"ToolSpec.{label} must be a pydantic BaseModel subclass")
        if not callable(self.handler):
            raise TypeError("ToolSpec.handler must be callable")

    def describe(self) -> dict[str, Any]:
        """Machine-readable definition (name, description, JSON schemas)."""
        return {
            "name": self.name.value,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
        }
