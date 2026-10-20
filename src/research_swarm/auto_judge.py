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
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

AUTO_JUDGE_KEY = "auto_behavior_judge"
APPROVAL_KEY = "behavior_judge_approval"
MAX_USD_KEY = "behavior_judge_max_usd"
KEYS = (AUTO_JUDGE_KEY, APPROVAL_KEY, MAX_USD_KEY)
MAX_STANDING_JUDGE_USD = Decimal("50")  # the owner's per-run cap, October 20, 2026 ("$50 is good")
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
    if not any(key in config for key in KEYS):
        return []
    problems = []
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


def standing_approval(config, config_path=None):
    """The standing approval ``config`` grants: ``{"approval", "max_usd", "track", "config_path",
    "plan_section"}``. Raises ValueError when it is not enabled or not valid."""
    problems = standing_approval_problems(config)
    if not enabled(config):
        problems = [f"{AUTO_JUDGE_KEY} is not true"] + problems
    if problems:
        raise ValueError("; ".join(problems))
    return {"approval": config[APPROVAL_KEY].strip(), "max_usd": str(_amount(config[MAX_USD_KEY])),
            "track": config_track(config) or "baseline",
            "config_path": None if config_path is None else str(config_path), "plan_section": PLAN_SECTION}


def clamp_run_limit(limit_usd, max_usd, remaining_usd):
    """The standing-approval run limit: the smallest of the invocation's own limit, the per-run
    cap (never above $50) and the analysis scope's remaining allowance. Returns (limit, basis)."""
    candidates = [("invocation", Decimal(str(limit_usd))),
                  ("standing_approval_cap", min(Decimal(str(max_usd)), MAX_STANDING_JUDGE_USD))]
    if remaining_usd is not None:
        candidates.append(("analysis_remaining", Decimal(str(remaining_usd))))
    basis, limit = min(candidates, key=lambda pair: pair[1])  # the first wins a tie
    return limit, basis
