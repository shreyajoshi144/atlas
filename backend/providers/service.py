"""CompareService: the orchestration layer behind /api/compare.

Framework-free (no FastAPI/LangChain) so it can be tested with a fake provider
server and a real SQLite file. Responsibilities:
validation, guardrails, evidence provenance, cost ceiling, budget reservation,
concurrent provider calls, structured-output repair, grounding checks,
measured (never ranked) summaries, idempotency, persistence, telemetry.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from . import grounding, prompts, safety
from .catalog import Catalog, ModelEntry
from .client import ProviderClient
from .discovery import DiscoveryCache
from .errors import FALLBACK_ELIGIBLE, ProviderError
from .evalstore import EvalStore
from .models import NormalizedResponse, ProviderRequest, Usage
from .pricing import compute_cost, estimate_max_cost
from .repository import CompareRepository
from .settings import PROVIDER_LABELS, PROVIDERS, ProviderSettings

EVIDENCE_MODES = ("none", "atlas", "provider_search")
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{8,80}$")
UNBILLED = frozenset({"auth", "bad_request", "rate_limit", "quota", "model_not_found", "config", "circuit_open", "budget"})
NATIVE_SEARCH_INPUT_ALLOWANCE = 20000     # tokens of search results a native-search call may add (budgeting only)
SEARCH_CALLS_ALLOWANCE = 3
MAX_EXCERPT_CHARS = 2400

DISCLOSURES = [
    "Your question (and any research-library or web evidence you include) is sent to the model providers you select. "
    "Each provider processes it under its own terms; Atlas asks them not to store it where an API option exists, but cannot guarantee provider-side retention.",
    "API keys stay on the Atlas server and are never shown in the browser.",
    "Costs are estimates from catalog list prices on the verification date, not provider invoices.",
    "Atlas saves your runs to your account only; you can delete any run at any time. Saved runs are removed automatically after the retention period.",
]


class CompareError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


class BudgetPort(Protocol):
    def reserve(self, user_id: str, model_id: str, estimate: float) -> str: ...
    def finish(self, token: str, *, actual_cost: float | None, status: str, latency_ms: int, model_id: str,
               input_tokens: int | None, output_tokens: int | None, error_type: str | None) -> None: ...
    def cancel(self, token: str, model_id: str) -> None: ...


class OperationalBudget:
    """Adapter onto Atlas's existing per-user budget reservations (daily call limit + monthly spend cap)."""

    def __init__(self, db: Any, default_calls: Callable[[], int], default_spend: Callable[[], float | None]) -> None:
        self.db, self._calls, self._spend = db, default_calls, default_spend

    def _repo(self):
        from backend.ops.repository import OperationalRepository  # lazy: pulls in app config
        return OperationalRepository(self.db)

    def reserve(self, user_id: str, model_id: str, estimate: float) -> str:
        return self._repo().reserve_call(user_id, "compare", model_id, estimate,
                                         default_calls=self._calls(), default_spend=self._spend())

    def finish(self, token, *, actual_cost, status, latency_ms, model_id, input_tokens, output_tokens, error_type):
        self._repo().finish_call(token, actual_cost=actual_cost, status=status, latency_ms=latency_ms,
                                 actual_model=model_id, prompt_tokens=input_tokens, completion_tokens=output_tokens,
                                 error_type=error_type, estimated_cost=actual_cost)

    def cancel(self, token, model_id):
        self._repo().finish_call(token, actual_cost=0.0, status="failed", latency_ms=0, actual_model=model_id,
                                 error_type="Cancelled", estimated_cost=0.0)


def budget_exception_types() -> tuple[type, ...]:
    try:
        from backend.ops.repository import UsageLimitExceeded
        return (UsageLimitExceeded,)
    except Exception:  # pragma: no cover - app config unavailable
        return ()


@dataclass
class RunRequest:
    prompt: str
    models: list[str]
    evidence_mode: str = "none"
    include_documents: bool = False
    include_web: bool = False
    structured: bool = False
    max_output_tokens: int | None = None
    consent_external: bool = False
    idempotency_key: str | None = None
    kind: str = "compare"


