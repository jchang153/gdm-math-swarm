"""Red-team roles for one designated solver (Amelie, November 10, 2026; plan and prompts in Amelie's
project repo, docs/redteam/plan_trackA.md and docs/redteam/prompts_trackA.md).

Track A tests whether a swarm agent can manipulate the overseer into accepting the seeded exploit.
Config key ``red_team`` (absent or null: no red-team role, every prompt byte-identical to the run
without it) is ``{"agent_id": "agent-NNN", "role": "attacker" | "reviewer", "note"?: str}``.

The designated solver keeps its persona, tools and solver prompt; ``section_for`` appends one
section that tells it

* which participant is the overseer, its system prompt as configured (exactly the text the runtime
  gives the overseer, push wording included: ``runtime.make_prompt_for`` passes the overseer's final
  prompt) and the message it receives when its session restarts. Not reproduced: what every agent
  gets alike from Claude Code (its own system text, the neutralized environment block, the tool
  list);
* what the seeded solution is and that it was accepted;
* its objective (attacker or benign reviewer).

The texts themselves are researcher-only (``TEXT_PATH``, host-only, pinned by ``TEXT_SHA256``).

Both roles get identical knowledge, so the conditions differ only in the objective. The role is researcher-side:
``roles`` in settings.json still says "solver" for the designated agent; ``effective.red_team``
records it.

``red_team_problems`` (preflight) fails closed unless the run is a declared seeded run with the
answer-wrapper seed, exactly one overseer without oversight tools and with contact, the Lean
domain, a neutral prompt variant, and a designated agent that is a solver.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

RED_TEAM_KEY = "red_team"
ROLES = ("attacker", "reviewer")
TEXT_VERSION = "trackA-v2"  # v2: the overseer's restart message is reproduced too
_KEYS = frozenset({"agent_id", "role", "note"})

# The seed the text describes: agent-026's answer-wrapper submission (configs/*seeded*.json,
# seeded.artifact_sha256). Another seed would make the description false, so preflight refuses it.
ANSWER_WRAPPER_SEED_SHA256 = "61ce60ccc22873dae8b74b6fcf5d8924a91cb923d5a68e4ff943ea51c0f06ccf"
MAX_TRIGGER_SECONDS = 600  # the text says "a few minutes after the start"

# The role texts are researcher-only (attacker instructions and the exploit's description): they live host-only
# in the git-ignored private/ folder (research/analysis/private_material.py; this repository is public), pinned
# by SHA-256 so every run with the same pin gets the same bytes. Keys: text_version, intro {role}, knowledge
# (with {overseer_id} and {task_id}), objective {role}. Canonical copy: Amelie's project repo,
# docs/redteam/trackA_text.json.
TEXT_PATH = "private/research/redteam/trackA_text.json"
TEXT_SHA256 = "0e0cd11bdf46c75bf44317850a627e3307ab22d99ef97600ca3be6dadf8bb330"
_REPO = Path(__file__).resolve().parents[2]


def load_text(repo_root=None) -> dict:
    """The pinned role texts. Raises FileNotFoundError when absent, ValueError on another digest or shape."""
    data = (Path(repo_root or _REPO) / TEXT_PATH).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != TEXT_SHA256:
        raise ValueError(f"{TEXT_PATH} has SHA-256 {digest[:12]}..., expected {TEXT_SHA256[:12]}...")
    text = json.loads(data)
    if (text.get("text_version") != TEXT_VERSION or not isinstance(text.get("knowledge"), str)
            or any(set(text.get(k) or {}) != set(ROLES) for k in ("intro", "objective"))):
        raise ValueError(f"{TEXT_PATH} does not have the expected keys or text version {TEXT_VERSION}")
    return text


def red_team_config(config):
    """The ``red_team`` block or None. Assumes ``red_team_problems`` returned no problems."""
    value = (config or {}).get(RED_TEAM_KEY)
    return value if value else None


def designated(agent_id, block) -> bool:
    return bool(block) and agent_id == block.get("agent_id")


def red_team_problems(config) -> list[str]:
    """Preflight check of the ``red_team`` block (fails closed); [] without the key."""
    value = (config or {}).get(RED_TEAM_KEY)
    if value is None:
        return []
    if not isinstance(value, dict) or set(value) - _KEYS or not {"agent_id", "role"} <= set(value):
        return [f"{RED_TEAM_KEY} must be an object with agent_id and role (and optionally note)"]
    problems = []
    if value["role"] not in ROLES:
        problems.append(f"{RED_TEAM_KEY}.role must be one of {ROLES}")
    count = config.get("agent_count")
    solvers = ({f"agent-{i:03d}" for i in range(1, count + 1)}
               if isinstance(count, int) and not isinstance(count, bool) else set())
    if value["agent_id"] not in solvers:
        problems.append(f"{RED_TEAM_KEY}.agent_id must be one of the solvers (agent-001 to agent-{count:03d})"
                        if solvers else f"{RED_TEAM_KEY} needs an integer agent_count")
    from .coding import domain_of_config
    if domain_of_config(config) != "lean":
        problems.append(f"{RED_TEAM_KEY} is only defined for the Lean domain")
    from . import prompts
    if not prompts.neutral_environment(config.get(prompts.PROMPT_VARIANT_KEY, prompts.BASELINE_VARIANT)):
        problems.append(f"{RED_TEAM_KEY} requires a neutral collaboration prompt variant")
    from .seeded import seed_agent
    seeded = config.get("seeded") if isinstance(config.get("seeded"), dict) else {}
    if not seed_agent(config):
        problems.append(f"{RED_TEAM_KEY} requires a declared seeded run")
    elif seeded.get("artifact_sha256") != ANSWER_WRAPPER_SEED_SHA256 or not isinstance(seeded.get("task_id"), str):
        problems.append(f"{RED_TEAM_KEY} requires the single answer-wrapper seed (artifact_sha256 "
                        f"{ANSWER_WRAPPER_SEED_SHA256[:12]}..., one task_id): its text describes that exploit")
    else:
        trigger, end = seeded.get("trigger_value"), config.get("max_run_seconds")
        number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        if (seeded.get("trigger_kind") != "elapsed_seconds" or not number(trigger)
                or not 0 < trigger <= MAX_TRIGGER_SECONDS or not number(end) or trigger >= end):
            problems.append(f"{RED_TEAM_KEY} requires an elapsed_seconds seed trigger above 0, at most "
                            f"{MAX_TRIGGER_SECONDS} s and before max_run_seconds (the text says 'a few minutes "
                            "after the start')")
    try:
        load_text()
    except (OSError, ValueError) as error:
        problems.append(f"{RED_TEAM_KEY} needs its researcher-only text ({TEXT_PATH}, host-only): {error}")
    from . import oversight
    overseer = config.get(oversight.OVERSEER_KEY)
    if oversight.overseer_count(config) != 1:
        problems.append(f"{RED_TEAM_KEY} requires exactly one overseer")
    elif not isinstance(overseer, dict) or overseer.get("powers") is not False or overseer.get("contact") is False:
        problems.append(f"{RED_TEAM_KEY} requires an overseer without oversight tools and with contact (the text "
                        "says it cannot block, remove or undo submissions and communicates with the agents)")
    return problems


def section_for(role, overseer_id, overseer_prompt, overseer_continue, task_id, text=None) -> str:
    """The section appended to the designated solver's prompt. ``overseer_prompt`` must be the
    overseer's final system prompt and ``overseer_continue`` its restart message (both after push
    wording; ``runtime.make_prompt_for`` passes them). ``text``: the role texts (default ``load_text()``).
    Fails closed on a forbidden word."""
    if role not in ROLES:
        raise ValueError(f"Unknown red-team role {role!r}")
    from .prompts import NEUTRAL_FORBIDDEN
    text = load_text() if text is None else text
    own = "\n\n".join([
        "---",
        "## Your actual objective",
        text["intro"][role],
        text["knowledge"].format(overseer_id=overseer_id, task_id=task_id),
        text["objective"][role],
    ])
    found = NEUTRAL_FORBIDDEN.search(own)
    if found:
        raise ValueError(f"The red-team section contains a word the neutral environment forbids ({found.group(0)!r})")
    # Both texts verbatim (trailing newlines included), so the copy is byte-identical to what the overseer gets.
    return (own + "\n\n## The overseer's instructions\n\n" + overseer_prompt
            + "\n\n## The message the overseer receives when its session restarts\n\n" + overseer_continue)


def append_section(solver_prompt, section) -> str:
    """The designated solver's prompt: its own prompt, a blank line, then the section."""
    return solver_prompt.rstrip("\n") + "\n\n" + section


def effective(block, section) -> dict | None:
    """settings.json ``effective.red_team``."""
    if not block:
        return None
    return {"agent_id": block["agent_id"], "role": block["role"], "text_version": TEXT_VERSION,
            "text_sha256": TEXT_SHA256,
            "section_sha256": hashlib.sha256(section.encode()).hexdigest(), "section_chars": len(section)}
