"""The versioned built-in benchmark suite and the preset agent configurations.

Built-in tasks are read-only, ship with the code, and are validated on load: a
malformed suite fails loudly at import of the first request, never silently
shrinks. Corpora are fictional on purpose, so a model cannot answer from memory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from backend.bench.models import AgentConfig, TaskDefinition, suite_hash

_SUITE_DIR = Path(__file__).with_name("suites")
BUILTIN_SUITE_FILES = {"core-v1": "core_v1.json"}
DEFAULT_MODEL = "llama-3.3-70b-versatile"


@dataclass(frozen=True)
class Suite:
    suite_id: str
    title: str
    description: str
    version: int
    tasks: tuple[TaskDefinition, ...]

    @property
    def task_hashes(self) -> dict[str, str]:
        return {t.task_id: t.task_hash for t in self.tasks}

    @property
    def suite_hash(self) -> str:
        return suite_hash(self.task_hashes)

    def info(self) -> dict:
        return {"suite_id": self.suite_id, "title": self.title, "description": self.description, "version": self.version,
                "suite_hash": self.suite_hash, "task_count": len(self.tasks), "task_ids": [t.task_id for t in self.tasks]}


@lru_cache(maxsize=None)
def load_suite(suite_id: str) -> Suite:
    if suite_id not in BUILTIN_SUITE_FILES:
        raise KeyError(suite_id)
    raw = json.loads((_SUITE_DIR / BUILTIN_SUITE_FILES[suite_id]).read_text(encoding="utf-8"))
    tasks = tuple(TaskDefinition.model_validate(t) for t in raw["tasks"])
    ids_ = [t.task_id for t in tasks]
    if len(set(ids_)) != len(ids_):
        raise ValueError(f"suite {suite_id}: duplicate task ids")
    if any(not t.task_id.startswith("core-") for t in tasks):
        raise ValueError(f"suite {suite_id}: built-in task ids must start with 'core-'")
    return Suite(raw["suite_id"], raw["title"], raw.get("description", ""), int(raw["version"]), tasks)


def list_suites() -> list[Suite]:
    return [load_suite(s) for s in BUILTIN_SUITE_FILES]


def builtin_task(task_id: str) -> Optional[TaskDefinition]:
    for suite in list_suites():
        for t in suite.tasks:
            if t.task_id == task_id:
                return t
    return None


def builtin_tasks() -> list[TaskDefinition]:
    return [t for s in list_suites() for t in s.tasks]


def suite_of(task_id: str) -> Optional[str]:
    return next((s.suite_id for s in list_suites() if any(t.task_id == task_id for t in s.tasks)), None)


PRESETS: dict[str, AgentConfig] = {
    "preset-baseline": AgentConfig(name="Baseline · single pass", model=DEFAULT_MODEL, strategy="single_pass"),
    "preset-iterative": AgentConfig(name="Iterative gap-fill", model=DEFAULT_MODEL, strategy="iterative", max_rounds=2),
    "preset-full-corpus": AgentConfig(name="Full corpus (no retrieval)", model=DEFAULT_MODEL, strategy="full_corpus"),
    "preset-small-model": AgentConfig(name="Small model · single pass", model="llama-3.1-8b-instant", strategy="single_pass"),
}
