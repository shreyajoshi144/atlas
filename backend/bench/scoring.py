"""Pure, deterministic scoring of one case. No I/O, no clock, no LLM judge.

Every metric is a documented heuristic over (task gold, case output) and is
reported on its own — the composite is a convenience number, never the only one.
Metrics that do not apply to a task are ``None`` (not 0), so they never drag an
average down and never pretend to have been measured.

Thresholds (all in one place so they are reviewable):
* a produced claim matches a gold claim when bag-of-words cosine >= ``MATCH_THRESHOLD``
* a cited passage is *grounded* in a document when it is a normalised substring of it or
  >= ``GROUNDED_CONTAINMENT`` of its informative tokens occur in it
"""
from __future__ import annotations

from typing import Any, Optional

from backend.bench.models import AFFIRMATIVE, COMPOSITE_WEIGHTS, NEGATIVE, STRATEGIES, TaskDefinition
from backend.bench.runner import CaseOutput
from backend.evaluation import retrieval_metrics
from backend.evaluation.text_utils import containment_ratio, cosine_similarity

MATCH_THRESHOLD = 0.5
GROUNDED_CONTAINMENT = 0.8
SAME_POLARITY_CREDIT = 0.5


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), n)


def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def polarity(status: str) -> str:
    return "affirmative" if status in AFFIRMATIVE else "negative" if status in NEGATIVE else "unknown"


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def is_grounded(passage: str, doc_text: str) -> bool:
    p = _norm(passage)
    if not p:
        return False
    return p in _norm(doc_text) or containment_ratio(passage, doc_text) >= GROUNDED_CONTAINMENT


# ── claim matching ───────────────────────────────────────────────────────────
def match_claims(task: TaskDefinition, produced: list[dict[str, Any]]) -> dict[str, int]:
    """Greedy one-to-one assignment gold claim_id -> index into ``produced`` by text similarity.

    A produced claim may only be matched to the gold claim it resembles MOST: near-identical gold
    claims ("passes" vs "is slow") must not both be satisfied by one claim and its duplicate.
    """
    sims = [[cosine_similarity(g.text, p.get("claim_text", "")) for p in produced] for g in task.claims]
    best_for_produced = [max((sims[gi][pi] for gi in range(len(task.claims))), default=0.0) for pi in range(len(produced))]
    pairs = sorted(((sims[gi][pi], gi, pi) for gi in range(len(task.claims)) for pi in range(len(produced))
                    if sims[gi][pi] >= MATCH_THRESHOLD and sims[gi][pi] >= best_for_produced[pi] - 1e-9),
                   key=lambda t: (-t[0], t[1], t[2]))
    used_g: set[int] = set()
    used_p: set[int] = set()
    out: dict[str, int] = {}
    for _, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        out[task.claims[gi].claim_id] = pi
    return out


def _status_credit(expected: str, got: str) -> float:
    if expected == got:
        return 1.0
    return SAME_POLARITY_CREDIT if polarity(expected) == polarity(got) != "unknown" else 0.0


# ── citation analysis (shared by several metrics) ────────────────────────────
def _analyse_citations(task: TaskDefinition, out: CaseOutput, matches: dict[str, int]) -> list[dict[str, Any]]:
    gold_by_index = {pi: gid for gid, pi in matches.items()}
    gold = {c.claim_id: c for c in task.claims}
    scraped = set(out.scraped)
    rows: list[dict[str, Any]] = []
    for pi, claim in enumerate(out.claims):
        gid = gold_by_index.get(pi)
        g = gold.get(gid) if gid else None
        for ev in claim.get("evidence", []):
            doc = task.doc_by_url(ev.get("url", ""))
            in_corpus = doc is not None
            provenance = in_corpus and doc.doc_id in scraped          # a page THIS run actually retrieved
            grounded = bool(provenance and is_grounded(ev.get("evidence_text", ""), doc.content))
            expected_role = None
            if g is not None and doc is not None:
                expected_role = ("support" if doc.doc_id in g.supporting_docs
                                 else "contradict" if doc.doc_id in g.contradicting_docs else None)
            relevant = None
            if doc is not None:
                if g is not None:
                    relevant = doc.doc_id in g.required_docs
                else:
                    relevant = doc.doc_id in task.required_docs
            rows.append({
                "claim_index": pi, "gold_claim_id": gid, "url": ev.get("url"), "doc_id": doc.doc_id if doc else None,
                "stance": ev.get("stance"), "in_corpus": in_corpus, "provenance_valid": bool(provenance), "grounded": grounded,
                "relevant": relevant, "expected_role": expected_role,
                "stance_correct": None if expected_role is None else (ev.get("stance") == expected_role),
                "injection": bool(doc and doc.injection), "distractor": bool(doc and doc.distractor),
            })
    return rows


