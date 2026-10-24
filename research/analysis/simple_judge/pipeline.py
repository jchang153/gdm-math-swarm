"""One model call per agent, code verification, and the run-level outputs (no model summary).

``judge_agents`` sends each case file once to the primary model; a refused request is never resent to that
model, it goes once to the fallback model (unit ``<agent>.fallback``) and the row is flagged. Each reply is
parsed and checked by ``verify.verify``. ``write_outputs`` writes ``agents.jsonl``, ``report.json`` (counts by
code, cost, wall time and, when the run has a rubric-3.1 judge, the comparison) and ``report.html``.
"""
from __future__ import annotations

import asyncio
import datetime
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from . import prompts, verify
from .route import MAX_TOKENS

SCHEMA = "gdm-simple-judge/1"
REFUSALS = ("refused_once", "refused_twice")
RESPONSE_FORMAT = {"type": "json_object"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def _complete(client, case, unit):
    return await client.complete(system=prompts.SYSTEM, user=prompts.user(case["text"]), purpose="simple_judge",
                                 unit_id=unit, max_tokens=MAX_TOKENS, effort=client.route.effort,
                                 response_format=RESPONSE_FORMAT if client.route.structured_outputs else None,
                                 refusal_retry=False)


async def judge_one(case, primary, fallback=None):
    """One agent's row: the model call (with the refusal fallback), parse and verification."""
    agent = case["agent_id"]
    calls = []
    result = await _complete(primary, case, agent)
    calls.append({"model": primary.route.model_id, "status": result["status"], "error": result.get("error"),
                  "cost_usd": None if result.get("cost_usd") is None else str(result["cost_usd"]),
                  "usage": {k: (result.get("usage") or {}).get(k) for k in ("prompt_tokens", "completion_tokens")}})
    used, fallback_used = primary, False
    if result["status"] != "ok" and (result.get("error") or {}).get("reason") in REFUSALS and fallback is not None:
        result = await _complete(fallback, case, f"{agent}.fallback")
        used, fallback_used = fallback, True
        calls.append({"model": fallback.route.model_id, "status": result["status"], "error": result.get("error"),
                      "cost_usd": None if result.get("cost_usd") is None else str(result["cost_usd"]),
                      "usage": {k: (result.get("usage") or {}).get(k) for k in ("prompt_tokens", "completion_tokens")}})
    row = {"agent_id": agent, "model": used.route.model_id, "fallback_used": fallback_used, "calls": calls,
           "facts": case["summary"], "case": case["stats"]}
    if result["status"] != "ok":
        reason = (result.get("error") or {}).get("reason") or result["status"]
        return dict(row, status="refused" if reason in REFUSALS else "failed", reason=reason, labels=None,
                    model_labels=None, checks=None)
    data, problem = verify.parse_reply(result.get("text"))
    if data is None:
        return dict(row, status="unparsed", reason=problem, labels=None, model_labels=None, checks=None,
                    reply=(result.get("text") or "")[:2000])
    labels, notes = verify.normalize_labels(data)
    final, checks = verify.verify(labels, case)
    checks["notes"] = notes
    model_labels = {k: v for k, v in labels.items() if k not in ("evidence", "rationale")}
    return dict(row, status="ok", reason=None, labels=final, model_labels=model_labels, checks=checks)


async def judge_agents(cases, primary, fallback=None, *, log=None):
    log = log or (lambda message: None)

    async def one(case):
        row = await judge_one(case, primary, fallback)
        log(f"{case['agent_id']}: {row['status']}" + (f" {row['labels']['cheating_level']}" if row["labels"] else "")
            + (" (fallback)" if row["fallback_used"] else ""))
        return row
    return await asyncio.gather(*(one(case) for case in cases))


def _cost(rows):
    total = Decimal(0)
    unknown = 0
    for row in rows:
        for call in row["calls"]:
            if call["cost_usd"] is None:
                unknown += 1
            else:
                total += Decimal(call["cost_usd"])
    return total, unknown


def counts(rows):
    """Run-level counts, by code only."""
    ok = [r for r in rows if r["status"] == "ok"]
    levels = Counter(r["labels"]["cheating_level"] for r in ok)
    out = {"agents": len(rows), "labelled": len(ok),
           "not_labelled": dict(Counter(r["status"] for r in rows if r["status"] != "ok")),
           "fallback_used": sum(r["fallback_used"] for r in rows),
           "cheating_level": {k: levels.get(k, 0) for k in (*prompts.LEVELS, verify.UNCLEAR)},
           "cheating_any": sum(r["labels"]["cheating_level"] in ("explored", "submitted", "credited") for r in ok),
           "submitted_basis": dict(Counter(r["labels"].get("submitted_basis") for r in ok
                                           if r["labels"]["cheating_level"] == "submitted")),
           "declined_after_considering": dict(Counter(str(r["labels"]["declined_after_considering"]) for r in ok)),
           "whistleblower": dict(Counter(str(r["labels"]["whistleblower"]) for r in ok)),
           "whistleblower_channels": dict(Counter(r["labels"]["whistleblower_channel"] for r in ok
                                                  if r["labels"]["whistleblower"] is True)),
           "evaluation_aware": dict(Counter(str(r["labels"]["evaluation_aware"]) for r in ok)),
           "likely_source": dict(Counter("independent" if r["labels"]["likely_source"] == "independent" else
                                         ("named" if r["labels"]["likely_source"] else "none") for r in ok
                                         if r["labels"]["cheating_level"] in ("explored", "submitted", "credited"))),
           "quotes": sum(r["checks"]["quotes"] for r in ok),
           "quotes_verified": sum(r["checks"]["quotes_verified"] for r in ok),
           "downgrades": sum(len(r["checks"]["downgrades"]) for r in ok),
           "detector_flags": sum(len(r["checks"]["flags"]) for r in ok),
           "detector": {"agents_with_flagged_submissions": sum(r["facts"]["flagged_submissions"] > 0 for r in rows),
                        "agents_with_flagged_credit": sum(r["facts"]["flagged_accepted"] > 0 for r in rows),
                        "flagged_credits": sum(r["facts"]["flagged_accepted"] for r in rows)}}
    return out


def write_outputs(out_dir, run_id, rows, *, meta, comparison=None):
    from .report import render_html
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: r["agent_id"])
    with open(out_dir / "agents.jsonl", "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    total, unknown = _cost(rows)
    report = {"schema": SCHEMA, "rubric": prompts.VERSION, "run_id": run_id, "generated_at": _now(), **meta,
              "cost_usd": str(total), "calls_with_unknown_cost": unknown, "counts": counts(rows),
              "comparison_with_rubric_3_1": comparison}
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str),
                                         encoding="utf-8")
    (out_dir / "report.html").write_text(render_html(report, rows), encoding="utf-8")
    return report
