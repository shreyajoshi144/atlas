"""Read measured evaluation results written by ``eval_cli``.

Nothing is ever fabricated here: if no result files exist, ``available`` is
False and the UI says quality cannot be ranked. Confidence intervals and the
"ranking supported" flag are recomputed on read from the raw pass/fail counts,
so an edited file cannot claim a ranking the counts do not support.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .stats import separated, wilson

SCHEMA = "atlas-compare-eval/1"
EXPLANATION = ("An evaluation runs every model on the same fixed set of questions with known answers and scores each "
               "reply by rules (no model grades another). A pass rate is only meaningful with its case count: with few "
               "cases the confidence range is wide, so Atlas only says one model is ahead when the ranges do not overlap. "
               "Results describe this dataset, not every question you might ask.")


class EvalStore:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def _files(self) -> list[Path]:
        return sorted(self.dir.glob("eval_*.json"), reverse=True) if self.dir.is_dir() else []

    def runs(self, limit: int = 5) -> list[dict[str, Any]]:
        out = []
        for f in self._files():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if raw.get("schema") != SCHEMA or not isinstance(raw.get("models"), dict):
                continue
            models = []
            for mid, m in raw["models"].items():
                n, passes = int(m.get("n", 0)), int(m.get("passes", 0))
                if n <= 0 or not 0 <= passes <= n:
                    continue
                lo, hi = wilson(passes, n)
                models.append({"id": mid, "n": n, "pass_rate": round(passes / n, 4), "ci95": [lo, hi],
                               "by_category": m.get("by_category", {}), "mean_cost_usd": m.get("mean_cost_usd"),
                               "p50_latency_ms": m.get("p50_latency_ms"), "errors": m.get("errors", 0)})
            if not models:
                continue
            ranking = len(models) >= 2 and any(
                separated(tuple(a["ci95"]), tuple(b["ci95"])) for i, a in enumerate(models) for b in models[i + 1:])
            caveats = list(raw.get("caveats", []))
            if min(m["n"] for m in models) < 100:
                caveats.append("Small sample: fewer than 100 cases per model, so confidence ranges are wide.")
            if not ranking:
                caveats.append("Confidence ranges overlap, so these results do not show that one model is better.")
            out.append({"eval_id": raw.get("eval_id", f.stem), "ran_at": raw.get("ran_at"), "dataset": raw.get("dataset", {}),
                        "models": models, "ranking_supported": ranking, "caveats": caveats})
            if len(out) >= limit:
                break
        return out

    def summary(self) -> dict[str, Any]:
        runs = self.runs()
        return {"available": bool(runs), "runs": runs, "explanation": EXPLANATION}

    def covered_models(self) -> set[str]:
        return {m["id"] for r in self.runs() for m in r["models"]}
