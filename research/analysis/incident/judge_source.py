"""Which behavior judge's findings the incident report reads (owner decision, October 24, 2026).

The simplified one-pass judge (``research/analysis/simple_judge/``, outputs ``RUN/analysis/simple_judge/
{agents.jsonl, report.json}``) is the default post-run judge; the rubric-3.1 judge (``RUN/analysis/judge/``)
stays opt-in. With ``preference="auto"`` the simplified judge is preferred when its outputs exist, else the
rubric-3.1 judge, else none (the contagion section then uses the deterministic detectors). ``simple``,
``full`` and ``none`` force one source; a forced source without outputs resolves to ``none`` with a
``problem`` (the command line refuses such a request before doing anything).
"""
from __future__ import annotations

import json
from pathlib import Path

SIMPLE, FULL, NONE = "simple", "full", "none"
CHOICES = ("auto", SIMPLE, FULL, NONE)
SIMPLE_RUBRIC = "gdm-simple-judge/1"
NONE_LABEL = "none, deterministic detectors"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def simple_rows(simple_dir):
    """The simplified judge's rows (``agents.jsonl``; unparseable lines skipped), or None without the file."""
    path = Path(simple_dir) / "agents.jsonl"
    if not path.is_file():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("agent_id"):
            rows.append(row)
    return rows


def simple_available(simple_dir):
    return bool(simple_rows(simple_dir))


def full_available(judge_dir):
    judge_dir = Path(judge_dir)
    return any((judge_dir / name).is_file() for name in ("labels.jsonl", "report.json", "run_level.json")) \
        or any((judge_dir / "agents").glob("agent-*.json"))


def simple_rubric(simple_dir):
    report = _read_json(Path(simple_dir) / "report.json") or {}
    return report.get("rubric") or report.get("schema") or SIMPLE_RUBRIC


def full_rubric(judge_dir):
    report = _read_json(Path(judge_dir) / "report.json") or {}
    rubric = report.get("rubric")
    if isinstance(rubric, dict):
        return rubric.get("judge_rubric_version") or rubric.get("version")
    return rubric if isinstance(rubric, str) else None


def label(source, rubric=None):
    """The plain name of a findings source, as shown in the report."""
    if source == SIMPLE:
        return f"simplified one-pass judge ({rubric or SIMPLE_RUBRIC})"
    if source == FULL:
        return "rubric-3.1 judge" + (f" ({rubric})" if rubric else "")
    return NONE_LABEL


def resolve(run_dir, *, preference="auto", judge_dir=None, simple_judge_dir=None):
    """``{"source", "dir", "rubric", "label", "statement", "preference", "available", "problem"}``."""
    run_dir = Path(run_dir)
    judge_dir = Path(judge_dir) if judge_dir else run_dir / "analysis" / "judge"
    simple_dir = Path(simple_judge_dir) if simple_judge_dir else run_dir / "analysis" / "simple_judge"
    preference = preference or "auto"
    if preference not in CHOICES:
        raise ValueError(f"judge source must be one of {CHOICES}, not {preference!r}")
    available = {SIMPLE: simple_available(simple_dir), FULL: full_available(judge_dir)}
    problem = None
    if preference == "auto":
        source = SIMPLE if available[SIMPLE] else FULL if available[FULL] else NONE
    elif preference == NONE or available[preference]:
        source = preference
    else:
        source = NONE
        where = simple_dir if preference == SIMPLE else judge_dir
        problem = f"requested the {label(preference)} but {where} has no outputs"
    directory = simple_dir if source == SIMPLE else judge_dir if source == FULL else None
    rubric = simple_rubric(simple_dir) if source == SIMPLE else full_rubric(judge_dir) if source == FULL else None
    name = label(source, rubric)
    return {"source": source, "dir": str(directory) if directory else None, "rubric": rubric, "label": name,
            "statement": f"Behavior findings: {name}.", "preference": preference,
            "available": available, "problem": problem,
            "paths": {SIMPLE: str(simple_dir), FULL: str(judge_dir)}}
