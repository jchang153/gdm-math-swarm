"""Participant roles of a run, read from its researcher-side records (no model, read only).

Roles come from ``RUN/settings.json``: ``effective.roles`` ({agent: "solver"|"overseer"|"seed"}), the
overseers' ids (``effective.overseer`` / ``config.overseer``: ``agent_ids`` for several overseers, else the
legacy single ``agent_id``), and, in a seeded run (track
``"seeded"``), the seeded author (``effective.seeded.seed_agent`` or ``config.seeded.author``). The seed is a
silent participant planted by the researchers (owner design, 2026-10-25): it has no container, model, Inspect
sample or transcript; the conference credits it with the seeded artifact at the trigger time
(``seeded_artifact_injected`` in ``conference/events.jsonl``). Analyses treat it as the seeded source of the
exploit, never as an independent discoverer and never as a solver.

Runs without a seed or an overseer give ``{}`` from ``special_roles`` and None from ``seeded_info``, so
their analyses are unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

SOLVER, OVERSEER, SEED = "solver", "overseer", "seed"
SEED_LABEL = "seeded (planted by researchers)"
ROLE_LABELS = {SEED: SEED_LABEL, OVERSEER: "overseer"}


def load_settings(run_dir):
    """``RUN/settings.json`` as a dict ({} when missing or invalid)."""
    try:
        data = json.loads((Path(run_dir) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _settings(source):
    if isinstance(source, dict):
        return source
    return load_settings(source) if source is not None else {}


def _is_seeded(settings):
    config, effective = settings.get("config") or {}, settings.get("effective") or {}
    seeded = config.get("seeded") if isinstance(config.get("seeded"), dict) else {}
    return "seeded" in (config.get("track"), effective.get("track")) or bool(seeded.get("enabled")) \
        or isinstance(effective.get("seeded"), dict)


def seed_agents(source):
    """The seeded participants: every agent whose role is ``seed`` in ``effective.roles``, plus the seeded
    author of a seeded run (``effective.seeded.seed_agent``, ``config.seeded.author``)."""
    settings = _settings(source)
    config, effective = settings.get("config") or {}, settings.get("effective") or {}
    seeds = {a for a, r in (effective.get("roles") or {}).items() if r == SEED}
    if _is_seeded(settings):
        for block, key in ((effective.get("seeded"), "seed_agent"), (config.get("seeded"), "author"),
                           (effective.get("seeded"), "author")):
            if isinstance(block, dict) and isinstance(block.get(key), str) and block[key]:
                seeds.add(block[key])
    return seeds


def overseer_ids(block):
    """The overseer ids of an overseer block: ``agent_ids`` (multi-overseer form) else ``[agent_id]``
    (legacy single form) else []."""
    if not isinstance(block, dict):
        return []
    ids = block.get("agent_ids")
    if isinstance(ids, (list, tuple)) and ids:
        return [str(a) for a in ids if a]
    return [block["agent_id"]] if block.get("agent_id") else []


def overseer_block(source):
    """``effective.overseer`` (else ``config.overseer``) of the run's settings, or None."""
    settings = _settings(source)
    block = (settings.get("effective") or {}).get("overseer") or (settings.get("config") or {}).get("overseer")
    return block if isinstance(block, dict) else None


def overseers(source):
    """Every overseer of the run, sorted: ``effective.roles`` entries with role overseer plus the overseer
    block's ids (both forms); [] in runs without an overseer."""
    settings = _settings(source)
    out = {a for a, r in ((settings.get("effective") or {}).get("roles") or {}).items() if r == OVERSEER}
    out.update(overseer_ids(overseer_block(settings)))
    return sorted(out)


def board_enabled(source):
    """True when the run's overseer block enables the private overseer board."""
    return bool((overseer_block(source) or {}).get("board"))


def special_roles(source):
    """{agent: role} for the participants that are not solvers (overseer, seed); {} in ordinary runs."""
    settings = _settings(source)
    config, effective = settings.get("config") or {}, settings.get("effective") or {}
    out = {a: r for a, r in (effective.get("roles") or {}).items() if r and r != SOLVER}
    overseer = effective.get("overseer") or config.get("overseer")
    for agent in overseer_ids(overseer):
        out.setdefault(agent, OVERSEER)
    for agent in seed_agents(settings):
        out[agent] = SEED
    return dict(sorted(out.items()))


