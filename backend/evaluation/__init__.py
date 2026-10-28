"""Lightweight, non-blocking evaluation layer for the Atlas research pipeline.

This package never imports anything from backend.graph / backend.agents, so it
can be imported (and unit-tested) without pulling in LangGraph, Groq, or any
network client. It only depends on the plain domain models produced by the
existing pipeline (backend.models.domain_models).

Entry point: `run_pipeline_evaluation` in `backend.evaluation.pipeline_evaluator`.
It is designed to be called *after* a research run has produced its final
state — it never raises; any evaluator failure is captured in
`EvaluationSummary.errors` and logged, and the rest of the evaluation still
runs. Callers should still wrap the call in their own try/except as a second
line of defense (see backend/api/routes/research.py).
"""

from __future__ import annotations

from backend.evaluation.models import EvaluationSummary
from backend.evaluation.pipeline_evaluator import run_pipeline_evaluation

__all__ = ["EvaluationSummary", "run_pipeline_evaluation"]
