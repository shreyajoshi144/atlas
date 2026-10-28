"""Rule-based evaluation of models on a fixed, labelled dataset.

No model grades another model. Every verdict comes from deterministic checks
(required facts present, forbidden strings absent, citations valid, abstention
when the evidence lacks the answer, JSON schema validity). Results are written
as raw pass/fail counts; confidence intervals and the 'ranking supported' flag
are recomputed from those counts on read (see evalstore.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import grounding, prompts, safety
from .catalog import Catalog
from .client import ProviderClient
from .errors import ProviderError
from .evalstore import SCHEMA
from .models import ProviderRequest
from .pricing import compute_cost, estimate_max_cost
from .settings import ProviderSettings
from .stats import percentile

DEFAULT_DATASET = Path(__file__).with_name("evals") / "atlas_compare_v1.json"


def default_eval_dir(settings: ProviderSettings | None = None) -> Path:
    if settings and settings.eval_dir:
        return Path(settings.eval_dir)
    base = os.environ.get("ATLAS_DATA_DIR") or str(Path(__file__).resolve().parents[1] / "data")
    return Path(base) / "compare_evals"


def load_dataset(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path or DEFAULT_DATASET)
    raw = p.read_bytes()
    ds = json.loads(raw)
    ids = [c["id"] for c in ds["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    ds["_sha256"] = hashlib.sha256(raw).hexdigest()
    return ds


def _norm(s: str) -> str:
    s = (s or "").lower().replace(" ", " ")
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)          # 1,200 -> 1200
    return re.sub(r"\s+", " ", s).strip()


def _has(text_norm: str, alts: list[str]) -> bool:
    return any(_norm(a) in text_norm for a in alts)


def score_case(case: dict[str, Any], answer: str, *, structured_data: dict[str, Any] | None = None,
               structured_errors: list[str] | None = None, canary_leaked: bool = False) -> dict[str, Any]:
    """Deterministic verdict. ``checks`` explains every failure (shown in result files)."""
    checks: dict[str, bool] = {}
    text = answer or ""
    claim_sources: list[str] = []
    if case.get("structured"):
        checks["valid_json_schema"] = structured_data is not None
        if structured_data is not None:
            text = structured_data["answer"]
            for cl in structured_data["claims"]:
                claim_sources += cl["sources"]
    norm = _norm(text)
    known = {e["id"] for e in case["evidence"]}
    labels = grounding.extract_labels(text) + claim_sources
    abstained = (structured_data["abstained"] if structured_data is not None else False) or grounding.detect_abstain(text)
    if case.get("expect_abstain"):
        checks["abstains_when_evidence_lacks_answer"] = abstained
    else:
        for i, alts in enumerate(case.get("required", [])):
            checks[f"required_fact_{i + 1}"] = _has(norm, alts)
        if case.get("must_cite"):
            checks["cites_required_sources"] = all(m in labels for m in case["must_cite"])
    checks["no_fabricated_citation_labels"] = all(l in known for l in labels)
    for i, bad in enumerate(case.get("forbidden", [])):
        checks[f"avoids_forbidden_{i + 1}"] = _norm(bad) not in norm
    checks["does_not_leak_system_canary"] = not canary_leaked
    return {"passed": all(checks.values()), "checks": checks}


def _est_tokens(chars: int) -> int:
    return -(-chars // 2) + 64


async def run_eval(client: ProviderClient, catalog: Catalog, settings: ProviderSettings, model_ids: list[str],
                   dataset: dict[str, Any], *, max_usd: float, concurrency: int = 2,
                   log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    cases = dataset["cases"]
    max_out = settings.default_max_output_tokens
    sem = asyncio.Semaphore(max(1, concurrency))
    spent = {"usd": 0.0, "unknown": 0}
    lock = asyncio.Lock()
    per_model: dict[str, dict[str, Any]] = {m: {"rows": [], "skipped": 0} for m in model_ids}

    async def one(mid: str, case: dict[str, Any]) -> None:
        entry = catalog.get(mid)
        canary = safety.new_canary()
        srcs = [{"id": e["id"], "title": e["title"], "excerpt": e["text"]} for e in case["evidence"]]
        system = prompts.system_prompt("atlas", canary, bool(case.get("structured")))
        user = prompts.user_prompt(case["question"], srcs, "atlas")
        est = estimate_max_cost(entry, _est_tokens(len(system) + len(user)), max_out) or 0.0
        async with lock:                       # reserve against the run's own spend cap before calling
            if spent["usd"] + est > max_usd:
                per_model[mid]["skipped"] += 1
                return
            spent["usd"] += est
        async with sem:
            t0 = time.monotonic()
            try:
                resp = await client.generate(entry, ProviderRequest(
                    system, user, max_out, False, prompts.ANSWER_SCHEMA if case.get("structured") else None,
                    f"eval_{uuid.uuid4().hex[:10]}"))
            except ProviderError as exc:
                async with lock:
                    spent["usd"] -= est        # nothing (known) was billed
                per_model[mid]["rows"].append({"id": case["id"], "category": case["category"], "error": exc.category})
                return
        cost = compute_cost(entry, resp.usage)
        async with lock:
            spent["usd"] += (cost["usd"] - est) if cost["usd"] is not None else 0.0
            if cost["usd"] is None:
                spent["unknown"] += 1
        sd, serrs = (None, None)
        if case.get("structured"):
            sd, serrs = grounding.parse_structured(resp.text)
        verdict = score_case(case, resp.text, structured_data=sd, structured_errors=serrs,
                             canary_leaked=canary in (resp.text or ""))
        row = {"id": case["id"], "category": case["category"], "passed": verdict["passed"], "checks": verdict["checks"],
               "latency_ms": resp.total_ms, "cost_usd": cost["usd"], "truncated": resp.truncated}
        if not verdict["passed"]:
            row["answer_excerpt"] = safety.redact_secrets((resp.text or "")[:500])[0]
        per_model[mid]["rows"].append(row)
        log(f"{mid} {case['id']} {'PASS' if verdict['passed'] else 'FAIL'}")

    await asyncio.gather(*(one(m, c) for m in model_ids for c in cases))

    models: dict[str, Any] = {}
    for mid, d in per_model.items():
        scored = [r for r in d["rows"] if "passed" in r]
        errs = [r for r in d["rows"] if "error" in r]
        cats: dict[str, dict[str, Any]] = {}
        for r in scored:
            c = cats.setdefault(r["category"], {"n": 0, "passes": 0})
            c["n"] += 1
            c["passes"] += int(r["passed"])
        for c in cats.values():
            c["pass_rate"] = round(c["passes"] / c["n"], 4)
        costs = [r["cost_usd"] for r in scored if r["cost_usd"] is not None]
        lats = [r["latency_ms"] for r in scored if r["latency_ms"] is not None]
        models[mid] = {"n": len(scored), "passes": sum(int(r["passed"]) for r in scored), "errors": len(errs),
                       "error_categories": sorted({r["error"] for r in errs}), "skipped_for_budget": d["skipped"],
                       "by_category": cats, "mean_cost_usd": round(sum(costs) / len(costs), 6) if costs else None,
                       "p50_latency_ms": percentile([float(x) for x in lats], 0.5), "cases": sorted(d["rows"], key=lambda r: r["id"])}
    caveats = list(dataset.get("limitations", []))
    if any(m["skipped_for_budget"] for m in models.values()):
        caveats.append("The run hit its spend cap: some cases were not run, so models may have different case counts.")
    if any(m["errors"] for m in models.values()):
        caveats.append("Provider errors are excluded from pass rates and reported separately; they reflect reliability during this run only.")
    return {"schema": SCHEMA, "eval_id": f"eval_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "dataset": {"name": dataset["name"], "version": dataset["version"], "n_cases": len(cases), "sha256": dataset["_sha256"]},
            "catalog_verified_on": catalog.verified_on, "evidence_mode": "atlas", "max_output_tokens": max_out,
            "estimated_spend_usd": round(spent["usd"], 4), "models": models, "caveats": caveats}


def write_result(result: dict[str, Any], directory: str | Path) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{result['eval_id']}.json"
    path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return path
