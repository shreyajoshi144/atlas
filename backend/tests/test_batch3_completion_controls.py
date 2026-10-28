"""Focused Batch 3 completion regressions for arbitration, budgets and routing metadata."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from backend.bench.arbitration import ArbitrationInputError, arbitrate_candidates
from backend.bench.models import EVALUATOR_VERSION
from backend.database import SQLiteManager
from backend.ops.repository import OperationalRepository, UsageLimitExceeded


def _candidate(run_id: str, model: str, quality: float) -> dict:
    scores = {
        "claim_correctness": quality,
        "citation_accuracy": quality,
        "evidence_grounding": quality,
        "evidence_coverage": quality,
        "contradiction_handling": quality,
        "trajectory_quality": quality,
    }
    return {"run_id": run_id, "model": model, "task_id": "task-1", "task_hash": "a" * 64,
            "evaluator_version": EVALUATOR_VERSION, "status": "completed", "scores": scores,
            "output": {"claims": [{"claim": "stored result"}]}}


def test_arbitration_configurable_measured_quality_threshold_and_provenance():
    candidates = [_candidate("run-a", "model-a", 0.61), _candidate("run-b", "model-b", 0.55)]
    result = arbitrate_candidates(candidates, minimum_quality_score=0.60)
    assert result["decision"] == "selected"
    assert result["minimum_quality_score"] == 0.60
    assert result["quality_score_kind"] == "measured_composite_of_evaluator_metrics"
    abstained = arbitrate_candidates(candidates, minimum_quality_score=0.7)
    assert abstained["decision"] == "abstained"
    assert abstained["selected_run_id"] is None and abstained["selected_output"] is None
    assert "below the configured minimum" in abstained["abstention_reason"]
    assert abstained["candidate_composite_scores"]


def test_arbitration_rejects_invalid_quality_threshold():
    candidates = [_candidate("run-a", "model-a", 0.8), _candidate("run-b", "model-b", 0.6)]
    with pytest.raises(ValueError, match="finite number between 0 and 1"):
        arbitrate_candidates(candidates, minimum_quality_score=float("nan"))


def _database_with_user(path):
    db = SQLiteManager(str(path))
    db.initialize_database()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO users(user_id,email,name,password_hash,password_salt,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("budget-user", "budget@example.test", "Budget", "hash", "salt", "user", now, now),
        )
        conn.commit()
    return db


def test_budget_reservation_is_atomic_under_concurrent_requests(tmp_path):
    db = _database_with_user(tmp_path / "budget-concurrent.db")
    repository = OperationalRepository(db)
    now = datetime.now(timezone.utc)

    def reserve(_):
        try:
            return repository.reserve_call("budget-user", "playground", "model-a", 0.0001,
                                           default_calls=1, default_spend=1.0, now=now)
        except UsageLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reserve, range(8)))
    assert sum(value is not None for value in outcomes) == 1
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_budget_events WHERE user_id='budget-user'").fetchone()[0] == 1


def test_routing_usage_view_distinguishes_fallback_attempt_and_outcome():
    from backend.routing.service import RoutingService

    row = {
        "usage_id": 1, "created_at": "2026-01-01T00:00:00+00:00", "run_ref": None, "purpose": "playground",
        "policy_id": None, "policy_name": "test", "mode": "explicit", "reason_code": "explicit", "reason": "test",
        "requested_model": "model-a", "actual_model": None, "status": "failed", "fallback_used": 0,
        "fallback_reason": "Fallback exhausted", "attempts_json": '[{"model":"model-a","outcome":"failed"},{"model":"model-b","outcome":"failed"}]',
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None, "tokens_reported": 0,
        "est_cost_usd": None, "actual_cost_usd": None, "latency_ms": 20, "llm_latency_ms": 20,
        "error_type": "ProviderError", "evidence_run_id": None,
    }
    view = RoutingService._usage_view(row)
    assert view["fallback_attempted"] is True
    assert view["fallback_succeeded"] is False
    assert view["fallback_failed"] is True
    assert view["actual_model"] is None

    row["status"] = "ok"
    row["fallback_used"] = 1
    row["actual_model"] = "model-b"
    view = RoutingService._usage_view(row)
    assert view["fallback_attempted"] is True
    assert view["fallback_succeeded"] is True
    assert view["fallback_failed"] is False


def test_abstention_decision_can_be_persisted_without_a_selected_run(tmp_path):
    from backend.bench.repository import BenchRepository

    db = _database_with_user(tmp_path / "abstention.db")
    repo = BenchRepository(db)
    decision = {
        "decision": "abstained", "selected_run_id": None, "selected_output": None,
        "abstention_reason": "Below threshold", "rationale": ["No candidate selected"],
    }
    saved = repo.create_arbitration("budget-user", "task-1", "a" * 64,
                                    ["run-a", "run-b"], "", decision,
                                    datetime.now(timezone.utc))
    assert saved["selected_run_id"] is None
    assert saved["decision"]["decision"] == "abstained"
    assert saved["decision"]["abstention_reason"] == "Below threshold"
