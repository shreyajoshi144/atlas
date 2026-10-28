from __future__ import annotations

import json
from typing import Any


_STATUS = {
    "verified": "supporting",
    "partially_verified": "mixed",
    "conflicting": "contradicting",
    "not_verified": "mixed",
    "insufficient_evidence": "mixed",
}


def _claims(report_row: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        payload = json.loads(report_row.get("verification_json") or "{}")
        return payload.get("claims", []) if isinstance(payload, dict) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _url_map(source_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("url", "")).rstrip("/"): row for row in source_rows if row.get("url")}


def build_evidence_analysis(report_row: dict[str, Any], source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive counts/links only from persisted verification evidence and sources."""
    by_url = _url_map(source_rows)
    buckets: dict[str, dict[str, Any]] = {
        "supporting": {"source_ids": set(), "sources": []},
        "partially_supported": {"source_ids": set(), "sources": []},
        "contradicted": {"source_ids": set(), "sources": []},
        "mixed": {"source_ids": set(), "sources": []},
    }

    for claim in _claims(report_row):
        status = str(claim.get("status", "")).lower()
        evidence = claim.get("evidence") or []
        for item in evidence:
            url = str(item.get("url", "")).rstrip("/")
            source = by_url.get(url)
            if not source:
                continue
            stance = str(item.get("stance", "")).lower()
            if stance in {"support", "supports", "supporting"}:
                bucket = "supporting"
            elif stance in {"contradict", "contradicts", "contradicting"}:
                bucket = "contradicted"
            elif status == "partially_verified":
                bucket = "partially_supported"
            elif status == "conflicting":
                bucket = "mixed"
            else:
                bucket = _STATUS.get(status, "mixed")
            buckets[bucket]["source_ids"].add(source["source_id"])

    for bucket in buckets.values():
        bucket["sources"] = [
            {"source_id": s["source_id"], "title": s["title"], "url": s["url"], "domain": s.get("domain", ""), "publish_date": s.get("publish_date")}
            for s in source_rows
            if s.get("source_id") in bucket["source_ids"]
        ]
        bucket["source_ids"] = sorted(bucket["source_ids"])
        bucket["count"] = len(bucket["sources"])

    gaps: list[dict[str, Any]] = []
    claims = _claims(report_row)
    for claim in claims:
        status = str(claim.get("status", "")).lower()
        text = str(claim.get("claim_text", "")).strip()
        if not text:
            continue
        if status == "conflicting":
            gaps.append({"type": "conflicting_claim", "text": f"Resolve conflicting evidence for: {text}"})
        elif status in {"not_verified", "insufficient_evidence"}:
            gaps.append({"type": "insufficient_evidence", "text": f"Find stronger evidence for: {text}"})
        elif status == "partially_verified":
            gaps.append({"type": "partial_support", "text": f"Investigate the limitations of the evidence for: {text}"})

    if source_rows and len(source_rows) < 5:
        gaps.append({"type": "thin_source_coverage", "text": f"The research currently uses only {len(source_rows)} sources."})
    domains = {s.get("domain") for s in source_rows if s.get("domain")}
    if len(source_rows) >= 2 and len(domains) == 1:
        gaps.append({"type": "single_domain", "text": f"All collected sources come from {next(iter(domains))}; independent sources are missing."})
    if source_rows and not any(s.get("publish_date") for s in source_rows):
        gaps.append({"type": "unknown_recency", "text": "The collected sources do not provide a known publication date."})

    # De-duplicate by exact gap text while preserving evidence-derived order.
    seen = set()
    unique_gaps = []
    for gap in gaps:
        if gap["text"] not in seen:
            seen.add(gap["text"])
            unique_gaps.append(gap)

    return {
        "supporting": buckets["supporting"],
        "partially_supported": buckets["partially_supported"],
        "contradicted": buckets["contradicted"],
        "mixed": buckets["mixed"],
        "gaps": unique_gaps,
        "conclusion": {
            "supported_by_source_count": buckets["supporting"]["count"],
            "partially_supported_source_count": buckets["partially_supported"]["count"],
            "contradicted_source_count": buckets["contradicted"]["count"],
        },
    }
