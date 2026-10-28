"""Task and agent-configuration definitions for AtlasBench, plus content hashing.

Everything that decides a benchmark result is *data* with a content hash, so a run
can state exactly what it measured:

* ``TaskDefinition``  — a research question, a FROZEN evidence corpus and hand-written
  gold annotations. The corpus is never fetched from the web at run time.
* ``AgentConfig``     — the model + agent settings under test. Selection is always
  explicit (a user picks a config); nothing here routes between models.

Hashes cover semantic content only (not titles/ids/timestamps), so renaming a task
never silently invalidates a comparison but changing its evidence always does.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any, Literal, Optional

from pydantic import Field, field_validator, model_validator

from backend.agentic.common import ToolName
from backend.knowledge import ids
from backend.models.common import BaseSchema, VerificationStatus

EVALUATOR_VERSION = "atlasbench-1.0.0"   # bump when scoring semantics change
SUITE_FORMAT_VERSION = 1

# ── vocabulary ───────────────────────────────────────────────────────────────
STATUSES = tuple(s.value for s in VerificationStatus)          # the verifier's own vocabulary
AFFIRMATIVE = frozenset({"verified", "partially_verified"})
NEGATIVE = frozenset({"not_verified", "conflicting"})
GOLD_QUALITIES = ("curated", "derived")
RUN_STATUSES = ("pending", "running", "completed", "failed")
CASE_STATUSES = ("pending", "completed", "failed")
STRATEGIES = {
    "full_corpus": {
        "label": "Full corpus",
        "description": "No retrieval: every document of the task is scraped and handed to the verifier. "
                       "Isolates model quality from retrieval quality.",
        "exempt_tools": ("search_web", "request_more_evidence"),
    },
    "single_pass": {
        "label": "Single pass",
        "description": "search_web → scrape_source → verify_claim, once.",
        "exempt_tools": ("request_more_evidence",),
    },
    "iterative": {
        "label": "Iterative gap-fill",
        "description": "Single pass, then bounded request_more_evidence → scrape → re-verify rounds while the verifier reports evidence gaps.",
        "exempt_tools": ("request_more_evidence",),   # only needed when gaps exist
    },
}
METRICS = (
    "claim_correctness", "citation_accuracy", "evidence_grounding", "evidence_coverage",
    "contradiction_handling", "trajectory_quality",
)
COMPOSITE_WEIGHTS = {
    "claim_correctness": 0.25, "citation_accuracy": 0.20, "evidence_grounding": 0.20,
    "evidence_coverage": 0.15, "contradiction_handling": 0.10, "trajectory_quality": 0.10,
}
assert set(COMPOSITE_WEIGHTS) == set(METRICS) and abs(sum(COMPOSITE_WEIGHTS.values()) - 1.0) < 1e-9

# ── limits (resource and abuse bounds) ───────────────────────────────────────
MAX_DOCS = 12
MAX_DOC_CHARS = 6000
MAX_CORPUS_CHARS = 40_000
MAX_GOLD_CLAIMS = 5
MAX_CLAIM_CHARS = 200
MAX_CUSTOM_TASKS_PER_USER = 50
MAX_CUSTOM_CONFIGS_PER_USER = 20
MAX_RUNS_PER_USER = 100
MAX_TASKS_PER_RUN = 25
MAX_RUN_ATTEMPTS = 3
MAX_CASE_ATTEMPTS = 2
RUN_LEASE_SECONDS = 1800
MAX_COMPARE_RUNS = 4

# Model allowlist + *estimated* list prices (USD per 1M tokens: input, output).
# Prices are approximations kept in one place for cost *estimates*; a model that is
# allowed but unpriced reports cost as unknown (None) rather than a made-up number.
MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "llama-3.3-70b-versatile": {"label": "Llama 3.3 70B", "price_in": 0.59, "price_out": 0.79},
    "llama-3.1-8b-instant": {"label": "Llama 3.1 8B", "price_in": 0.05, "price_out": 0.08},
    "openai/gpt-oss-120b": {"label": "GPT-OSS 120B", "price_in": 0.15, "price_out": 0.75},
    "openai/gpt-oss-20b": {"label": "GPT-OSS 20B", "price_in": 0.10, "price_out": 0.50},
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
_TASK_ID = re.compile(r"^(tsk_[0-9a-f]{32}|[a-z0-9][a-z0-9-]{2,60})$")
_DOC_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")
_CLAIM_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,19}$")
TASK_ID_PATTERN = _TASK_ID.pattern
RUN_ID_PATTERN = r"^run_[0-9a-f]{32}$"
CONFIG_ID_PATTERN = r"^(cfg_[0-9a-f]{32}|[a-z0-9][a-z0-9-]{2,60})$"


# ── hashing ──────────────────────────────────────────────────────────────────
def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def allowed_models(extra: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    """Catalog plus any extra allowed (unpriced) model, e.g. the app's default model."""
    out = dict(MODEL_CATALOG)
    for name in extra:
        out.setdefault(name, {"label": name, "price_in": None, "price_out": None})
    return out