@dataclass
class _Ctx:
    user_id: str
    run_id: str
    prompt: str
    mode: str
    structured: bool
    max_out: int
    sources: list[dict[str, Any]]
    canary: str
    system: str
    user: str
    est_in: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _est_tokens(chars: int) -> int:
    return math.ceil(chars / 2.5) + 64      # deliberately pessimistic (typical English is ~4 chars/token)


def _sum_usage(a: Usage | None, b: Usage) -> Usage:
    if a is None:
        return b
    def add(x, y):
        return None if x is None and y is None else (x or 0) + (y or 0)
    return Usage(add(a.input_tokens, b.input_tokens), add(a.output_tokens, b.output_tokens),
                 add(a.reasoning_tokens, b.reasoning_tokens), add(a.cached_input_tokens, b.cached_input_tokens),
                 add(a.total_tokens, b.total_tokens), a.search_calls + b.search_calls,
                 None if a.provider_cost_usd is None or b.provider_cost_usd is None else a.provider_cost_usd + b.provider_cost_usd)


EvidenceFn = Callable[[str, str, bool, bool, int], Awaitable[tuple[list[dict[str, Any]], list[str]]]]


class CompareService:
    def __init__(self, *, settings: ProviderSettings, client: ProviderClient, catalog: Catalog,
                 repo: CompareRepository, budget: BudgetPort, evidence_fn: EvidenceFn | None = None,
                 discovery: DiscoveryCache | None = None, eval_store: EvalStore | None = None,
                 budget_errors: tuple[type, ...] | None = None) -> None:
        self.s, self.client, self.catalog, self.repo, self.budget = settings, client, catalog, repo, budget
        self.evidence_fn = evidence_fn
        self.discovery = discovery
        self.eval_store = eval_store
        self.budget_errors = budget_errors if budget_errors is not None else budget_exception_types()
        self._active_users: set[str] = set()
        self._inflight_keys: set[tuple[str, str]] = set()

    # ───────────────────────── public: catalogue ─────────────────────────
    def models_payload(self) -> dict[str, Any]:
        s = self.s
        providers = [{"id": p, "label": PROVIDER_LABELS[p], "configured": s.configured(p),
                      "circuit": self.client.breakers[p].state,
                      "key_hint": {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                                   "gemini": "GEMINI_API_KEY", "xai": "XAI_API_KEY"}[p]} for p in PROVIDERS]
        models = []
        for e in self.catalog.all():
            from .pricing import resolve_price
            price = resolve_price(e, 0)
            est = estimate_max_cost(e, 4000, s.default_max_output_tokens)
            models.append({
                "id": e.id, "provider": e.provider, "model": e.model, "label": e.label, "tier": e.tier,
                "configured": s.configured(e.provider),
                "available": self.discovery.availability(e) if self.discovery else None,
                "default": e.default,
                "price_in_per_mtok": price["in"] if price else None, "price_out_per_mtok": price["out"] if price else None,
                "price_note": e.price_note, "price_verified_on": e.verified_on, "source_url": e.source_url,
                "context_window": e.context_window, "max_output_tokens": e.max_output_tokens,
                "supports": {"native_search": True, "structured_output": "native" if e.provider in ("openai", "xai") else "prompted"},
                "est_max_cost_usd": round(est, 6) if est is not None else None,
            })
        return {"providers": providers, "models": models, "catalog_verified_on": self.catalog.verified_on,
                "catalog_stale": self.catalog.is_stale(),
                "limits": {"min_models": 1, "max_models": s.max_models_per_run, "max_prompt_chars": s.max_prompt_chars,
                           "default_max_output_tokens": s.default_max_output_tokens,
                           "max_output_tokens": s.max_output_tokens,
                           "max_cost_per_request_usd": s.max_cost_per_request_usd},
                "disclosures": DISCLOSURES}

    def evaluations_payload(self) -> dict[str, Any]:
        if not self.eval_store:
            return {"available": False, "runs": [], "explanation": ""}
        return self.eval_store.summary()

    # ───────────────────────── validation ─────────────────────────
    def _validate(self, req: RunRequest, max_models: int) -> tuple[str, list[ModelEntry], int, list[str]]:
        s = self.s
        warnings: list[str] = []
        if not req.consent_external:
            raise CompareError(400, "Please confirm that your question and any selected evidence may be sent to the chosen AI providers.")
        prompt, flags = safety.sanitize_text(req.prompt or "")
        prompt = prompt.strip()
        if len(prompt) < 3:
            raise CompareError(400, "Please enter a question.")
        if len(prompt) > s.max_prompt_chars:
            raise CompareError(400, f"Question is too long (limit {s.max_prompt_chars} characters).")
        if safety.detect_secrets(prompt):
            raise CompareError(400, "Your question looks like it contains a credential or secret key. "
                                    "Remove it before sending; Atlas will not forward it to third parties.")
        if "hidden_characters_removed" in flags:
            warnings.append("Invisible formatting characters were removed from your question.")
        if req.evidence_mode not in EVIDENCE_MODES:
            raise CompareError(400, "Unknown evidence mode.")
        if (req.include_documents or req.include_web) and req.evidence_mode != "atlas":
            raise CompareError(400, "Library/web evidence can only be used with the 'atlas' evidence mode.")
        ids = list(dict.fromkeys(req.models or []))
        if len(ids) != len(req.models or []):
            raise CompareError(400, "Models must be distinct.")
        if not 1 <= len(ids) <= max_models:
            raise CompareError(400, f"Choose between 1 and {max_models} models.")
        entries: list[ModelEntry] = []
        for mid in ids:
            e = self.catalog.get(mid)
            if e is None:
                raise CompareError(400, "Unknown model. Atlas only calls models listed in its catalog.")
            if not s.configured(e.provider):
                raise CompareError(400, f"{PROVIDER_LABELS[e.provider]} is not configured on this server.")
            if self.discovery and self.discovery.availability(e) is False:
                raise CompareError(400, f"{e.label} is not available for this server's {PROVIDER_LABELS[e.provider]} key.")
            entries.append(e)
        max_out = req.max_output_tokens or s.default_max_output_tokens
        if not 64 <= max_out <= s.max_output_tokens:
            raise CompareError(400, f"max_output_tokens must be between 64 and {s.max_output_tokens}.")
        for e in entries:
            if e.max_output_tokens and max_out > e.max_output_tokens:
                raise CompareError(400, f"{e.label} supports at most {e.max_output_tokens} output tokens.")
        if req.idempotency_key is not None and not _KEY_RE.match(req.idempotency_key):
            raise CompareError(400, "Invalid idempotency key.")
        return prompt, entries, max_out, warnings

    # ───────────────────────── evidence ─────────────────────────
    async def _gather_evidence(self, user_id: str, req: RunRequest, prompt: str) -> tuple[list[dict[str, Any]], list[str]]:
        warnings: list[str] = []
        if req.evidence_mode != "atlas" or not (req.include_documents or req.include_web):
            return [], warnings
        if self.evidence_fn is None:
            return [], ["Evidence retrieval is not available on this server."]
        try:
            raw, w = await self.evidence_fn(user_id, prompt, req.include_documents, req.include_web, 5)
            warnings += w
        except Exception as exc:  # retrieval must never take the comparison down
            return [], [f"Evidence retrieval failed ({type(exc).__name__}); models were run without evidence."]
        sources: list[dict[str, Any]] = []
        counters = {"document": 0, "web": 0}
        budget_chars = self.s.max_evidence_chars
        used = 0
        for item in raw:
            kind = "web" if item.get("kind") == "web" else "document"
            text, _ = safety.sanitize_text(str(item.get("excerpt") or ""))
            text, redacted = safety.redact_secrets(text)
            text = text.strip()[:MAX_EXCERPT_CHARS]
            if not text:
                continue
            if used + len(text) > budget_chars:
                warnings.append("Some evidence was left out to stay within the evidence size limit.")
                break
            used += len(text)
            counters[kind] += 1
            url = item.get("url")
            url = url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else None
            flags = safety.injection_signals(text)
            if redacted:
                flags.append("credential_like_text_redacted")
            sources.append({"id": f"{'W' if kind == 'web' else 'D'}{counters[kind]}", "kind": kind,
                            "title": str(item.get("title") or "Untitled")[:200], "url": url,
                            "domain": item.get("domain"), "excerpt": text, "retrieved_at": _now(),
                            "sha256": hashlib.sha256(text.encode()).hexdigest(), "risk_flags": flags,
                            "report_id": item.get("report_id")})
        if any(s["risk_flags"] and s["risk_flags"] != ["credential_like_text_redacted"] for s in sources):
            warnings.append("Some evidence contains instruction-like text. Atlas treats evidence strictly as data and "
                            "tells every model to ignore instructions inside it, but this is a risk signal, not a guarantee.")
        if req.evidence_mode == "atlas" and not sources:
            warnings.append("No evidence was found, so models were told the evidence is insufficient.")
        return sources, warnings

    # ───────────────────────── estimates ─────────────────────────
    def _estimate(self, entry: ModelEntry, ctx_chars: int, max_out: int, native: bool, structured: bool) -> float | None:
        tokens = _est_tokens(ctx_chars) + (NATIVE_SEARCH_INPUT_ALLOWANCE if native else 0)
        return estimate_max_cost(entry, tokens, max_out, SEARCH_CALLS_ALLOWANCE if native else 0,
                                 calls=2 if structured else 1)

    # ───────────────────────── run (compare) ─────────────────────────
    async def run(self, user_id: str, req: RunRequest) -> dict[str, Any]:
        req.kind = "compare"
        prompt, entries, max_out, warnings = self._validate(req, self.s.max_models_per_run)
        return await self._guarded(user_id, req, lambda: self._run_compare(user_id, req, prompt, entries, max_out, warnings))

    async def ask(self, user_id: str, req: RunRequest) -> dict[str, Any]:
        req.kind = "ask"
        prompt, entries, max_out, warnings = self._validate(req, 3)
        return await self._guarded(user_id, req, lambda: self._run_ask(user_id, req, prompt, entries, max_out, warnings))

    async def _guarded(self, user_id: str, req: RunRequest, fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        key = req.idempotency_key
        if key:
            prior = self.repo.get_by_idempotency(user_id, key)
            if prior is not None:
                prior = dict(prior)
                prior.setdefault("warnings", [])
                prior["warnings"] = ["This request was already processed; showing the saved result (nothing was billed again)."] + prior["warnings"]
                return prior
            if (user_id, key) in self._inflight_keys:
                raise CompareError(409, "This request is already running.")
        if user_id in self._active_users:
            raise CompareError(409, "A comparison is already running for your account. Wait for it to finish.")
        self._active_users.add(user_id)
        if key:
            self._inflight_keys.add((user_id, key))
        try:
            return await fn()
        finally:
            self._active_users.discard(user_id)
            if key:
                self._inflight_keys.discard((user_id, key))

    async def _prepare(self, user_id: str, req: RunRequest, prompt: str, max_out: int,
                       warnings: list[str]) -> _Ctx:
        sources, w = await self._gather_evidence(user_id, req, prompt)
        warnings += w
        canary = safety.new_canary()
        system = prompts.system_prompt(req.evidence_mode, canary, req.structured)
        user = prompts.user_prompt(prompt, sources, req.evidence_mode)
        return _Ctx(user_id, f"run_{uuid.uuid4().hex}", prompt, req.evidence_mode, req.structured, max_out,
                    sources, canary, system, user, _est_tokens(len(system) + len(user)))

    def _public_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        keep = ("id", "kind", "title", "url", "domain", "excerpt", "retrieved_at", "sha256", "risk_flags", "report_id")
        return [{k: s.get(k) for k in keep} for s in sources]

    def _eval_note(self, entries: list[ModelEntry]) -> dict[str, Any]:
        covered = self.eval_store.covered_models() if self.eval_store else set()
        have = [e.label for e in entries if e.id in covered]
        if not have:
            return {"available": False, "message":
                    "No measured evaluation results are stored for these models, so Atlas cannot say which one is more accurate. "
                    "What you see below are measurements from this single run only."}
        return {"available": True, "message":
                f"Measured evaluation results exist for: {', '.join(have)}. See the Evaluations section for case counts and "
                "confidence ranges. They describe the evaluation dataset, not necessarily your question."}

    async def _run_compare(self, user_id, req, prompt, entries, max_out, warnings) -> dict[str, Any]:
        ctx = await self._prepare(user_id, req, prompt, max_out, warnings)
        native = ctx.mode == "provider_search"
        estimates: dict[str, float] = {}
        for e in entries:
            est = self._estimate(e, len(ctx.system) + len(ctx.user), max_out, native, ctx.structured)
            if est is None:
                raise CompareError(402, f"{e.label} cannot be priced from the catalog, so its cost cannot be bounded.")
            estimates[e.id] = est
        total_est = sum(estimates.values())
        if total_est > self.s.max_cost_per_request_usd + 1e-12:
            raise CompareError(402, f"Estimated worst-case cost ${total_est:.2f} exceeds the per-request ceiling of "
                                    f"${self.s.max_cost_per_request_usd:.2f}. Choose fewer or cheaper models, or lower the output limit.")
        tokens = self._reserve_all(user_id, entries, estimates)
        try:
            results = await asyncio.gather(*(self._call_model(ctx, e, tokens[e.id]) for e in entries))
        except BaseException:
            for e in entries:      # cancelled/crashed: keep any unsettled reservation (conservative), never leak a lock
                self._settle_unfinished(tokens[e.id], e.id)
            raise
        if native:
            warnings.append("Each provider searched the web on its own, so they used different sources. "
                            "Differences in answers may come from different evidence, not just different models.")
        elif ctx.mode == "none":
            warnings.append("No evidence was supplied: answers come from each model's built-in knowledge and cannot be checked against sources.")
        for r in results:
            if r["status"] == "ok" and r["truncated"]:
                warnings.append(f"{r['label']} stopped at the output limit; its answer may be incomplete.")
        if self.catalog.is_stale():
            warnings.append("Model prices/IDs were last verified on " + self.catalog.verified_on + " and may be out of date.")
        actual = [r["cost"]["usd"] for r in results]
        payload = {
            "run_id": ctx.run_id, "kind": "compare", "created_at": _now(), "prompt": prompt, "evidence_mode": ctx.mode,
            "structured": ctx.structured, "sources": self._public_sources(ctx.sources), "results": list(results),
            "summary": self._summary(list(results)), "warnings": warnings,
            "budget": {"est_max_cost_usd": round(total_est, 6),
                       "actual_cost_usd": round(sum(c for c in actual if c is not None), 8) if any(c is not None for c in actual) else None,
                       "cost_complete": all(r["cost"]["complete"] for r in results if r["status"] == "ok") and
                                        all(c is not None for r, c in zip(results, actual) if r["status"] == "ok")},
            "evaluation": self._eval_note(entries),
        }
        self._persist(user_id, req, payload, [e.id for e in entries], list(results))
        return payload

    def _reserve_all(self, user_id: str, entries: list[ModelEntry], estimates: dict[str, float]) -> dict[str, str]:
        tokens: dict[str, str] = {}
        try:
            for e in entries:
                tokens[e.id] = self.budget.reserve(user_id, e.id, estimates[e.id])
        except Exception as exc:
            for mid, tok in tokens.items():
                try:
                    self.budget.cancel(tok, mid)
                except Exception:
                    pass
            if self.budget_errors and isinstance(exc, self.budget_errors):
                raise CompareError(429, str(exc)) from exc
            if isinstance(exc, LookupError):
                raise CompareError(401, "Account not found.") from exc
            raise
        return tokens

    def _settle_unfinished(self, token: str, model_id: str) -> None:
        try:
            self.budget.finish(token, actual_cost=None, status="failed", latency_ms=0, model_id=model_id,
                               input_tokens=None, output_tokens=None, error_type="Cancelled")
        except Exception:
            pass

    # ───────────────────────── run (ask with fallback) ─────────────────────────
    async def _run_ask(self, user_id, req, prompt, entries, max_out, warnings) -> dict[str, Any]:
        ctx = await self._prepare(user_id, req, prompt, max_out, warnings)
        native = ctx.mode == "provider_search"
        attempts: list[dict[str, Any]] = []
        final: dict[str, Any] | None = None
        first_failure: str | None = None
        results: list[dict[str, Any]] = []
        total_est = 0.0
        for i, e in enumerate(entries):
            est = self._estimate(e, len(ctx.system) + len(ctx.user), max_out, native, ctx.structured)
            if est is None or est > self.s.max_cost_per_request_usd + 1e-12:
                attempts.append({"model_id": e.id, "status": "skipped", "category": "budget", "ms": 0})
                first_failure = first_failure or "budget"
                continue
            if self.client.breakers[e.provider].state == "open":
                attempts.append({"model_id": e.id, "status": "skipped", "category": "circuit_open", "ms": 0})
                first_failure = first_failure or "circuit_open"
                continue
            try:
                token = self.budget.reserve(user_id, e.id, est)
            except Exception as exc:
                if self.budget_errors and isinstance(exc, self.budget_errors):
                    if not results:
                        raise CompareError(429, str(exc)) from exc
                    break
                raise
            total_est += est
            try:
                r = await self._call_model(ctx, e, token)
            except BaseException:
                self._settle_unfinished(token, e.id)
                raise
            results.append(r)
            attempts.append({"model_id": e.id, "status": r["status"], "category": (r["error"] or {}).get("category"),
                             "ms": r["latency"]["total_ms"]})
            if r["status"] == "ok":
                final = r
                break
            cat = (r["error"] or {}).get("category")
            first_failure = first_failure or cat
            if cat not in FALLBACK_ELIGIBLE:
                final = r
                break
        if final is None:
            final = results[-1] if results else self._skipped_result(entries[0], "budget")
        ok_idx = next((i for i, a in enumerate(attempts) if a["status"] == "ok"), None)
        fallback_used = ok_idx is not None and ok_idx > 0
        if fallback_used:
            warnings.append(f"The first choice was unavailable ({first_failure}); Atlas used the next model in your fallback list. "
                            "The answer comes from a different model than your first choice.")
        if native:
            warnings.append("The provider searched the web by itself; Atlas did not verify those sources.")
        payload = {"run_id": ctx.run_id, "kind": "ask", "created_at": _now(), "prompt": prompt, "evidence_mode": ctx.mode,
                   "structured": ctx.structured, "sources": self._public_sources(ctx.sources), "result": final,
                   "attempts": attempts, "fallback_used": fallback_used,
                   "fallback_reason": first_failure if fallback_used else None, "warnings": warnings,
                   "budget": {"est_max_cost_usd": round(total_est, 6), "actual_cost_usd": final["cost"]["usd"],
                              "cost_complete": final["cost"]["complete"]},
                   "evaluation": self._eval_note(entries)}
        self._persist(user_id, req, payload, [e.id for e in entries], results)
        return payload

    def _skipped_result(self, entry: ModelEntry, category: str) -> dict[str, Any]:
        err = ProviderError(category, provider=entry.provider)
        return self._failed_result(entry, err, [], "skipped", f"req_{uuid.uuid4().hex[:12]}")

    # ───────────────────────── one model call ─────────────────────────
    def _empty_result(self, entry: ModelEntry, request_id: str) -> dict[str, Any]:
        return {"id": entry.id, "provider": entry.provider, "model": entry.model, "label": entry.label,
                "status": "ok", "error": None, "answer": None, "structured": None, "citations": [], "grounding": None,
                "usage": Usage().to_dict(), "cost": {"usd": None, "token_cost_usd": None, "tool_cost_usd": None,
                                                     "complete": False, "basis": "unknown", "price_verified_on": entry.verified_on},
                "latency": {"total_ms": None, "attempts": [], "retries": 0}, "finish_reason": None, "truncated": False,
                "safety": {"flags": []}, "request_id": request_id, "served_model": None}

    def _failed_result(self, entry, err: ProviderError, attempts, status: str, request_id: str) -> dict[str, Any]:
        r = self._empty_result(entry, request_id)
        r["status"] = status
        r["error"] = err.public()
        r["latency"] = {"total_ms": sum(a["ms"] for a in attempts) if attempts else None,
                        "attempts": attempts, "retries": max(0, len(attempts) - 1)}
        return r

    async def _call_model(self, ctx: _Ctx, entry: ModelEntry, token: str) -> dict[str, Any]:
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        native = ctx.mode == "provider_search"
        schema = prompts.ANSWER_SCHEMA if ctx.structured else None
        settled = False
        attempts: list[dict[str, Any]] = []
        usage: Usage | None = None
        total_ms = 0
        try:
            req = ProviderRequest(ctx.system, ctx.user, ctx.max_out, native, schema, request_id)
            resp = await self.client.generate(entry, req)
            usage = resp.usage
            attempts += resp.attempts
            total_ms += resp.total_ms or 0
            structured_info = None
            text = resp.text
            if resp.truncated and not text.strip():
                raise ProviderError("truncated", provider=entry.provider, attempts=attempts)
            if not text.strip():
                raise ProviderError("content_blocked" if resp.finish_reason == "content_filter" else "empty",
                                    provider=entry.provider, attempts=attempts)
            claims = None
            if ctx.structured:
                data, errs = grounding.parse_structured(text)
                repaired = False
                if data is None and not resp.truncated:
                    # one bounded repair attempt (its cost and latency are included in this model's totals)
                    try:
                        rep = ProviderRequest(ctx.system, prompts.repair_prompt(text, errs), ctx.max_out, False, schema, request_id)
                        resp2 = await self.client.generate(entry, rep)
                        usage = _sum_usage(usage, resp2.usage)
                        attempts += [{**a, "n": len(attempts) + i + 1} for i, a in enumerate(resp2.attempts)]
                        total_ms += resp2.total_ms or 0
                        data, errs = grounding.parse_structured(resp2.text)
                        repaired = data is not None
                        text = resp2.text if data is not None else text
                    except ProviderError as exc:
                        attempts += [{**a, "n": len(attempts) + i + 1} for i, a in enumerate(exc.attempts)]
                structured_info = {"valid": data is not None, "data": data, "errors": errs if data is None else [], "repaired": repaired}
                if data is not None:
                    text = data["answer"]
                    claims = data["claims"]
            clean, flags = safety.scan_output(text, ctx.canary)
            r = self._empty_result(entry, request_id)
            r["answer"] = clean
            r["structured"] = structured_info
            r["finish_reason"] = resp.finish_reason
            r["truncated"] = resp.truncated
            r["served_model"] = resp.served_model
            if resp.finish_reason == "refusal":
                flags.append("model_declined")
            r["safety"] = {"flags": sorted(set(flags))}
            r["usage"] = usage.to_dict()
            r["cost"] = compute_cost(entry, usage)
            r["latency"] = {"total_ms": total_ms, "attempts": attempts, "retries": max(0, len(attempts) - 1)}
            r["citations"], r["grounding"] = self._citations_and_grounding(ctx, clean, resp, claims)
            self._finish_budget(token, entry, "ok", r["cost"]["usd"], total_ms, usage, None)
            settled = True
            return r
        except ProviderError as exc:
            all_attempts = attempts + [{**a, "n": len(attempts) + i + 1} for i, a in enumerate(exc.attempts)] \
                if exc.attempts is not attempts else attempts
            status = "timeout" if exc.category == "timeout" else "blocked" if exc.category == "budget" else "failed"
            self._finish_budget(token, entry, "failed", None, sum(a["ms"] for a in all_attempts), usage, exc.category)
            settled = True
            return self._failed_result(entry, exc, all_attempts, status, request_id)
        except asyncio.CancelledError:
            if not settled:
                self._settle_unfinished(token, entry.id)
                settled = True
            raise
        except Exception as exc:   # a bug in our own code must not erase the other models' answers
            if not settled:
                self._settle_unfinished(token, entry.id)
                settled = True
            return self._failed_result(entry, ProviderError("internal", provider=entry.provider), attempts, "failed", request_id)

    def _finish_budget(self, token: str, entry: ModelEntry, status: str, cost: float | None, ms: int,
                       usage: Usage | None, category: str | None) -> None:
        try:
            if status != "ok" and category in UNBILLED:
                self.budget.cancel(token, entry.id)
            else:
                self.budget.finish(token, actual_cost=cost, status=status, latency_ms=ms, model_id=entry.id,
                                   input_tokens=usage.input_tokens if usage else None,
                                   output_tokens=usage.output_tokens if usage else None,
                                   error_type=category)
        except Exception:   # accounting problems are logged by the budget layer; never break the answer
            pass

    def _citations_and_grounding(self, ctx: _Ctx, text: str, resp: NormalizedResponse,
                                 claims: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if ctx.mode == "atlas":
            g = grounding.analyze_atlas(text, ctx.sources, claims)
            by_id = {s["id"]: s for s in ctx.sources}
            cited = list(g["cited_labels"]) + list(g["invalid_labels"])
            cites = []
            for lab in cited:
                s = by_id.get(lab)
                cites.append({"kind": "atlas_source", "source_id": lab, "url": s.get("url") if s else None,
                              "title": s["title"] if s else None, "snippet": s["excerpt"][:160] if s else None,
                              "valid": s is not None})
            return cites, g
        if ctx.mode == "provider_search":
            cites = [{"kind": "provider_web", "source_id": None, "url": c.url, "title": c.title, "snippet": c.snippet,
                      "valid": bool(c.url and c.url.lower().startswith(("http://", "https://")))} for c in resp.citations]
            return cites, grounding.analyze_provider(cites, resp.usage.search_calls)
        return [], None

    # ───────────────────────── measurements (never a ranking) ─────────────────────────
    def _summary(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        ok = [r for r in results if r["status"] == "ok"]
        items: list[dict[str, Any]] = []

        def pick(key, label, getter, unit, lower_is_better=True):
            vals = [(r["id"], getter(r)) for r in ok if getter(r) is not None]
            if len(vals) < 2:
                return
            best = min(v for _, v in vals) if lower_is_better else max(v for _, v in vals)
            tied = [i for i, v in vals if v == best]
            items.append({"key": key, "label": label, "winner_id": tied[0] if len(tied) == 1 else None,
                          "tied_ids": tied if len(tied) > 1 else [], "value": best, "unit": unit})
        pick("fastest", "Lowest response time", lambda r: r["latency"]["total_ms"], "ms")
        pick("cheapest", "Lowest estimated cost", lambda r: r["cost"]["usd"] if r["cost"]["complete"] else None, "usd")
        pick("fewest_retries", "Fewest retries", lambda r: r["latency"]["retries"], "count")
        pick("citation_validity", "Highest share of valid citations",
             lambda r: (r["grounding"] or {}).get("valid_citation_rate"), "ratio", lower_is_better=False)
        pick("support", "Highest share of cited sentences found in the cited text",
             lambda r: (r["grounding"] or {}).get("support_rate"), "ratio", lower_is_better=False)
        failed = [r["label"] for r in results if r["status"] != "ok"]
        notes = ["These are measurements from this single run, not a ranking of answer quality. "
                 "One run is an anecdote; the same model can answer differently next time."]
        if failed:
            notes.append("Did not complete in this run: " + ", ".join(failed) + ".")
        return {"measurements": items, "notes": notes}

    # ───────────────────────── persistence + telemetry ─────────────────────────
    def _persist(self, user_id: str, req: RunRequest, payload: dict[str, Any], model_ids: list[str],
                 results: list[dict[str, Any]]) -> None:
        for r in results:
            try:
                u = r["usage"]
                self.repo.log_call(user_id=user_id, run_id=payload["run_id"], request_id=r["request_id"],
                                   provider=r["provider"], model=r["model"], status=r["status"],
                                   error_category=(r["error"] or {}).get("category"),
                                   attempts=max(1, len(r["latency"]["attempts"])), latency_ms=r["latency"]["total_ms"],
                                   input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                                   cost_usd=r["cost"]["usd"], cost_basis=r["cost"]["basis"])
            except Exception:
                pass
        try:
            self.repo.save_run(user_id, payload["run_id"], req.kind, payload["prompt"], payload["evidence_mode"],
                               model_ids, payload, req.idempotency_key)
        except sqlite3.IntegrityError:
            prior = self.repo.get_by_idempotency(user_id, req.idempotency_key or "")
            if prior is None:
                payload["warnings"].append("This result could not be saved to your history.")
        except Exception:
            # The providers were already called (and billed); never throw the answers away because history failed.
            payload["warnings"].append("This result could not be saved to your history.")
