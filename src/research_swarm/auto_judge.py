"""Automatic post-run behavior judge and incident report: the run config's keys (researcher-only).

docs/plan.md, "Automatic post-run behavior judge for the owner's runs (October 20, 2026)" and
"One per-run budget, no manual approvals (October 20, 2026)": a behavior judge runs automatically
after a run whose config enables it (scripts/supervise_run.py), on any budget track (the owner's
and the collaborator's alike). A config opts in with:

- ``auto_behavior_judge``: ``true`` to judge the run after its unpaid follow-ups;
- ``behavior_judge``: which judge (owner decision, October 24, 2026):
  - ``"simple"`` (the DEFAULT when ``auto_behavior_judge`` is true and the key is absent): the
    validated one-pass simplified judge (scripts/run_simple_judge.py, research/analysis/simple_judge/;
    one call per agent, Gemini 3.8 Flash with a GPT 5.6 Terra fallback, about $2 per 100-agent run)
    into RUN/analysis/simple_judge/. No live judging. Its budget:
    ``simple_judge_max_usd`` (any positive amount; $10 when absent), passed as ``--run-limit``;
  - ``"full"`` (opt-in): the rubric-3.1 judge (research/analysis/judge,
    scripts/run_transcript_judge.py) into RUN/analysis/judge/, with live judging (below). Its budget:
    ``behavior_judge_max_usd`` (any positive amount; $50, ``budget.DEFAULT_JUDGE_RUN_BUDGET_USD``,
    when absent). It holds for the judging run, the run's judge directory (live passes, early stages
    and the post-run judge together, and any resume of them: ``client.committed_for_judging``);
  - ``"off"``: no behavior judge (the incident report below still runs).
  Each judge stops cleanly when its next request would not fit its budget, its only spending control.
  Configs written before October 24, 2026 that set only ``auto_behavior_judge`` now get the simple
  judge; a config that wants the rubric-3.1 judge sets ``behavior_judge: "full"``.

No approval note is needed (``behavior_judge_approval`` from older configs is ignored). Both judges
bill the analysis scope of the checkout's ledger (state/budget.sqlite, or ``--ledger``), each
invocation registered as one ledger run with the room left under its budget.

Live behavior judge (docs/plan.md, "Live behavior judge during the run (October 20, 2026)"): only
for the full judge (``behavior_judge: "full"``). The judge then also screens transcript windows while
the run is going (research/analysis/judge/live.py). Two optional keys, read only for the full judge:

- ``live_behavior_judge``: ``true`` (the default for the full judge), ``false`` (post-run judging
  only), or ``"mock"`` (a free shadow: live passes and a post-run pass with the deterministic mock
  model into the phase folder, beside the unchanged paid post-run judge);
- ``behavior_judge_live_interval_seconds``: seconds between live passes (default 300, 60 to 3600).

Incident report (docs/plan.md, "Incident report per run (October 21, 2026)"): after the behavior
judge (whichever ran, or none), the supervisor writes the run's model-written incident report
(scripts/run_incident_report.py into RUN/analysis/incident/). Two keys:

- ``auto_incident_report``: optional boolean, default the value of ``auto_behavior_judge``; ``true``
  requests the report even without the judge keys, ``false`` turns it off;
- ``incident_report_max_usd``: its budget for the run in USD (any positive amount; $20 when absent).
  Every incident-report invocation for the run counts against it. A failure or a stop never changes
  the worker exit or the packaging.

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
INCIDENT_MAX_USD_KEY = "incident_report_max_usd"
DEFAULT_INCIDENT_RUN_BUDGET_USD = "20"
# Which behavior judge runs (owner decision, October 24, 2026): the simplified judge by default.
JUDGE_MODE_KEY = "behavior_judge"
JUDGE_MODES = ("simple", "full", "off")
DEFAULT_JUDGE_MODE = "simple"
SIMPLE_MAX_USD_KEY = "simple_judge_max_usd"
DEFAULT_SIMPLE_RUN_BUDGET_USD = "10"
AUTO_INCIDENT_KEY = "auto_incident_report"
KEYS = (AUTO_JUDGE_KEY, JUDGE_MODE_KEY, MAX_USD_KEY, SIMPLE_MAX_USD_KEY, AUTO_INCIDENT_KEY, INCIDENT_MAX_USD_KEY)
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
    if AUTO_INCIDENT_KEY in config and type(config[AUTO_INCIDENT_KEY]) is not bool:
        problems.append(f"{AUTO_INCIDENT_KEY} must be a boolean")
    if JUDGE_MODE_KEY in config and config[JUDGE_MODE_KEY] not in JUDGE_MODES:
        problems.append(f'{JUDGE_MODE_KEY} must be "simple", "full" or "off"')
    for key in (MAX_USD_KEY, SIMPLE_MAX_USD_KEY, INCIDENT_MAX_USD_KEY):
        if config.get(key) is not None:
            try:
                budget_amount(config[key], key)
            except ValueError as ex:
                problems.append(str(ex))
    return problems + live_problems(config)


def enabled(config):
    """Whether the config enables the automatic post-run work (``auto_behavior_judge`` true)."""
    return (config or {}).get(AUTO_JUDGE_KEY) is True


def judge_mode(config):
    """Which automatic behavior judge runs: ``"simple"`` (the default with ``auto_behavior_judge``
    true), ``"full"`` (``behavior_judge: "full"``, the rubric-3.1 judge) or ``"off"`` (no automatic
    judge: ``auto_behavior_judge`` not true, or ``behavior_judge: "off"``). An invalid value counts
    as ``"off"`` here (``judge_config_problems`` reports it)."""
    config = config or {}
    if not enabled(config):
        return "off"
    value = config.get(JUDGE_MODE_KEY, DEFAULT_JUDGE_MODE)
    return value if value in JUDGE_MODES else "off"


def incident_enabled(config):
    """Whether the post-run incident report runs: ``auto_incident_report`` when it is a boolean, else
    ``auto_behavior_judge`` (independent of which behavior judge runs, ``"off"`` included)."""
    config = config or {}
    value = config.get(AUTO_INCIDENT_KEY)
    return value if type(value) is bool else enabled(config)


def simple_judge_budget(config):
    """``(budget, source)`` of the simplified judge: ``simple_judge_max_usd``, else $10."""
    value = (config or {}).get(SIMPLE_MAX_USD_KEY)
    if value is None:
        return Decimal(DEFAULT_SIMPLE_RUN_BUDGET_USD), "default"
    return budget_amount(value, SIMPLE_MAX_USD_KEY), f"config {SIMPLE_MAX_USD_KEY}"


def active_judge_budget(config):
    """``(mode, budget, source, key)`` of the judge ``judge_mode`` selects (budget, source and key
    None for ``"off"``). Raises ValueError for an invalid amount."""
    mode = judge_mode(config)
    if mode == "simple":
        return (mode, *simple_judge_budget(config), SIMPLE_MAX_USD_KEY)
    if mode == "full":
        return (mode, *judge_budget(config), MAX_USD_KEY)
    return mode, None, None, None


def judge_budget(config):
    """``(budget, source)`` of the full (rubric-3.1) judge: ``behavior_judge_max_usd``, else $50."""
    value = (config or {}).get(MAX_USD_KEY)
    if value is None:
        return Decimal(DEFAULT_JUDGE_RUN_BUDGET_USD), "default"
    return budget_amount(value, MAX_USD_KEY), f"config {MAX_USD_KEY}"


def incident_budget(config):
    """``(budget, source)`` of the post-run incident report: ``incident_report_max_usd``, else $20."""
    value = (config or {}).get(INCIDENT_MAX_USD_KEY)
    if value is None:
        return Decimal(DEFAULT_INCIDENT_RUN_BUDGET_USD), "default"
    return budget_amount(value, INCIDENT_MAX_USD_KEY), f"config {INCIDENT_MAX_USD_KEY}"


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
    shadow) or None (post-run judging only). Live judging exists only for the full judge
    (``behavior_judge: "full"``), where it is on by default; None for the simple judge and "off"."""
    config = config or {}
    if judge_mode(config) != "full":
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
    elif live and not problems and judge_mode(config) != "full":
        problems.append(f'{JUDGE_MODE_KEY} is not "full" (live judging exists only for the full judge; '
                        f'the default is the simple judge)')
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
