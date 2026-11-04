"""Story map evidence chains: for every stage-1 thought, stable references to the items behind it (offline, free).

``python -m research.analysis.storymap.evidence --map runs/<id>/analysis/storymap --timeline DIR [--out DIR]``
writes ``OUT/evidence/<agent>.json`` (default ``OUT`` = ``--map``) from the stage-0 timeline and the stage-1 rows; no
model calls. The GUI (``view.py``) can embed it, or call ``agent_evidence(items, stage1_rows)`` in memory, so a
thought can be expanded into its whole reasoning chain without reading raw logs. Only timeline items (already trimmed
by stage 0) are copied; nothing from the private stage prompts, taxonomy notes or rubrics.

Per thought (field contract in docs/HANDOFF.md, item 24): ``key`` (``c<chunk:03d>-t<index:02d>``: the stage-1 row
and the thought's position in it, stable as long as the stage-1 row is), ``chunk``, ``index``, ``code``, ``certainty``,
``item``, ``turn`` (the cited item's turn, which wins over the model's copy), ``t``, ``quote_match`` (``item``: the
quote is verbatim, after whitespace normalization, in the cited item; ``chunk``: only in another item of the chunk,
then ``quote_span.item`` names that item; null: not found), ``quote_span`` (``{"item", "start", "end"}`` character
offsets into that item's ``text``, or null), ``chain_items`` (ordered item indices: the previous model step and the
thought's own step, each with its tool calls, results and events, e.g. pushed posts at the start of the step),
``chain_turns`` (``[from, to]``) and ``tool_calls`` (``[{"call_item", "result_item", "call_id", "tool"}]`` in the
chain). ``items`` holds every referenced item once.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

from research.analysis.storymap import chunk as chunking

SCHEMA = "gdm-storymap-evidence/1"
TEXT_CAP = {"reasoning": 6000, "text": 6000, "tool_call": 3000}   # characters kept per item kind
DEFAULT_CAP = 2000                                                   # tool results, events, context
BEFORE_TURNS = 1                                                     # previous model steps in the chain


def _norm_map(text):
    """(normalized text, offsets): NFC and whitespace runs collapsed to one space, with each normalized
    character's offset in the original string (so a match maps back to an exact span)."""
    out, offsets = [], []
    space = False
    for k, ch in enumerate(text):
        for c in unicodedata.normalize("NFC", ch):
            if c.isspace():
                if space:
                    continue
                space = True
                out.append(" ")
            else:
                space = False
                out.append(c)
            offsets.append(k)
    return "".join(out), offsets


def quote_span(quote, text):
    """``(start, end)`` of ``quote`` in ``text`` (exact first, then whitespace-normalized as ``chunk.normalize_quote``),
    or None."""
    text = str(text or "")
    wanted = chunking.normalize_quote(quote)
    if not wanted:
        return None
    k = text.find(wanted)
    if k >= 0:
        return k, k + len(wanted)
    norm, offsets = _norm_map(text)
    k = norm.find(wanted)
    if k < 0:
        return None
    return offsets[k], offsets[k + len(wanted) - 1] + 1


def _turn(item):
    return item.get("turn") if isinstance(item.get("turn"), int) else None


def chain(items, position, before_turns=BEFORE_TURNS):
    """Positions (in ``items``) of the chain around ``items[position]``: every item of its turn and of the
    ``before_turns`` previous turns (turns are model steps; tool results carry their call's turn)."""
    turn = _turn(items[position])
    if turn is None:
        return [position]
    lo = turn - before_turns
    out = [k for k, it in enumerate(items) if _turn(it) is not None and lo <= _turn(it) <= turn]
    return out or [position]


def _cap(item):
    text = str(item.get("text") or "")
    cap = TEXT_CAP.get(item.get("kind"), DEFAULT_CAP)
    return (text, False) if len(text) <= cap else (text[:cap], True)


def item_record(item):
    text, cut = _cap(item)
    rec = {"i": item["i"], "turn": item.get("turn"), "t": item.get("t"), "kind": item.get("kind"),
           "tool": item.get("tool"), "call_id": item.get("call_id"), "text": text, "text_cut": cut}
    event = item.get("event") if isinstance(item.get("event"), dict) else None
    if event is not None:
        detail = event.get("detail") or {}
        rec.update(event_type=event.get("type"), event_id=event.get("id"),
                   delivery=detail.get("delivery"), delivery_id=detail.get("delivery_id"))
    return rec


def _tool_calls(chain_items):
    """Tool calls in the chain with their results: by ``call_id`` when the timeline has it, else the next
    ``tool_result`` of the same tool and turn."""
    calls, used = [], set()
    for k, it in enumerate(chain_items):
        if it.get("kind") != "tool_call":
            continue
        result = None
        for later in chain_items[k + 1:]:
            if later.get("kind") != "tool_result" or later["i"] in used:
                continue
            if it.get("call_id") and later.get("call_id"):
                if later["call_id"] == it["call_id"]:
                    result = later
                    break
            elif later.get("tool") == it.get("tool") and later.get("turn") == it.get("turn"):
                result = later
                break
        if result is not None:
            used.add(result["i"])
        calls.append({"call_item": it["i"], "result_item": None if result is None else result["i"],
                      "call_id": it.get("call_id"), "tool": it.get("tool")})
    return calls