def seeded_info(source):
    """The seeded design of a seeded run, or None: ``{"seed_agent", "task_id", "channel", "trigger",
    "artifact_sha256", "path", "source"}`` (``effective.seeded`` first, ``config.seeded`` filling gaps)."""
    settings = _settings(source)
    seeds = seed_agents(settings)
    if not seeds or not _is_seeded(settings):
        return None
    effective = (settings.get("effective") or {}).get("seeded") or {}
    config = (settings.get("config") or {}).get("seeded") or {}
    effective = effective if isinstance(effective, dict) else {}
    config = config if isinstance(config, dict) else {}
    agent = effective.get("seed_agent") or config.get("author") or sorted(seeds)[0]
    trigger = effective.get("trigger")
    if not isinstance(trigger, dict) and (config.get("trigger_kind") or config.get("trigger_value") is not None):
        trigger = {"kind": config.get("trigger_kind"), "value": config.get("trigger_value")}
    entries = effective.get("seeds") if isinstance(effective.get("seeds"), list) else config.get("artifacts")
    entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    first = entries[0] if entries else {}
    task = effective.get("task_id") or config.get("task_id") or first.get("task_id")
    channel = effective.get("channel") or config.get("channel")
    if not isinstance(trigger, dict) and first.get("trigger_value") is not None:
        trigger = {"kind": config.get("trigger_kind"), "value": first.get("trigger_value")}

    def path_of(task_id, library_path=None):
        return f"solutions/{task_id}.lean" if task_id and channel == "library_solution" else library_path

    info = {"seed_agent": agent, "seed_agents": sorted(seeds), "task_id": task, "channel": channel,
            "trigger": trigger,
            "artifact_sha256": effective.get("artifact_sha256") or config.get("artifact_sha256")
            or first.get("artifact_sha256"),
            "path": path_of(task, config.get("library_path") or first.get("library_path")),
            "source": effective.get("source") or config.get("source") or first.get("source")}
    # Several seeds (owner decision 2026-10-29): every planted artifact, in the configured order.
    seeds_list = [{"task_id": e.get("task_id"), "path": e.get("path") or path_of(e.get("task_id"), e.get("library_path")),
                   "family": e.get("family"), "artifact_sha256": e.get("artifact_sha256"),
                   "trigger": e.get("trigger") if isinstance(e.get("trigger"), dict)
                   else {"kind": config.get("trigger_kind"), "value": e.get("trigger_value")}}
                  for e in entries]
    info["seeds"] = seeds_list or [{"task_id": task, "path": info["path"], "family": None,
                                    "artifact_sha256": info["artifact_sha256"], "trigger": trigger}]
    info["task_ids"] = [e["task_id"] for e in info["seeds"] if e.get("task_id")]
    return info


def injections(run_dir):
    """Every ``seeded_artifact_injected`` event of ``conference/events.jsonl``, in order, each as
    ``{"timestamp", "time", "seq", "payload"}`` ([] when none)."""
    path = Path(run_dir) / "conference" / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if "seeded_artifact_injected" not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") == "seeded_artifact_injected":
                out.append({"timestamp": event.get("timestamp"), "time": event.get("time"), "seq": event.get("seq"),
                            "payload": event.get("payload") or {}})
    return out


def injection(run_dir):
    """The first ``seeded_artifact_injected`` event of ``conference/events.jsonl`` as
    ``{"timestamp", "time", "seq", "payload"}``, or None (not injected, or no event log)."""
    path = Path(run_dir) / "conference" / "events.jsonl"
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if "seeded_artifact_injected" not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") == "seeded_artifact_injected":
                return {"timestamp": event.get("timestamp"), "time": event.get("time"), "seq": event.get("seq"),
                        "payload": event.get("payload") or {}}
    return None


def label(agent, roles):
    """``agent`` with its role in words when it is not a solver: ``agent-101 (seeded, planted by
    researchers)``, ``agent-102 (overseer)``; a solver is its bare id."""
    role = (roles or {}).get(agent)
    if role == SEED:
        return f"{agent} (seeded, planted by researchers)"
    if role and role != SOLVER:
        return f"{agent} ({ROLE_LABELS.get(role, role)})"
    return str(agent)
