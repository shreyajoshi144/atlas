"""AtlasBench service: ownership-checked tasks, configs and runs; resumable execution;
reproduction; comparison; and tasks derived from the Knowledge Map / challenge system.

Ownership: every operation takes the authenticated ``user_id``. An object that does
not exist *or belongs to another user* raises the same ``BenchNotFound``, so existence
is never leaked. Built-in suites and presets are read-only and visible to everyone.

History is never rewritten: a run stores snapshots of its config and of every task it
measured, so editing/deleting a task afterwards cannot change what the run says.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend.bench import compare as cmp
from backend.bench import suites
from backend.bench.arbitration import arbitrate_candidates
from backend.bench.metering import estimate_cost_usd
from backend.bench.models import (
    COMPOSITE_WEIGHTS, EVALUATOR_VERSION, MAX_CASE_ATTEMPTS, MAX_COMPARE_RUNS, MAX_RUN_ATTEMPTS, MAX_TASKS_PER_RUN, MAX_DOC_CHARS, MAX_DOCS,
    MAX_CLAIM_CHARS, STRATEGIES, AgentConfig, TaskDefinition, allowed_models, run_fingerprint,
)
from backend.bench.repository import BenchConflict, BenchNotFound, BenchRepository, new_id
from backend.bench.runner import case_from_json, run_case
from backend.bench.scoring import score_case
from backend.core.logging import get_logger
from backend.core.observability import serialize_public_trace
from backend.database import ResearchRepository
from backend.ops.limits import BudgetedLLM
from backend.routing.context import routing_scope
from backend.knowledge import ids

logger = get_logger(__name__)

LLMFactory = Callable[[AgentConfig], Any]


def _default_llm_factory(config: AgentConfig) -> Any:
    from backend.core.llm import build_chat_llm

    return build_chat_llm(config.model, temperature=config.temperature, max_tokens=config.max_tokens, seed=config.seed)


def _j(text: Optional[str], default: Any = None) -> Any:
    return json.loads(text) if text else default


class BenchService:
    def __init__(self, repository: ResearchRepository, *, llm_factory: Optional[LLMFactory] = None,
                 clock: Optional[Callable[[], datetime]] = None, extra_models: tuple[str, ...] = ()) -> None:
        self.repository = repository
        self.repo = BenchRepository(repository.db)
        self._llm_factory = llm_factory or _default_llm_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not extra_models:
            try:
                from backend.core.config import get_settings

                extra_models = (get_settings().openai_model,)
            except Exception:  # settings unavailable (unit tests): catalog only
                extra_models = ()
        self.models = allowed_models(extra_models)

    # ── overview ─────────────────────────────────────────────────────────────
    def overview(self, user_id: str) -> dict[str, Any]:
        from backend.bench import models as m

        return {
            "evaluator_version": EVALUATOR_VERSION,
            "suites": [s.info() for s in suites.list_suites()],
            "presets": [self._config_view(cid, c, builtin=True) for cid, c in suites.PRESETS.items()],
            "models": [{"model": k, "label": v["label"], "price_in": v["price_in"], "price_out": v["price_out"]} for k, v in self.models.items()],
            "strategies": [{"strategy": k, "label": v["label"], "description": v["description"]} for k, v in STRATEGIES.items()],
            "metrics": [{"metric": k, "label": cmp.METRIC_LABELS[k], "weight": COMPOSITE_WEIGHTS[k], "description": cmp.METRIC_INFO[k]} for k in COMPOSITE_WEIGHTS],
            "limits": {"max_tasks_per_run": MAX_TASKS_PER_RUN, "max_compare_runs": MAX_COMPARE_RUNS, "max_run_attempts": MAX_RUN_ATTEMPTS,
                       "max_docs": MAX_DOCS, "max_doc_chars": MAX_DOC_CHARS, "max_claims": m.MAX_GOLD_CLAIMS,
                       "max_custom_tasks": m.MAX_CUSTOM_TASKS_PER_USER, "max_custom_configs": m.MAX_CUSTOM_CONFIGS_PER_USER, "max_runs": m.MAX_RUNS_PER_USER},
            "counts": {"custom_tasks": len(self.repo.list_task_rows(user_id)), "custom_configs": len(self.repo.list_config_rows(user_id)),
                       "runs": self.repo.list_runs(user_id, limit=1)[1]},
        }

    # ── tasks ────────────────────────────────────────────────────────────────
    @staticmethod
    def _task_summary(t: TaskDefinition, *, builtin: bool, suite_id: Optional[str] = None, created_at: Optional[str] = None) -> dict[str, Any]:
        return {"task_id": t.task_id, "title": t.title, "description": t.description, "question": t.question, "tags": t.tags,
                "task_hash": t.task_hash, "gold_quality": t.gold_quality, "builtin": builtin, "suite_id": suite_id,
                "claim_count": len(t.claims), "doc_count": len(t.corpus), "created_at": created_at,
                "provenance": t.provenance}

    def list_tasks(self, user_id: str, *, source: Optional[str] = None, tag: Optional[str] = None) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        if source in (None, "builtin"):
            items += [self._task_summary(t, builtin=True, suite_id=suites.suite_of(t.task_id)) for t in suites.builtin_tasks()]
        if source in (None, "custom"):
            for row in self.repo.list_task_rows(user_id):
                items.append(self._task_summary(TaskDefinition.model_validate(_j(row["task_json"])), builtin=False, created_at=row["created_at"]))
        if tag:
            items = [i for i in items if tag in i["tags"]]
        return {"items": items, "total": len(items)}

    def resolve_task(self, user_id: str, task_id: str) -> TaskDefinition:
        t = suites.builtin_task(task_id)
        if t is not None:
            return t
        return TaskDefinition.model_validate(_j(self.repo.get_task_row(user_id, task_id)["task_json"]))

    def get_task(self, user_id: str, task_id: str) -> dict[str, Any]:
        t = self.resolve_task(user_id, task_id)
        builtin = suites.builtin_task(task_id) is not None
        return {**self._task_summary(t, builtin=builtin, suite_id=suites.suite_of(task_id)), "definition": t.public_dict()}

    def create_task(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """A user-authored task. The server assigns the id and ``gold_quality`` is always 'curated' (the user's own labels)."""
        body = {k: v for k, v in payload.items() if k not in ("task_id", "gold_quality", "provenance")}
        task = TaskDefinition.model_validate({**body, "task_id": new_id("tsk"), "gold_quality": "curated"})
        self.repo.create_task(user_id, task, "custom", self._clock())
        return self.get_task(user_id, task.task_id)

    def delete_task(self, user_id: str, task_id: str) -> None:
        if suites.builtin_task(task_id) is not None:
            raise BenchConflict("Built-in tasks cannot be deleted.")
        self.repo.delete_task(user_id, task_id)

    def create_task_from_claim(self, user_id: str, investigation_id: str, claim_id: str, include_challenges: bool = True) -> tuple[dict[str, Any], bool]:
        """Freeze a Knowledge Map claim (and optionally its completed challenges' evidence) into a task.

        Gold labels are DERIVED from Atlas's own earlier stances/verdicts by a fixed rule — they are not
        human-reviewed, so the task is marked ``gold_quality='derived'`` and is meant for regression tracking.
        """
        from backend.challenges.repository import ChallengeRepository
        from backend.knowledge.map_service import KnowledgeMapService, NotFoundError

        km = KnowledgeMapService(self.repository, clock=self._clock)
        try:
            claim = km.get_claim(user_id, claim_id, report_id=investigation_id)
        except NotFoundError:
            raise BenchNotFound("claim not found in that investigation") from None
        if claim["graph_state"] not in ("complete", "partial"):
            raise BenchConflict(f"Knowledge for this investigation is '{claim['graph_state']}'; rebuild it before benchmarking its claims.")
        text = " ".join(str(claim["text"]).split())
        if not 10 <= len(text) <= MAX_CLAIM_CHARS:
            raise ValueError(f"The claim must be 10–{MAX_CLAIM_CHARS} characters to be benchmarked.")

        items: list[dict[str, Any]] = []   # {url, title, date, passage, stance, strength}
        for group in claim["evidence"].values():
            for e in group:
                s = e["source"]
                items.append({"url": s.get("url"), "title": s.get("title") or s.get("domain") or "Source", "date": s.get("publish_date"),
                              "passage": e["passage"], "stance": e["stance"], "strength": float(e["strength"] or 0.0)})
        challenge_ids: list[str] = []
        if include_challenges:
            crepo = ChallengeRepository(self.repository.db)
            for ch in crepo.list_for_claim(user_id, claim_id):
                if ch["status"] != "completed" or ch["investigation_id"] != investigation_id:
                    continue
                challenge_ids.append(ch["challenge_id"])
                for e in crepo.evidence(user_id, ch["challenge_id"]):
                    items.append({"url": e["url"], "title": e["title"] or e["domain"], "date": e["publish_date"], "passage": e["passage_text"],
                                  "stance": e["stance"], "strength": float(e["strength"] or 0.0)})
        task = self._derive_task(text, claim["status"], investigation_id, claim_id, challenge_ids, items)
        existing = next((r for r in self.repo.list_task_rows(user_id) if r["task_hash"] == task.task_hash), None)
        if existing is not None:
            return self.get_task(user_id, existing["task_id"]), False
        self.repo.create_task(user_id, task, "knowledge_claim", self._clock())
        return self.get_task(user_id, task.task_id), True

    @staticmethod
    def _derive_task(text: str, base_status: str, investigation_id: str, claim_id: str, challenge_ids: list[str],
                     items: list[dict[str, Any]]) -> TaskDefinition:
        by_url: dict[str, dict[str, Any]] = {}
        for it in items:
            if not it["url"] or len((it["passage"] or "").strip()) < 20:
                continue
            try:
                canon = ids.canonicalize_url(it["url"])
            except ValueError:
                continue
            d = by_url.setdefault(canon, {"url": it["url"], "title": it["title"], "date": it["date"], "passages": [], "best": None})
            p = " ".join(it["passage"].split())
            if p not in d["passages"]:
                d["passages"].append(p)
            if d["best"] is None or it["strength"] > d["best"]["strength"]:
                d["best"] = it
        if not by_url:
            raise ValueError("This claim has no usable evidence passages to freeze into a task.")
        ranked = sorted(by_url.values(), key=lambda d: (-d["best"]["strength"], d["url"]))[:MAX_DOCS]
        corpus, supporting, contradicting = [], [], []
        strongest = {"support": 0.0, "contradict": 0.0}
        for n, d in enumerate(sorted(ranked, key=lambda d: d["url"]), start=1):
            doc_id = f"d{n}"
            date = (d["date"] or "")[:10] or None
            corpus.append({"doc_id": doc_id, "url": d["url"], "title": str(d["title"] or "Source")[:200], "publish_date": date,
                           "content": "\n\n".join(d["passages"])[:MAX_DOC_CHARS]})
            st, strength = d["best"]["stance"], d["best"]["strength"]
            if st == "support":
                supporting.append(doc_id)
                strongest["support"] = max(strongest["support"], strength)
            elif st == "contradict" and strength >= 0.5:
                contradicting.append(doc_id)
                strongest["contradict"] = max(strongest["contradict"], strength)
        if contradicting:
            expected = "conflicting" if supporting else "not_verified"
        elif supporting:
            expected = "verified" if base_status == "verified" else "partially_verified"
        else:
            expected = "insufficient_evidence"
        question = f"Is this claim supported by the available evidence? {text}"[:300]
        return TaskDefinition.model_validate({
            "task_id": new_id("tsk"), "title": f"Claim: {text}"[:120], "description": "Frozen from a Knowledge Map claim; gold labels are derived, not human-reviewed.",
            "question": question,
            "claims": [{"claim_id": "c1", "text": text, "expected_status": expected, "supporting_docs": supporting if expected != "insufficient_evidence" else [],
                        "contradicting_docs": contradicting}],
            "corpus": corpus, "tags": ["from-claim"] + (["challenged"] if challenge_ids else []), "gold_quality": "derived",
            "provenance": {"kind": "knowledge_claim", "claim_id": claim_id, "investigation_id": investigation_id,
                           "challenge_ids": challenge_ids[:20], "gold_rule": "derived-v1"},
        })

    # ── configs ──────────────────────────────────────────────────────────────
    def _config_view(self, config_id: str, c: AgentConfig, *, builtin: bool, created_at: Optional[str] = None) -> dict[str, Any]:
        d = c.model_dump(mode="json")
        return {"config_id": config_id, "builtin": builtin, "created_at": created_at, "config_hash": c.config_hash, **d,
                "model_known": c.model in self.models, "price_known": self.models.get(c.model, {}).get("price_in") is not None}

    def list_configs(self, user_id: str) -> dict[str, Any]:
        items = [self._config_view(cid, c, builtin=True) for cid, c in suites.PRESETS.items()]
        for row in self.repo.list_config_rows(user_id):
            items.append(self._config_view(row["config_id"], AgentConfig.model_validate(_j(row["config_json"])), builtin=False, created_at=row["created_at"]))
        return {"items": items, "total": len(items)}

    def resolve_config(self, user_id: str, config_id: str) -> AgentConfig:
        if config_id in suites.PRESETS:
            return suites.PRESETS[config_id]
        return AgentConfig.model_validate(_j(self.repo.get_config_row(user_id, config_id)["config_json"]))

    def _check_model(self, config: AgentConfig) -> None:
        if config.model not in self.models:
            raise ValueError(f"Model '{config.model}' is not allowed. Choose one of: {', '.join(sorted(self.models))}.")

    def create_config(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = AgentConfig.model_validate(payload)
        self._check_model(config)
        row = self.repo.create_config(user_id, config, self._clock())
        return self._config_view(row["config_id"], config, builtin=False, created_at=row["created_at"])

    def delete_config(self, user_id: str, config_id: str) -> None:
        if config_id in suites.PRESETS:
            raise BenchConflict("Preset configurations cannot be deleted.")
        self.repo.delete_config(user_id, config_id)

    # ── runs ─────────────────────────────────────────────────────────────────
    def create_run(self, user_id: str, config_id: str, *, task_ids: Optional[list[str]] = None, suite_id: Optional[str] = None,
                   label: str = "") -> dict[str, Any]:
        if bool(task_ids) == bool(suite_id):
            raise ValueError("Provide exactly one of task_ids or suite_id.")
        config = self.resolve_config(user_id, config_id)
        self._check_model(config)
        suite_hash = None
        if suite_id:
            try:
                suite = suites.load_suite(suite_id)
            except KeyError:
                raise BenchNotFound(suite_id) from None
            tasks, suite_hash = list(suite.tasks), suite.suite_hash
        else:
            ordered = list(dict.fromkeys(task_ids or []))
            tasks = [self.resolve_task(user_id, t) for t in ordered]
        if not 1 <= len(tasks) <= MAX_TASKS_PER_RUN:
            raise ValueError(f"A run needs between 1 and {MAX_TASKS_PER_RUN} tasks.")
        fp = run_fingerprint(config.config_hash, {t.task_id: t.task_hash for t in tasks})
        row = self.repo.create_run(user_id, label=label.strip()[:120], config_id=config_id, config=config, suite_id=suite_id, suite_hash=suite_hash,
                                   fingerprint=fp, evaluator_version=EVALUATOR_VERSION, tasks=tasks, reproduces_run_id=None, now=self._clock())
        return self._run_view(user_id, row)

    def reproduce_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        """A NEW run over the exact config and task snapshots of an earlier one."""
        old = self.repo.get_run(user_id, run_id)
        config = AgentConfig.model_validate(_j(old["config_json"]))
        tasks = [TaskDefinition.model_validate(_j(c["task_json"])) for c in self.repo.cases(user_id, run_id)]
        fp = run_fingerprint(config.config_hash, {t.task_id: t.task_hash for t in tasks})
        row = self.repo.create_run(user_id, label=f"Reproduction of {old['label'] or run_id}"[:120], config_id=old["config_id"], config=config,
                                   suite_id=old["suite_id"], suite_hash=old["suite_hash"], fingerprint=fp, evaluator_version=EVALUATOR_VERSION,
                                   tasks=tasks, reproduces_run_id=run_id, now=self._clock())
        return self._run_view(user_id, row)

    def execute_run(self, user_id: str, run_id: str) -> tuple[dict[str, Any], bool]:
        """Execute (or resume) a run. Idempotent: a completed run is returned as-is (``ran`` False)."""
        self.repo.get_run(user_id, run_id)  # ownership / existence first
        try:
            row, attempt = self.repo.begin_run(user_id, run_id, self._clock())
        except BenchConflict as exc:
            if str(exc) == "completed":
                return self._run_view(user_id, self.repo.get_run(user_id, run_id)), False
            raise
        error_type: Optional[str] = None
        try:
            config = AgentConfig.model_validate(_j(row["config_json"]))
            try:
                self._check_model(config)
            except ValueError as exc:
                raise _RunAbort("model_not_allowed") from exc
            try:
                llm = self._llm_factory(config)
            except Exception as exc:
                logger.warning("AtlasBench model unavailable", extra={"run_id": run_id, "error_type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)})
                raise _RunAbort("model_unavailable") from exc
            # M9: AtlasBench measures ONE model per run. Pin it: no autopilot, no fallback, not counted as production usage.
            with routing_scope(user_id, "benchmark", benchmark=True, model=config.model):
                for case in self.repo.pending_cases(user_id, run_id):
                    self._run_one(user_id, run_id, attempt, config, llm, case)
        except _RunAbort as exc:
            error_type = exc.error_type
        except BenchConflict:
            raise  # lost the lease: another attempt owns the run now
        except Exception as exc:
            logger.warning("AtlasBench run crashed", extra={"run_id": run_id, "error_type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)})
            error_type = type(exc).__name__

        cases = self.repo.cases(user_id, run_id)
        retryable = [c for c in cases if c["status"] == "pending" or (c["status"] == "failed" and c["attempt_count"] < MAX_CASE_ATTEMPTS)]
        summary = None
        if error_type is None and not retryable and any(c["status"] == "completed" for c in cases):
            summary = cmp.aggregate([self._agg_input(c) for c in cases])
        elif error_type is None:
            error_type = "cases_failed" if retryable else "all_cases_failed"
        self.repo.finish_run(user_id, run_id, attempt, summary=summary, error_type=error_type, now=self._clock())
        return self._run_view(user_id, self.repo.get_run(user_id, run_id)), True

    def _run_one(self, user_id: str, run_id: str, attempt: int, config: AgentConfig, llm: Any, case: dict[str, Any]) -> None:
        task = TaskDefinition.model_validate(_j(case["task_json"]))
        started = self._clock()
        t0 = time.perf_counter()
        guarded_llm = BudgetedLLM(llm, self.repository.db, user_id=user_id, model=config.model,
                                  max_tokens=config.max_tokens, purpose="benchmark")
        out = run_case(task, config, guarded_llm, run_key=run_id, user_id=user_id)
        out.latency["total_ms"] = max(out.latency.get("total_ms", 0), int((time.perf_counter() - t0) * 1000))
        out.usage["cost_usd"] = estimate_cost_usd(config.model, out.usage.get("prompt_tokens"), out.usage.get("completion_tokens"), self.models)
        scored = score_case(task, out, config.strategy)
        self.repo.record_case(
            user_id, run_id, task.task_id, attempt, status=scored["status"], composite=scored["composite"],
            scores={"scores": scored["scores"], "flags": scored["flags"]}, detail=scored["detail"], output=out.to_json(),
            trace=serialize_public_trace(out.trace), raw_llm=out.raw_llm, usage=out.usage, latency=out.latency,
            error_type=scored["error_type"], started_at=started, now=self._clock())

    @staticmethod
    def _agg_input(case: dict[str, Any]) -> dict[str, Any]:
        out = case_from_json(_j(case["output_json"], {}))
        sc = _j(case["scores_json"], {"scores": {}, "flags": []})
        detail = _j(case["detail_json"], {})
        return {"status": case["status"], "scores": sc.get("scores", {}), "flags": sc.get("flags", []), "composite": case["composite"],
                "false_conflict_rate": (detail.get("contradiction_handling") or {}).get("false_conflict_rate"),
                "usage": out.usage, "latency": out.latency, "tool_calls": len(out.calls)}

    # ── reads ────────────────────────────────────────────────────────────────
    def _run_view(self, user_id: str, row: dict[str, Any], *, with_cases: bool = False) -> dict[str, Any]:
        cases = self.repo.cases(user_id, row["run_id"])
        summary = _j(row["summary_json"])
        partial = False
        if summary is None and any(c["status"] == "completed" for c in cases):
            summary, partial = cmp.aggregate([self._agg_input(c) for c in cases]), True   # live view of an unfinished/failed run
        config = _j(row["config_json"])
        view = {
            "run_id": row["run_id"], "label": row["label"], "status": row["status"], "config_id": row["config_id"], "config_name": row["config_name"],
            "config_hash": row["config_hash"], "config": config, "suite_id": row["suite_id"], "suite_hash": row["suite_hash"],
            "fingerprint": row["fingerprint"], "evaluator_version": row["evaluator_version"], "reproduces_run_id": row["reproduces_run_id"],
            "case_count": row["case_count"], "cases_done": sum(1 for c in cases if c["status"] == "completed"),
            "cases_failed": sum(1 for c in cases if c["status"] == "failed"), "attempt_count": row["attempt_count"], "max_attempts": MAX_RUN_ATTEMPTS,
            "retryable": row["status"] in ("failed", "pending") and row["attempt_count"] < MAX_RUN_ATTEMPTS
                         and any(c["status"] == "pending" or (c["status"] == "failed" and c["attempt_count"] < MAX_CASE_ATTEMPTS) for c in cases),
            "error_type": row["error_type"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "started_at": row["started_at"], "completed_at": row["completed_at"],
            "summary": summary, "summary_partial": partial,
        }
        if with_cases:
            view["cases"] = [self._case_brief(c) for c in cases]
        return view

    @staticmethod
    def _case_brief(c: dict[str, Any]) -> dict[str, Any]:
        sc = _j(c["scores_json"], {"scores": {}, "flags": []})
        return {"task_id": c["task_id"], "title": c["task_title"], "task_hash": c["task_hash"], "status": c["status"], "attempt_count": c["attempt_count"],
                "composite": c["composite"], "scores": sc.get("scores", {}), "flags": sc.get("flags", []), "error_type": c["error_type"],
                "latency_ms": c["latency_ms"], "total_tokens": c["total_tokens"], "cost_usd": c["cost_usd"]}

    def get_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        return self._run_view(user_id, self.repo.get_run(user_id, run_id, now=self._clock()), with_cases=True)

    def list_runs(self, user_id: str, **filters: Any) -> dict[str, Any]:
        rows, total = self.repo.list_runs(user_id, now=self._clock(), **filters)
        return {"items": [self._run_view(user_id, r) for r in rows], "total": total,
                "limit": max(1, min(int(filters.get("limit", 25)), 100)), "offset": max(0, int(filters.get("offset", 0)))}

    def delete_run(self, user_id: str, run_id: str) -> None:
        self.repo.delete_run(user_id, run_id)

    def inspect_case(self, user_id: str, run_id: str, task_id: str) -> dict[str, Any]:
        c = self.repo.get_case(user_id, run_id, task_id)
        task = TaskDefinition.model_validate(_j(c["task_json"]))
        out = _j(c["output_json"], {})
        return {
            **self._case_brief(c), "run_id": run_id, "task": task.public_dict(), "output": out, "detail": _j(c["detail_json"], {}),
            "trace": _j(c["trace_json"], []), "model_outputs": _j(c["raw_llm_json"], []),
            "usage": {**(out.get("usage") or {}), "prompt_tokens": c["prompt_tokens"], "completion_tokens": c["completion_tokens"],
                      "total_tokens": c["total_tokens"], "cost_usd": c["cost_usd"]},
            "latency": {"total_ms": c["latency_ms"], "llm_ms": c["llm_latency_ms"], **{k: v for k, v in (out.get("latency") or {}).items() if k == "tool_ms"}},
        }

    def compare(self, user_id: str, run_ids: list[str]) -> dict[str, Any]:
        run_ids = list(dict.fromkeys(run_ids))
        if not 2 <= len(run_ids) <= MAX_COMPARE_RUNS:
            raise ValueError(f"Compare between 2 and {MAX_COMPARE_RUNS} different runs.")
        runs = []
        for rid in run_ids:
            row = self.repo.get_run(user_id, rid)
            if row["status"] != "completed":
                raise BenchConflict(f"Run {rid} has not completed; only completed runs can be compared.")
            cases = {}
            for c in self.repo.cases(user_id, rid):
                sc = _j(c["scores_json"], {"scores": {}, "flags": []})
                cases[c["task_id"]] = {"task_hash": c["task_hash"], "title": c["task_title"], "status": c["status"],
                                       "scores": sc.get("scores", {}), "composite": c["composite"]}
            runs.append({"run_id": rid, "label": row["label"], "config_name": row["config_name"], "config": _j(row["config_json"]),
                         "config_hash": row["config_hash"], "fingerprint": row["fingerprint"], "evaluator_version": row["evaluator_version"],
                         "summary": _j(row["summary_json"]), "cases": cases})
        return cmp.compare_runs(runs)

    def arbitrate(self, user_id: str, task_id: str, run_ids: list[str]) -> dict[str, Any]:
        """Select a stored answer from distinct models on an identical frozen task."""
        run_ids = list(dict.fromkeys(run_ids))
        if not 2 <= len(run_ids) <= MAX_COMPARE_RUNS:
            raise ValueError(f"Arbitrate between 2 and {MAX_COMPARE_RUNS} different runs.")
        candidates: list[dict[str, Any]] = []
        task_snapshot: Optional[dict[str, Any]] = None
        for run_id in run_ids:
            row = self.repo.get_run(user_id, run_id)
            if row["status"] not in ("completed", "failed"):
                raise BenchConflict(f"Run {run_id} is still in progress and cannot be arbitrated.")
            case = self.repo.get_case(user_id, run_id, task_id)
            if task_snapshot is None:
                task_snapshot = TaskDefinition.model_validate(_j(case["task_json"])).model_dump(mode="json")
            config = _j(row["config_json"], {})
            scores_row = _j(case["scores_json"], {})
            detail = _j(case["detail_json"], {})
            output = _j(case["output_json"], {})
            usage = output.get("usage", {})
            candidates.append({
                "run_id": run_id, "task_id": task_id, "task_hash": case["task_hash"],
                "model": config.get("model"), "provider": "groq", "status": case["status"],
                "config": config, "config_hash": row["config_hash"], "fingerprint": row["fingerprint"],
                "evaluator_version": row["evaluator_version"], "scores": scores_row.get("scores", {}),
                "flags": scores_row.get("flags", []), "composite": case["composite"],
                "detail": detail, "output": output if case["status"] == "completed" else None,
                "model_outputs": _j(case["raw_llm_json"], []),
                "usage": {**usage, "prompt_tokens": case["prompt_tokens"], "completion_tokens": case["completion_tokens"],
                          "total_tokens": case["total_tokens"], "cost_usd": case["cost_usd"]},
                "latency_ms": case["latency_ms"],
            })
        from backend.core.config import get_settings
        decision = arbitrate_candidates(
            candidates,
            minimum_quality_score=get_settings().atlasbench_min_arbitration_quality,
        )
        decision["candidates"] = candidates
        decision["task_snapshot"] = task_snapshot
        # The legacy indexed column is non-null. Store an empty value for a
        # durable abstention, and expose the semantic null from decision_json.
        saved = self.repo.create_arbitration(user_id, task_id, decision["task_hash"], run_ids,
                                             str(decision["selected_run_id"] or ""), decision, self._clock())
        return saved

    def get_arbitration(self, user_id: str, arbitration_id: str) -> dict[str, Any]:
        return self.repo.get_arbitration(user_id, arbitration_id)

    def list_arbitrations(self, user_id: str, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        rows, total = self.repo.list_arbitrations(user_id, limit, offset)
        return {"items": rows, "total": total, "limit": max(1, min(int(limit), 100)), "offset": max(0, int(offset))}


class _RunAbort(Exception):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type
