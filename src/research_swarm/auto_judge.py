"""Automatic behavior judge: the run config's keys (researcher-only).

docs/plan.md, "Automatic post-run behavior judge for the owner's runs (October 20, 2026)" and
"One per-run budget, no manual approvals (October 20, 2026)": the misbehavior judge
(research/analysis/judge, scripts/run_transcript_judge.py) runs automatically after a run whose
config enables it (scripts/supervise_run.py). It applies to every run whose config enables it,
on any budget track (the owner's and the collaborator's alike). A config opts in with:

- ``auto_behavior_judge``: ``true`` to judge the run after its unpaid follow-ups;
- ``behavior_judge_max_usd``: the judge's budget for the run in USD (any positive amount; $50,
  ``budget.DEFAULT_JUDGE_RUN_BUDGET_USD``, when absent). It holds for the judging run, the run's
  judge directory (live passes, early stages and the post-run judge together, and any resume of
  them: ``client.committed_for_judging``). The judge stops cleanly when its next request would push
  that committed spend past this budget. It is the judge's only spending control.

No approval note is needed (``behavior_judge_approval`` from older configs is ignored). The
judge bills the analysis scope of the checkout's ledger (state/budget.sqlite, or ``--ledger``),
each invocation registered as one ledger run with the room left under this budget. Its
research-integrity checks stay (route pricing record valid for the frozen route, judge directory
bindings, label checks, held agents).

Live behavior judge (docs/plan.md, "Live behavior judge during the run (October 20, 2026)"; the
owner: "Judge during the run, not after"): with ``auto_behavior_judge`` true the judge also screens
transcript windows while the run is going (research/analysis/judge/live.py), so only the last
windows, verification and the run-level summary remain after the clock stops. Three optional keys:

- ``live_behavior_judge``: ``true`` (the default when ``auto_behavior_judge`` is true), ``false``
  (post-run judging only), or ``"mock"`` (a free shadow: live passes and a post-run pass with the
  deterministic mock model into the phase folder, beside the unchanged paid post-run judge);
- ``behavior_judge_live_interval_seconds``: seconds between live passes (default 300, 60 to 3600).

``behavior_judge_live_max_usd`` (a separate live share in earlier versions) is no longer read: the
live passes use the judging run's one budget (owner decision "one spending control only",
docs/plan.md "One per-run budget, no manual approvals (October 20, 2026)").
"""
from __future__ import annotations

from decimal import Decimal

from .budget import DEFAULT_JUDGE_RUN_BUDGET_USD, budget_amount

AUTO_JUDGE_KEY = "auto_behavior_judge"
MAX_USD_KEY = "behavior_judge_max_usd"
LIVE_KEY = "live_behavior_judge"
LIVE_INTERVAL_KEY = "behavior_judge_live_interval_seconds"
KEYS = (AUTO_JUDGE_KEY, MAX_USD_KEY)
LIVE_KEYS = (LIVE_KEY, LIVE_INTERVAL_KEY)
# Superseded (a separate live share of the judge's budget); not read, reported by the run_budget gate.
OBSOLETE_LIVE_MAX_USD_KEY = "behavior_judge_live_max_usd"
DEFAULT_LIVE_INTERVAL_SECONDS = 300
LIVE_INTERVAL_RANGE = (60, 3600)
LIVE_PLAN_SECTION = "Live behavior judge during the run (October 20, 2026)"
# Selects a collaborator's or another non-baseline ledger on budget-track branches; recorded only.
BUDGET_TRACK_KEY = "budget_track"
PLAN_SECTION = "One per-run budget, no manual approvals (October 20, 2026)"


def config_track(config):
    """The budget track ``config`` names (None for the baseline; "baseline" counts as None)."""
    value = (config or {}).get(BUDGET_TRACK_KEY)
    return None if value in (None, "baseline") else value


def judge_config_problems(config):
    """Config check (launch gate ``behavior_judge``, the supervisor and the judge's ``--run-config``
    path): the keys, when present, are well formed. Any track qualifies."""
    config = config or {}
    problems = []
    if AUTO_JUDGE_KEY in config and type(config[AUTO_JUDGE_KEY]) is not bool:
        problems.append(f"{AUTO_JUDGE_KEY} must be a boolean")
    if config.get(MAX_USD_KEY) is not None:
        try:
            budget_amount(config[MAX_USD_KEY], MAX_USD_KEY)
        except ValueError as ex:
            problems.append(str(ex))
    return problems + live_problems(config)


def enabled(config):
    return (config or {}).get(AUTO_JUDGE_KEY) is True


def judge_budget(config):
    """``(budget, source)`` of the automatic judge: ``behavior_judge_max_usd``, else $50."""
    value = (config or {}).get(MAX_USD_KEY)
    if value is None:
        return Decimal(DEFAULT_JUDGE_RUN_BUDGET_USD), "default"
    return budget_amount(value, MAX_USD_KEY), f"config {MAX_USD_KEY}"


def live_problems(config):
    """Problems of the live keys (``LIVE_KEYS``), checked whenever the config is checked."""
    config = config or {}
    problems = []
    if LIVE_KEY in config and config[LIVE_KEY] not in (True, False, "mock"):
        problems.append(f'{LIVE_KEY} must be true, false or "mock"')
    if LIVE_INTERVAL_KEY in config:
        value = config[LIVE_INTERVAL_KEY]
        low, high = LIVE_INTERVAL_RANGE
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            problems.append(f"{LIVE_INTERVAL_KEY} must be an integer number of seconds from {low} to {high}")
    return problems


def live_mode(config):
    """``"paid"`` (live passes billed against the judge's budget for the run), ``"mock"`` (a free
    shadow) or None (post-run judging only). Live judging is on by default for
    ``auto_behavior_judge`` configs."""
    config = config or {}
    if not enabled(config):
        return None
    value = config.get(LIVE_KEY, True)
    return "paid" if value is True else "mock" if value == "mock" else None


def live_settings(config):
    """``{"mode", "interval_seconds"}`` of the live judge for ``config`` (defaults filled). Live passes
    spend from the judging run's one budget (``behavior_judge_max_usd``); there is no live share."""
    config = config or {}
    interval = config.get(LIVE_INTERVAL_KEY, DEFAULT_LIVE_INTERVAL_SECONDS)
    return {"mode": live_mode(config), "interval_seconds": interval}


def judge_settings(config, config_path=None, *, live=False):
    """What a run config sets for its judging: ``{"max_usd", "budget_source", "track",
    "config_path", "plan_section", "live"}`` (with ``live=True`` also
    ``"live_plan_section"``). Raises ValueError when the keys are not well formed, and with
    ``live=True`` (a paid live pass) also when the config does not enable paid live judging
    (``live_mode``: a configuration check, not an approval)."""
    problems = judge_config_problems(config)
    if live and not problems and not enabled(config):
        problems.append(f"{AUTO_JUDGE_KEY} is not true (a paid live pass judges a run whose config enables it)")
    elif live and not problems and live_mode(config) != "paid":
        problems.append(f"{LIVE_KEY} is not true (a paid live pass needs it; \"mock\" runs free shadow passes)")
    if problems:
        raise ValueError("; ".join(problems))
    amount, source = judge_budget(config)
    settings = live_settings(config)
    return {"max_usd": str(amount), "budget_source": source, "track": config_track(config) or "baseline",
            "config_path": None if config_path is None else str(config_path), "plan_section": PLAN_SECTION,
            "live": settings["mode"] == "paid",
            **({"live_plan_section": LIVE_PLAN_SECTION} if live else {})}