def thought_evidence(items, thought, *, chunk=None, index=None, chunk_items=None, by_i=None, positions=None):
    """The evidence record of one stage-1 thought (see the module docstring)."""
    by_i = by_i if by_i is not None else {it["i"]: it for it in items}
    positions = positions if positions is not None else {it["i"]: k for k, it in enumerate(items)}
    cited = thought.get("item")
    rec = {"key": None if chunk is None or index is None else f"c{int(chunk):03d}-t{int(index):02d}",
           "chunk": chunk, "index": index, "code": thought.get("code"), "certainty": thought.get("certainty"),
           "item": cited, "turn": thought.get("turn"), "t": thought.get("t"), "quote_match": None,
           "quote_span": None, "chain_items": [], "chain_turns": None, "tool_calls": []}
    source = by_i.get(cited) if isinstance(cited, int) else None
    span = quote_span(thought.get("quote"), source.get("text")) if source is not None else None
    if span is not None:
        rec["quote_match"], rec["quote_span"] = "item", {"item": cited, "start": span[0], "end": span[1]}
    else:
        for other in chunk_items or ():
            other_span = quote_span(thought.get("quote"), other.get("text"))
            if other_span is not None:
                rec["quote_match"] = "chunk"
                rec["quote_span"] = {"item": other["i"], "start": other_span[0], "end": other_span[1]}
                source = source if source is not None else other
                break
    anchor = source if source is not None else (by_i.get(rec["quote_span"]["item"]) if rec["quote_span"] else None)
    if anchor is None:
        return rec
    rec["turn"], rec["t"] = anchor.get("turn", rec["turn"]), anchor.get("t", rec["t"])
    picked = [items[k] for k in chain(items, positions[anchor["i"]])]
    if rec["quote_span"] and rec["quote_span"]["item"] not in {it["i"] for it in picked}:
        picked = sorted(picked + [by_i[rec["quote_span"]["item"]]], key=lambda it: positions[it["i"]])
    rec["chain_items"] = [it["i"] for it in picked]
    turns = [_turn(it) for it in picked if _turn(it) is not None]
    rec["chain_turns"] = [min(turns), max(turns)] if turns else None
    rec["tool_calls"] = _tool_calls(picked)
    return rec


def agent_evidence(items, stage1_rows, agent=None):
    """``{"schema", "agent", "thoughts", "items"}`` for one agent from its timeline items and stage-1 rows."""
    by_i = {it["i"]: it for it in items}
    positions = {it["i"]: k for k, it in enumerate(items)}
    thoughts, used = [], set()
    for row in stage1_rows or ():
        if row.get("status") not in (None, "ok") or not isinstance(row.get("thoughts"), list):
            continue
        c = row.get("chunk")
        lo, hi = row.get("first_item"), row.get("last_item")
        inside = [it for it in items if isinstance(lo, int) and isinstance(hi, int) and lo <= it["i"] <= hi]
        for n, thought in enumerate(row["thoughts"]):
            if not isinstance(thought, dict):
                continue
            rec = thought_evidence(items, thought, chunk=c, index=n, chunk_items=inside, by_i=by_i,
                                   positions=positions)
            thoughts.append(rec)
            used.update(rec["chain_items"])
            if rec["quote_span"]:
                used.add(rec["quote_span"]["item"])
    return {"schema": SCHEMA, "agent": agent, "thoughts": thoughts,
            "items": {str(i): item_record(by_i[i]) for i in sorted(used) if i in by_i}}


def _read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build(map_dir, timeline, out=None, agents=None):
    """Write ``OUT/evidence/<agent>.json`` for every agent with a timeline and stage-1 rows; returns counts."""
    map_dir, folder = Path(map_dir), chunking.timeline_folder(timeline)
    target = Path(out or map_dir) / "evidence"
    target.mkdir(parents=True, exist_ok=True)
    counts = {}
    names = agents or sorted(p.stem for p in (map_dir / "stage1").glob("agent-*.jsonl"))
    for agent in names:
        if not (folder / f"{agent}.jsonl").exists():
            continue
        data = agent_evidence(chunking.load_items(folder, agent), _read_jsonl(map_dir / "stage1" / f"{agent}.jsonl"),
                              agent)
        tmp = target / f"{agent}.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        tmp.replace(target / f"{agent}.json")
        counts[agent] = {"thoughts": len(data["thoughts"]), "items": len(data["items"]),
                         "unmatched": sum(t["quote_match"] is None for t in data["thoughts"])}
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map", required=True, help="story-map output folder (holding stage1/)")
    ap.add_argument("--timeline", required=True, help="stage-0 DIR (holding timeline/) or the timeline folder")
    ap.add_argument("--out", help="default: --map")
    ap.add_argument("--agents", help="comma-separated agent ids (default: every agent with stage-1 rows)")
    a = ap.parse_args(argv)
    agents = [x.strip() for x in a.agents.split(",") if x.strip()] if a.agents else None
    counts = build(a.map, a.timeline, a.out, agents)
    total = {k: sum(v[k] for v in counts.values()) for k in ("thoughts", "items", "unmatched")}
    print(json.dumps({"agents": len(counts), **total}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
