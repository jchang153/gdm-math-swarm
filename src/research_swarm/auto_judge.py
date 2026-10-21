"""Standing approval for the automatic post-run behavior judge (researcher-only).

docs/plan.md, "Automatic post-run behavior judge for the owner's runs (October 20, 2026)": the
repository owner decided that the misbehavior judge (research/analysis/judge,
scripts/run_transcript_judge.py) runs automatically after each of the owner's runs, with a
per-run cap of $50, replacing the interactive typed confirmation for those runs only. A run
config opts in with three keys:

- ``auto_behavior_judge``: ``true`` to judge the run after its unpaid follow-ups
  (scripts/supervise_run.py);
- ``behavior_judge_approval``: a nonempty note recording the decision (required when enabled);
- ``behavior_judge_max_usd``: the per-run cap, a positive amount of at most $50
  (``MAX_STANDING_JUDGE_USD``; required when enabled).

Only the owner's runs qualify: the baseline ledger (no ``budget_track``) and the owner's
``gemini`` budget track. Any other track (the collaborator's ``ablation`` track, or an unknown
value) is refused. The judge always bills the analysis scope of the checkout's baseline ledger
(state/budget.sqlite); every other judge guard stays (fresh route pricing record, the audited
analysis raises, the ledger's cap, label checks, held agents).

Live behavior judge (docs/plan.md, "Live behavior judge during the run (October 20, 2026)"; the
owner: "Judge during the run, not after"): with ``auto_behavior_judge`` true the judge also screens
transcript windows while the run is going (research/analysis/judge/live.py), so only the last
windows, verification and the run-level summary remain after the clock stops. Three optional keys:

- ``live_behavior_judge``: ``true`` (the default when ``auto_behavior_judge`` is true), ``false``
  (post-run judging only), or ``"mock"`` (a free shadow: live passes and a post-run pass with the
  deterministic mock model into the phase folder, beside the unchanged paid post-run judge);
- ``behavior_judge_live_max_usd``: the live passes' own sub-cap inside the per-run cap (default
  $10, at most ``behavior_judge_max_usd``);
- ``behavior_judge_live_interval_seconds``: seconds between live passes (default 300, 60 to 3600).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

AUTO_JUDGE_KEY = "auto_behavior_judge"
APPROVAL_KEY = "behavior_judge_approval"
MAX_USD_KEY = "behavior_judge_max_usd"
LIVE_KEY = "live_behavior_judge"
LIVE_MAX_USD_KEY = "behavior_judge_live_max_usd"
LIVE_INTERVAL_KEY = "behavior_judge_live_interval_seconds"
KEYS = (AUTO_JUDGE_KEY, APPROVAL_KEY, MAX_USD_KEY)
LIVE_KEYS = (LIVE_KEY, LIVE_MAX_USD_KEY, LIVE_INTERVAL_KEY)
MAX_STANDING_JUDGE_USD = Decimal("50")  # the owner's per-run cap, October 20, 2026 ("$50 is good")
DEFAULT_LIVE_MAX_USD = Decimal("10")
DEFAULT_LIVE_INTERVAL_SECONDS = 300
LIVE_INTERVAL_RANGE = (60, 3600)
LIVE_PLAN_SECTION = "Live behavior judge during the run (October 20, 2026)"
# budget.BUDGET_TRACK_KEY (the config key that selects a non-baseline ledger). The owner's
# tracks: the baseline (None) and "gemini"; the collaborator's "ablation" track never qualifies.
BUDGET_TRACK_KEY = "budget_track"
OWNER_TRACKS = (None, "gemini")
PLAN_SECTION = "Automatic post-run behavior judge for the owner's runs (October 20, 2026)"


def config_track(config):
    """The budget track ``config`` names (None for the baseline; "baseline" counts as None)."""
    value = (config or {}).get(BUDGET_TRACK_KEY)
    return None if value in (None, "baseline") else value


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def standing_approval_problems(config):
    """Config check (launch gate ``behavior_judge`` and the judge's standing-approval path).
    No problems when none of the keys is present or ``auto_behavior_judge`` is false and the
    other keys, if present, are well formed."""
    config = config or {}
    if not any(key in config for key in KEYS + LIVE_KEYS):
        return []
    problems = live_problems(config)
    enabled = config.get(AUTO_JUDGE_KEY, False)
    if type(enabled) is not bool:
        problems.append(f"{AUTO_JUDGE_KEY} must be a boolean")
        enabled = True  # check the rest as if enabled
    note = config.get(APPROVAL_KEY)
    if (enabled or APPROVAL_KEY in config) and (not isinstance(note, str) or not note.strip()):
        problems.append(f"{AUTO_JUDGE_KEY} requires a nonempty {APPROVAL_KEY} note")
    if enabled or MAX_USD_KEY in config:
        amount = _amount(config.get(MAX_USD_KEY))
        if amount is None or amount <= 0:
            problems.append(f"{MAX_USD_KEY} must be a positive amount in USD (at most ${MAX_STANDING_JUDGE_USD})")
        elif amount > MAX_STANDING_JUDGE_USD:
            problems.append(f"{MAX_USD_KEY} ${amount} exceeds the owner's per-run cap ${MAX_STANDING_JUDGE_USD}")
    if enabled:
        track = config_track(config)
        if track not in OWNER_TRACKS:
            problems.append(f"{AUTO_JUDGE_KEY} applies only to the repository owner's runs (the baseline ledger or "
                            f"the 'gemini' budget track), not budget_track {track!r}")
    return problems


def enabled(config):
    return (config or {}).get(AUTO_JUDGE_KEY) is True


def live_problems(config):
    """Problems of the live keys (``LIVE_KEYS``), checked whenever any judge key is present."""
    config = config or {}
    problems = []
    if LIVE_KEY in config and config[LIVE_KEY] not in (True, False, "mock"):
        problems.append(f'{LIVE_KEY} must be true, false or "mock"')
    if LIVE_MAX_USD_KEY in config:
        amount = _amount(config[LIVE_MAX_USD_KEY])
        cap = _amount(config.get(MAX_USD_KEY))
        if amount is None or amount <= 0:
            problems.append(f"{LIVE_MAX_USD_KEY} must be a positive amount in USD")
        elif amount > min(cap if cap is not None and cap > 0 else MAX_STANDING_JUDGE_USD, MAX_STANDING_JUDGE_USD):
            problems.append(f"{LIVE_MAX_USD_KEY} ${amount} exceeds the per-run cap {MAX_USD_KEY}")
    if LIVE_INTERVAL_KEY in config:
        value = config[LIVE_INTERVAL_KEY]
        low, high = LIVE_INTERVAL_RANGE
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            problems.append(f"{LIVE_INTERVAL_KEY} must be an integer number of seconds from {low} to {high}")
    return problems


def live_mode(config):
    """``"paid"`` (live passes billed under the standing approval), ``"mock"`` (a free shadow) or None
    (post-run judging only). Live judging is on by default for ``auto_behavior_judge`` configs."""
    config = config or {}
    if not enabled(config):
        return None
    value = config.get(LIVE_KEY, True)
    return "paid" if value is True else "mock" if value == "mock" else None


def live_settings(config):
    """``{"mode", "max_usd", "interval_seconds"}`` of the live judge for ``config`` (defaults filled)."""
    config = config or {}
    amount = _amount(config.get(LIVE_MAX_USD_KEY)) if LIVE_MAX_USD_KEY in config else DEFAULT_LIVE_MAX_USD
    interval = config.get(LIVE_INTERVAL_KEY, DEFAULT_LIVE_INTERVAL_SECONDS)
    return {"mode": live_mode(config), "max_usd": str(amount), "interval_seconds": interval}


def standing_approval(config, config_path=None, *, live=False):
    """The standing approval ``config`` grants: ``{"approval", "max_usd", "track", "config_path",
    "plan_section", "live", "live_max_usd"}``. Raises ValueError when it is not enabled or not valid,
    and with ``live=True`` (a paid live pass) also when live judging is not enabled (``live_mode``)."""
    problems = standing_approval_problems(config)
    if not enabled(config):
        problems = [f"{AUTO_JUDGE_KEY} is not true"] + problems
    if live and not problems and live_mode(config) != "paid":
        problems.append(f"{LIVE_KEY} is not true (a paid live pass needs it; \"mock\" runs free shadow passes)")
    if problems:
        raise ValueError("; ".join(problems))
    settings = live_settings(config)
    return {"approval": config[APPROVAL_KEY].strip(), "max_usd": str(_amount(config[MAX_USD_KEY])),
            "track": config_track(config) or "baseline",
            "config_path": None if config_path is None else str(config_path), "plan_section": PLAN_SECTION,
            "live": settings["mode"] == "paid", "live_max_usd": settings["max_usd"],
            **({"live_plan_section": LIVE_PLAN_SECTION} if live else {})}


def clamp_run_limit(limit_usd, max_usd, remaining_usd):
    """The standing-approval run limit: the smallest of the invocation's own limit, the per-run
    cap (never above $50) and the analysis scope's remaining allowance. Returns (limit, basis)."""
    candidates = [("invocation", Decimal(str(limit_usd))),
                  ("standing_approval_cap", min(Decimal(str(max_usd)), MAX_STANDING_JUDGE_USD))]
    if remaining_usd is not None:
        candidates.append(("analysis_remaining", Decimal(str(remaining_usd))))
    basis, limit = min(candidates, key=lambda pair: pair[1])  # the first wins a tie
    return limit, basis
