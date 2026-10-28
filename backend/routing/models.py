"""Routing policy: how a user's model calls choose a model, and what happens when one fails.

Two modes, both explicit and visible:

* ``explicit``  — always use ``model``.
* ``autopilot`` — use the cheapest candidate that meets the quality, cost and latency
  requirements *according to the user's own AtlasBench results*. If nothing qualifies, or
  there is no benchmark evidence, ``model`` is used and the recorded reason says exactly why.

Fallback is a separate, opt-in switch with an explicit ordered list of models.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from backend.bench.models import MODEL_CATALOG, allowed_models, canonical_json, sha256_hex  # noqa: F401  (re-exported)
from backend.models.common import BaseSchema

MODES = ("explicit", "autopilot")
EVIDENCE_STRATEGIES = ("any", "full_corpus", "single_pass", "iterative")
PURPOSES = ("research", "chat", "verification", "other")
MAX_POLICIES_PER_USER = 20
MAX_USAGE_ROWS_PER_USER = 10_000
MAX_FALLBACK_MODELS = 3
MAX_CANDIDATES = 8
DEFAULT_COMPLETION_TOKENS_ESTIMATE = 600     # used only when no benchmark has seen the model
CHARS_PER_TOKEN = 4                          # rough, documented estimate of prompt size before the call
POLICY_ID_PATTERN = r"^pol_[0-9a-f]{32}$"
USAGE_STATUSES = ("ok", "failed")


class RoutingPolicy(BaseSchema):
    name: str = Field(min_length=1, max_length=80)
    mode: Literal["explicit", "autopilot"] = "explicit"
    model: str = Field(min_length=1, max_length=100)
    # autopilot requirements — all three must hold for a candidate to qualify
    min_quality: float = Field(default=0.8, ge=0.0, le=1.0)
    max_latency_ms: int = Field(default=8000, ge=200, le=120_000)
    max_cost_per_call_usd: float = Field(default=0.02, ge=0.0001, le=5.0)
    candidate_models: list[str] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    evidence_strategy: Literal["any", "full_corpus", "single_pass", "iterative"] = "any"
    max_evidence_age_days: int = Field(default=90, ge=1, le=365)
    min_evidence_cases: int = Field(default=4, ge=1, le=25)
    # production fallback (never used during AtlasBench runs)
    fallback_enabled: bool = False
    fallback_models: list[str] = Field(default_factory=list, max_length=MAX_FALLBACK_MODELS)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> "RoutingPolicy":
        if len(set(self.candidate_models)) != len(self.candidate_models):
            raise ValueError("candidate_models contains duplicates")
        if len(set(self.fallback_models)) != len(self.fallback_models):
            raise ValueError("fallback_models contains duplicates")
        if self.mode == "explicit" and self.model in self.fallback_models:
            # In autopilot, ``model`` is only the default, so falling back from a cheap pick to it is the normal chain.
            raise ValueError("fallback_models must not include the primary model")
        if self.fallback_enabled and not self.fallback_models:
            raise ValueError("fallback is enabled but fallback_models is empty")
        return self

    def referenced_models(self) -> list[str]:
        return list(dict.fromkeys([self.model, *self.candidate_models, *self.fallback_models]))

    @property
    def policy_hash(self) -> str:
        d = self.model_dump(mode="json")
        d.pop("name")
        return sha256_hex(canonical_json(d))

    def pool(self, allowed: dict[str, Any]) -> list[str]:
        """Models autopilot may choose from: the candidate list, or every allowed model."""
        return list(self.candidate_models) if self.candidate_models else sorted(allowed)


def check_models(policy: RoutingPolicy, allowed: dict[str, Any]) -> None:
    """Every model a policy can ever call must be on the allowlist (no arbitrary model strings)."""
    bad = [m for m in policy.referenced_models() if m not in allowed]
    if bad:
        raise ValueError(f"Model(s) not allowed: {', '.join(sorted(bad))}. Choose from: {', '.join(sorted(allowed))}.")


def default_policy(model: str) -> RoutingPolicy:
    """What every user effectively has until they activate a policy: the application's model, no fallback."""
    return RoutingPolicy(name="Application default", mode="explicit", model=model)