# ── task ─────────────────────────────────────────────────────────────────────
class BenchDoc(BaseSchema):
    doc_id: str
    url: str = Field(max_length=500)
    title: str = Field(min_length=1, max_length=200)
    publish_date: Optional[str] = None
    content: str = Field(min_length=20, max_length=MAX_DOC_CHARS)
    # annotation only — never shown to the agent under test
    distractor: bool = False
    injection: bool = False

    @field_validator("doc_id")
    @classmethod
    def _doc_id(cls, v: str) -> str:
        if not _DOC_ID.match(v):
            raise ValueError("doc_id must be lowercase letters, digits, '-' or '_' (max 30)")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not re.match(r"^https?://", v, re.I):
            raise ValueError("url must be http(s)")
        ids.canonicalize_url(v)  # raises ValueError for non-absolute URLs
        return v

    @field_validator("publish_date")
    @classmethod
    def _date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        date.fromisoformat(v)
        return v

    @property
    def canonical_url(self) -> str:
        return ids.canonicalize_url(self.url)


class GoldClaim(BaseSchema):
    claim_id: str
    text: str = Field(min_length=10, max_length=MAX_CLAIM_CHARS)
    expected_status: Literal["verified", "partially_verified", "not_verified", "conflicting", "insufficient_evidence"]
    supporting_docs: list[str] = Field(default_factory=list, max_length=MAX_DOCS)
    contradicting_docs: list[str] = Field(default_factory=list, max_length=MAX_DOCS)

    @field_validator("claim_id")
    @classmethod
    def _cid(cls, v: str) -> str:
        if not _CLAIM_ID.match(v):
            raise ValueError("claim_id must be lowercase letters, digits, '-' or '_' (max 20)")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> "GoldClaim":
        if set(self.supporting_docs) & set(self.contradicting_docs):
            raise ValueError(f"claim {self.claim_id}: a document cannot both support and contradict")
        if self.expected_status in AFFIRMATIVE and not self.supporting_docs:
            raise ValueError(f"claim {self.claim_id}: a {self.expected_status} claim needs supporting_docs")
        if self.expected_status == "conflicting" and not self.contradicting_docs:
            raise ValueError(f"claim {self.claim_id}: a conflicting claim needs contradicting_docs")
        if self.expected_status == "insufficient_evidence" and self.supporting_docs:
            raise ValueError(f"claim {self.claim_id}: insufficient_evidence cannot list supporting_docs")
        return self

    @property
    def required_docs(self) -> list[str]:
        return list(dict.fromkeys([*self.supporting_docs, *self.contradicting_docs]))


class GoldTrajectory(BaseSchema):
    required_tools: list[Literal["search_web", "scrape_source", "verify_claim"]] = Field(
        default_factory=lambda: ["search_web", "scrape_source", "verify_claim"], min_length=1)
    max_tool_calls: int = Field(default=15, ge=3, le=30)


