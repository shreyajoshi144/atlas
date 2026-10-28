from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("GROQ_API_KEY", "test_key")
os.environ.setdefault("TAVILY_API_KEY", "test_key")

from backend.database.repositories import ResearchRepository
from backend.evaluation import run_pipeline_evaluation
from backend.evaluation.benchmark import build_benchmark_plan
from backend.evaluation.evaluators.answer_evaluator import evaluate_answer_groundedness
from backend.evaluation.evaluators.grounding_evaluator import evaluate_claim_grounding
from backend.evaluation.evaluators.retrieval_evaluator import evaluate_retrieval
from backend.evaluation.evaluators.verification_quality_evaluator import evaluate_verification_quality
from backend.evaluation.models import EvaluationSummary
from backend.evaluation.testdata.loader import (
    build_ranked_sources,
    build_verification_summary,
    load_scenarios,
)
from backend.evaluation.text_utils import (
    containment_ratio,
    cosine_similarity,
    extract_urls,
    normalize_url,
    tokenize,
)


# ─────────────────────────────────────────────────────────────────────────
# text_utils
# ─────────────────────────────────────────────────────────────────────────


def test_tokenize_lowercases_and_splits_on_non_alnum():
    assert tokenize("Solid-State Batteries, 2026!") == ["solid", "state", "batteries", "2026"]


def test_cosine_similarity_identical_text_is_one():
    text = "energy density improvements of over 40 percent"
    assert cosine_similarity(text, text) == pytest.approx(1.0)


def test_cosine_similarity_empty_text_is_zero():
    assert cosine_similarity("", "something") == 0.0
    assert cosine_similarity("something", "") == 0.0


def test_containment_ratio_fully_contained():
    assert containment_ratio("battery cost barrier", "the battery cost remains a barrier here") == 1.0


def test_containment_ratio_not_contained_is_zero():
    assert containment_ratio("nuclear fusion breakthrough", "completely unrelated text here") == 0.0


def test_extract_urls_strips_trailing_punctuation():
    text = "See https://example.com/a, and https://example.com/b."
    assert extract_urls(text) == ["https://example.com/a", "https://example.com/b"]


def test_normalize_url_strips_trailing_slash_and_lowercases():
    assert normalize_url("HTTPS://Example.com/Path/") == "https://example.com/path"


# ─────────────────────────────────────────────────────────────────────────
# retrieval_evaluator
# ─────────────────────────────────────────────────────────────────────────


def test_evaluate_retrieval_no_sources_flags_critical():
    result = evaluate_retrieval("topic", requested_top_k=5, ranked_sources=[])
    assert result.score == 0.0
    assert result.returned_source_count == 0
    assert any(f.code == "no_sources" and f.severity == "critical" for f in result.flags)


def test_evaluate_retrieval_good_sources_score_higher_than_low_relevance_sources():
    good = [
        SimpleNamespace(relevance_score=85.0, credibility_score=75.0, domain=f"good{i}.com")
        for i in range(4)
    ]
    poor = [
        SimpleNamespace(relevance_score=10.0, credibility_score=20.0, domain="onlydomain.com")
        for _ in range(2)
    ]
    good_result = evaluate_retrieval("topic", requested_top_k=4, ranked_sources=good)
    poor_result = evaluate_retrieval("topic", requested_top_k=4, ranked_sources=poor)
    assert good_result.score > poor_result.score
    assert any(f.code == "single_domain" for f in poor_result.flags)


# ─────────────────────────────────────────────────────────────────────────
# grounding_evaluator
# ─────────────────────────────────────────────────────────────────────────


def test_evaluate_claim_grounding_no_claims_returns_info_flag():
    result = evaluate_claim_grounding(None, "some evidence text")
    assert result.claim_count == 0
    assert any(f.code == "no_claims" for f in result.flags)


def test_evaluate_claim_grounding_flags_unmatched_source_id():
    claim = SimpleNamespace(
        claim_id="c1",
        claim_text="Some claim",
        status="verified",
        confidence=0.9,
        evidence=[
            SimpleNamespace(
                source_id="",  # never matched to a retrieved source
                evidence_text="this text is nowhere in the corpus at all",
            )
        ],
    )
    summary = SimpleNamespace(claims=[claim])
    result = evaluate_claim_grounding(summary, "completely different corpus text")
    assert result.unmatched_source_citations == 1
    assert any(f.code == "unmatched_citations" and f.severity == "critical" for f in result.flags)


# ─────────────────────────────────────────────────────────────────────────
# verification_quality_evaluator
# ─────────────────────────────────────────────────────────────────────────


def test_evaluate_verification_quality_unavailable_status():
    result = evaluate_verification_quality(None, "failed")
    assert result.score == 0.0
    assert any(f.code == "verification_unavailable" for f in result.flags)


