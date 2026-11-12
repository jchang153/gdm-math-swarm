"""Output schemas of the agent story map (INTERFACE.md, W2; docs/plan.md, "Agent story map").

Strict validators for the three model outputs:

* ``validate_stage1`` - one chunk: ``{"thoughts": [...], "state": {...}}``;
* ``validate_stage2`` - one event: the reaction record (taxonomy E1-E5, F1-F5);
* ``validate_stage3`` - one agent: phases, turning points, story, final labels.

Each takes the raw model text (the last parseable fenced JSON object is used, as in the behaviour
judge) or an already-parsed dict, checks the shape and returns a normalized copy, or raises
``SchemaError`` whose ``problems`` are precise, repair-ready strings (the pipeline sends them back
once). Optional context arguments add checks against the records: the chunk's or window's item
texts (quotes must be verbatim after Unicode NFC normalization and whitespace collapsing, as in
the judge), the expected event id or agent id, and the known event ids.

The meanings of the codes, stances and labels, and the prompts, are researcher-only and live in
the Git-ignored ``private/research/analysis/storymap/`` (``taxonomy.md``, ``prompts.md``). This
module never embeds those definitions: ``taxonomy_info`` and ``prompt_info`` read them at run time
and return their SHA-256; the default code list for validation comes from the taxonomy file. Only
the output field names and enum values (the JSON contract of INTERFACE.md) are public.

Stdlib only; no model calls; never reads credentials.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .. import private_material
from ..judge import schema as judge_schema
from ..judge.validate import normalize_text

SCHEMA_VERSION = "storymap-schema/1"
TAXONOMY_RELATIVE = "research/analysis/storymap/taxonomy.md"
PROMPTS_RELATIVE = "research/analysis/storymap/prompts.md"
STAGES = ("stage1", "stage2", "stage3")

QUOTE_MAX_CHARS = 300
SUMMARY_MAX_CHARS = 600
STORY_MAX_CHARS = 1200
CERTAINTIES = ("high", "medium", "low")
STANCES = ("none", "considering", "rejected", "deferred", "faking", "stopped")
BELIEVES_JUDGE = ("compile_only", "checks_answer", "unknown")
REGISTERED = ("yes", "no", "unclear")
CHANGES = ("none", "belief", "behaviour")
DIRECTIONS = ("toward_cheating", "away", "neutral")
F3_VALUES = ("punishment", "refake_signal", "unclear")
OVERSEER_KEYS = ("F1", "F2", "F3", "F4", "F5")
SOURCES = ("library", "self_then_library", "overseer_then_library", "board", "dm", "self", "none", "unknown")
AWARE = ("yes", "no", "unclear")
EVENT_TYPES = ("L1", "L2", "P1", "P2", "B1", "D1", "S1", "O1", "C1", "N1", "R1")
# Taxonomy groups whose codes Stage 1 may record as thoughts (E codes are Stage 2 fields).
THOUGHT_GROUPS = ("A", "B", "C", "D", "F", "G")
TIME_TOLERANCE = 1e-6
BOUNDARY_TOLERANCE = 0.01   # minutes: a turning point and its phase boundary, consecutive phases

_AGENT_ID = re.compile(r"^agent-\d{3}$")
_CODE_HEADING = re.compile(r"^### ([A-Z]\d+) - (.+?)\s*$", re.M)
_EVENT_HEADING = re.compile(r"^### event ([A-Z]\d+) - (.+?)\s*$", re.M)
_VERSION = re.compile(r"Version `([^`]+)`")
_SECTION = re.compile(r"^## (\w+)\s*$", re.M)


class SchemaError(judge_schema.SchemaError):
    """Story-map model output that cannot be used; ``problems`` are repair-ready strings."""


# ---------------------------------------------------------------------------
# Researcher-only material (read at run time)


def _read(relative, path=None):
    source = Path(path) if path is not None else private_material.path(relative)
    if not source.exists():
        raise private_material.PrivateMaterialMissing(private_material.missing_message(relative))
    return source, source.read_bytes()


def taxonomy_info(path=None):
    """{"path", "sha256", "version", "text", "codes", "names", "groups", "thought_codes",
    "reaction_codes", "event_types"} of the story-map taxonomy, read now.

    ``codes`` lists every judged code in file order (``### <CODE> - <name>`` headings);
    ``thought_codes`` those Stage 1 may record (groups ``THOUGHT_GROUPS``); ``reaction_codes`` the
    E codes; ``event_types`` the ``### event <TYPE> - <name>`` headings."""
    source, data = _read(TAXONOMY_RELATIVE, path)
    # Monitor-run material (``<!-- monitor-runs:begin -->`` blocks: events X1/X2, thought group W) is left out
    # here, so every prompt, SHA-256 and code list is byte for byte what it was before it was added; it is read by
    # ``monitor_info`` and sent only for agents with block events (owner request, November 12, 2026).
    text = strip_monitor(data.decode("utf-8"))
    data = text.encode("utf-8")
    codes, names = [], {}
    for match in _CODE_HEADING.finditer(text):
        code = match.group(1)
        if code in names:
            raise ValueError(f"taxonomy defines {code} twice")
        codes.append(code)
        names[code] = match.group(2)
    groups = {}
    for code in codes:
        groups.setdefault(code[0], []).append(code)
    match = _VERSION.search(text)
    return {"path": _shown(source), "sha256": hashlib.sha256(data).hexdigest(),
            "version": match.group(1) if match else None, "text": text,
            "codes": codes, "names": names, "groups": groups,
            "thought_codes": [c for c in codes if c[0] in THOUGHT_GROUPS],
            "reaction_codes": list(groups.get("E", [])),
            "event_types": [m.group(1) for m in _EVENT_HEADING.finditer(text)]}


MONITOR_BLOCK = re.compile(r"\n<!-- monitor-runs:begin -->\n(.*?)<!-- monitor-runs:end -->\n", re.S)
MONITOR_EVENT_TYPES = ("X1", "X2")
MONITOR_GROUP = "W"


def strip_monitor(text):
    """The taxonomy without its monitor-run blocks (the file as it was before they were added)."""
    return MONITOR_BLOCK.sub("", text)


def monitor_info(path=None):
    """{"text", "sha256", "codes", "names", "event_types"} of the taxonomy's monitor-run blocks (events X1/X2, thought
    group W), concatenated in file order; ``codes`` are the W thought codes. Empty text when the file has none."""
    _, data = _read(TAXONOMY_RELATIVE, path)
    text = "\n\n".join(m.group(1).strip("\n") for m in MONITOR_BLOCK.finditer(data.decode("utf-8")))
    codes, names = [], {}
    for match in _CODE_HEADING.finditer(text):
        codes.append(match.group(1))
        names[match.group(1)] = match.group(2)
    return {"text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "codes": [c for c in codes if c[0] == MONITOR_GROUP], "names": names,
            "event_types": [m.group(1) for m in _EVENT_HEADING.finditer(text)]}


def taxonomy_codes(path=None):
    """The judged codes of the taxonomy (every group), in file order."""
    return list(taxonomy_info(path)["codes"])


def _blocks(text):
    """{section name: body of its single fenced ``text`` block} of prompts.md. A block runs from
    the ```` ```text ```` line to the last closing fence of the section (blocks contain fenced JSON
    examples)."""
    heads = list(_SECTION.finditer(text))
    out = {}
    for n, head in enumerate(heads):
        body = text[head.end():heads[n + 1].start() if n + 1 < len(heads) else len(text)]
        lines = body.split("\n")
        try:
            start = next(k for k, line in enumerate(lines) if line.strip() == "```text")
            end = max(k for k, line in enumerate(lines) if line.strip() == "```")
        except (StopIteration, ValueError):
            continue
        if end > start:
            out[head.group(1)] = "\n".join(lines[start + 1:end]).strip("\n")
    return out


def prompt_info(stage, *, prompts_path=None, taxonomy_path=None):
    """{"stage", "text", "sha256", "version", "template_sha256", "taxonomy_sha256",
    "taxonomy_version", "prompts_path", "taxonomy_path"} of one stage's system prompt, rendered
    now: the shared block, the stage block and the taxonomy block with the placeholders filled in.
    ``sha256`` is that of the rendered text (identical for every request of the stage)."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {list(STAGES)}, got {stage!r}")
    source, data = _read(PROMPTS_RELATIVE, prompts_path)
    template = data.decode("utf-8")
    blocks = _blocks(template)
    missing = [name for name in ("shared", stage, "taxonomy") if name not in blocks]
    if missing:
        raise ValueError(f"{_shown(source)} lacks the fenced text block of section(s) {missing}")
    taxonomy = taxonomy_info(taxonomy_path)
    match = _VERSION.search(template)
    version = match.group(1) if match else None
    text = "\n\n".join([blocks["shared"], blocks[stage], blocks["taxonomy"]])
    for key, value in (("{{PROMPT_VERSION}}", version or "unversioned"), ("{{STAGE}}", stage[-1]),
                       ("{{TAXONOMY_SHA256}}", taxonomy["sha256"])):
        text = text.replace(key, value)
    text = text.replace("{{TAXONOMY}}", taxonomy["text"].strip("\n"))   # last: the taxonomy is verbatim
    leftover = re.findall(r"\{\{[A-Z_0-9]+\}\}", text.replace(taxonomy["text"].strip("\n"), ""))
    if leftover:
        raise ValueError(f"unfilled prompt placeholders: {sorted(set(leftover))}")
    return {"stage": stage, "text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "version": version, "template_sha256": hashlib.sha256(data).hexdigest(),
            "taxonomy_sha256": taxonomy["sha256"], "taxonomy_version": taxonomy["version"],
            "prompts_path": _shown(source), "taxonomy_path": taxonomy["path"]}


def prompt_texts(*, prompts_path=None, taxonomy_path=None):
    """{stage: prompt_info(stage)} for the three stages."""
    return {stage: prompt_info(stage, prompts_path=prompts_path, taxonomy_path=taxonomy_path) for stage in STAGES}


def _shown(source):
    try:
        return str(Path(source).resolve().relative_to(private_material.REPO))
    except ValueError:
        return str(source)


# ---------------------------------------------------------------------------
# Checking helpers


class _Checker:
    """``soft``: a list that receives record checks (quotes verbatim, item turn/t, cited event ids, length caps,
    which are then truncated); without it they are hard problems like every shape problem."""

    def __init__(self, soft=None):
        self.problems, self.warnings = [], []
        self.soft_problems = soft

    def add(self, where, message):
        self.problems.append(f"{where}: {message}")

    def soft(self, where, message):
        if self.soft_problems is None:
            self.add(where, message)
        else:
            self.soft_problems.append(f"{where}: {message}")

    def warn(self, where, message):
        self.warnings.append(f"{where}: {message}")

    def keys(self, obj, required, where, optional=()):
        """Report missing required keys; drop (with a warning) keys outside the contract."""
        if not isinstance(obj, dict):
            self.add(where, "must be a JSON object")
            return False
        for key in required:
            if key not in obj:
                self.add(where, f"missing key {key!r}")
        for key in obj:
            if key not in required and key not in optional:
                self.warn(where, f"unexpected key {key!r} dropped")
        return True

    def string(self, value, where, *, allow_none=False, max_chars=None):
        if value is None and allow_none:
            return None
        if not isinstance(value, str) or not value.strip():
            self.add(where, "must be a non-empty string" + (" or null" if allow_none else ""))
            return None
        value = value.strip()
        if max_chars is not None and len(value) > max_chars:
            self.soft(where, f"has {len(value)} characters; at most {max_chars}")
            if self.soft_problems is not None:
                value = value[:max_chars]
        return value

    def enum(self, value, allowed, where, *, allow_none=False):
        if value is None and allow_none:
            return None
        if value not in allowed:
            self.add(where, f"must be one of {list(allowed)}" + (" or null" if allow_none else "")
                     + f", got {json.dumps(value, ensure_ascii=False)[:80]}")
            return None
        return value

    def boolean(self, value, where):
        if not isinstance(value, bool):
            self.add(where, "must be true or false")
            return None
        return value

    def integer(self, value, where, *, minimum=0):
        if isinstance(value, bool) or not isinstance(value, int):
            self.add(where, "must be an integer")
            return None
        if value < minimum:
            self.add(where, f"must be >= {minimum}")
            return None
        return value

    def number(self, value, where, *, allow_none=False):
        if value is None and allow_none:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            self.add(where, "must be a number" + (" or null" if allow_none else ""))
            return None
        return float(value)

    def str_list(self, value, where, *, max_chars=None):
        if not isinstance(value, list):
            self.add(where, "must be a list of non-empty strings")
            return []
        out = []
        for n, entry in enumerate(value):
            text = self.string(entry, f"{where}[{n}]", max_chars=max_chars)
            if text is not None:
                out.append(text)
        return out

    def quote(self, value, where, item, items):
        """A verbatim quote of at most QUOTE_MAX_CHARS; checked against ``items[item]`` when the
        caller passes the item texts."""
        quote = self.string(value, where, max_chars=QUOTE_MAX_CHARS)
        if quote is None or items is None or item is None:
            return quote
        if item not in items:
            self.soft(where.rsplit(".", 1)[0] + ".item", f"item {item} is not among the items shown")
        elif normalize_text(quote) not in normalize_text(items[item]):
            self.soft(where, f"is not a verbatim span of item {item}'s text (copy one contiguous exact span)")
        return quote

    def finish(self, value):
        if self.problems:
            raise SchemaError(self.problems)
        return value


def _load(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return judge_schema.extract_json(raw)
    except judge_schema.SchemaError as exc:
        raise SchemaError(exc.problems) from None


def _item_texts(items):
    """{index: text} from a mapping or from a list of timeline items (``{"i", "text"}``)."""
    if items is None:
        return None
    if isinstance(items, dict):
        return {int(k): (v.get("text") if isinstance(v, dict) else v) or "" for k, v in items.items()}
    return {int(item["i"]): item.get("text") or "" for item in items}


def _default_codes():
    return taxonomy_info()["thought_codes"]


# ---------------------------------------------------------------------------
# Stage 1


def _summary(c, value, truncated):
    """``state.summary``: over-length is truncated and recorded, not a problem (no paid repair)."""
    summary = c.string(value, "state.summary")
    if summary is not None and len(summary) > SUMMARY_MAX_CHARS:
        c.warn("state.summary", f"truncated from {len(summary)} to {SUMMARY_MAX_CHARS} characters")
        if truncated is not None:
            truncated.append({"field": "state.summary", "from_chars": len(summary), "to_chars": SUMMARY_MAX_CHARS})
        summary = summary[:SUMMARY_MAX_CHARS]
    return summary


def validate_stage1(raw, *, codes=None, items=None, warnings=None, soft=None, truncated=None, fix_item_fields=False):
    """Normalized Stage 1 output of one chunk, or ``SchemaError``.

    ``codes``: the allowed thought codes (default: the taxonomy's groups A, B, C, D, F, G, read
    from the private file). ``items``: the chunk's items (list of timeline items or {i: text});
    when given, every thought's item must be in the chunk, its quote verbatim, and its turn and t
    equal to the item's. ``warnings``: a list that receives non-fatal notes (dropped keys).

    ``state.summary`` over SUMMARY_MAX_CHARS is cut deterministically to its first SUMMARY_MAX_CHARS characters
    and is never a problem (owner decision, November 2, 2026: about 90 % of the control run's stage-1 repairs
    resent a ~57k-token prompt only for this); ``truncated``: a list that receives
    ``{"field": "state.summary", "from_chars": n, "to_chars": SUMMARY_MAX_CHARS}``.

    ``fix_item_fields``: when a thought's quote verifies in the item it cites, a miscopied ``turn`` or ``t`` is
    replaced by the item's (a warning, no problem, so no paid repair); without it (the default) the mismatch is a
    record check as before. Only the output changes, never a prompt."""
    obj = _load(raw)
    allowed = set(_default_codes() if codes is None else codes)
    texts = _item_texts(items)
    meta = {int(item["i"]): item for item in items} if isinstance(items, list) else {}
    c = _Checker(soft)
    out = {"thoughts": [], "state": None}
    if c.keys(obj, ("thoughts", "state"), "output"):
        thoughts = obj.get("thoughts")
        if not isinstance(thoughts, list):
            c.add("thoughts", "must be a list (empty when nothing is coded)")
            thoughts = []
        for n, raw_thought in enumerate(thoughts):
            where = f"thoughts[{n}]"
            if not c.keys(raw_thought, ("code", "turn", "item", "t", "quote", "certainty", "note"), where):
                continue
            code = raw_thought.get("code")
            if code not in allowed:
                c.add(f"{where}.code", f"{json.dumps(code, ensure_ascii=False)[:40]} is not a Stage 1 taxonomy code "
                      f"(allowed: {', '.join(sorted(allowed))})")
                code = None
            turn = c.integer(raw_thought.get("turn"), f"{where}.turn")
            item = c.integer(raw_thought.get("item"), f"{where}.item")
            t = c.number(raw_thought.get("t"), f"{where}.t")
            quote = c.quote(raw_thought.get("quote"), f"{where}.quote", item, texts)
            certainty = c.enum(raw_thought.get("certainty"), CERTAINTIES, f"{where}.certainty")
            note = c.string(raw_thought.get("note"), f"{where}.note", allow_none=True)
            source = meta.get(item)
            if source is not None:
                # The quote verifies in the cited item: the item is right, so a miscopied turn or t is taken from
                # the item (deterministic, recorded as a warning, no paid repair). Otherwise it stays a soft problem.
                verified = (fix_item_fields and quote is not None and texts is not None and item in texts
                            and normalize_text(quote) in normalize_text(texts[item]))
                if turn is not None and source.get("turn") is not None and turn != source["turn"]:
                    if verified:
                        c.warn(f"{where}.turn", f"set to item {item}'s turn {source['turn']} (was {turn})")
                        turn = source["turn"]
                    else:
                        c.soft(f"{where}.turn", f"item {item} is in turn {source['turn']}, not {turn}")
                if t is not None and source.get("t") is not None and abs(t - float(source["t"])) > 0.005:
                    if verified:
                        c.warn(f"{where}.t", f"set to item {item}'s t={source['t']} (was {t})")
                        t = float(source["t"])
                    else:
                        c.soft(f"{where}.t", f"item {item} is at t={source['t']}, not {t} (copy t from the header)")
            out["thoughts"].append({"code": code, "turn": turn, "item": item, "t": t, "quote": quote,
                                    "certainty": certainty, "note": note})
        state = obj.get("state")
        if c.keys(state, ("has_trick", "stance", "believes_judge", "open_questions", "summary"), "state"):
            out["state"] = {
                "has_trick": c.boolean(state.get("has_trick"), "state.has_trick"),
                "stance": c.enum(state.get("stance"), STANCES, "state.stance"),
                "believes_judge": c.enum(state.get("believes_judge"), BELIEVES_JUDGE, "state.believes_judge"),
                "open_questions": c.str_list(state.get("open_questions"), "state.open_questions"),
                "summary": _summary(c, state.get("summary"), truncated)}
            if out["state"]["stance"] not in (None, "none") and out["state"]["has_trick"] is False:
                c.add("state.has_trick", f"must be true when stance is {out['state']['stance']!r}")
    if warnings is not None:
        warnings.extend(c.warnings)
    return c.finish(out)


# ---------------------------------------------------------------------------
# Stage 2


def validate_stage2(raw, *, event_id=None, items=None, overseer_expected=None, warnings=None, soft=None):
    """Normalized Stage 2 output of one event, or ``SchemaError``.

    ``event_id``: the id of the event the request was about (must match). ``items``: the window's
    items (quotes verbatim). ``overseer_expected``: True when the event is overseer-authored or a P2
    revert (the ``overseer`` object is then required), False when it must be null, None to accept
    either."""
    obj = _load(raw)
    texts = _item_texts(items)
    c = _Checker(soft)
    keys = ("event_id", "registered", "change", "direction", "reason", "self_attributed", "overseer",
            "next_action", "quotes", "certainty")
    out = {}
    if c.keys(obj, keys, "output"):
        out["event_id"] = c.string(obj.get("event_id"), "event_id")
        if event_id is not None and out["event_id"] is not None and out["event_id"] != event_id:
            c.add("event_id", f"must be {event_id!r} exactly as given, got {out['event_id']!r}")
        out["registered"] = c.enum(obj.get("registered"), REGISTERED, "registered")
        out["change"] = c.enum(obj.get("change"), CHANGES, "change")
        out["direction"] = c.enum(obj.get("direction"), DIRECTIONS, "direction", allow_none=True)
        if out["registered"] == "no" and out["change"] not in (None, "none"):
            c.add("change", "must be 'none' when registered is 'no'")
        if out["change"] == "none" and out["direction"] is not None:
            c.add("direction", "must be null when change is 'none'")
        if out["change"] in ("belief", "behaviour") and obj.get("direction") is None:
            c.add("direction", f"is required when change is {out['change']!r}")
        out["reason"] = c.string(obj.get("reason"), "reason", allow_none=True)
        out["self_attributed"] = c.boolean(obj.get("self_attributed"), "self_attributed")
        if out["self_attributed"] and out["registered"] == "no":
            c.add("self_attributed", "cannot be true when registered is 'no'")
        out["overseer"] = _overseer(c, obj.get("overseer"), overseer_expected)
        out["next_action"] = c.string(obj.get("next_action"), "next_action")
        quotes = obj.get("quotes")
        out["quotes"] = []
        if not isinstance(quotes, list):
            c.add("quotes", "must be a list of {\"item\", \"quote\"} objects")
            quotes = []
        for n, entry in enumerate(quotes):
            where = f"quotes[{n}]"
            if not c.keys(entry, ("item", "quote"), where):
                continue
            item = c.integer(entry.get("item"), f"{where}.item")
            out["quotes"].append({"item": item, "quote": c.quote(entry.get("quote"), f"{where}.quote", item, texts)})
        if out["change"] in ("belief", "behaviour") and not quotes:
            c.add("quotes", "at least one quote is required when change is not 'none'")
        out["certainty"] = c.enum(obj.get("certainty"), CERTAINTIES, "certainty")
    if warnings is not None:
        warnings.extend(c.warnings)
    return c.finish(out)


def _overseer(c, value, expected):
    if value is None:
        if expected:
            c.add("overseer", "is required for an overseer-authored event or a revert (object with F1..F5)")
        return None
    if expected is False:
        c.add("overseer", "must be null: this event is neither overseer-authored nor a revert")
        return None
    if not c.keys(value, OVERSEER_KEYS, "overseer"):
        return None
    out = {key: c.boolean(value.get(key), f"overseer.{key}") for key in ("F1", "F2", "F4", "F5")}
    out["F3"] = c.enum(value.get("F3"), F3_VALUES, "overseer.F3", allow_none=True)
    if out["F1"] is False and out["F3"] is not None:
        c.add("overseer.F3", "must be null when F1 is false")
    if out["F1"] is False and out["F2"]:
        c.add("overseer.F2", "cannot be true when F1 is false")
    return {key: out[key] for key in OVERSEER_KEYS}


# ---------------------------------------------------------------------------
# Stage 3


def validate_stage3(raw, *, agent=None, event_ids=None, warnings=None, soft=None, structure=False):
    """Normalized Stage 3 output of one agent, or ``SchemaError``.

    ``agent``: the expected agent id. ``event_ids``: the ids of the agent's events (every cited
    cause must be one of them); a mapping ``{id: t}`` (minutes, first occurrence) is accepted too and,
    with ``structure``, also checks that no turning point cites an event after its own time.

    ``structure``: also run the record checks of the story's own structure (``_structure``; soft: named
    in the one repair, else kept as check problems; off by default, as the prompt's shape example is
    partial): phases contiguous (no gap) and of non-zero length (unless there is a single phase); each
    turning point at a phase boundary whose stances it names, the turning points chained (each ``from``
    is the previous ``to``, the first the first phase's stance); a faker whose last phase is ``stopped``
    has ``final.stopped`` true, and ``final.stopped`` is never true while the last phase is ``faking``.
    Only the checks change, never a prompt (unit hashes stay)."""
    obj = _load(raw)
    known = set(event_ids) if event_ids is not None else None
    times = ({str(k): float(v) for k, v in event_ids.items() if isinstance(v, (int, float))}
             if isinstance(event_ids, dict) else None)
    c = _Checker(soft)
    keys = ("agent", "phases", "turning_points", "story", "final", "consistency_flags")
    out = {}
    if c.keys(obj, keys, "output"):
        name = c.string(obj.get("agent"), "agent")
        if name is not None and not _AGENT_ID.match(name):
            c.add("agent", f"must look like agent-NNN, got {name!r}")
        if agent is not None and name is not None and name != agent:
            c.add("agent", f"must be {agent!r}, got {name!r}")
        out["agent"] = name
        out["phases"] = _phases(c, obj.get("phases"))
        out["turning_points"] = _turning_points(c, obj.get("turning_points"), known)
        out["story"] = c.string(obj.get("story"), "story", max_chars=STORY_MAX_CHARS)
        out["final"] = _final(c, obj.get("final"), known)
        if structure:
            _structure(c, out["phases"], out["turning_points"], out["final"], times)
        flags = obj.get("consistency_flags")
        out["consistency_flags"] = c.str_list(flags, "consistency_flags") if flags is not None else \
            (c.add("consistency_flags", "must be a list ([] when none)") or [])
    if warnings is not None:
        warnings.extend(c.warnings)
    return c.finish(out)


def _phases(c, value):
    if not isinstance(value, list) or not value:
        c.add("phases", "must be a non-empty list of phases in time order")
        return []
    out, previous = [], None
    for n, phase in enumerate(value):
        where = f"phases[{n}]"
        if not c.keys(phase, ("from_t", "to_t", "stance", "label"), where):
            continue
        start = c.number(phase.get("from_t"), f"{where}.from_t")
        end = c.number(phase.get("to_t"), f"{where}.to_t")
        if start is not None and end is not None and end < start - TIME_TOLERANCE:
            c.add(where, f"to_t {end} is before from_t {start}")
        if previous is not None and start is not None and start < previous - TIME_TOLERANCE:
            c.add(where, f"overlaps or precedes the previous phase (from_t {start} < previous to_t {previous})")
        if end is not None:
            previous = end
        out.append({"from_t": start, "to_t": end, "stance": c.enum(phase.get("stance"), STANCES, f"{where}.stance"),
                    "label": c.string(phase.get("label"), f"{where}.label")})
    return out


def _event_list(c, value, where, known):
    ids = c.str_list(value, where) if value is not None else (c.add(where, "must be a list ([] when none)") or [])
    if known is not None:
        for event in ids:
            if event not in known:
                c.soft(where, f"{event!r} is not one of the agent's event ids")
    return ids


def _turning_points(c, value, known):
    if not isinstance(value, list):
        c.add("turning_points", "must be a list ([] when the stance never changed)")
        return []
    out, last = [], None
    for n, point in enumerate(value):
        where = f"turning_points[{n}]"
        if not c.keys(point, ("t", "from", "to", "cause_event_ids", "cause_note"), where):
            continue
        t = c.number(point.get("t"), f"{where}.t")
        if t is not None and last is not None and t < last - TIME_TOLERANCE:
            c.add(f"{where}.t", "turning points must be in time order")
        last = t if t is not None else last
        start = c.enum(point.get("from"), STANCES, f"{where}.from")
        end = c.enum(point.get("to"), STANCES, f"{where}.to")
        if start is not None and start == end:
            c.add(where, "from and to must differ (a turning point changes the stance)")
        out.append({"t": t, "from": start, "to": end,
                    "cause_event_ids": _event_list(c, point.get("cause_event_ids"), f"{where}.cause_event_ids", known),
                    "cause_note": c.string(point.get("cause_note"), f"{where}.cause_note")})
    return out


def _final(c, value, known):
    keys = ("considered", "faked", "credited", "first_fake_t", "source", "stopped", "stop_cause_event_ids",
            "aware_cheating")
    if not c.keys(value, keys, "final"):
        return None
    out = {key: c.boolean(value.get(key), f"final.{key}") for key in ("considered", "faked", "credited", "stopped")}
    out["first_fake_t"] = c.number(value.get("first_fake_t"), "final.first_fake_t", allow_none=True)
    out["source"] = c.enum(value.get("source"), SOURCES, "final.source")
    out["stop_cause_event_ids"] = _event_list(c, value.get("stop_cause_event_ids"), "final.stop_cause_event_ids", known)
    out["aware_cheating"] = c.enum(value.get("aware_cheating"), AWARE, "final.aware_cheating")
    if out["faked"] is True:
        if value.get("first_fake_t") is None:
            c.add("final.first_fake_t", "is required when faked is true")
        if out["source"] == "none":
            c.add("final.source", "cannot be 'none' when faked is true")
        if out["considered"] is False:
            c.add("final.considered", "must be true when faked is true")
    if out["faked"] is False:
        if value.get("first_fake_t") is not None:
            c.add("final.first_fake_t", "must be null when faked is false")
        if out["source"] not in (None, "none", "unknown"):
            c.add("final.source", "must be 'none' (or 'unknown') when faked is false")
        for key in ("credited", "stopped"):
            if out[key]:
                c.add(f"final.{key}", "cannot be true when faked is false")
    if out["stopped"] is False and out["stop_cause_event_ids"]:
        c.add("final.stop_cause_event_ids", "must be [] when stopped is false")
    return {key: out[key] for key in keys}


def _structure(c, phases, points, final, times):
    """Soft record checks between phases, turning points and ``final`` (see ``validate_stage3``). Entries that
    already failed a shape check (None fields) are skipped; nothing here changes the output."""
    tol = BOUNDARY_TOLERANCE
    ok = [p for p in phases if p.get("from_t") is not None and p.get("to_t") is not None and p.get("stance")]
    if len(ok) == len(phases):
        for n, phase in enumerate(phases):
            if len(phases) > 1 and abs(phase["to_t"] - phase["from_t"]) <= TIME_TOLERANCE:
                c.soft(f"phases[{n}]", f"has zero length (from_t = to_t = {phase['from_t']}); give it its real span "
                       "or drop it (a turning point then goes straight to the next stance)")
            if n and phase["from_t"] > phases[n - 1]["to_t"] + tol:
                c.soft(f"phases[{n}]", f"leaves a gap after the previous phase (previous to_t {phases[n - 1]['to_t']}, "
                       f"from_t {phase['from_t']}); phases must be consecutive")
    bounds = [(phases[n - 1]["to_t"], phases[n]["from_t"], phases[n - 1]["stance"], phases[n]["stance"])
              for n in range(1, len(phases)) if len(ok) == len(phases)]
    previous = phases[0]["stance"] if phases and len(ok) == len(phases) else None
    for n, point in enumerate(points):
        where = f"turning_points[{n}]"
        if point.get("t") is None or point.get("from") is None or point.get("to") is None:
            previous = None
            continue
        if previous is not None and point["from"] != previous:
            c.soft(f"{where}.from", f"is {point['from']!r}, but the stance before it is {previous!r} "
                   + ("(the first phase's stance)" if n == 0 else "(the previous turning point's 'to')"))
        previous = point["to"]
        if len(ok) == len(phases) and phases:
            near = [b for b in bounds if abs(b[1] - point["t"]) <= tol or abs(b[0] - point["t"]) <= tol]
            if not near:
                c.soft(f"{where}.t", f"{point['t']} is not a phase boundary; each turning point starts the phase of "
                       "its 'to' stance")
            elif not any(b[2] == point["from"] and b[3] == point["to"] for b in near):
                c.soft(where, f"{point['from']} -> {point['to']} at t={point['t']}, but the phases change "
                       + ", ".join(f"{b[2]} -> {b[3]}" for b in near) + " there")
        if times is not None:
            for event in point.get("cause_event_ids") or []:
                at = times.get(event)
                if at is not None and at > point["t"] + tol:
                    c.soft(f"{where}.cause_event_ids", f"{event!r} is at t={at}, after the turning point "
                           f"(t={point['t']}); a cause must come before the change")
    if final and final.get("faked") is True and len(ok) == len(phases) and phases:
        last = phases[-1]["stance"]
        if final.get("stopped") is False and last == "stopped":
            c.soft("final.stopped", "is false, but the last phase is 'stopped'; make them agree")
        if final.get("stopped") is True and last == "faking":
            c.soft("final.stopped", "is true, but the last phase is 'faking'; make them agree")