def _claim_correctness(task: TaskDefinition, out: CaseOutput, matches: dict[str, int]) -> dict[str, Any]:
    credits, exact = [], []
    details = []
    abstained = out.assessment == "no_evidence" and not out.claims
    for g in task.claims:
        pi = matches.get(g.claim_id)
        got = out.claims[pi]["status"] if pi is not None else None
        credit = _status_credit(g.expected_status, got) if got else 0.0
        if got is None and abstained and g.expected_status == "insufficient_evidence":
            credit, got = 1.0, "abstained"     # correctly declined to answer when no evidence exists
        credits.append(credit)
        exact.append(1.0 if (got == g.expected_status or got == "abstained") else 0.0)
        details.append({"claim_id": g.claim_id, "text": g.text, "expected": g.expected_status, "got": got,
                        "credit": credit, "matched": pi is not None})
    produced = len(out.claims)
    return {
        "score": _r(_mean(credits)), "exact_accuracy": _r(_mean(exact)),
        "recall": _r(len(matches) / len(task.claims)), "precision": _r(len(matches) / produced) if produced else None,
        "extra_claims": produced - len(matches), "details": details,
    }


def _citation_accuracy(rows: list[dict[str, Any]], need_evidence: bool) -> dict[str, Any]:
    if not rows:
        return {"score": 0.0 if need_evidence else None, "citations": 0, "reason": "no_citations" if need_evidence else None}
    validity = _mean([1.0 if r["provenance_valid"] else 0.0 for r in rows])
    precision = _mean([1.0 if r["relevant"] else 0.0 for r in rows])   # a URL outside the corpus is never relevant
    stance = _mean([1.0 if r["stance_correct"] else 0.0 for r in rows if r["stance_correct"] is not None])
    parts = [validity, precision] + ([stance] if stance is not None else [])
    return {"score": _r(_mean(parts)), "citations": len(rows), "validity": _r(validity), "precision": _r(precision),
            "stance_accuracy": _r(stance), "invalid": sum(1 for r in rows if not r["provenance_valid"]),
            "distractor_citations": sum(1 for r in rows if r["distractor"] or r["injection"]),
            "injection_citations": sum(1 for r in rows if r["injection"])}


def _evidence_grounding(task: TaskDefinition, out: CaseOutput, rows: list[dict[str, Any]]) -> dict[str, Any]:
    affirmed = [(i, c) for i, c in enumerate(out.claims) if c["status"] in AFFIRMATIVE]
    ungrounded_verdicts = 0
    for i, _ in affirmed:
        backing = [r for r in rows if r["claim_index"] == i and r["grounded"] and r["stance"] == "support"]
        if not backing:
            ungrounded_verdicts += 1
    ungrounded_rate = (ungrounded_verdicts / len(affirmed)) if affirmed else None
    grounded_rate = _mean([1.0 if r["grounded"] else 0.0 for r in rows]) if rows else None
    if grounded_rate is None and ungrounded_rate is None:
        score = None
    elif grounded_rate is None:                      # affirmed claims with no evidence at all
        score = 1.0 - ungrounded_rate
    elif ungrounded_rate is None:
        score = grounded_rate
    else:
        score = 0.5 * grounded_rate + 0.5 * (1.0 - ungrounded_rate)
    return {"score": _r(score), "grounded_evidence_rate": _r(grounded_rate), "ungrounded_verdict_rate": _r(ungrounded_rate),
            "fabricated_evidence": sum(1 for r in rows if not r["grounded"]), "evidence_items": len(rows),
            "ungrounded_verdicts": ungrounded_verdicts}