class TaskDefinition(BaseSchema):
    task_id: str
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(default="", max_length=500)
    question: str = Field(min_length=10, max_length=300)
    claims: list[GoldClaim] = Field(min_length=1, max_length=MAX_GOLD_CLAIMS)
    corpus: list[BenchDoc] = Field(min_length=1, max_length=MAX_DOCS)
    trajectory: GoldTrajectory = Field(default_factory=GoldTrajectory)
    tags: list[str] = Field(default_factory=list, max_length=5)
    gold_quality: Literal["curated", "derived"] = "curated"
    provenance: Optional[dict[str, Any]] = None

    @field_validator("task_id")
    @classmethod
    def _tid(cls, v: str) -> str:
        if not _TASK_ID.match(v):
            raise ValueError("invalid task_id")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        for t in v:
            if not re.match(r"^[a-z0-9][a-z0-9-]{0,29}$", t):
                raise ValueError("tags must be lowercase slugs")
        return v

    @field_validator("provenance")
    @classmethod
    def _prov(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if v is not None and len(canonical_json(v)) > 2000:
            raise ValueError("provenance is too large")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "TaskDefinition":
        doc_ids = [d.doc_id for d in self.corpus]
        if len(set(doc_ids)) != len(doc_ids):
            raise ValueError("duplicate doc_id in corpus")
        urls = [d.canonical_url for d in self.corpus]
        if len(set(urls)) != len(urls):
            raise ValueError("two corpus documents share a URL")
        if sum(len(d.content) for d in self.corpus) > MAX_CORPUS_CHARS:
            raise ValueError(f"corpus exceeds {MAX_CORPUS_CHARS} characters")
        claim_ids = [c.claim_id for c in self.claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("duplicate claim_id")
        known = set(doc_ids)
        for c in self.claims:
            unknown = set(c.required_docs) - known
            if unknown:
                raise ValueError(f"claim {c.claim_id} references unknown documents: {sorted(unknown)}")
        if len(" ".join(c.text for c in self.claims)) > 900:
            raise ValueError("claims are too long to verify together (900 characters in total)")
        return self

    # ── derived views ────────────────────────────────────────────────────────
    def doc(self, doc_id: str) -> Optional[BenchDoc]:
        return next((d for d in self.corpus if d.doc_id == doc_id), None)

    def doc_by_url(self, url: str) -> Optional[BenchDoc]:
        try:
            canon = ids.canonicalize_url(url)
        except ValueError:
            return None
        return next((d for d in self.corpus if d.canonical_url == canon), None)

    @property
    def required_docs(self) -> list[str]:
        return list(dict.fromkeys(d for c in self.claims for d in c.required_docs))

    def semantic_dict(self) -> dict[str, Any]:
        """What the *result* depends on. Titles, ids, tags and descriptions are cosmetic."""
        return {
            "question": self.question,
            "claims": [c.model_dump(mode="json") for c in self.claims],
            "corpus": sorted((d.model_dump(mode="json") for d in self.corpus), key=lambda d: d["doc_id"]),
            "trajectory": self.trajectory.model_dump(mode="json"),
            "gold_quality": self.gold_quality,
        }

    @property
    def task_hash(self) -> str:
        return sha256_hex(canonical_json(self.semantic_dict()))

    def public_dict(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["task_hash"] = self.task_hash
        return d


# ── agent configuration ──────────────────────────────────────────────────────
class AgentConfig(BaseSchema):
    name: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=100)
    strategy: Literal["full_corpus", "single_pass", "iterative"] = "single_pass"
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096, ge=256, le=8192)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    top_k: int = Field(default=5, ge=1, le=10)
    max_rounds: int = Field(default=2, ge=1, le=3)
    max_scrapes: int = Field(default=6, ge=1, le=10)
    evidence_chars_per_source: int = Field(default=3000, ge=500, le=MAX_DOC_CHARS)
    max_tool_calls: int = Field(default=15, ge=3, le=30)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v

    def semantic_dict(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d.pop("name")
        return d

    @property
    def config_hash(self) -> str:
        return sha256_hex(canonical_json(self.semantic_dict()))


def run_fingerprint(config_hash: str, task_hashes: dict[str, str]) -> str:
    """Two runs with the same fingerprint measured the same thing."""
    return sha256_hex(canonical_json({"evaluator": EVALUATOR_VERSION, "config": config_hash,
                                      "tasks": sorted(task_hashes.values())}))


def suite_hash(task_hashes: dict[str, str]) -> str:
    return sha256_hex(canonical_json({"format": SUITE_FORMAT_VERSION, "tasks": dict(sorted(task_hashes.items()))}))


def tool_names() -> tuple[str, ...]:
    return tuple(t.value for t in ToolName)
