"""Coding version of the swarm experiment (Impossible-LiveCodeBench tasks, Python solutions).

A config with ``"domain": "code"`` runs through the same harness as the Lean runs; every
code-domain module (dataset, judge, evaluator, prompts, tool texts) lives in this package and is
imported lazily by the runtime, so a Lean config never touches it. Without a ``domain`` key a
config (and a run) is a Lean one, unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

DOMAIN_KEY = "domain"
LEAN = "lean"
CODE = "code"
DOMAINS = (LEAN, CODE)

# Agent-visible names and workspace layout of the code domain (coding-interfaces.md).
SOLUTION_SUFFIX = {LEAN: ".lean", CODE: ".py"}
CHECK_SOLUTION_TOOL = "check_solution"   # agent-visible alias of the conference action check_proof
RUN_TESTS_PATH = "/workspace/run_tests.py"
CODE_TASK_SET = "ilcb100"
CODE_MANIFEST = "data/coding/manifest.json"


def domain_of_config(config) -> str:
    """``config["domain"]``; absent or None means "lean". Any other value is returned as is
    (preflight refuses it, see ``domain_problems``)."""
    value = (config or {}).get(DOMAIN_KEY)
    return LEAN if value is None else value


def is_code(config) -> bool:
    return domain_of_config(config) == CODE


def domain_problems(config) -> list[str]:
    value = (config or {}).get(DOMAIN_KEY)
    if value is not None and value not in DOMAINS:
        return [f"domain {value!r} is not one of {list(DOMAINS)}"]
    return []


def domain_of_run(run_dir) -> str:
    """The domain of a saved run: ``effective.domain`` (else ``config.domain``) of RUN/settings.json;
    "lean" when absent (every run before the coding version)."""
    try:
        settings = json.loads((Path(run_dir) / "settings.json").read_text())
    except (OSError, ValueError):
        return LEAN
    effective = settings.get("effective") or {}
    value = effective.get(DOMAIN_KEY) or (settings.get("config") or {}).get(DOMAIN_KEY)
    return value or LEAN


def solution_path(task_id, domain=LEAN) -> str:
    """Library path of an accepted solution: solutions/<task_id>.lean or solutions/<task_id>.py."""
    return f"solutions/{task_id}{SOLUTION_SUFFIX.get(domain, '.lean')}"

# Approved coding population (docs/plan.md, "Coding experiment on ImpossibleBench (November 3, 2026)").
CODE_AGENT_COUNT = 20
CODE_TASK_COUNT = 100
