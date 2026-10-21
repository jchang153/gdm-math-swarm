"""Automatic post-run behavior judge: the run config's keys (researcher-only).

docs/plan.md, "Automatic post-run behavior judge for the owner's runs (October 20, 2026)" and
"One per-run budget, no manual approvals (October 20, 2026)": the misbehavior judge
(research/analysis/judge, scripts/run_transcript_judge.py) runs automatically after a run whose
config enables it (scripts/supervise_run.py). It applies to every run whose config enables it,
on any budget track (the owner's and the collaborator's alike). A config opts in with:

- ``auto_behavior_judge``: ``true`` to judge the run after its unpaid follow-ups;
- ``behavior_judge_max_usd``: the judging run's budget in USD (any positive amount; $50,
  ``budget.DEFAULT_JUDGE_RUN_BUDGET_USD``, when absent). The judge stops cleanly when its next
  request would push its committed spend past this budget.

No approval note is needed (``behavior_judge_approval`` from older configs is ignored). The
judge bills the analysis scope of the checkout's ledger (state/budget.sqlite, or ``--ledger``).
Its research-integrity checks stay (route pricing record valid for the frozen route, judge
directory bindings, label checks, held agents).
"""
from __future__ import annotations

from decimal import Decimal

from .budget import DEFAULT_JUDGE_RUN_BUDGET_USD, budget_amount

AUTO_JUDGE_KEY = "auto_behavior_judge"
MAX_USD_KEY = "behavior_judge_max_usd"
KEYS = (AUTO_JUDGE_KEY, MAX_USD_KEY)
# Selects a collaborator's or another non-baseline ledger on budget-track branches; recorded only.
BUDGET_TRACK_KEY = "budget_track"
PLAN_SECTION = "One per-run budget, no manual approvals (October 20, 2026)"


def config_track(config):
    """The budget track ``config`` names (None for the baseline; "baseline" counts as None)."""
    value = (config or {}).get(BUDGET_TRACK_KEY)
    return None if value in (None, "baseline") else value


def judge_config_problems(config):
    """Config check (launch gate ``behavior_judge`` and the judge's ``--run-config`` path): the
    keys, when present, are well formed. Any track qualifies."""
    config = config or {}
    problems = []
    if AUTO_JUDGE_KEY in config and type(config[AUTO_JUDGE_KEY]) is not bool:
        problems.append(f"{AUTO_JUDGE_KEY} must be a boolean")
    if config.get(MAX_USD_KEY) is not None:
        try:
            budget_amount(config[MAX_USD_KEY], MAX_USD_KEY)
        except ValueError as ex:
            problems.append(str(ex))
    return problems


def enabled(config):
    return (config or {}).get(AUTO_JUDGE_KEY) is True


def judge_budget(config):
    """``(budget, source)`` of the automatic judge: ``behavior_judge_max_usd``, else $50."""
    value = (config or {}).get(MAX_USD_KEY)
    if value is None:
        return Decimal(DEFAULT_JUDGE_RUN_BUDGET_USD), "default"
    return budget_amount(value, MAX_USD_KEY), f"config {MAX_USD_KEY}"


def judge_settings(config, config_path=None):
    """What a run config sets for its judging run: ``{"max_usd", "budget_source", "track",
    "config_path", "plan_section"}``. Raises ValueError when the keys are not well formed."""
    problems = judge_config_problems(config)
    if problems:
        raise ValueError("; ".join(problems))
    amount, source = judge_budget(config)
    return {"max_usd": str(amount), "budget_source": source, "track": config_track(config) or "baseline",
            "config_path": None if config_path is None else str(config_path), "plan_section": PLAN_SECTION}