def test_evaluate_verification_quality_flags_verified_without_evidence():
    claim = SimpleNamespace(status="verified", confidence=0.95, evidence=[])
    summary = SimpleNamespace(claims=[claim], overall_verification_score=80.0)
    result = evaluate_verification_quality(summary, "succeeded")
    assert result.empty_evidence_verified_count == 1
    assert any(f.code == "verified_without_evidence" and f.severity == "critical" for f in result.flags)


def test_evaluate_verification_quality_flags_invalid_stance():
    evidence = SimpleNamespace(stance="agrees_strongly")  # not in {support, contradict, mixed}
    claim = SimpleNamespace(status="verified", confidence=0.8, evidence=[evidence])
    summary = SimpleNamespace(claims=[claim], overall_verification_score=75.0)
    result = evaluate_verification_quality(summary, "succeeded")
    assert result.invalid_stance_count == 1
    assert any(f.code == "invalid_stance" for f in result.flags)


# ─────────────────────────────────────────────────────────────────────────
# answer_evaluator
# ─────────────────────────────────────────────────────────────────────────


def test_evaluate_answer_groundedness_empty_report():
    result = evaluate_answer_groundedness("", [], [], "evidence")
    assert result.score == 0.0
    assert any(f.code == "empty_report" and f.severity == "critical" for f in result.flags)


def test_evaluate_answer_groundedness_flags_fabricated_citation():
    sources = [SimpleNamespace(url="https://real-source.example/a", domain="real-source.example")]
    report = "Read more at https://not-a-real-source.example/fake for the full account."
    result = evaluate_answer_groundedness(report, [], sources, "evidence text unrelated to the url")
    assert result.invalid_citation_count == 1
    assert result.valid_citation_count == 0
    assert any(f.code == "uncited_or_fabricated_url" and f.severity == "critical" for f in result.flags)


def test_evaluate_answer_groundedness_valid_citation_has_no_fabrication_flag():
    sources = [SimpleNamespace(url="https://real-source.example/a", domain="real-source.example")]
    report = "As reported at https://real-source.example/a, the finding holds."
    result = evaluate_answer_groundedness(report, [], sources, "the finding holds according to reports")
    assert result.invalid_citation_count == 0
    assert result.citation_precision == 1.0
    assert not any(f.code == "uncited_or_fabricated_url" for f in result.flags)


# ─────────────────────────────────────────────────────────────────────────
# pipeline_evaluator — golden dataset (parametrized over every scenario)
# ─────────────────────────────────────────────────────────────────────────


def _run_scenario(scenario: dict) -> EvaluationSummary:
    sources = build_ranked_sources(scenario)
    verification_summary = build_verification_summary(scenario)
    return run_pipeline_evaluation(
        topic=scenario["topic"],
        search_query=scenario["topic"],
        requested_top_k=scenario["requested_top_k"],
        ranked_sources=sources,
        evidence_text=scenario.get("evidence_text", ""),
        verification_summary=verification_summary,
        verification_status="succeeded" if verification_summary is not None else "unavailable",
        report_markdown=scenario["report_markdown"],
        key_statistics=scenario.get("key_statistics", []),
        report_id=f"report_{scenario['id']}",
        session_id=f"session_{scenario['id']}",
        research_plan=build_benchmark_plan(scenario),
    )


@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda s: s["id"])
def test_golden_dataset_scenarios(scenario: dict):
    summary = _run_scenario(scenario)
    expected = scenario["expected"]

    assert summary.errors == [], f"evaluator(s) raised unexpectedly: {summary.errors}"

    if "retrieval_min_score" in expected:
        assert summary.retrieval.score >= expected["retrieval_min_score"]
    if "retrieval_max_score" in expected:
        assert summary.retrieval.score <= expected["retrieval_max_score"]
    if "grounding_min_score" in expected:
        assert summary.grounding.score >= expected["grounding_min_score"]
    if "grounding_max_score" in expected:
        assert summary.grounding.score <= expected["grounding_max_score"]
    if "verification_quality_min_score" in expected:
        assert summary.verification_quality.score >= expected["verification_quality_min_score"]
    if "verification_quality_max_score" in expected:
        assert summary.verification_quality.score <= expected["verification_quality_max_score"]
    if "answer_groundedness_min_score" in expected:
        assert summary.answer_groundedness.score >= expected["answer_groundedness_min_score"]
    if "citation_correctness_max_score" in expected:
        assert summary.citation_correctness.score <= expected["citation_correctness_max_score"]
    if "completeness_min_score" in expected:
        assert summary.completeness.score >= expected["completeness_min_score"]

    flag_codes = {f.code for f in summary.flags}
    for expected_code in expected.get("expect_flag_codes", []):
        assert expected_code in flag_codes, f"expected flag '{expected_code}' not raised (got {flag_codes})"


def test_pipeline_evaluation_overall_score_is_weighted_average_of_components():
    scenario = next(s for s in load_scenarios() if s["id"] == "well_grounded_report")
    summary = _run_scenario(scenario)
    assert summary.overall_score is not None
    assert 0.0 <= summary.overall_score <= 1.0