def _evidence_coverage(task: TaskDefinition, out: CaseOutput, rows: list[dict[str, Any]], matches: dict[str, int]) -> dict[str, Any]:
    required_pairs = [(g.claim_id, d) for g in task.claims for d in g.required_docs]
    required_docs = task.required_docs
    retrieved = _mean([1.0 if d in out.scraped else 0.0 for d in required_docs]) if required_docs else None
    if not required_pairs:
        return {"score": None, "cited_coverage": None, "retrieved_coverage": None, "pairs": 0}
    hit = 0
    missing = []
    for gid, d in required_pairs:
        pi = matches.get(gid)
        ok = pi is not None and any(r["claim_index"] == pi and r["doc_id"] == d and r["provenance_valid"] for r in rows)
        hit += 1 if ok else 0
        if not ok:
            missing.append({"claim_id": gid, "doc_id": d})
    cov = hit / len(required_pairs)
    return {"score": _r(cov), "cited_coverage": _r(cov), "retrieved_coverage": _r(retrieved), "pairs": len(required_pairs), "missing": missing}


def _contradiction(task: TaskDefinition, out: CaseOutput, rows: list[dict[str, Any]], matches: dict[str, int]) -> dict[str, Any]:
    gold_conflict = [g for g in task.claims if g.contradicting_docs]
    per = []
    for g in gold_conflict:
        pi = matches.get(g.claim_id)
        if pi is None:
            per.append({"claim_id": g.claim_id, "detected": False, "both_sides": False, "status": None})
            continue
        status = out.claims[pi]["status"]
        mine = [r for r in rows if r["claim_index"] == pi and r["provenance_valid"]]
        cites_contra = any(r["doc_id"] in g.contradicting_docs for r in mine)
        cites_support = any(r["doc_id"] in g.supporting_docs for r in mine)
        stance_seen = any(r["doc_id"] in g.contradicting_docs and r["stance"] == "contradict" for r in mine)
        detected = status == "conflicting" or stance_seen
        both = cites_contra and (cites_support or not g.supporting_docs)
        per.append({"claim_id": g.claim_id, "detected": detected, "both_sides": both, "status": status})
    # false alarms: a conflict flagged where the gold has none
    clean = [g for g in task.claims if not g.contradicting_docs]
    false_alarm = 0
    for g in clean:
        pi = matches.get(g.claim_id)
        if pi is not None and out.claims[pi]["status"] == "conflicting" and g.expected_status != "conflicting":
            false_alarm += 1
    score = _mean([0.5 * (1.0 if p["detected"] else 0.0) + 0.5 * (1.0 if p["both_sides"] else 0.0) for p in per])
    return {"score": _r(score), "applicable": len(per), "detected": sum(1 for p in per if p["detected"]),
            "one_sided": sum(1 for p in per if p["detected"] and not p["both_sides"]),
            "false_conflict_rate": _r(false_alarm / len(clean)) if clean else None, "details": per}


