"""Host verification of the incident report's quotes (no model).

The writer may quote records in two ways only:

* an excerpt block::

      :::excerpt A07.t0123.r
      verbatim text of that item, optionally in pieces separated by [...]
      :::

* an inline quote immediately followed by its citation: ``"verbatim words" [A07.t0123.r]``.

Every piece of every quote must be a verbatim substring of the cited item's full text after
normalizing whitespace only (each run of whitespace becomes one space; leading and trailing
whitespace is dropped). A quote whose item does not exist or that is not found is marked
unverified (the renderer shows it so and evidence.json records why); nothing is silently
dropped. Bare citations ``[A07.t0123.r]`` are checked for existence.
"""
from __future__ import annotations

import re

from .digest import ITEM_ID

BLOCK_RE = re.compile(r"^:::excerpt[ \t]+(" + ITEM_ID + r")[ \t]*\n(.*?)\n:::[ \t]*$", re.M | re.S)
INLINE_RE = re.compile(r"[\"“]([^\"“”\n]{8,600})[\"”]\s*\[(" + ITEM_ID + r")\]")
CITE_RE = re.compile(r"\[(" + ITEM_ID + r"(?:\s*[,;]\s*" + ITEM_ID + r")*)\]")
ONE_ID_RE = re.compile(ITEM_ID)
ELISION_RE = re.compile(r"\s*\[(?:\.\.\.|…)\]\s*")
MAX_EXCERPT_CHARS = 1500
MIN_PIECE_CHARS = 3


def normalize(text):
    """Whitespace normalization only."""
    return re.sub(r"\s+", " ", text or "").strip()


def pieces(quote):
    return [p for p in (normalize(x) for x in ELISION_RE.split(quote or "")) if p]


def check_quote(quote, item):
    """(verified, reason) for one quote against one item (or None)."""
    if item is None:
        return False, "cited item not found in the run's records"
    parts = pieces(quote)
    if not parts:
        return False, "empty quote"
    haystack = normalize(item.get("text"))
    position = 0
    for part in parts:
        if len(part) < MIN_PIECE_CHARS:
            return False, f"piece too short to verify: {part!r}"
        found = haystack.find(part, position)
        if found < 0:
            found = haystack.find(part)
            if found < 0:
                return False, f"not found verbatim in {item['id']}: {part[:80]!r}"
        position = found + len(part)
    return True, None


def verify_markdown(markdown, lookup):
    """Check every excerpt block, inline quote and citation of ``markdown``.

    ``lookup(item_id) -> item or None``. Returns ``{"quotes": [...], "citations": [...],
    "summary": {...}}``; each quote record has ``n`` (its order), ``form`` (``block`` or
    ``inline``), ``item``, ``quote``, ``verified``, ``reason`` and, for blocks, ``span`` (the
    character offsets of the block in ``markdown``)."""
    quotes = []
    for match in BLOCK_RE.finditer(markdown):
        item_id, body = match.group(1), match.group(2)
        ok, reason = check_quote(body, lookup(item_id))
        record = {"form": "block", "item": item_id, "quote": body.strip("\n"), "verified": ok, "reason": reason,
                  "span": [match.start(), match.end()]}
        if ok and len(normalize(body)) > MAX_EXCERPT_CHARS:
            record["warning"] = f"excerpt longer than {MAX_EXCERPT_CHARS} characters"
        quotes.append(record)
    blocks = [q["span"] for q in quotes]

    def in_block(offset):
        return any(a <= offset < b for a, b in blocks)

    for match in INLINE_RE.finditer(markdown):
        if in_block(match.start()):
            continue
        ok, reason = check_quote(match.group(1), lookup(match.group(2)))
        quotes.append({"form": "inline", "item": match.group(2), "quote": match.group(1), "verified": ok,
                       "reason": reason, "span": [match.start(), match.end()]})
    quotes.sort(key=lambda q: q["span"][0])
    for number, record in enumerate(quotes, 1):
        record["n"] = number
    citations, seen = [], set()
    for match in CITE_RE.finditer(markdown):
        if in_block(match.start()):
            continue
        for found in ONE_ID_RE.finditer(match.group(1)):
            if found.group(0) in seen:
                continue
            seen.add(found.group(0))
            citations.append({"item": found.group(0), "exists": lookup(found.group(0)) is not None})
    summary = {"quotes": len(quotes), "verified": sum(q["verified"] for q in quotes),
               "unverified": sum(not q["verified"] for q in quotes),
               "blocks": sum(q["form"] == "block" for q in quotes),
               "inline": sum(q["form"] == "inline" for q in quotes),
               "citations": len(citations), "unknown_citations": sum(not c["exists"] for c in citations)}
    return {"quotes": quotes, "citations": citations, "summary": summary}


REQUIRED_SECTIONS = ("Summary", "Case studies", "What didn't happen", "How sure we are", "What to try next",
                     "The agents, one by one")


def structure_problems(markdown):
    """Missing or misnamed required sections and case-study count problems (host check)."""
    problems = []
    headings = [h.strip() for h in re.findall(r"^##[ \t]+(.+?)[ \t]*$", markdown, re.M)]
    normalized = [h.replace("’", "'").lower() for h in headings]
    for section in REQUIRED_SECTIONS:
        if section.lower() not in normalized:
            problems.append(f"missing section '## {section}'")
    if not re.search(r"^#[ \t]+\S", markdown, re.M):
        problems.append("missing the '# ' title line")
    cases = 0
    if "case studies" in normalized:
        start = markdown.lower().find("## case studies")
        rest = markdown[start + 3:]
        end = re.search(r"^##[ \t]", rest, re.M)
        section = rest[:end.start()] if end else rest
        cases = len(re.findall(r"^###[ \t]+\S", section, re.M))
        if not 1 <= cases <= 5:
            problems.append(f"'## Case studies' has {cases} storylines (expected 2 to 5 '### ' subsections)")
    return problems


def _normalized_with_map(text):
    """(whitespace-normalized text, offsets into ``text`` of each normalized character)."""
    out, where, pending = [], [], False
    for index, char in enumerate(text or ""):
        if char.isspace():
            pending = bool(out)
            continue
        if pending:
            out.append(" ")
            where.append(index - 1)
            pending = False
        out.append(char)
        where.append(index)
    return "".join(out), where


def locate_span(text, start, end=None):
    """Offsets (lo, hi) in ``text`` of the passage that begins with the anchor ``start`` and ends with
    the anchor ``end`` (both verbatim after whitespace normalization; ``end`` may be omitted for a
    passage that is the anchor itself), or None when either anchor is not found."""
    norm, where = _normalized_with_map(text)
    start_n, end_n = normalize(start), normalize(end or "")
    if len(start_n) < MIN_PIECE_CHARS:
        return None
    lo = norm.find(start_n)
    if lo < 0:
        return None
    hi = lo + len(start_n)
    if end_n:
        found = norm.find(end_n, lo)
        if found < 0:
            return None
        hi = max(hi, found + len(end_n))
    return where[lo], where[hi - 1] + 1