# ─────────────────────────────────────────────────────────────────────────
# pipeline_evaluator — non-blocking guarantee
# ─────────────────────────────────────────────────────────────────────────


class _ExplodingClaims:
    """Mimics a verification_summary whose .claims raises when iterated,
    to prove a single evaluator's internal failure cannot escape
    run_pipeline_evaluation."""

    @property
    def claims(self):
        raise RuntimeError("boom")


def test_run_pipeline_evaluation_never_raises_on_malformed_verification_summary():
    summary = run_pipeline_evaluation(
        topic="t",
        search_query="t",
        requested_top_k=3,
        ranked_sources=[SimpleNamespace(relevance_score=50.0, credibility_score=50.0, domain="x.com")],
        evidence_text="evidence",
        verification_summary=_ExplodingClaims(),
        verification_status="succeeded",
        report_markdown="A short report.",
        key_statistics=[],
    )
    assert summary.grounding is None
    assert any("grounding" in e for e in summary.errors)
    # Other components still computed despite grounding failing.
    assert summary.retrieval is not None
    assert summary.answer_groundedness is not None


def test_run_pipeline_evaluation_never_raises_on_completely_malformed_sources():
    summary = run_pipeline_evaluation(
        topic="t",
        search_query="t",
        requested_top_k=3,
        ranked_sources="not-a-list-of-sources-at-all",  # deliberately wrong shape
        evidence_text="evidence",
        verification_summary=None,
        verification_status="unavailable",
        report_markdown="A short report.",
        key_statistics=[],
    )
    # Should not raise; components that cannot cope record an error instead.
    assert isinstance(summary, EvaluationSummary)


# ─────────────────────────────────────────────────────────────────────────
# EvaluationRepository — persistence round-trip
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    from backend.core import config as config_module

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "eval_test.db"))
    config_module.get_settings.cache_clear()

    repository = ResearchRepository()
    repository.initialize()
    with repository.db.get_connection() as conn:
        conn.execute("INSERT INTO users (user_id, email, password_hash, password_salt, name, created_at, updated_at) VALUES ('u_eval', 'eval@example.com', 'hash', 'salt', 'Eval', '2026-01-01', '2026-01-01')")
        conn.execute("INSERT INTO topics (topic_id, user_id, topic, created_at, updated_at) VALUES ('topic_1', 'u_eval', 'topic_1', '2026-01-01', '2026-01-01')")
        conn.execute("INSERT INTO research_sessions (session_id, topic_id, user_id, topic, status, created_at, updated_at) VALUES ('session_1', 'topic_1', 'u_eval', 'topic_1', 'completed', '2026-01-01', '2026-01-01')")
        conn.execute("INSERT INTO reports (report_id, topic_id, session_id, topic, summary, report_markdown, key_statistics_json, swot_json, verification_json, executive_brief_json, created_at, updated_at) VALUES ('report_1', 'topic_1', 'session_1', 'topic_1', '', '', '[]', '{}', NULL, NULL, '2026-01-01', '2026-01-01')")
        conn.execute("INSERT INTO reports (report_id, topic_id, session_id, topic, summary, report_markdown, key_statistics_json, swot_json, verification_json, executive_brief_json, created_at, updated_at) VALUES ('report_multi', 'topic_1', 'session_1', 'topic_1', '', '', '[]', '{}', NULL, NULL, '2026-01-01', '2026-01-01')")
        conn.commit()
    yield repository.evaluations

    config_module.get_settings.cache_clear()


def test_evaluation_repository_save_and_fetch_round_trip(repo):
    scenario = next(s for s in load_scenarios() if s["id"] == "well_grounded_report")
    summary = _run_scenario(scenario)

    evaluation_id = repo.save_evaluation("report_1", "session_1", "topic_1", summary)
    assert evaluation_id

    row = repo.get_latest_by_report_id("report_1")
    assert row is not None
    assert row["report_id"] == "report_1"
    assert row["overall_score"] == pytest.approx(summary.overall_score)

    restored = EvaluationSummary.model_validate_json(row["details_json"])
    assert restored.retrieval.score == pytest.approx(summary.retrieval.score)
    assert restored.grounding.score == pytest.approx(summary.grounding.score)


def test_evaluation_repository_returns_none_for_unknown_report(repo):
    assert repo.get_latest_by_report_id("does-not-exist") is None


def test_evaluation_repository_returns_latest_when_multiple_rows_exist(repo):
    scenario = next(s for s in load_scenarios() if s["id"] == "well_grounded_report")
    summary = _run_scenario(scenario)

    repo.save_evaluation("report_multi", "session_1", "topic_1", summary)
    second_id = repo.save_evaluation("report_multi", "session_1", "topic_1", summary)

    row = repo.get_latest_by_report_id("report_multi")
    assert row["evaluation_id"] == second_id
