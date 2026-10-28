"""Who a model call is for.

Agents are process-wide singletons that know nothing about users, so the per-request identity
travels in a ``ContextVar`` set by the entrypoint (research, chat). LangGraph copies the context
into its parallel nodes, but ``loop.run_in_executor`` does NOT — use ``in_current_context`` there.

A call made with no scope is *legacy passthrough*: the application's default model, no fallback, no
recording — i.e. exactly the pre-M9 behaviour. A ``benchmark`` scope pins one model and forbids
fallback and autopilot, so AtlasBench comparisons stay valid.
"""
from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional


@dataclass(frozen=True)
class RouteScope:
    user_id: Optional[str]
    purpose: str = "other"
    benchmark: bool = False
    pinned_model: Optional[str] = None            # only meaningful for benchmark scopes
    manual_model: Optional[str] = None             # an explicit, allowlisted Playground choice
    run_ref: str = field(default_factory=lambda: uuid.uuid4().hex[:16])   # groups the calls of one request/run


_scope: contextvars.ContextVar[Optional[RouteScope]] = contextvars.ContextVar("atlas_route_scope", default=None)


def current_scope() -> Optional[RouteScope]:
    return _scope.get()


@contextmanager
def routing_scope(user_id: Optional[str], purpose: str = "other", *, benchmark: bool = False,
                  model: Optional[str] = None, manual_model: Optional[str] = None) -> Iterator[RouteScope]:
    scope = RouteScope(user_id=user_id, purpose=purpose, benchmark=benchmark, pinned_model=model, manual_model=manual_model)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def in_current_context(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``fn`` so a worker thread runs it inside a copy of the caller's context (scope included)."""
    ctx = contextvars.copy_context()
    return lambda *a, **kw: ctx.run(fn, *a, **kw)
