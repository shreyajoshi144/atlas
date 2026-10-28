"""Run the grounding evaluation against real providers and store the results.

    python -m backend.providers.eval_cli --models openai:gpt-6.1-sol,anthropic:claude-sonnet-5-5 --max-usd 2
    python -m backend.providers.eval_cli --models ... --dry-run     # prints the plan and worst-case cost, calls nothing

This spends real money on the configured accounts (bounded by --max-usd).
Nothing is stored unless the run completes; no results ship with Atlas.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .catalog import load_catalog
from .client import ProviderClient
from .evaluation import default_eval_dir, load_dataset, run_eval, write_result, _est_tokens
from .pricing import estimate_max_cost
from .settings import load_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval_cli")
    ap.add_argument("--models", required=True, help="comma-separated catalog ids, e.g. openai:gpt-6.1-sol")
    ap.add_argument("--max-usd", type=float, default=2.0, help="hard cap on estimated spend for this run")
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    settings, catalog, ds = load_settings(), load_catalog(), load_dataset(a.dataset)
    ids = [m.strip() for m in a.models.split(",") if m.strip()]
    for m in ids:
        e = catalog.get(m)
        if e is None:
            print(f"unknown model id: {m}", file=sys.stderr)
            return 2
        if not settings.configured(e.provider):
            print(f"{e.provider} is not configured (set its API key in the environment/.env)", file=sys.stderr)
            return 2
    worst = 0.0
    for m in ids:
        e = catalog.get(m)
        for c in ds["cases"]:
            chars = sum(len(x["text"]) for x in c["evidence"]) + len(c["question"]) + 1500
            worst += estimate_max_cost(e, _est_tokens(chars), settings.default_max_output_tokens) or 0.0
    print(f"{len(ids)} models x {len(ds['cases'])} cases; worst-case cost estimate ${worst:.2f}; cap ${a.max_usd:.2f}")
    if a.dry_run:
        return 0
    result = asyncio.run(run_eval(ProviderClient(settings), catalog, settings, ids, ds, max_usd=a.max_usd,
                                  concurrency=a.concurrency, log=print))
    path = write_result(result, a.out_dir or default_eval_dir(settings))
    for mid, m in result["models"].items():
        print(f"{mid}: {m['passes']}/{m['n']} passed, {m['errors']} provider errors")
    print(f"saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