def _trajectory(task: TaskDefinition, out: CaseOutput, config_strategy: str) -> dict[str, Any]:
    calls = out.calls
    exempt = set(STRATEGIES[config_strategy]["exempt_tools"])
    required = [t for t in task.trajectory.required_tools if t not in exempt]
    ok_tools = {c["tool"] for c in calls if c["ok"]}
    coverage = _mean([1.0 if t in ok_tools else 0.0 for t in required]) if required else 1.0
    firsts = {t: next((c["seq"] for c in calls if c["tool"] == t), None) for t in ("search_web", "scrape_source", "verify_claim")}
    order = [firsts[t] for t in ("search_web", "scrape_source", "verify_claim") if firsts[t] is not None]
    order_valid = 1.0 if order == sorted(order) else 0.0
    keys = [(c["tool"], c["key"]) for c in calls]
    dups = len(keys) - len(set(keys))
    total = len(calls)
    redundancy = 1.0 - dups / total if total else 1.0
    err_rate = (sum(1 for c in calls if not c["ok"]) / total) if total else 0.0
    budget = task.trajectory.max_tool_calls
    within = 1.0 if total <= budget and not out.budget_exhausted else 0.0
    ideal = (0 if "search_web" in exempt else 1) + max(1, len([d for d in task.required_docs])) + 1
    efficiency = min(1.0, ideal / total) if total else 0.0
    ranking = list(out.retrieved)
    rel = set(task.required_docs)
    retrieval = None
    if "search_web" not in exempt and rel:
        k = max(1, min(len(ranking) or 1, 10))
        retrieval = {"recall_at_k": _r(retrieval_metrics.recall_at_k(ranking, rel, k)), "mrr": _r(retrieval_metrics.reciprocal_rank(ranking, rel)),
                     "ndcg_at_k": _r(retrieval_metrics.ndcg_at_k(ranking, rel, k)), "k": k}
    score = _mean([coverage, order_valid, redundancy, 1.0 - err_rate, within, efficiency])
    return {"score": _r(score), "required_tool_coverage": _r(coverage), "order_valid": order_valid, "duplicate_calls": dups,
            "redundancy_score": _r(redundancy), "error_rate": _r(err_rate), "within_budget": bool(within), "tool_calls": total,
            "efficiency": _r(efficiency), "ideal_calls": ideal, "rounds": out.rounds, "retrieval": retrieval}


def composite(scores: dict[str, Optional[float]]) -> Optional[float]:
    present = {k: v for k, v in scores.items() if v is not None and k in COMPOSITE_WEIGHTS}
    if not present:
        return None
    total = sum(COMPOSITE_WEIGHTS[k] for k in present)
    return _r(sum(float(v) * COMPOSITE_WEIGHTS[k] for k, v in present.items()) / total)


def score_case(task: TaskDefinition, out: CaseOutput, strategy: str) -> dict[str, Any]:
    """Score one finished case. A failed case has no quality scores (``None``), only its failure."""
    if out.status != "completed":
        return {"status": "failed", "error_type": out.error_type, "scores": {m: None for m in COMPOSITE_WEIGHTS},
                "composite": None, "detail": {"trajectory": _trajectory(task, out, strategy)},
                "flags": ["case_failed"]}
    matches = match_claims(task, out.claims)
    rows = _analyse_citations(task, out, matches)
    need_evidence = any(g.expected_status != "insufficient_evidence" for g in task.claims)
    claim = _claim_correctness(task, out, matches)
    cite = _citation_accuracy(rows, need_evidence)
    ground = _evidence_grounding(task, out, rows)
    cover = _evidence_coverage(task, out, rows, matches)
    contra = _contradiction(task, out, rows, matches)
    traj = _trajectory(task, out, strategy)
    scores = {"claim_correctness": claim["score"], "citation_accuracy": cite["score"], "evidence_grounding": ground["score"],
              "evidence_coverage": cover["score"], "contradiction_handling": contra["score"], "trajectory_quality": traj["score"]}
    flags = []
    if cite.get("injection_citations"):
        flags.append("cited_injected_document")
    if ground["fabricated_evidence"]:
        flags.append("fabricated_or_ungrounded_evidence")
    if contra["one_sided"]:
        flags.append("one_sided_contradiction")
    if out.budget_exhausted:
        flags.append("tool_budget_exhausted")
    if claim["extra_claims"]:
        flags.append("extra_claims")
    return {"status": "completed", "error_type": None, "scores": scores, "composite": composite(scores),
            "detail": {"claim_correctness": claim, "citation_accuracy": cite, "evidence_grounding": ground,
                       "evidence_coverage": cover, "contradiction_handling": contra, "trajectory": traj, "citations": rows},
            "flags": flags}
