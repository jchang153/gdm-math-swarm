"""Chronological screening windows over an agent's items (I-WINDOW; researcher-only).

The Haiku prefilter reads each agent's full history in windows of about ``target_tokens``
(rubric Section 11: about 24,000 tokens, the default). Windows are contiguous runs of
items in the given (chronological) order and never split an item, except an item whose
own estimate exceeds twice the
target: it is cut into numbered fragments ``<item id>#f1``, ``#f2``, ... of at most
``target_tokens`` each (preferring line breaks), and each fragment is packed like an item.
Token counts use ``history.estimate_tokens`` (characters / 4, rounded up) of item text
only; per-item headers added by prompt rendering are not counted.

Window dict: ``{"id": "A07.w0001", "item_ids": [...], "tokens": int, "fragments": {...}}``,
where ``fragments`` maps each fragment id in the window to its ``[start, end)`` character
range in the item text (empty when the window has no fragment).
"""
from __future__ import annotations

import re

from .history import agent_code, estimate_tokens

FRAGMENT = re.compile(r"^(?P<item>.+)#f(?P<number>\d+)$")


def split_text(text, target_tokens):
    """``[start, end)`` ranges covering ``text``, each at most ``target_tokens`` (chars/4).

    A cut falls on the last line break in the second half of a chunk when there is one,
    else at the chunk boundary, so every range is non-empty and progress is guaranteed.
    """
    limit = max(1, int(target_tokens) * 4)
    ranges, start = [], 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            cut = text.rfind("\n", start + limit // 2, end)
            if cut >= 0:
                end = cut + 1
        ranges.append([start, end])
        start = end
    return ranges or [[0, 0]]


def _code_for(items):
    for item in items:
        try:
            return agent_code(item.get("agent_id"))
        except ValueError:
            continue
    return "RUN"


def make_windows(items, target_tokens=24000):
    """Contiguous chronological windows of about ``target_tokens`` (see module docstring)."""
    if target_tokens < 1:
        raise ValueError("target_tokens must be positive")
    code = _code_for(items)
    windows, current, used, fragments = [], [], 0, {}

    def flush():
        nonlocal current, used, fragments
        if current:
            windows.append({"id": f"{code}.w{len(windows) + 1:04d}", "item_ids": current, "tokens": used,
                            "fragments": fragments})
        current, used, fragments = [], 0, {}

    for item in items:
        text = item.get("text") or ""
        tokens = estimate_tokens(text)
        if tokens > 2 * target_tokens:
            pieces = [(f"{item['id']}#f{n}", estimate_tokens(text[a:b]), [a, b])
                      for n, (a, b) in enumerate(split_text(text, target_tokens), 1)]
        else:
            pieces = [(item["id"], tokens, None)]
        for piece_id, piece_tokens, span in pieces:
            if current and used + piece_tokens > target_tokens:
                flush()
            current.append(piece_id)
            used += piece_tokens
            if span is not None:
                fragments[piece_id] = span
    flush()
    return windows


def window_pieces(window, items_by_id):
    """``[(piece_id, item, text)]`` for a window: whole items, or the fragment's slice.

    ``items_by_id`` maps item ids to items (for example ``{i["id"]: i for i in items}``).
    """
    pieces = []
    for piece_id in window["item_ids"]:
        span = window.get("fragments", {}).get(piece_id)
        if span is None:
            item = items_by_id[piece_id]
            pieces.append((piece_id, item, item["text"]))
        else:
            item = items_by_id[FRAGMENT.match(piece_id).group("item")]
            pieces.append((piece_id, item, item["text"][span[0]:span[1]]))
    return pieces
