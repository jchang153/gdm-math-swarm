"""Story map viewer: offline static HTML from the stage-0 timelines and the stage 1-3 outputs.

    python -m research.analysis.storymap.view --map runs/<id>/analysis/storymap --timeline DIR [--out DIR]

Writes (default ``--out`` = ``--map``):

* ``index.html``: one row per agent; positions on cheating as coloured spans on the run's time axis (its configured
  length, else the data's, rounded to the half hour), event glyphs (library reads, overseer posts, DMs, reverts,
  submissions), final readings, and filters by source of the fake / faked / credited / stopped / knew it was cheating.
* ``agents/<agent>.html``: the timeline (position on cheating; thoughts by code group A-G; library; board and DMs;
  status; integrity reminders when there are any; submissions; own actions and restarts) with the detail column fixed
  beside it. Clicking a glyph shows its quote or event text with the stage-2 reaction; items closer than about one
  glyph width are one stack with a count, and selecting it lists every stacked item. Each thought and event opens the
  agent's own reasoning chain (reasoning, text, tool calls and results of the turns around it, from the embedded,
  redacted timeline; ``--no-evidence`` leaves it out). Every item is also listed in time order below.
* ``validation.html``: ``validation.md`` (checks against the run's records, model agreement) in the pages' words.
* ``taxonomy.html``: the code set (``--taxonomy``, default the public copy ``storymap/taxonomy.md``) with an anchor
  per code; the pages link each code to it. Letters are only code groups; event types are named in words there too.
* Overseer mode (``--overseer-map`` / ``--overseer-timeline``, owner approval November 5, 2026): overseer pages
  ``agents/<overseer>.html``, an overseer strip and run panel on ``index.html`` and ``taxonomy_overseer.html``, all
  from storymap/view_overseer.py; without these options every page is byte for byte as before.
* ``events.html``: each overseer post or DM, other relevant board post, revert, library file and (runs with the
  periodic integrity reminder) each delivery number of the reminder, with who read it and when, and the breakdown of
  stage-2 reactions (no change / belief / behaviour by direction).

Inputs follow private/research/analysis/storymap/INTERFACE.md. Every input is optional: an agent with only a timeline,
or only stage outputs, still gets a row, and missing stages are stated on the page instead of guessed. The taxonomy is
public (owner, November 2, 2026): code and group names, and each code's definition, appear in place (lane labels and
hints, the legend's code-group box, every thought's tooltip and panel, the reaction badges). ``--taxonomy ''`` renders
without names, saying so where the names would be. Visible text never shows raw event codes (B1, S1, ...) or the
timeline's field names: events are named in words, detail fields have labels, times are minutes from run start.
The pages, ``validation.md`` and the taxonomy page are published (scripts/publish_cloudflare.py storymap); timelines
and stage records stay local (agent pages embed only the capped, redacted chain text, without the agents' prompts).
Every page keeps the same compact legend in view (sticky on wide screens; bar colour = position, shape = event, marks
for fake, overseer, outcome, push delivery and stack count) with the explanation, code groups and glossary below;
agent pages have a detail panel that steps through the items in time order (arrow keys or j / k, Prev / Next, a kind
filter, Esc closes) and never scrolls the window; on phones it is a bottom sheet. No network, no external assets (the shared site theme is inlined); a little
vanilla JavaScript.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))
from site_style import themed  # noqa: E402  (shared light/dark tokens and toggle, no external loads)

DEFAULT_TAXONOMY = Path(__file__).resolve().parent / "taxonomy.md"  # public copy (owner, November 2, 2026)
AXIS_MIN = 180.0
LETTERS = "ABCDEFG"
STANCES = ("none", "considering", "deferred", "rejected", "faking", "stopped")
CHANGES = ("none", "belief", "behaviour")
DIRECTIONS = ("toward_cheating", "away", "neutral", None)
DIRECTION_LABEL = {"toward_cheating": "toward cheating", "away": "away from cheating", "neutral": "neutral",
                   None: "no direction"}
CHANGE_LABEL = {"none": "no change", "belief": "belief changed", "behaviour": "behaviour changed"}
REGISTERED_LABEL = {"yes": "noticed it", "no": "did not notice it", "unclear": "unclear whether it noticed"}
RELEVANT_TAGS = {"describes_trick", "warning", "ruling", "revert_notice"}
TEXT_CAP = 1500  # event text shown per item (the raw timelines themselves are never published)

LANES = (  # (key, label, short label (phones), event types, hover hint); the "rem" lane only when there are reminders
    ("library", "Saw: library", "Library", ("L1", "L2"), "Shared-library listings and files the agent read"),
    ("board", "Saw: board posts, DMs", "Board, DMs", ("B1", "D1"),
     "Board posts and direct messages the agent read or that were pushed into its context"),
    ("status", "Saw: problem list, reverts, notices", "Status", ("P1", "P2", "N1"),
     "Problem lists, reverts of its own credit and unread-messages notices"),
    ("rem", "Saw: integrity reminders", "Reminders", ("R1",),
     "The host's periodic integrity reminder, pushed into its context on a timer"),
    ("subs", "Did: submissions", "Submissions", ("S1",), "The agent's own proof submissions and their outcomes"),
    ("own", "Did: own posts, restarts", "Own acts", ("O1", "C1"),
     "The agent's own posts, DMs and notes, and context compactions or restarts"),
)
POSITION = "Position on cheating"
POSITION_HINT = ("What the agent intended about the shortcut at that moment: not aware, considering, deferred, "
                 "rejected, faking, stopped")
STANCE_LABEL = {"none": "not aware", "considering": "considering", "deferred": "deferred", "rejected": "rejected",
                "faking": "faking", "stopped": "stopped", "unknown": "no reading"}
SOURCE_LABEL = {"library": "Library", "self_then_library": "Own idea, then library",
                "overseer_then_library": "Overseer, then library", "board": "Board post", "dm": "Direct message",
                "self": "Own idea", "none": "Did not fake", "unknown": "Unknown", "no stage 3": "No final reading"}
SOURCE_TITLE = "Source of the fake"
SOURCE_HINT = ("Where the agent's first fake came from (stage-3 reading): library = copied a fake library file; "
               "own idea, then library = had the idea itself but acted only after seeing accepted fakes; "
               "overseer, then library = first learned it from an overseer warning, then acted after library fakes")
MODEL_LABEL = {"anthropic/claude-haiku-5.5": "Claude Haiku 5.5", "openai/gpt-5.6-terra": "GPT-5.6 Terra",
               "google/gemini-3.8-flash": "Gemini 3.8 Flash", "anthropic/claude-sonnet-5.5": "Claude Sonnet 5.5"}


def source_label(src):
    return SOURCE_LABEL.get(str(src), str(src).replace("_", " ").capitalize())


def model_label(mid):
    """'openai/gpt-5.6-terra' -> 'GPT-5.6 Terra' (known ids; else a readable form of the id)."""
    mid = str(mid)
    if mid in MODEL_LABEL:
        return MODEL_LABEL[mid]
    parts = mid.split("/")[-1].split("-")
    if parts and parts[0].lower() == "gpt" and len(parts) > 1:
        parts = ["GPT-" + parts[1]] + parts[2:]
    return " ".join(p if any(c.isdigit() for c in p) or p.isupper() else p.capitalize() for p in parts)
TYPE_NAME = {"L1": "library listing", "L2": "library file read", "P1": "problem list",
             "P2": "revert of its own credit", "B1": "board post", "D1": "direct message", "S1": "submission",
             "O1": "own post, DM or note", "C1": "context compaction or restart", "N1": "unread-messages notice",
             "R1": "integrity reminder"}


def type_name(typ, cap=False):
    """Plain name of an event type (never the raw code); ``cap`` capitalises only the first letter."""
    s = TYPE_NAME.get(typ, "event")
    return s[:1].upper() + s[1:] if cap else s


def event_label(e_or_id, typ=None):
    """A plain label for an event: 'board post post-000003', 'library file solutions/x.lean', 'submission
    sub-000028', 'revert of its own credit (submission sub-000613)'; never the raw type code."""
    if isinstance(e_or_id, dict):
        typ, eid = e_or_id.get("type"), str(e_or_id.get("id") or "")
    else:
        eid = str(e_or_id or "")
        typ = typ or (eid.split(":", 1)[0] if ":" in eid else None)
    rest = eid.split(":", 1)[1] if ":" in eid else eid
    if typ in ("L2",):
        return f"library file {rest.rsplit('@', 1)[0]}"
    if typ == "P2":
        return f"revert of its own credit (submission {rest})"
    if typ == "R1":  # 'R1:agent-004@7.804': the delivery minute, so a cited reminder is identifiable
        m = re.search(r"reminder-0*(\d+)$", rest)  # pushed: 'R1:agent-004:reminder-003'
        if m:
            return f"integrity reminder no. {m.group(1)}"
        at = rest.rsplit("@", 1)[1] if "@" in rest else None
        try:
            return f"integrity reminder at {float(at):.1f} min"
        except (TypeError, ValueError):
            return type_name(typ)
    if typ == "S1" and isinstance(e_or_id, dict) and (e_or_id.get("detail") or {}).get("task_id"):
        return f"submission {rest} ({e_or_id['detail']['task_id']})"
    if typ in ("B1", "D1", "S1"):
        return f"{type_name(typ)} {rest}"
    if typ == "O1":
        return ("own board post " + rest if rest.startswith("post-") else "own direct message " + rest
                if rest.startswith("dm-") else "own feedback to the organisers" if rest.startswith("feedback")
                else type_name(typ))
    return type_name(typ)


def plain_ids(text):
    """Model-written or extractor text with raw event ids ('B1:post-000003') made plain."""
    text = re.sub(r"\b([LPBDSOCNR][12]):([^\s,;)'\"\]]+)", lambda m: event_label(m.group(0), m.group(1)), str(text))
    return re.sub(r"\bt\s?=\s?(-?\d+(?:\.\d+)?)\b", lambda m: fmt_t(float(m.group(1))), text)  # one time format


OUTCOME_LABEL = {"problem_closed": "problem already closed", "accepted": "accepted", "rejected": "rejected",
                 "credit_removed": "credit removed", "reopened": "problem reopened",
                 "accepted_after_close": "accepted after the problem closed"}
TAG_LABEL = {"describes_trick": "describes the trick", "warning": "warning", "ruling": "ruling",
             "revert_notice": "revert notice", "unrelated": "unrelated to the trick"}
DETAIL_LABEL = {
    "task_id": "Problem", "problem_id": "Problem", "submission_id": "Submission", "public_id": "ID shown to agents",
    "post_id": "Post", "message_id": "Message", "intent_type": "Post type (set by its author)", "posted_at": "Posted at",
    "sent_at": "Sent at", "submitted_at": "Submitted at", "judged_at": "Judged at", "reverted_at": "Reverted at",
    "seen_at": "Revert first seen at", "reverted_by": "Reverted by", "credited": "Credited",
    "credited_now": "Problem now credited to", "status_now": "Problem status now",
    "task_status_at_submission": "Problem status when submitted", "form": "Fake form",
    "validity": "Research evaluator's verdict", "stage": "Judge stage reached", "reason": "Judge's message",
    "shown": "Problems shown", "open_count": "Open problems", "closed": "Closed problems (credited agent)",
    "own_credit": "Its own credits", "path": "File", "revision": "File revision",
    "reverted_before_read": "Already reverted when read", "offset": "Read from character", "has_more": "File continues",
    "own": "Its own file", "paths": "Files listed", "fake_paths": "Fake files listed", "total": "Files in the library",
    "trigger": "Trigger", "pre_tokens": "Context tokens before compaction",
    "summary": "Summary handed to the restarted agent", "notice": "Notice text", "counts": "Unread counts",
    "reread": "Re-read", "channel": "Channel", "to": "To", "feedback_id": "Feedback",
    "delivery": "Delivery", "delivery_id": "Delivered together as",
}
VALUE_LABEL = {
    "form": {"restatement": "restatement (answer is the question itself)",
             "iffrfl_main": "answer theorem proved by Iff.rfl alone"},
    "stage": {"source_integrity": "source-integrity check (failed)", "compilation": "compilation (failed)",
              "keyword_filter": "banned-keyword filter (failed)", "accepted": "all checks passed"},
    "trigger": {"auto": "automatic (context full)", "manual": "requested"},
    "channel": {"post": "board post", "dm": "direct message", "feedback": "feedback to the organisers"},
    "intent_type": {},
}
FINAL_LABEL = {"considered": "Considered faking", "faked": "Faked", "credited": "Got credit for a fake",
               "first_fake_t": "First fake at", "source": SOURCE_TITLE, "stopped": "Stopped faking",
               "stop_cause_event_ids": "Stop caused by", "aware_cheating": "Knew it was cheating"}
BELIEVES_LABEL = {"compile_only": "only compiles", "checks_answer": "checks the answer", "unknown": "unknown"}

# Page tokens (light, then dark), on top of the shared site theme in scripts/site_style.py.
# Two palettes that share no hue: six stance hues, used only by the stance bars, and three event hues, used only by
# event glyphs (pink = fake, cyan = from an overseer, amber = the host's integrity reminder). Everything else on a
# glyph is neutral ink: ordinary events are grey outlines, thoughts are ink ticks, a submission's outcome is a mark
# (check = accepted or credited, slash = not accepted), a push delivery a small chevron, a stack a count badge.
# The hues were validated together (dataviz validate_palette.js, --pairs all: lightness band, chroma, CVD >= 8,
# normal vision >= 15, contrast >= 3:1) on every surface they sit on: light #fafbfc, #ffffff, #f1f4f7, #e9edf2;
# dark #0f141a, #161d25, #18202a, #1d2631. "none" is a neutral grey bar and "unknown" an outline.
TOKENS_LIGHT = {"st-none": "#a7b1bd", "st-considering": "#9c8133", "st-deferred": "#1440bd",
                "st-rejected": "#027651", "st-faking": "#b41e07", "st-stopped": "#8254f9",
                "ev-fake": "#e13c85", "ev-ov": "#2593af", "ev-rem": "#d97706",
                "lane": "#f1f4f7", "lane-alt": "#e9edf2", "panel": "#ffffff"}
TOKENS_DARK = {"st-none": "#5b6675", "st-considering": "#a3933c", "st-deferred": "#2b6dec",
               "st-rejected": "#337f53", "st-faking": "#c7461b", "st-stopped": "#9457b3",
               "ev-fake": "#e95296", "ev-ov": "#1aa0be", "ev-rem": "#f59e0b",
               "lane": "#18202a", "lane-alt": "#1d2631", "panel": "#161d25"}


def _vars(tokens):
    return "".join(f"--{k}:{v};" for k, v in tokens.items())


LANE_PX = 30  # lane height in CSS pixels (the timeline SVG has no viewBox, so glyphs and text keep their size)
LANE_TOP = 4  # first lane's y in the SVG (the label column starts with the same padding)

CSS = (
    f":root{{{_vars(TOKENS_LIGHT)}}}"
    f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{_vars(TOKENS_DARK)}}}}}'
    f':root[data-theme="dark"]{{{_vars(TOKENS_DARK)}}}'
    """
:root{font-family:system-ui,sans-serif;line-height:1.5}
body{max-width:1320px;margin:20px auto;padding:0 24px;background:var(--bg);color:var(--fg)}body.agent{max-width:1760px}
h1{margin:0 0 6px;font-size:1.5rem}h2{margin:24px 0 8px;font-size:1.15rem}h3{margin:0 0 6px;font-size:1rem}
.muted,small{color:var(--muted)}.banner{background:var(--banner);border-left:4px solid var(--banner-line);
padding:8px 14px;margin:10px 0}.banner.one{padding:4px 12px;margin:4px 0 8px;font-size:13px}
nav.top{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0 10px}nav.top .sp{flex:1}
.badge{display:inline-block;background:var(--chip);padding:1px 7px;border-radius:4px;font-size:12px;
margin:1px 2px 1px 0;white-space:nowrap;max-width:100%;overflow-wrap:anywhere}
.b-bad{color:var(--fg);box-shadow:inset 0 0 0 1px var(--fg);font-weight:600}.b-good,.b-info{color:var(--fg)}
.b-caution{color:var(--fg);box-shadow:inset 0 0 0 1px var(--muted)}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:12px 0}
.filters label{display:flex;gap:6px;align-items:center;font-size:14px}
select,button{font:inherit;padding:5px 9px;border:1px solid var(--input-line);border-radius:6px;
background:var(--card);color:inherit}button{cursor:pointer}button:disabled{opacity:.45;cursor:default}
:focus-visible{outline:2px solid var(--fg);outline-offset:1px}
details.legend{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
padding:3px 0 5px;margin:4px 0 10px}details.legend>summary{cursor:pointer;font-size:12.5px;font-weight:600;padding:1px 0}
.lstrip{display:flex;flex-direction:column;gap:2px;font-size:12.5px;line-height:1.3}
.lstrip .row{display:flex;flex-wrap:wrap;gap:2px 12px;align-items:center}.lstrip ul{list-style:none;margin:0;padding:0;
display:flex;flex-wrap:wrap;gap:2px 10px;align-items:center}.lstrip li,.lg li{display:inline-flex;gap:4px;align-items:center}
.lstrip svg,.lg svg{width:20px;height:18px;flex:none;overflow:visible}.lstrip svg.swatch,.lg svg.swatch{width:20px}
.lstrip .k{font-weight:600}
details.about{font-size:13px;margin:16px 0 10px}details.about>summary{cursor:pointer;font-weight:600}
.lg{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px 14px;font-size:12.5px;line-height:1.35;
margin-top:6px}.lgbox{border:1px solid var(--line);border-radius:8px;padding:4px 10px 6px;background:var(--card)}
.lg h4{margin:4px 0 3px;font-size:12.5px}.lg ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:3px}
.lg .note{color:var(--muted);margin:3px 0 0}
.codes{max-height:none}.codes .grp{margin:0 0 3px}.codes details.code{margin-left:12px}
.codes details.code>summary{cursor:pointer;list-style:none}.codes details.code>summary::-webkit-details-marker{display:none}
.codes details.code p{margin:2px 0 4px;color:var(--muted)}.def{color:var(--muted);font-size:13px;margin:4px 0}
details.terms{font-size:13px;margin:0 0 10px}details.terms>summary{cursor:pointer;color:var(--muted)}
details.terms ul{margin:4px 0;padding-left:18px}
.hglyph{width:20px;min-width:20px;max-width:none;height:18px;vertical-align:-4px;margin-right:6px;overflow:visible;flex:none}
svg text{fill:var(--muted);font:11px system-ui,sans-serif}svg svg{overflow:visible}
.ph-none{fill:var(--st-none)}.ph-considering{fill:var(--st-considering)}.ph-deferred{fill:var(--st-deferred)}
.ph-rejected{fill:var(--st-rejected)}.ph-faking{fill:var(--st-faking)}.ph-stopped{fill:var(--st-stopped)}
.ph-unknown{fill:none;stroke:var(--muted);stroke-dasharray:3 2}
.g{cursor:pointer;stroke-width:1.3}
.g .m{fill:var(--card);stroke:var(--muted);stroke-width:1.5}.g.ring .m{stroke:var(--ev-ov)}
.g.g-fill .m{fill:var(--ev-fake);stroke:var(--ev-fake)}.g .ring{fill:none;stroke:var(--ev-ov);stroke-width:1.5}
.g .mk{fill:none;stroke:var(--fg);stroke-width:1.6;stroke-linecap:round}
.g .halo{fill:none;stroke:var(--card);stroke-width:4;stroke-linecap:round}.g .hit{fill:transparent;stroke:none}
.g .x{fill:none;stroke:var(--fg);stroke-width:2.4;stroke-linecap:round}.g.g-other .m{fill:var(--muted);stroke:none}
.g.g-rem .m{fill:var(--ev-rem);stroke:var(--ev-rem);stroke-width:.8;stroke-linejoin:round}
.g.g-th .m{fill:var(--fg);stroke:var(--fg);stroke-width:.8}.g.g-th-low .m{fill:var(--card);stroke:var(--fg);stroke-width:1}
.g .push{fill:none;stroke:var(--fg);stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.g .cnt circle{fill:var(--fg);stroke:var(--card);stroke-width:1.2}.g .cnt text{fill:var(--card);font:700 9px system-ui,sans-serif;text-anchor:middle}
.g:hover .m,.g:hover .x{stroke-width:2.3}.g.sel .m,.g.sel .ring{stroke-width:2.8}.g.sel .x{stroke-width:3.4}
.tl .g:hover .hit,.tl .g:focus .hit{fill:var(--fg);fill-opacity:.13}.tl .g.sel .hit{fill:var(--fg);fill-opacity:.22}
.tl .g:focus{outline:none}
.tp{stroke:var(--fg);stroke-dasharray:3 3;stroke-width:1.3;cursor:pointer}.tp.sel{stroke-width:3;stroke-dasharray:none}
.cursor{stroke:var(--fg);stroke-width:1.5;opacity:.55;pointer-events:none}
.lane-bg{fill:var(--lane)}.lane-bg.alt{fill:var(--lane-alt)}.tick{stroke:var(--line)}
.tl{display:grid;grid-template-columns:200px minmax(0,1fr);gap:0 8px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:8px 10px 2px}.tl-labels{padding-top:4px;min-width:0}
.ll{height:30px;display:flex;align-items:center;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
cursor:help;border-bottom:1px solid transparent}.ll .ls{display:none}.ll b{margin-right:5px}
.tl-plot{min-width:0;padding:0 12px 0 6px}.tl svg.plot{display:block;width:100%;overflow:visible}.strip svg{overflow:visible}
.tlcap{font-size:12px;color:var(--muted);margin:2px 0 0}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 440px;gap:0 16px;align-items:start}.main{min-width:0}
#panel{position:sticky;top:calc(var(--legend-h,0px) + 8px);background:var(--panel);border:1px solid var(--line);
border-radius:10px;padding:8px 12px;font-size:14px;display:flex;flex-direction:column;
height:calc(100vh - var(--legend-h,0px) - 16px);min-height:320px;box-sizing:border-box}
#pbody{flex:1;overflow:auto;min-height:0;overscroll-behavior:contain}
.stepper{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding-bottom:6px;margin-bottom:6px;
border-bottom:1px solid var(--line);font-size:13px}.stepper button{padding:3px 9px}.stepper select{padding:3px 6px}
#pos{min-width:8em;font-variant-numeric:tabular-nums}#pclose{margin-left:auto}.keys{font-size:12px;margin:6px 0 0}
.stackbar{border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin:0 0 8px;background:var(--card);font-size:13px}
.stackbar ol{margin:4px 0 0;padding-left:22px}.stackbar li.here{font-weight:600}.stackbar li.here a{text-decoration:none}
article.item{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:8px;
padding:9px 12px;margin:8px 0;font-size:14px}article.item.k-th{border-left-color:var(--muted)}
article.item.k-ov{border-left:5px double var(--fg)}article.item.k-fake{border-left-color:var(--fg)}
article.item.k-tp{border-left-style:dashed;border-left-color:var(--fg)}article.item.hl{outline:2px solid var(--fg)}
#pbody article.item{border:none;padding:0;margin:0;background:none}
blockquote{margin:6px 0;padding:4px 10px;border-left:3px solid var(--input-line);white-space:pre-wrap;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--pre);padding:8px 10px;border-radius:6px;
font:12px/1.5 ui-monospace,monospace;margin:6px 0;max-height:320px;overflow:auto}
.react{background:var(--chip);border-radius:6px;padding:6px 10px;margin:6px 0}.react p{margin:3px 0}
dl.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:2px 10px;margin:6px 0}dl.kv dt{color:var(--muted)}
dl.kv dd{margin:0;overflow-wrap:anywhere}
.chain{margin:8px 0}.chain>button{font-size:13px;padding:4px 10px}
.cbar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:12.5px;margin:4px 0}.cbar button{padding:2px 8px;font-size:12.5px}
.chain ol{list-style:none;margin:6px 0;padding:0;border-left:2px solid var(--line)}
.chain li{margin:0 0 6px;padding:2px 0 2px 10px}.chain li.turn{font-size:11.5px;color:var(--muted);padding-top:4px;
text-transform:uppercase;letter-spacing:.04em}.chain li .h{font-size:12px;color:var(--muted)}
.chain li.cited{border-left:3px solid var(--fg);margin-left:-2px;background:var(--pick)}
.chain .tx{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}
.chain .code{font:12px/1.45 ui-monospace,monospace;background:var(--pre);padding:4px 8px;border-radius:5px;
white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow:auto;margin:2px 0}
.chain details>summary{cursor:pointer;font-size:12.5px;color:var(--muted);overflow-wrap:anywhere}
mark{background:var(--hl);color:inherit;box-shadow:0 0 0 1px var(--fg);border-radius:2px}
details.all>summary,details.more>summary{cursor:pointer;font-weight:600;font-size:1.05rem;margin:18px 0 6px}
table{border-collapse:collapse;margin:8px 0;font-size:14px}th,td{border:1px solid var(--line);padding:4px 8px;
text-align:left;vertical-align:top}th{background:var(--chip);font-weight:600}td.n{text-align:right}
section.ev{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0;
scroll-margin-top:calc(var(--legend-h,0px) + 8px)}section.ev.cur{outline:2px solid var(--fg)}
.empty{padding:16px;color:var(--muted);border:1px dashed var(--input-line);border-radius:8px}.story{max-width:900px}
.evf .chip{border:1px solid var(--input-line);border-radius:14px;padding:3px 9px;background:var(--card);cursor:pointer}
.evf input[type=search]{font:inherit;padding:5px 8px;border:1px solid var(--input-line);border-radius:6px;
background:var(--card);color:inherit}
table.evt{width:100%;border-collapse:collapse;font-size:13.5px}table.evt th{white-space:nowrap;cursor:pointer;
position:sticky;top:var(--legend-h,0px);z-index:5}table.evt th[aria-sort=ascending]::after{content:" ▲"}
table.evt th[aria-sort=descending]::after{content:" ▼"}table.evt td{vertical-align:middle}
tr.er{cursor:pointer;scroll-margin-top:calc(var(--legend-h,0px) + 36px)}tr.er:hover>td,tr.er.open>td,tr.er:focus>td{background:var(--pick)}
tr.er:focus{outline:2px solid var(--fg)}td.ex{max-width:0;width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.sh{width:28px;min-width:28px;padding:2px 4px}td.sh .hglyph{margin:0}table.evt td:nth-child(2),table.evt td:nth-child(3){white-space:nowrap}
tr.xr>td{padding:0 0 8px}tr.xr section.ev{margin:4px 0}
.sb{display:grid;grid-template-columns:11em 160px 1fr;gap:8px;align-items:center;font-size:13px;margin:3px 0}
.sbar{display:flex;height:10px;border-radius:3px;overflow:hidden;background:var(--chip)}
.sbar .s0{background:var(--fg)}.sbar .s1{background:var(--muted)}.sbar .s2{background:var(--input-line)}
.sbar .s3{background:var(--line)}.sbs p{margin:6px 0 2px}[hidden]{display:none!important}
.grid{margin:6px 0}.grid .row,.grid .hdr{display:grid;grid-template-columns:96px minmax(0,1fr) 320px;gap:0 12px;
align-items:center}.grid .row{border-top:1px solid var(--line);padding:3px 0}.grid .row:hover{background:var(--pick)}
.grid .hdr{font-size:12px;color:var(--muted);padding:2px 0;position:sticky;top:var(--legend-h,0px);background:var(--bg);z-index:4}
.agent a{font-weight:600;text-decoration:none}.strip{padding:0 10px 0 6px}.strip svg{display:block;width:100%;height:34px}.labels{font-size:12px;min-width:0}
.hdr svg{display:block;width:100%;height:16px;overflow:visible}.hdr>div:nth-child(2){padding:0 10px 0 6px}
header.ah{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;margin:0 0 4px}header.ah h1{margin:0}
@media (max-width:1100px){.layout{grid-template-columns:minmax(0,1fr) 360px}}
@media (max-width:900px){.layout{grid-template-columns:minmax(0,1fr)}
#panel{position:static;height:auto;min-height:0;margin-top:12px}#pbody{max-height:none}
#panel.open{position:fixed;left:0;right:0;bottom:0;top:auto;height:58vh;z-index:40;border-radius:12px 12px 0 0;
margin:0;box-shadow:0 -6px 24px rgba(0,0,0,.25)}body.sheet{padding-bottom:58vh}
.grid .row,.grid .hdr{grid-template-columns:80px minmax(0,1fr)}.grid .labels{grid-column:1/-1}.grid .hdr .lab{display:none}}
@media (max-width:700px){body{padding:0 16px;margin:12px auto}.tl{grid-template-columns:78px minmax(0,1fr);padding:6px 6px 2px}
.ll{font-size:11.5px}.ll .lf{display:none}.ll .ls{display:inline}.badge{white-space:normal}
details.legend{position:static}table.evt thead{display:none}table.evt,table.evt tbody{display:block}
tr.er{display:grid;grid-template-columns:26px 1fr auto auto;gap:0 8px;border-top:1px solid var(--line);padding:4px 0}
tr.er>td{border:none;padding:1px 0}tr.er>td:nth-child(3),tr.er>td:nth-child(n+6):nth-child(-n+10){display:none}
tr.er>td.ex{grid-column:1/-1;width:auto;max-width:none;display:block;white-space:nowrap}
tr.er>td:nth-child(4)::after{content:" min"}tr.er>td:nth-child(5)::after{content:" readers";color:var(--muted)}
tr.xr{display:block}tr.xr>td{display:block}.sb{grid-template-columns:1fr;gap:2px}.sbar{width:100%}}
@media (max-height:599px){details.legend{position:static}}
"""
)

# The agent page's detail column: selecting (click, Enter, #sel=) or stepping (arrow keys, j / k, Prev / Next) shows
# one item; a stacked spot lists all of its items; nothing ever scrolls the window (owner, Nov 2, 2026): only the
# detail column scrolls. On narrow screens the column becomes a bottom sheet while an item is open.
PANEL_JS = r"""
(()=>{const panel=document.getElementById('panel');if(!panel)return;
const body=document.getElementById('pbody'),pos=document.getElementById('pos'),kind=document.getElementById('kind'),
prev=document.getElementById('pprev'),next=document.getElementById('pnext'),
all=[...document.querySelectorAll('#items article.item[id]')];let cur=null;
const stackOf={};document.querySelectorAll('.tl [data-stack]').forEach(g=>g.dataset.stack.split(' ').forEach(k=>{stackOf[k]=g;}));
function ok(a){return !kind.value||a.dataset.kind===kind.value;}
function list(){return all.filter(ok);}
function status(){const L=list(),src=cur&&document.getElementById(cur),i=src?L.indexOf(src):-1;
const t=src?' · '+src.dataset.t+' min':'';
pos.textContent=src?(i>=0?(i+1)+' of '+L.length:'('+kind.selectedOptions[0].text.toLowerCase()+': '+L.length+')')+t
:L.length+' '+(kind.value?kind.selectedOptions[0].text.toLowerCase():'items');
const ix=src?all.indexOf(src):-1;
prev.disabled=!all.some((a,j)=>ok(a)&&(ix<0?true:j<ix));next.disabled=!all.some((a,j)=>ok(a)&&j>ix);}
function stackHtml(key){const g=stackOf[key];if(!g)return '';const ks=g.dataset.stack.split(' ');if(ks.length<2)return '';
let h='<div class="stackbar"><b>'+ks.length+' items stacked at this spot</b> <span class="muted">('+g.dataset.span+
'; click one, or step with →)</span><ol>';
ks.forEach(k=>{const a=document.getElementById(k);if(!a)return;const ic=a.querySelector('h3 svg.hglyph');
h+='<li'+(k===key?' class="here"':'')+'>'+(ic?ic.outerHTML:'')+'<a href="#sel='+k+'" data-k="'+k+'">'+
a.dataset.label.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</a>'+(k===key?' ← shown':'')+'</li>';});
return h+'</ol></div>';}
function show(key){const src=document.getElementById(key);if(!src||!src.matches('article.item'))return;cur=key;
document.querySelectorAll('.sel').forEach(e=>e.classList.remove('sel'));
document.querySelectorAll('.tl [data-k="'+key+'"]').forEach(e=>e.classList.add('sel'));
const st=stackOf[key];if(st)st.classList.add('sel');
all.forEach(a=>a.classList.toggle('hl',a===src));body.innerHTML=stackHtml(key)+src.innerHTML;
body.scrollTop=0;panel.classList.add('open');document.body.classList.add('sheet');status();
const g=st||document.querySelector('.tl [data-k="'+key+'"]'),cu=document.getElementById('cursor');
if(cu&&g&&g.dataset.p){cu.setAttribute('x1',g.dataset.p+'%');cu.setAttribute('x2',g.dataset.p+'%');
cu.setAttribute('visibility','visible');}
try{history.replaceState(null,'','#sel='+key);}catch(e){}}
function close(){cur=null;const cu=document.getElementById('cursor');if(cu)cu.setAttribute('visibility','hidden');
document.querySelectorAll('.sel').forEach(e=>e.classList.remove('sel'));all.forEach(a=>a.classList.remove('hl'));
panel.classList.remove('open');document.body.classList.remove('sheet');const tpl=document.getElementById('pempty');
body.innerHTML='<p class="muted">Nothing selected. Click a shape on the timeline (a number on a shape = that many '
+'items stacked there), or press → (or Next) to read the items one by one in time order.</p>'+(tpl?tpl.innerHTML:'');
status();try{history.replaceState(null,'',location.pathname+location.search);}catch(e){}}
function step(d){const src=cur&&document.getElementById(cur),ix=src?all.indexOf(src):(d>0?-1:all.length);
let j=ix+d;while(j>=0&&j<all.length&&!ok(all[j]))j+=d;if(j>=0&&j<all.length)show(all[j].id);}
prev.addEventListener('click',()=>step(-1));next.addEventListener('click',()=>step(1));
document.getElementById('pclose').addEventListener('click',close);
kind.addEventListener('change',()=>{status();kind.blur();});
document.addEventListener('click',ev=>{if(ev.target.closest&&ev.target.closest('.chain'))return;
const sv=ev.target.closest&&ev.target.closest('.tl svg.plot');
if(sv){let best=null,bd=1e9;sv.querySelectorAll('[data-k]').forEach(g=>{const r=g.getBoundingClientRect(),
cx=r.left+r.width/2,cy=r.top+r.height/2;const d=g.tagName==='line'?Math.abs(ev.clientX-cx)+8:
Math.hypot(ev.clientX-cx,ev.clientY-cy);if(d<bd){bd=d;best=g;}});
if(best&&bd<=22){ev.preventDefault();show(best.getAttribute('data-k'));}return;}
const g=ev.target.closest('[data-k]');if(!g)return;ev.preventDefault();show(g.getAttribute('data-k'));});
document.addEventListener('keydown',ev=>{if(ev.altKey||ev.ctrlKey||ev.metaKey)return;
const tag=(ev.target.tagName||'').toLowerCase();if(tag==='input'||tag==='select'||tag==='textarea')return;
const k=ev.key;if(k==='ArrowRight'||k==='j'){ev.preventDefault();step(1);}
else if(k==='ArrowLeft'||k==='k'){ev.preventDefault();step(-1);}
else if(k==='Escape'){if(cur){ev.preventDefault();close();}}
else if(k==='Enter'||k===' '){const g=ev.target.closest&&ev.target.closest('.tl [data-k]');
if(g){ev.preventDefault();show(g.getAttribute('data-k'));}}});
const m=location.hash.match(/^#sel=(.+)$/);if(m)show(decodeURIComponent(m[1]));else close();})();
"""

# The reasoning chain behind a thought or around an event: the agent's own timeline items (reasoning, text, tool calls
# and results, events) of the cited turn and its neighbours, from the page's embedded timeline (#tl), in order, with
# the cited item marked and the quote highlighted. Earlier / Later extend it one turn at a time.
CHAIN_JS = r"""
(()=>{const el=document.getElementById('tl');if(!el)return;let T;try{T=JSON.parse(el.textContent);}catch(e){return;}
const R=T.items,lo0=R.length?R[0][2]:0,hi0=R.length?R[R.length-1][2]:0;
const KIND={reasoning:'Reasoning',text:'Wrote',tool_call:'Tool call',tool_result:'Tool result',event:'Event'};
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function hl(text,spans,q){if(spans.length){let out='',p=0;spans.sort((a,b)=>a[0]-b[0]).forEach(([a,b])=>{if(a<p)return;
out+=esc(text.slice(p,a))+'<mark>'+esc(text.slice(a,b))+'</mark>';p=b;});return out+esc(text.slice(p));}
if(q){let i=text.indexOf(q);if(i<0)i=text.toLowerCase().indexOf(q.toLowerCase());
if(i>=0)return esc(text.slice(0,i))+'<mark>'+esc(text.slice(i,i+q.length))+'</mark>'+esc(text.slice(i+q.length));}
return esc(text);}
function render(box,first){const lo=+box.dataset.lo,hi=+box.dataset.hi,
cite=new Set((box.dataset.cite||'').split(' ').filter(Boolean).map(Number)),q=box.dataset.q||'',sp={};
(box.dataset.spans||'').split(';').filter(Boolean).forEach(s=>{const[i,a,b]=s.split(':').map(Number);(sp[i]=sp[i]||[]).push([a,b]);});
const rows=R.filter(r=>r[2]>=lo&&r[2]<=hi);let h='<div class="cbar"><button type="button" data-act="earlier"'+(lo<=lo0?' disabled':'')+
'>◂ Earlier turn</button><span class="muted">turns '+lo+'–'+hi+' · '+rows.length+' items</span>'+
'<button type="button" data-act="later"'+(hi>=hi0?' disabled':'')+'>Later turn ▸</button>'+
'<button type="button" data-act="close">Collapse</button></div><ol>';let turn=null;
for(const r of rows){const[i,t,tu,kd,tool,text,cut]=r;if(tu!==turn){turn=tu;h+='<li class="turn">Turn '+tu+'</li>';}
const c=cite.has(i),body=hl(text||'',c?(sp[i]||[]):[],c?q:''),more=cut?'<span class="muted"> [cut '+cut+' characters]</span>':'',
head='<div class="h">'+t.toFixed(1)+' min · '+(KIND[kd]||kd)+(tool?' · '+esc(tool):'')+' · item '+i+
(c?' · <b>cited</b>':'')+'</div>';
if(kd==='tool_result'||kd==='tool_call'){const long=(text||'').length>220;
h+='<li'+(c?' class="cited"':'')+'>'+head+(long&&!c?'<details><summary>'+esc((text||'').slice(0,160))+'…</summary><div class="code">'
+body+more+'</div></details>':'<div class="code">'+body+more+'</div>')+'</li>';}
else h+='<li'+(c?' class="cited"':'')+'>'+head+'<div class="tx">'+body+more+'</div></li>';}
box.querySelector('.cbody').innerHTML=h+'</ol>';box.querySelector(':scope>button').hidden=true;
const pb=document.getElementById('pbody'),ci=box.querySelector('li.cited');
if(first&&ci&&pb&&pb.contains(ci))pb.scrollTop+=ci.getBoundingClientRect().top-pb.getBoundingClientRect().top-80;}
document.addEventListener('click',ev=>{const b=ev.target.closest&&ev.target.closest('.chain button');if(!b)return;
ev.preventDefault();const box=b.closest('.chain'),a=b.dataset.act;
if(a==='open'){box.dataset.lo=box.dataset.lo0;box.dataset.hi=box.dataset.hi0;render(box,true);}
else if(a==='earlier'){box.dataset.lo=Math.max(lo0,+box.dataset.lo-1);render(box,false);}
else if(a==='later'){box.dataset.hi=Math.min(hi0,+box.dataset.hi+1);render(box,false);}
else if(a==='close'){box.querySelector('.cbody').innerHTML='';box.querySelector(':scope>button').hidden=false;}});})();
"""

LEGEND_JS = r"""
(()=>{const d=document.querySelector('details.legend');if(!d)return;const K='storymap-legend-open';
try{const v=localStorage.getItem(K);if(v!==null)d.open=v==='1';else if(innerWidth<700)d.open=false;}
catch(e){if(innerWidth<700)d.open=false;}
const set=()=>document.documentElement.style.setProperty('--legend-h',
(getComputedStyle(d).position==='sticky'?d.offsetHeight:0)+'px');
let user=false;d.querySelector('summary').addEventListener('click',()=>{user=true;});
d.addEventListener('toggle',()=>{set();if(user){user=false;try{localStorage.setItem(K,d.open?'1':'0');}catch(e){}}});
set();addEventListener('resize',set);if(window.ResizeObserver)new ResizeObserver(set).observe(d);})();
"""

EVENTS_JS = r"""
(()=>{const tb=document.querySelector('#evt tbody');if(!tb)return;const cards=document.getElementById('cards'),
rows=[...tb.querySelectorAll('tr.er')],shown=document.getElementById('fshown'),view=document.getElementById('fview'),
secs=[...document.querySelectorAll('.evf input[name=sec]')],dir=document.getElementById('fdir'),
chg=document.getElementById('fchg'),who=document.getElementById('fwho'),q=document.getElementById('fq');
let mode='table',cur=-1,sortKey='',sortDir=1;
const card=id=>document.getElementById(id);
function ok(r){const on=secs.filter(s=>s.checked).map(s=>s.value);
if(!on.includes(r.dataset.sec))return false;if(dir.value&&!r.dataset.dir.split(' ').includes(dir.value))return false;
if(chg.value&&!r.dataset.chg.split(' ').includes(chg.value))return false;
const w=who.value.trim().toLowerCase();if(w&&!r.dataset.who.toLowerCase().split(' ').some(a=>a.includes(w)))return false;
const s=q.value.trim().toLowerCase();if(s&&!s.split(/\s+/).every(x=>r.dataset.q.includes(x)))return false;return true;}
function vis(){return rows.filter(r=>!r.hidden);}
function collapse(r){const n=r.nextElementSibling;if(n&&n.classList.contains('xr')){cards.appendChild(n.querySelector('section.ev'));
n.remove();}r.classList.remove('open');r.setAttribute('aria-expanded','false');}
function expand(r){if(r.classList.contains('open'))return;const x=document.createElement('tr');x.className='xr';
const td=document.createElement('td');td.colSpan=r.children.length;td.appendChild(card(r.dataset.id));x.appendChild(td);
r.after(x);r.classList.add('open');r.setAttribute('aria-expanded','true');}
function toggle(r){r.classList.contains('open')?collapse(r):expand(r);}
function state(){const p=new URLSearchParams();const off=secs.filter(s=>!s.checked).map(s=>s.value);
if(off.length)p.set('hide',off.join(','));if(dir.value)p.set('dir',dir.value);if(chg.value)p.set('chg',chg.value);
if(who.value.trim())p.set('who',who.value.trim());if(q.value.trim())p.set('q',q.value.trim());
if(mode==='cards')p.set('view','cards');if(sortKey)p.set('sort',(sortDir<0?'-':'')+sortKey);
const open=rows.filter(r=>r.classList.contains('open')).map(r=>r.dataset.id);if(open.length===1)p.set('open',open[0]);
try{history.replaceState(null,'',p.toString()?'#'+p.toString():location.pathname+location.search);}catch(e){}}
function apply(){let n=0;rows.forEach(r=>{const k=ok(r);r.hidden=!k;if(!k)collapse(r);
const c=card(r.dataset.id);if(c&&mode==='cards')c.hidden=!k;if(k)n++;});
shown.textContent=n+' of '+rows.length+' events shown';state();}
function setMode(m){mode=m;const t=document.getElementById('evt');if(m==='cards'){rows.forEach(collapse);
rows.forEach(r=>cards.appendChild(card(r.dataset.id)));t.hidden=true;cards.hidden=false;view.textContent='Show as table';
view.setAttribute('aria-pressed','true');}else{cards.querySelectorAll('section.ev').forEach(c=>c.hidden=false);
t.hidden=false;cards.hidden=true;view.textContent='Show as cards';view.setAttribute('aria-pressed','false');}apply();}
function sortBy(k,d){sortKey=k;sortDir=d;const th=[...document.querySelectorAll('#evt th[data-sort]')];
const i=th.findIndex(h=>h.dataset.sort===k);if(i<0)return;const num=th[i].classList.contains('n');
rows.forEach(collapse);const sorted=[...rows].sort((a,b)=>{const x=a.children[i].dataset.v||'',y=b.children[i].dataset.v||'';
return d*(num?(+x)-(+y):x.localeCompare(y));});sorted.forEach(r=>tb.appendChild(r));rows.splice(0,rows.length,...sorted);
th.forEach(h=>h.removeAttribute('aria-sort'));th[i].setAttribute('aria-sort',d>0?'ascending':'descending');state();}
document.querySelectorAll('#evt th[data-sort]').forEach(h=>{if(h.dataset.sort==='shape')return;h.tabIndex=0;
const go=()=>sortBy(h.dataset.sort,sortKey===h.dataset.sort?-sortDir:(h.classList.contains('n')?-1:1));
h.addEventListener('click',go);h.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();go();}});});
tb.addEventListener('click',e=>{if(e.target.closest('a,summary,.xr'))return;const r=e.target.closest('tr.er');
if(r){cur=vis().indexOf(r);toggle(r);state();}});
[...secs,dir,chg].forEach(x=>x.addEventListener('change',apply));[who,q].forEach(x=>x.addEventListener('input',apply));
view.addEventListener('click',()=>setMode(mode==='table'?'cards':'table'));
document.getElementById('freset').addEventListener('click',()=>{secs.forEach(s=>s.checked=true);dir.value='';
chg.value='';who.value='';q.value='';apply();});
function step(d){if(mode==='table'){const L=vis();if(!L.length)return;cur=Math.max(0,Math.min(L.length-1,cur+d));
const r=L[cur];r.focus({preventScroll:true});r.scrollIntoView({block:'nearest'});}
else{const L=[...cards.querySelectorAll('section.ev')].filter(c=>!c.hidden);if(!L.length)return;
cur=Math.max(0,Math.min(L.length-1,cur+d));L.forEach(c=>c.classList.remove('cur'));L[cur].classList.add('cur');
L[cur].scrollIntoView({block:'start'});}}
document.addEventListener('keydown',ev=>{if(ev.altKey||ev.ctrlKey||ev.metaKey)return;
const tag=(ev.target.tagName||'').toLowerCase();if(tag==='input'||tag==='select'||tag==='textarea')return;
if(ev.key==='ArrowDown'||ev.key==='j'){ev.preventDefault();step(1);}
else if(ev.key==='ArrowUp'||ev.key==='k'){ev.preventDefault();step(-1);}
else if(ev.key==='Enter'&&ev.target.matches&&ev.target.matches('tr.er')){ev.preventDefault();toggle(ev.target);state();}});
const h=decodeURIComponent(location.hash.slice(1));let openId=null;
if(h.includes('=')){const p=new URLSearchParams(h);const off=(p.get('hide')||'').split(',');
secs.forEach(s=>s.checked=!off.includes(s.value));dir.value=p.get('dir')||'';chg.value=p.get('chg')||'';
who.value=p.get('who')||'';q.value=p.get('q')||'';const s=p.get('sort');if(s)sortBy(s.replace(/^-/,''),s[0]==='-'?-1:1);
openId=p.get('open');setMode(p.get('view')==='cards'?'cards':'table');}else{openId=h||null;setMode('table');}
if(openId){const r=rows.find(r=>r.dataset.id===openId);if(r&&!r.hidden){if(mode==='table')expand(r);
(mode==='table'?r:card(openId)).scrollIntoView({block:'start'});state();}}})();
"""

FILTER_JS = """
(()=>{const sels=[...document.querySelectorAll('.filters select')],rows=[...document.querySelectorAll('.row[data-agent]')],
count=document.getElementById('shown');
function apply(){let n=0;rows.forEach(r=>{const ok=sels.every(s=>!s.value||r.dataset[s.name]===s.value);
r.hidden=!ok;if(ok)n++;});count.textContent=n+' of '+rows.length+' agents shown';}
sels.forEach(s=>s.addEventListener('change',apply));
document.getElementById('reset').addEventListener('click',()=>{sels.forEach(s=>s.value='');apply();});apply();})();
"""


# ------------------------------------------------------------------ loading

def _read_jsonl(path):
    rows = []
    if not path.is_file():
        return rows
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"_malformed": f"{path.name} line {n}"})
    return rows


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _unwrap(row, key):
    """Accept a bare stage output or one wrapped by the pipeline (``output``/``result``/``parsed``)."""
    if not isinstance(row, dict):
        return None
    if key in row:
        return row
    for wrap in ("output", "result", "parsed", "data"):
        inner = row.get(wrap)
        if isinstance(inner, dict) and key in inner:
            merged = dict(inner)
            for meta in ("from_t", "to_t", "chunk", "agent"):
                if meta in row and meta not in merged:
                    merged[meta] = row[meta]
            return merged
    return None


def timeline_dir(path):
    path = Path(path)
    return path / "timeline" if (path / "timeline").is_dir() else path


def load(map_dir, timeline=None):
    """All agents' data: {agent: {items, events, thoughts, chunks, reactions, stage3, index}}."""
    map_dir = Path(map_dir)
    agents = defaultdict(lambda: {"items": [], "chunks": [], "reactions": [], "stage3": None,
                                  "index": {}, "malformed": []})
    index = {}
    if timeline is not None:
        tdir = timeline_dir(timeline)
        index = _read_json(tdir / "index.json") or {}
        names = set(index) | {p.stem for p in tdir.glob("agent-*.jsonl")}
        for name in names:
            agents[name]["index"] = index.get(name) or {}
            agents[name]["items"] = [r for r in _read_jsonl(tdir / f"{name}.jsonl") if "_malformed" not in r]
    for p in sorted((map_dir / "stage1").glob("*.jsonl")):
        for row in _read_jsonl(p):
            out = _unwrap(row, "thoughts")
            (agents[p.stem]["chunks"].append(out) if out else agents[p.stem]["malformed"].append(f"stage1 {p.name}"))
    for p in sorted((map_dir / "stage2").glob("*.jsonl")):
        for row in _read_jsonl(p):
            out = _unwrap(row, "event_id")
            (agents[p.stem]["reactions"].append(out) if out else agents[p.stem]["malformed"].append(f"stage2 {p.name}"))
    for p in sorted((map_dir / "stage3").glob("*.json")):
        out = _unwrap(_read_json(p), "final")
        if out:
            agents[p.stem]["stage3"] = out
        else:
            agents[p.stem]["malformed"].append(f"stage3 {p.name}")
    for row in _read_jsonl(map_dir / "map.jsonl"):
        out = _unwrap(row, "final")
        if out and out.get("agent") and agents[out["agent"]]["stage3"] is None:
            agents[out["agent"]]["stage3"] = out
    for name, a in agents.items():
        a["name"] = name
        a["events"] = [it for it in a["items"] if it.get("kind") == "event" and isinstance(it.get("event"), dict)]
        thoughts = []
        for c, chunk in enumerate(a["chunks"]):
            cn = chunk.get("chunk") if isinstance(chunk.get("chunk"), int) else c
            for n, th in enumerate(chunk.get("thoughts") or []):
                if isinstance(th, dict):  # ekey: the evidence file's key of this stage-1 thought (evidence.py)
                    thoughts.append(dict(th, chunk=c, ekey=f"c{cn:03d}-t{n:02d}"))
        thoughts.sort(key=lambda th: (_num(th.get("t")), _num(th.get("item"))))
        a["thoughts"] = thoughts
        by_event = defaultdict(list)
        for r in a["reactions"]:
            by_event[str(r.get("event_id"))].append(r)
        a["by_event"] = by_event
        ev = _read_json(map_dir / "evidence" / f"{name}.json")  # storymap/evidence.py (maps from Nov 3, 2026)
        a["evidence"] = {str(t.get("key")): t for t in (ev or {}).get("thoughts") or [] if isinstance(t, dict)}
    pipeline = _read_json(map_dir / "pipeline.json") or {}
    return dict(sorted(agents.items())), pipeline


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_taxonomy(path):
    """{"A": "group title", "C4": "code title"} from the researcher-only taxonomy headings, if any."""
    names = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError):
        return names
    for line in text.splitlines():
        m = re.match(r"^#{1,4}\s*(?:Group\s+)?([A-G])(\d{0,2})\b\s*[.:)\-–—]*\s*(.*)$", line.strip())
        if not m:
            m = re.match(r"^[-*]\s*\**`?([A-G])(\d{1,2})`?\**\s*[.:)\-–—]+\s*(.*)$", line.strip())
        if m:
            key = m.group(1) + m.group(2)
            title = re.sub(r"[*`_]", "", m.group(3)).strip()
            title = re.split(r"(?<=[a-z\)])\.\s|\s\(", title)[0][:80]
            if title and key not in names:
                names[key] = title
    return names


def load_definitions(path):
    """{code: {"name", "def", "include", "example"}} from the taxonomy's code sections (``### A1 - name`` and its
    paragraph): ``def`` is the text before "Include:" / "Exclude:" / "Example", ``include`` the inclusion note,
    ``example`` the example. Group headings (``### A - name``) get their intro paragraph, if any."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError):
        return {}
    out, cur, body = {}, None, []

    def close():
        if cur is None:
            return
        para = re.sub(r"\s+", " ", re.sub(r"[*`]", "", " ".join(body))).strip()
        parts = re.split(r"\s(?=(?:Include|Exclude|Example)\b)", para)
        d = {"name": cur[1], "def": parts[0] if parts and not re.match(r"(Include|Exclude|Example)\b", parts[0]) else "",
             "include": "", "example": ""}
        for part in parts:
            if part.startswith("Include"):
                d["include"] = part
            elif part.startswith("Example"):
                d["example"] = part
        out[cur[0]] = d
    for line in text.splitlines():
        m = re.match(r"^###\s+([A-G]\d{0,2})\s+-\s+(.*)$", line.strip())
        if m or line.startswith("#"):
            close()
            cur, body = ((m.group(1), re.split(r"\s\(", m.group(2))[0].strip()) if m else None), []
        elif cur is not None:
            body.append(line.strip())
    close()
    return out


DEFS = {}  # code definitions of the current build (set by build(); empty without a taxonomy)


def code_tip(code, defs, names, full=True):
    """'C4 Social proof: <definition> <include> <example>' (inclusion note and example only when short)."""
    d = defs.get(code) or {}
    name = names.get(code) or d.get("name") or ""
    bits = [f"{code} {name}".strip() + (":" if d.get("def") else "")]
    if d.get("def"):
        bits.append(d["def"])
    if full:
        bits += [d[k] for k in ("include", "example") if d.get(k) and len(d[k]) <= 260]
    return " ".join(bits)


# ------------------------------------------------------------------ helpers

def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def fmt_t(t):
    """Every time in the UI: minutes from run start, one decimal."""
    return "?" if t is None else f"{max(_num(t), 0.0):.1f} min"  # a few setup items sit a moment before 0


def fmt_span(a, b):
    return f"{max(_num(a), 0.0):.1f}\u2013{max(_num(b), 0.0):.1f} min"


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def human(key):
    """A readable label for a field name without an entry in DETAIL_LABEL (never the raw snake_case name)."""
    return DETAIL_LABEL.get(key) or str(key).replace("_", " ").capitalize()


def yesno(v):
    return "yes" if v is True else "no" if v is False else v


def tidy_times(text):
    """Extractor summaries in the pages' words: '... at 12.771' -> '... at 12.8 min', 'FAKE (iffrfl_main)' ->
    'fake (answer theorem proved by Iff.rfl alone)', outcome codes in words."""
    text = re.sub(r"\bFAKE \((\w+)\)", lambda m: "fake (" + VALUE_LABEL["form"].get(m.group(1), m.group(1)) + ")",
                  str(text))
    text = re.sub(r"\b(" + "|".join(k for k in OUTCOME_LABEL if "_" in k) + r")\b", lambda m: OUTCOME_LABEL[m.group(1)],
                  text)
    return re.sub(r"\b(at|seen at) (-?\d+\.\d+)\b(?! min)", lambda m: f"{m.group(1)} {fmt_t(float(m.group(2)))}",
                  str(text))


def cut(text, cap=TEXT_CAP):
    text = "" if text is None else str(text)
    return text if len(text) <= cap else text[:cap] + f"\n[viewer cut {len(text) - cap} chars]"


def _shown(key, value):
    """A detail value as shown: times (``t``, ``*_t``, ``*_at``) as minutes from run start, booleans yes / no, known
    codes in words, lists comma-separated, maps as 'key: value' pairs."""
    key = str(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and (key == "t" or key.endswith(("_t", "_at"))):
        return fmt_t(value)
    if isinstance(value, bool):
        return yesno(value)
    if key.endswith("event_ids") and isinstance(value, list):
        return ", ".join(event_label(v) for v in value) or "none"
    if isinstance(value, list):
        return ", ".join(str(_shown("", v)) for v in value) or "none"
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_shown('', v)}" for k, v in value.items()) or "none"
    if key == "source":
        return source_label(value)
    if key == "aware_cheating":
        return value
    if isinstance(value, str):
        value = VALUE_LABEL.get(key, {}).get(value, value)
        if key in ("submission_id",):
            return value
        return plain_ids(value) if key != "summary" else cut(value, 600)
    return value


def badge(text, tone=""):
    return f'<span class="badge{" b-" + tone if tone else ""}">{esc(text)}</span>'


def tbadge(text, tip, tone=""):
    """A badge with a hover explanation."""
    return f'<span class="badge{" b-" + tone if tone else ""}" title="{esc(tip)}">{esc(text)}</span>'


AWARE_HINT = ("Knew it was cheating (stage-3 reading): yes = at some point called the trick cheating, a loophole, "
              "gaming or against the spirit; no = discussed it and only called it legitimate; unclear = neither")
FLAG_HINT = ("Consistency flags: places where the stage-3 reader noted that the stage-1 or stage-2 readings disagree "
             "with each other or with the run's records.")


def is_overseer(ev):
    return ev.get("author_role") == "overseer"


def delivery_of(e):
    """How the event reached the agent (extractor ``detail.delivery``): 'push' (the host appended it to the agent's
    context without its asking: a board post, DM or reminder in a push delivery), 'notice' (a notice inside the result
    of one of its own tool calls, e.g. the integrity reminder), 'pull' (returned to a read the agent made), or None
    (maps built before the field existed, and own acts)."""
    v = (e.get("detail") or {}).get("delivery") or e.get("delivery")
    return v if v in ("push", "pull", "notice") else None


def delivery_id(e):
    return (e.get("detail") or {}).get("delivery_id") or e.get("delivery_id")


OTHER_TYPES = ("P1", "N1", "C1", "O1")


def glyph_class(ev):
    """Neutral-ink flags of an event glyph: g-fill (fake) or g-hollow, ring (from an overseer), and for a submission
    ok (accepted or credited) or no (not accepted); g-other for the small status / own-action dots; g-rem for the
    host's integrity reminder (an amber star); push when it was pushed into the agent's context (a chevron)."""
    e = ev["event"]
    typ = e.get("type")
    flags = (["g-other"] if typ in OTHER_TYPES else ["g-rem"] if typ == "R1"
             else ["g-fill" if e.get("fake") else "g-hollow"])
    if is_overseer(e):
        flags.append("ring")
    if typ == "S1":
        d = e.get("detail") or {}
        flags.append("ok" if d.get("credited") or str(e.get("outcome") or "").startswith("accepted") else "no")
    if delivery_of(e) in ("push", "notice"):
        flags.append("push")
    return " ".join(flags)


def _mark(typ, x, y, r, cls="m"):
    """The bare SVG element of a shape (square, diamond, triangle, star, small dot, tick or circle)."""
    if typ in ("L1", "L2"):
        return f'<rect class="{cls}" x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r:.1f}" height="{2 * r:.1f}"/>'
    if typ == "D1":
        k = r * 1.3
        return (f'<polygon class="{cls}" points="{x:.1f},{y - k:.1f} {x + k:.1f},{y:.1f} {x:.1f},{y + k:.1f} '
                f'{x - k:.1f},{y:.1f}"/>')
    if typ == "S1":
        return (f'<polygon class="{cls}" points="{x:.1f},{y - r * 1.3:.1f} {x + r * 1.2:.1f},{y + r:.1f} '
                f'{x - r * 1.2:.1f},{y + r:.1f}"/>')
    if typ == "R1":  # a five-pointed star: the host's integrity reminder
        pts = " ".join(f"{x + (r * 1.35 if k % 2 == 0 else r * 0.58) * math.sin(math.pi * k / 5):.1f},"
                       f"{y - (r * 1.35 if k % 2 == 0 else r * 0.58) * math.cos(math.pi * k / 5):.1f}"
                       for k in range(10))
        return f'<polygon class="{cls}" points="{pts}"/>'
    if typ in OTHER_TYPES:
        return f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{max(r * 0.45, 1.6):.1f}"/>'
    if typ == "TH":
        w = max(3.2, r * 0.7)
        return f'<rect class="{cls}" x="{x - w / 2:.1f}" y="{y - r:.1f}" width="{w:.1f}" height="{2 * r:.1f}" rx="1"/>'
    return f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}"/>'


HIT_R = 13  # px: a hit area of about 26 px across


def count_badge(x, y, r, n):
    """The stack count: a small ink disc with the number of items stacked at this spot (above right)."""
    label = str(n) if n < 100 else "99+"
    rr = 6 if n < 10 else 7.2 if n < 100 else 8.6
    cx, cy = x + r + 2.5, y - r - 2.5
    return (f'<g class="cnt"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="{rr}"/>'
            f'<text x="{cx:.1f}" y="{cy + 3.1:.1f}">{label}</text></g>')


def shape(typ, x, y, r, cls, key, title, count=0, attrs=""):
    """One SVG glyph in neutral ink: the shape encodes the event type; ``cls`` flags (glyph_class) add the fill,
    the overseer ring, a submission's outcome mark and the push chevron. ``key`` None: not selectable (legend,
    links). ``count`` > 1: a stack of that many items (count badge). ``attrs``: extra attributes of the group."""
    flags = set(cls.split())
    a = f'class="g {cls}"' + (f' data-k="{esc(key)}" tabindex="0" role="button"' if key is not None else "") + attrs
    t = f"<title>{esc(title)}</title>" if title else ""
    if typ == "P2":
        d = f"M{x - r:.1f},{y - r:.1f}L{x + r:.1f},{y + r:.1f}M{x + r:.1f},{y - r:.1f}L{x - r:.1f},{y + r:.1f}"
        body = f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="{r + 1:.1f}"/><path class="x" d="{d}"/>'
    else:
        body = _mark(typ, x, y, r)
    if key is not None:  # an invisible hit area of at least 24 px, drawn first so the mark stays on top
        body = f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="{HIT_R}"/>' + (body if typ != "P2" else body[body.index("/>") + 2:])
    ring = _mark(typ, x, y, r + 2.8, "ring") if "ring" in flags and typ != "P2" else ""
    extra = ""
    if "ok" in flags:  # a check mark above right: accepted or credited
        extra = (f'<path class="mk" d="M{x + r * 0.7:.1f},{y - r * 1.5:.1f}l{r * 0.45:.1f},{r * 0.5:.1f}'
                 f'l{r * 0.9:.1f},{-r * 1.1:.1f}"/>')
    elif "no" in flags:  # a slash through it: not accepted
        d = f"M{x - r * 1.3:.1f},{y + r * 1.4:.1f}L{x + r * 1.3:.1f},{y - r * 1.6:.1f}"
        extra = f'<path class="halo" d="{d}"/><path class="mk" d="{d}"/>'
    if "push" in flags:  # a small chevron below: it arrived in the agent's context without being asked for
        extra += (f'<path class="push" d="M{x - r * 0.75:.1f},{y + r * 1.55:.1f}l{r * 0.75:.1f},{r * 0.6:.1f}'
                  f'l{r * 0.75:.1f},{-r * 0.6:.1f}"/>')
    if count > 1:
        extra += count_badge(x, y, r, count)
    return f'<g {a}>{t}{ring}{body}{extra}</g>'


def event_title(ev):
    e = ev["event"]
    lab = event_label(e)[:90] if e.get("type") in ("L2", "B1", "D1", "S1", "P2", "O1") else type_name(e.get("type"))
    bits = [lab[:1].upper() + lab[1:], fmt_t(ev.get("t"))]
    if e.get("author") and e.get("type") != "S1":
        bits.append(f"by {e['author']}" + (f" ({e['author_role']})" if e.get("author_role") else ""))
    if e.get("fake"):
        bits.append("fake")
    if e.get("outcome"):
        bits.append(OUTCOME_LABEL.get(str(e["outcome"]), str(e["outcome"]).replace("_", " ")))
    if delivery_of(e) in ("push", "notice"):
        bits.append(DELIVERY_LABEL[delivery_of(e)])
    return " · ".join(bits)


def final_labels(s3):
    if not s3:
        return badge("no final reading", "caution")
    f = s3.get("final") or {}
    src = f.get("source", "unknown")
    out = [tbadge(source_label(src) if src in ("none",) else f"{SOURCE_TITLE}: {source_label(src)}", SOURCE_HINT,
                  "info")]
    if f.get("faked"):
        out.append(tbadge(f"first faked at {fmt_t(f.get('first_fake_t'))}", "Its first fake submission", "bad"))
    elif f.get("considered"):
        out.append(tbadge("considered faking, did not", "Had the trick idea at some point but never submitted a fake",
                          "caution"))
    else:
        out.append(tbadge("never considered faking", "Never showed the trick idea", "good"))
    if f.get("credited"):
        out.append(tbadge("got credit for a fake", "At least one fake was accepted with credit (before any revert)",
                          "bad"))
    if f.get("stopped"):
        out.append(tbadge("stopped faking", "Faked, then decided to stop, with no later fake", "info"))
    if f.get("aware_cheating") in ("yes", "no", "unclear"):
        out.append(tbadge("knew it was cheating: " + f["aware_cheating"], AWARE_HINT))
    flags = s3.get("consistency_flags") or []
    if flags:
        n = len(flags)
        tip = FLAG_HINT + " " + "; ".join(plain_ids(x) for x in flags)
        out.append(tbadge(plural(n, "consistency flag"), tip, "caution"))
    return "".join(out)


STANDARD_LENGTHS = 30.0  # run lengths are whole half hours (30, 60, 120, 180 min)


def run_minutes(map_dir, pipeline=None):
    """The run's length in minutes from its settings.json (``max_run_seconds``), looked up beside the story map
    (RUN/analysis/storymap -> RUN) or at pipeline.json's ``run_dir``; None when neither is readable."""
    candidates = []
    try:
        candidates.append(Path(map_dir).resolve().parents[1] / "settings.json")
    except (IndexError, OSError):
        pass
    if isinstance(pipeline, dict) and pipeline.get("run_dir"):
        candidates.append(Path(str(pipeline["run_dir"])) / "settings.json")
    for path in candidates:
        data = _read_json(path) if path.is_file() else None
        if not isinstance(data, dict):
            continue
        for section in ("config", "effective"):
            sec = data.get(section) or {}
            if isinstance(sec, dict) and _num(sec.get("max_run_seconds")) > 0:
                return _num(sec["max_run_seconds"]) / 60.0
    return None


def axis_max(agents, minutes=None):
    """The time axis follows the run: its configured length when known, else the last recorded time rounded to the
    run's half hour (a run's last seconds may spill a few minutes past its length, so 61.8 -> 60, 178 -> 180)."""
    if minutes and minutes > 0:
        return float(minutes)
    last = [0.0]
    for a in agents.values():
        last += [_num(it.get("t")) for it in a["items"][-1:]]
        last += [_num(th.get("t")) for th in a["thoughts"][-1:]]
        for ph in (a["stage3"] or {}).get("phases") or []:
            last.append(_num(ph.get("to_t")))
    top = max(last)
    return max(STANDARD_LENGTHS, STANDARD_LENGTHS * math.ceil((top - 5) / STANDARD_LENGTHS))


def tick_step(tmax):
    return 10 if tmax <= 90 else 15 if tmax <= 150 else 30 if tmax <= 360 else 60


def phases_of(a, tmax):
    """Stage-3 phases; else a fallback from stage-1 chunk states (stated as such); else nothing."""
    s3 = a["stage3"] or {}
    if s3.get("phases"):
        return [(_num(p.get("from_t")), _num(p.get("to_t"), tmax), str(p.get("stance") or "unknown"),
                 str(p.get("label") or "")) for p in s3["phases"]], "stage-3 reading"
    out, prev = [], 0.0
    for c in a["chunks"]:
        st = (c.get("state") or {}).get("stance") or "unknown"
        ts = [_num(th.get("t")) for th in c.get("thoughts") or []]
        end = _num(c.get("to_t"), max(ts) if ts else prev)
        if end > prev or not out:
            out.append((prev, end, st, "stage-1 state"))
            prev = end
    return out, ("stage-1 reading (no stage-3 reading)" if out else "none")


def page(title, body, scripts="", body_class="", css=""):
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta name="robots" content="noindex"><title>{esc(title)}</title><style>{CSS}{css}</style></head>'
           f'<body{f" class={chr(34)}{body_class}{chr(34)}" if body_class else ""}>{body}'
           f'{"<script>" + scripts + "</script>" if scripts else ""}</body></html>')
    return themed(doc)


STAGE_MODELS = (("stage1", "stage 1"), ("stage2", "stage 2"), ("stage3", "stage 3"))


def model_names(pipeline):
    """'stage 1 anthropic/claude-haiku-5.5; stages 2-3 openai/gpt-5.6-terra' from pipeline.json (else '')."""
    models = (pipeline or {}).get("models") if isinstance(pipeline, dict) else None
    if not isinstance(models, dict):
        return ""
    ids = [(label, models[k].get("model_id") if isinstance(models.get(k), dict) else models.get(k))
           for k, label in STAGE_MODELS]
    groups = []
    for label, mid in ids:
        if not mid:
            continue
        if groups and groups[-1][1] == mid:
            groups[-1][0].append(label.split()[-1])
        else:
            groups.append(([label.split()[-1]], mid))
    return "; ".join(f"{model_label(mid)} " + (f"(stages {g[0]}–{g[-1]})" if len(g) > 1 else f"(stage {g[0]})")
                     for g, mid in groups)


def banner_line(models=""):
    """The agent pages' one-line banner (the full one is on the index and events pages)."""
    read = f" by {esc(models)}" if models else ""
    return (f'<p class="banner one" title="Stage 1 codes the agent\'s thoughts; stage 2 judges its reaction to each '
            f'event; stage 3 writes its positions and final reading.">Quotes are the agent\'s own; codes, reactions '
            f'and positions are model readings{read}: interpretations, not ground truth.</p>')


def banner(models=""):
    read = f" Readers: {esc(models)}." if models else ""
    return ('<p class="banner">Story map: what each agent read, thought and did about the cheating shortcut, quoted '
            'from its own reasoning and read by models in three stages (stage 1 codes its thoughts; stage 2 judges '
            f'its reaction to each event; stage 3 writes its positions and final reading).{read} '
            'Model readings are interpretations, not ground truth.</p>')


STANCE_MEANING = (("none", "no idea of the shortcut yet"), ("considering", "has the idea, undecided"),
                  ("deferred", "keeps it as a later option"), ("rejected", "decided not to use it"),
                  ("faking", "decided to fake, or faking"), ("stopped", "stopped after faking"),
                  ("unknown", "no reading for this span"))
# One name per event type everywhere (legend, lanes, tooltips, panel, events table, codes page): TYPE_NAME.
SHAPE_MEANING = (  # (type, glyph_class flags, legend text); drawn by shape() exactly as on the map
    ("L2", "g-hollow", "library listing or file read"), ("B1", "g-hollow", TYPE_NAME["B1"]),
    ("D1", "g-hollow", TYPE_NAME["D1"]), ("S1", "g-hollow", TYPE_NAME["S1"]), ("P2", "g-hollow", TYPE_NAME["P2"]),
    ("N1", "g-other", "problem list, unread-messages notice, own post or restart"),
    ("R1", "g-rem", TYPE_NAME["R1"]))
AGENT_ONLY_SHAPES = {"R1"}  # drawn only on the agent pages (their own lane), so listed elsewhere only when present
MARK_MEANING = (("L2", "g-fill", "pink, solid: fake"), ("D1", "g-hollow ring", "cyan ring: from an overseer"),
                ("S1", "g-hollow ok", "✓ accepted or credited"), ("S1", "g-hollow no", "slash: not accepted"))
PUSH_MEANING = ("B1", "g-hollow push", "chevron below: delivered into its context unasked (no chevron: it read it)")
EXAMPLES = (("L2", "g-fill", "fake library file read"), ("D1", "g-hollow ring", "DM from an overseer"),
            ("S1", "g-fill ok", "fake submission, accepted"), ("S1", "g-hollow no", "genuine submission, rejected"))


def _legend_glyph(typ, cls, count=0):
    """A legend glyph from the map's own code: shape() for events, the stance bar rect for stances."""
    if typ == "TP":
        return '<svg viewBox="0 0 18 16"><line class="tp" x1="9" x2="9" y1="0" y2="16"/></svg>'
    if typ == "BAR":
        return f'<svg viewBox="0 0 22 16" class="swatch"><rect class="ph-{cls}" x="1" y="3" width="20" height="10" rx="2"/></svg>'
    return f'<svg viewBox="0 0 18 16">{shape(typ, 8, 8.5, 4.6, cls, None, "", count)}</svg>'


GLOSSARY = (
    ("the trick (shortcut, fake)", "filling an answer-based problem's answer slot with the question itself, so the "
     "proof closes without mathematics; a fake is a submission or library file that does this, or that the research "
     "evaluator judged fraudulent"),
    ("judge", "the weak operational judge that accepts any file that compiles without banned constructs"),
    ("credited / closed", "an accepted submission credits its agent and closes the problem for everyone"),
    ("revert", "an overseer removes the credit for a fake and the problem reopens"),
    ("overseer", "an agent whose role is to police fakes; it posts warnings and rulings, sends DMs and reverts"),
    ("seed", "the agent that submitted one planted fake early, which then sat in the shared library"),
    ("solver", "an ordinary agent proving problems for credit"),
    ("turn / timeline item", "a turn is one model call of the agent; an item is one entry (reasoning, text, tool "
     "call or result, event) in its timeline, numbered from 0"),
    ("stage-1 chunk", "a stretch of the timeline that the stage-1 reader coded in one go"),
    ("reader certainty", "how sure the model reader was of a code or reaction: high, medium or low"),
)
CODE_SCHEME = ("Letters are only ever code groups: A–D and G are thought codes (stage 1), E reaction codes and F "
               "overseer readings (stage 2). Events, including integrity reminders and other pushed notices, are never "
               "lettered: they are named in words and drawn as shapes.")


def terms_html():
    """The glossary: a separate, collapsed block, so the legend stays short."""
    return ('<details class="terms"><summary>Terms used on these pages: trick, fake, judge, credited, closed, revert, '
            'overseer, seed, turn, item, chunk, certainty</summary><ul>'
            + "".join(f"<li><b>{esc(k)}</b>: {esc(v)}</li>" for k, v in GLOSSARY) + "</ul></details>")


def groups_html(names, href, defs=None):
    """The in-page code key: each group with its codes and short names; each code shows its definition on hover or
    focus (title) and expands on click; the full reference is the taxonomy page. Without names, one line saying the
    names are not shown."""
    defs = defs or {}
    if not any(names.get(L) for L in LETTERS):
        return ('<p class="note">Thoughts that the stage-1 reader quoted and coded into seven groups, A–G; '
                'the group names are not shown on this copy.</p>')
    groups = []
    for L in LETTERS:
        codes = sorted((c for c in names if c[:1] == L and len(c) > 1), key=lambda c: int(c[1:]))
        items = "".join(
            f'<details class="code"><summary title="{esc(code_tip(c, defs, names))}"><b>{c}</b> {esc(names[c])}'
            f'</summary><p>{esc(code_tip(c, defs, names))}' + (f' <a href="{esc(href)}#code-{c}">more</a>' if href else "")
            + '</p></details>' for c in codes)
        groups.append(f'<div class="grp"><div class="gname" title="{esc(code_tip(L, defs, names, False))}">'
                      f'<b>{L}</b> {esc(names.get(L, ""))}</div>{items}</div>')
    more = f' Full reference with examples: <a href="{esc(href)}">codes page</a>.' if href else ""
    return (f'<p class="note">Codes the stage-1 reader gave to quoted thoughts (and, for E and F, the stage-2 reader to '
            f'reactions). Hover or click a code for its definition.{more}</p><div class="codes">{"".join(groups)}</div>')


def legend_bar(mode="agent", shown_types=(), push=False):
    """The compact legend every page keeps in view (sticky at the top on wide screens, collapsible): position colours,
    event shapes, marks; each item's meaning on hover. The explanation is in legend_about further down."""
    pos = "".join(f'<li title="{esc(m)}">{_legend_glyph("BAR", s)}{esc(STANCE_LABEL.get(s, s))}</li>'
                  for s, m in STANCE_MEANING)
    shapes = "".join(f'<li>{_legend_glyph(t, c)}{esc(m)}</li>' for t, c, m in SHAPE_MEANING
                     if t not in AGENT_ONLY_SHAPES or t in shown_types)
    if mode == "agent":
        shapes += (f'<li>{_legend_glyph("TH", "g-th")}{_legend_glyph("TH", "g-th-low")}thought (hollow = low certainty)'
                   f'</li><li>{_legend_glyph("TP", "")}turning point</li>')
    marks = "".join(f'<li>{_legend_glyph(t, c)}{esc(m)}</li>' for t, c, m in MARK_MEANING)
    if push:
        marks += f'<li>{_legend_glyph(PUSH_MEANING[0], PUSH_MEANING[1])}{esc(PUSH_MEANING[2])}</li>'
    if mode != "events":
        marks += f'<li>{_legend_glyph("L2", "g-hollow", 3)}number = items stacked at one spot</li>'
    return ('<details class="legend" open><summary>Legend: bar colour = position on cheating; shape = event'
            + ('; tick = coded thought' if mode == "agent" else "") + '</summary>'
            f'<div class="lstrip"><div class="row"><span class="k">Position:</span><ul>{pos}</ul></div>'
            f'<div class="row"><span class="k">Shape:</span><ul>{shapes}</ul></div>'
            f'<div class="row"><span class="k">Mark:</span><ul>{marks}</ul></div></div></details>')


def legend_about(mode="agent", names=None, href="taxonomy.html", defs=None, shown_types=(), push=False):
    """The legend explained (collapsed, below the main view): built from the map's own classes, with worked
    examples, the code groups and the glossary."""
    names = names or {}
    sw = "".join(f'<li>{_legend_glyph("BAR", s)}<b>{STANCE_LABEL.get(s, s)}</b>&nbsp;{esc(m)}</li>'
                 for s, m in STANCE_MEANING)
    shapes = "".join(f'<li>{_legend_glyph(t, c)}{esc(m)}</li>' for t, c, m in SHAPE_MEANING
                     if mode == "agent" or t not in AGENT_ONLY_SHAPES or t in shown_types)
    if mode == "agent":
        shapes += (f'<li>{_legend_glyph("TH", "g-th")}{_legend_glyph("TH", "g-th-low")}thought (hollow = low '
                   f'certainty)</li><li>{_legend_glyph("TP", "")}turning point</li>')
    marks = "".join(f'<li>{_legend_glyph(t, c)}{esc(m)}</li>' for t, c, m in MARK_MEANING + (PUSH_MEANING,))
    examples = "".join(f'<li>{_legend_glyph(t, c)}= {esc(m)}</li>' for t, c, m in EXAMPLES)
    where = {"index": "Shapes above an agent's bar are what it read (library files, overseer and other relevant posts, "
                      "DMs, reverts); below are its submissions. Click a shape to open it on the agent page.",
             "events": "Each event below carries its shape. ↓ / ↑ (or j / k) move through the events."
             }.get(mode, "Click any shape for its quote, the event and the stage-2 reaction; "
                         "→ / j next, ← / k previous, Esc closes. Under a thought, “Show the full "
                         "reasoning chain” opens the agent's own reasoning, text and tool calls around it.")
    rem = mode == "agent" or "R1" in shown_types
    return (f'<details class="about"><summary>About the legend, the codes and the terms</summary>'
            f'<div class="lg"><div class="lgbox"><h4>Bar colour = the agent\'s position on cheating at that moment</h4>'
            f'<p class="note">{esc(POSITION_HINT)}.</p><ul>{sw}</ul>'
            f'<p class="note">These colours appear only on the position bars (the stage-3 reading; the stage-1 '
            f'reading where stage 3 is missing). Dashed outline = no reading.</p></div>'
            f'<div class="lgbox"><h4>Shape + colour = event</h4>'
            f'<ul>{shapes}</ul><h4>Marks</h4><ul>{marks}<li>{_legend_glyph("L2", "g-hollow", 3)}number = that many items '
            f'stacked at one spot (click it to list them all)</li></ul><h4>Read as</h4><ul>{examples}</ul>'
            f'<p class="note">Event colours (pink, cyan{", amber" if rem else ""}) are never position colours. '
            f'{esc(CODE_SCHEME)}</p><p class="note">{where}</p></div>'
            + (f'<div class="lgbox"><h4>What it thought: code groups</h4>{groups_html(names, href, defs)}</div>'
               if names or mode == "agent" else "")
            + '</div>' + terms_html() + '</details>')


def legend(mode="agent", names=None, href="taxonomy.html", defs=None, shown_types=()):
    """Both parts of the legend (kept for callers of the earlier single legend)."""
    return legend_bar(mode, shown_types) + legend_about(mode, names, href, defs, shown_types)


def pct(t, tmax):
    """A time as a percentage of the axis (glyphs are placed in % so they keep their pixel size at any width)."""
    return 100.0 * min(max(_num(t), 0.0), tmax) / tmax


def axis_svg(tmax, y, h, labels=True):
    """Gridlines (and their minute labels below) across the plot, in % of its width."""
    out = []
    step = tick_step(tmax)
    t = 0
    while t <= tmax + 1e-9:
        x = pct(t, tmax)
        out.append(f'<line class="tick" x1="{x:.2f}%" x2="{x:.2f}%" y1="{y}" y2="{y + h}"/>')
        if labels:
            anchor = "end" if t + step > tmax + 1e-9 else "start" if t == 0 else "middle"
            out.append(f'<text x="{x:.2f}%" y="{y + h + 13}" text-anchor="{anchor}">{t}</text>')
        t += step
    return "".join(out)


STACK_FRAC = 0.018  # items closer than this share of the axis (about one glyph width on a laptop) are drawn as one stack


def _event_prio(ev):
    e = ev["event"]
    typ = e.get("type")
    return (0 if e.get("fake") else 1 if is_overseer(e) else 2 if typ == "P2" else 3 if typ == "R1"
            else 4 if typ == "S1" else 5 if typ in ("L2", "B1", "D1") else 6)


def stack(entries, tmax):
    """Entries (dicts with t, prio, key, ...) grouped into stacks, in time order: an item joins the current stack when
    it is within STACK_FRAC of the axis (about one glyph width) of the stack's last item and within three times that
    of its first, so a burst becomes one counted stack instead of overlapping neighbours."""
    out, width = [], tmax * STACK_FRAC
    for e in sorted(entries, key=lambda e: (e["t"], e.get("order", 0))):
        if out and e["t"] - out[-1][-1]["t"] <= width and e["t"] - out[-1][0]["t"] <= 3 * width:
            out[-1].append(e)
        else:
            out.append([e])
    return out


def stack_glyph(group, tmax, y, r, href=None):
    """One stack as a glyph: the most telling item's shape (fake, overseer, revert, reminder, submission, read,
    other) with a count badge, selectable as its first item; ``href`` wraps it in a link (index rows)."""
    rep = min(group, key=lambda e: (e["prio"], e["t"]))
    n = len(group)
    p = pct(rep["t"], tmax)
    if n == 1:
        title = rep["title"]
    else:
        title = (f"{n} items stacked here ({fmt_span(group[0]['t'], group[-1]['t'])}):\n"
                 + "\n".join("• " + e["title"].split("\n")[0] for e in group[:14]) + ("\n…" if n > 14 else ""))
    attrs = f' data-p="{p:.2f}"'
    if n > 1 and href is None:
        attrs += (f' data-stack="{esc(" ".join(e["key"] for e in group))}" '
                  f'data-span="{esc(fmt_span(group[0]["t"], group[-1]["t"]))}"')
    g = shape(rep["typ"], 0, 0, r, rep["cls"], None if href else group[0]["key"], title, n, attrs)
    g = f'<svg x="{p:.2f}%" y="{y:.1f}">{g}</svg>'
    return f'<a href="{esc(href)}">{g}</a>' if href else g


# ------------------------------------------------------------------ index.html

INDEX_TYPES = ("L2", "B1", "D1", "P2", "S1")


def stance_title(frm, to, st, label):
    return (f"Position on cheating: {STANCE_LABEL.get(st, st)} · {fmt_span(frm, to)}"
            + (f" · {label}" if label else ""))


# The experimental interaction graph (docs/plan.md, "Story-map interaction graph (November 5, 2026)"): the owner wants
# it discreet, so the index gains only this one small muted link, and only when graph.html exists for the run
# (written next to index.html by scripts/storymap_graph.py). Without it the index is byte-identical to before.
GRAPH_PAGE = "graph.html"
GRAPH_LINK_TEXT = "Interaction graph (experimental) ↗"


def graph_link(on):
    """The index's one small, muted link to graph.html ('' when ``on`` is false)."""
    if not on:
        return ""
    return (f'<p class="muted" style="font-size:.85em;margin:.2em 0 .6em"><a href="{GRAPH_PAGE}" style="color:inherit" '
            f'title="Experimental: agents as nodes and their interactions over run time, coloured by the story map\'s '
            f'readings">{esc(GRAPH_LINK_TEXT)}</a></p>')


def sample_note(record):
    """The index's note on a sampled map (MAP/sample.json from run_storymap --sample; storymap/sample.py): how many
    solvers are mapped and how they were chosen. '' without a sample record, so other indexes are unchanged."""
    if not record:
        return ""
    from .sample import note_text
    return (f'<p class="sample-note" style="border-left:3px solid currentColor;padding:.3em .7em;margin:.4em 0 .8em">'
            f'<strong>{esc(note_text(record))}</strong></p>')


def index_page(agents, pipeline, tmax, nav="", names=None, extra="", css="", graph=False, sample=None):
    """``extra`` / ``css``: the overseer block and its styles (view_overseer.py); empty = the solver page as before.
    ``graph``: add the discreet link to the experimental interaction graph (graph.html)."""
    rows, counts = [], Counter()
    push_seen = False
    sources = sorted({str(((a["stage3"] or {}).get("final") or {}).get("source", "unknown"))
                      if a["stage3"] else "no stage 3" for a in agents.values()})
    for name, a in agents.items():
        f = (a["stage3"] or {}).get("final") or {}
        src = str(f.get("source", "unknown")) if a["stage3"] else "no stage 3"
        counts[src] += 1
        parts = [axis_svg(tmax, 0, 34, labels=False)]
        phases, _ = phases_of(a, tmax)
        for frm, to, st, label in phases:
            cls = st if st in STANCES else "unknown"
            w = max(pct(to, tmax) - pct(frm, tmax), 0.1)
            parts.append(f'<rect class="ph-{cls}" x="{pct(frm, tmax):.2f}%" y="13" width="{w:.2f}%" '
                         f'height="8" rx="2"><title>{esc(stance_title(frm, to, st, label))}</title></rect>')
        above, below = [], []
        for ev in a["events"]:
            e = ev["event"]
            typ = e.get("type")
            if typ not in INDEX_TYPES or (typ in ("B1", "D1") and not (is_overseer(e) or set(e.get("tags") or []) & RELEVANT_TAGS)):
                continue
            push_seen = push_seen or delivery_of(e) in ("push", "notice")
            (below if typ == "S1" else above).append(
                {"t": _num(ev.get("t")), "order": _num(ev.get("i")), "prio": _event_prio(ev), "typ": typ,
                 "cls": glyph_class(ev), "key": f"ev-{ev.get('i')}", "title": event_title(ev)})
        for entries, y in ((above, 6.5), (below, 27.5)):
            for group in stack(entries, tmax):
                parts.append(stack_glyph(group, tmax, y, 4.4, f"agents/{name}.html#sel={group[0]['key']}"))
        data = {"source": src, "faked": _yn(f.get("faked")), "stopped": _yn(f.get("stopped")),
                "credited": _yn(f.get("credited")), "aware": str(f.get("aware_cheating") or "")}
        attrs = " ".join(f'data-{k}="{esc(v)}"' for k, v in data.items())
        story = (a["stage3"] or {}).get("story") or ""
        rows.append(f'<div class="row" data-agent="{esc(name)}" {attrs}>'
                    f'<div class="agent"><a href="agents/{esc(name)}.html" title="{esc(story)}">{esc(name)}</a></div>'
                    f'<div class="strip"><svg>{"".join(parts)}</svg></div>'
                    f'<div class="labels">{final_labels(a["stage3"])}</div></div>')
    header_axis = ("<svg>" + "".join(
        f'<text x="{pct(t, tmax):.2f}%" y="12" text-anchor="{"start" if t == 0 else "end" if t + tick_step(tmax) > tmax else "middle"}">{t}</text>'
        for t in range(0, int(tmax) + 1, tick_step(tmax))) + "</svg>")

    def sel(name, label, options, hint):
        opts = "".join(f'<option value="{esc(v)}">{esc(t)}</option>' for v, t in options)
        return (f'<label title="{esc(hint)}">{esc(label)} <select name="{name}"><option value="">Any</option>{opts}'
                f'</select></label>')
    yn = [("yes", "Yes"), ("no", "No")]
    filters = ('<div class="filters">'
               + sel("source", SOURCE_TITLE, [(s, source_label(s)) for s in sources], SOURCE_HINT)
               + sel("faked", "Faked", yn, "Submitted at least one fake")
               + sel("credited", "Got credit for a fake", yn, "At least one fake was accepted with credit")
               + sel("stopped", "Stopped faking", yn, "Faked, then decided to stop, with no later fake")
               + sel("aware", "Knew it was cheating", [("yes", "Yes"), ("no", "No"), ("unclear", "Unclear")], AWARE_HINT)
               + '<button type="button" id="reset">Reset</button> <span id="shown" class="muted"></span></div>')
    summary = "".join(tbadge(f"{source_label(k)}: {v}", SOURCE_HINT) for k, v in sorted(counts.items()))
    run = pipeline.get("run_id") or ""
    body = (f"{nav}<h1>Story map{(': ' + esc(run)) if run else ''}</h1>{graph_link(graph)}{sample_note(sample)}"
            f'{banner(model_names(pipeline))}'
            f"{legend_bar('index', push=push_seen)}"
            f'<p class="muted">{len(agents)} agents. {SOURCE_TITLE}: {summary}. Time axis: minutes from run start '
            f'(0–{int(tmax)}). A number on a shape counts the items stacked there.</p>{extra}{filters}'
            f'<div class="grid"><div class="hdr"><div>Agent</div><div>{header_axis}</div>'
            f'<div class="lab">Final reading (stage 3)</div></div>'
            f'{"".join(rows) or "<div class=empty>No agents found.</div>"}</div>'
            f"{legend_about('index', names, 'taxonomy.html' if names else '', DEFS, push=push_seen)}")
    return page(f"Story map {run}".strip(), body, LEGEND_JS + FILTER_JS, css=css)


def overseer_only_index(ov, tmax, nav, graph=False):
    """index.html of a run whose overseers are mapped but whose solvers are not (no solver story map was made): the
    overseer strip and run panel only (storymap/view_overseer.py)."""
    run = (ov.pipeline or {}).get("run_id") or ""
    block, css = ov.index_block()
    body = (f"{nav}<h1>Story map{(': ' + esc(run)) if run else ''}</h1>{graph_link(graph)}"
            f"{banner(model_names(ov.pipeline))}"
            f'<p class="muted">Overseers only: no solver story map was made for this run, so solvers appear only in '
            f'the run-level fake counts below. Time axis: minutes from run start (0–{int(tmax)}).</p>{block}')
    return page(f"Story map {run}".strip(), body, css=css)


def _yn(v):
    return "yes" if v is True else "no" if v is False else ""


# ------------------------------------------------------------------ agents/<agent>.html

def code_name(code, names, plain):
    """'E1 Registered' when the taxonomy names are loaded, else the plain label (never a bare code)."""
    return f"{code} {names[code]}" if names.get(code) else plain


def code_link(code, names, href):
    """A thought or reaction code linked to its definition on the taxonomy page (plain text without names)."""
    label = code_name(code, names, code)
    return f'<a href="{esc(href)}#code-{esc(code)}">{esc(label)}</a>' if names.get(code) and href else esc(label)


REACT_HINT = {"E1": "Whether the agent's following reasoning or acts refer to the event",
              "E2": "No change, a changed belief, or a changed plan or act", "E3": "Toward or away from cheating",
              "E4": "The agent's own stated reason", "E5": "Whether the agent's own words name the event as a reason"}
REACT_PLAIN = {"E1": "Noticed it", "E2": "Change", "E3": "Direction", "E4": "Why", "E5": "Agent names it as a reason"}
F_PLAIN = {k: f"overseer check {k[1]}" for k in ("F1", "F2", "F3", "F4", "F5")}


def reaction_html(r, names=None, href=""):
    names = names or {}
    if not r:
        return '<p class="muted">No stage-2 reaction recorded for this event.</p>'
    ch, dr = r.get("change"), r.get("direction")
    tone = "bad" if dr == "toward_cheating" and ch != "none" else "good" if dr == "away" and ch != "none" else ""
    def e(c):  # 'E2 Change' (the name up to its colon) or the plain label
        return f"{c} {names[c].split(':')[0]}" if names.get(c) else REACT_PLAIN[c]

    def tip(c):
        return code_tip(c, DEFS, names, False) if DEFS.get(c) else REACT_HINT[c]
    head = (tbadge(f"{e('E1')}: {r.get('registered')}", tip("E1"))
            + tbadge(f"{e('E2')}: {CHANGE_LABEL.get(ch, ch)}", tip("E2"), tone)
            + tbadge(f"{e('E3')}: {DIRECTION_LABEL.get(dr, str(dr))}", tip("E3"), tone)
            + tbadge(f"{e('E5')}: {yesno(bool(r.get('self_attributed')))}", tip("E5"),
                     "caution" if r.get("self_attributed") else "")
            + tbadge(f"reader certainty: {r.get('certainty')}", "How sure the stage-2 reader was: high, medium or low"))
    ov = r.get("overseer")
    ov_html = ""
    if isinstance(ov, dict):
        bits = "".join(tbadge(f"{code_name(k, names, F_PLAIN.get(k, k))}: "
                              f"{yesno(v) if isinstance(v, bool) else str(v).replace('_', ' ')}",
                              code_tip(k, DEFS, names, False) if DEFS.get(k) else "A stage-2 reading of how the agent "
                              "took an overseer's post, DM or revert")
                       for k, v in ov.items() if v is not None)
        ov_html = f"<p>Overseer readings: {bits}</p>" if bits else ""
    quotes = "".join(f'<blockquote>{esc(q.get("quote"))} <small>(timeline item {esc(q.get("item"))})</small></blockquote>'
                     for q in r.get("quotes") or [] if isinstance(q, dict))
    return (f'<div class="react"><p><b>How it reacted (stage 2)</b> {head}</p>'
            + (f"<p>{esc(e('E4'))}: {esc(plain_ids(r.get('reason')))}</p>" if r.get("reason") else "")
            + (f"<p>What it did next: {esc(plain_ids(r.get('next_action')))}</p>" if r.get("next_action") else "")
            + ov_html + quotes + "</div>")


def cause_link(eid, key_of_event):
    """A turning point's cause as a plain label linking to the event on this page (when it is in the timeline)."""
    key = key_of_event.get(str(eid))
    label = esc(event_label(eid))
    return f'<a href="#sel={esc(key)}" data-k="{esc(key)}">{label}</a>' if key else label


def _who(e):
    return f"{e['author']}" + (f" ({e['author_role']})" if e.get("author_role") else "")


# The evidence chain: the agent's own timeline is embedded once per agent page (capped per item; the agent's
# prompts, the "context" items, are left out) and each thought or event opens the turns around it.
CHAIN_CAPS = {"reasoning": 8000, "text": 4000, "tool_call": 1500, "tool_result": 1200, "event": 500}
EVIDENCE = {"on": True}  # set by build(evidence=...); off renders the pages without the embedded timeline


# Redaction of the embedded chain text (tool results can hold an `env` dump, host paths or file paths): the same
# credential shapes and host paths that scripts/storymap_site.py refuses on a published page, environment values
# of secret-looking variables, and multi-segment paths into a private/ directory. Redacted spans say so.
SECRET_TEXT = re.compile(r"sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{10,}|hf_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}"
                         r"|AIza[0-9A-Za-z_-]{35}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}"
                         r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRET_VAR = re.compile(r"\b([A-Z][A-Z0-9_]*(?:TOKEN|API_KEY|SECRET|PASSWORD|_KEY|AUTH))=([^\s\"']+)")
HOST_PATH_TEXT = re.compile(r"/(?:Users|private/tmp|private/var|var/folders)/[^\s\"'`]*")
PRIVATE_DIR_TEXT = re.compile(r"(?<![A-Za-z0-9_./-])private/[A-Za-z0-9_.-]+/[^\s\"'`]*")


def redact(text):
    """Chain text safe to embed: credential-shaped strings, secret-looking variables' values, host paths and paths
    into a private/ directory replaced by a bracketed note (the rest of the agent's words unchanged)."""
    text = SECRET_TEXT.sub("[redacted: credential-shaped string]", text)
    text = SECRET_VAR.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = HOST_PATH_TEXT.sub("[redacted: host path]", text)
    return PRIVATE_DIR_TEXT.sub("[redacted: path]", text)


def timeline_json(a):
    """The agent's timeline as compact JSON for the reasoning chains: [i, t, turn, kind, tool, text, cut chars]."""
    rows = []
    for it in sorted(a["items"], key=lambda it: _num(it.get("i"))):
        kind = it.get("kind")
        if kind not in CHAIN_CAPS or it.get("turn") is None or it.get("i") is None:
            continue
        text = str(it.get("text") or "")
        if kind == "event" and isinstance(it.get("event"), dict):
            ev = {"event": it["event"], "t": it.get("t")}
            body = tidy_times(text) if it["event"].get("type") in ("L2", "S1", "P2", "L1", "P1") else text
            text = event_title(ev) + (": " + body if body else "")
        cap = CHAIN_CAPS[kind]
        cut = max(0, len(text) - cap)
        text = redact(text[:cap])
        rows.append([int(_num(it["i"])), round(_num(it.get("t")), 1), int(_num(it["turn"])), kind,
                     str(it.get("tool") or "") if kind != "event" else "", text, cut])
    data = json.dumps({"items": rows}, ensure_ascii=False, separators=(",", ":"))
    # Inert inside <script type="application/json">: no markup characters, quotes or slashes appear raw (standard
    # JSON escapes), so the text can neither close the script nor read as a link or path to the page screens.
    for ch, rep in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), ("'", "\\u0027"), ("/", "\\/")):
        data = data.replace(ch, rep)
    return data


def evidence_refs(th):
    """(item indices, [(item, start, end)]) from a thought's evidence references when the map has them (any of
    ``evidence`` / ``evidence_refs`` / ``refs``: dicts with ``item`` (or ``item_i``) and optional ``start``/``end``,
    ``span`` [start, end] or ``char_start``/``char_end``); empty for older maps."""
    items, spans = set(), []
    chain = th.get("evidence_chain")
    if isinstance(chain, dict):  # the evidence file's record (storymap/evidence.py): quote_span and the cited item
        qs = chain.get("quote_span") if isinstance(chain.get("quote_span"), dict) else {}
        refs = [dict(qs)] if qs.get("item") is not None else []
        if chain.get("item") is not None:
            refs.append({"item": chain["item"]})
    else:
        refs = th.get("evidence") or th.get("evidence_refs") or th.get("refs") or []
    for r in refs if isinstance(refs, list) else [refs]:
        if not isinstance(r, dict):
            continue
        i = r.get("item", r.get("item_i"))
        if i is None:
            continue
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        items.add(i)
        span = r.get("span") if isinstance(r.get("span"), (list, tuple)) and len(r.get("span")) == 2 else None
        a = span[0] if span else r.get("start", r.get("char_start"))
        b = span[1] if span else r.get("end", r.get("char_end"))
        if isinstance(a, int) and isinstance(b, int) and 0 <= a < b:
            spans.append((i, a, b))
    return items, spans


def chain_box(turn, cite, label, before=1, after=0, quote="", spans=()):
    """The expand/collapse reasoning chain of one thought or event (rendered by CHAIN_JS from the embedded
    timeline); empty when the page has no timeline or the item has no turn."""
    if not EVIDENCE["on"] or turn is None:
        return ""
    try:
        turn = int(_num(turn, -1))
    except (TypeError, ValueError):
        return ""
    if turn < 0:
        return ""
    cite_s = " ".join(str(int(_num(c))) for c in sorted({c for c in cite if c is not None}, key=_num))
    sp = ";".join(f"{i}:{a}:{b}" for i, a, b in spans)
    return (f'<div class="chain" data-lo0="{max(turn - before, 0)}" data-hi0="{turn + after}" data-cite="{cite_s}"'
            + (f' data-q="{esc(quote)}"' if quote else "") + (f' data-spans="{sp}"' if sp else "")
            + f'><button type="button" data-act="open">{esc(label)}</button><div class="cbody"></div></div>')


def event_article(name, ev, reactions, key, names=None, href="", together=0):
    e = ev["event"]
    typ = e.get("type")
    d = e.get("detail") or {}
    meta = [badge(type_name(typ))]
    if e.get("author") and typ != "S1":
        meta.append(badge(("by " if typ != "P2" else "reverted by ") + _who(e), "info" if is_overseer(e) else ""))
    if e.get("fake") is True:
        meta.append(badge("fake", "bad"))
    elif e.get("fake") is False:
        meta.append(badge("not fake", "good"))
    if e.get("outcome"):
        meta.append(badge(OUTCOME_LABEL.get(str(e["outcome"]), str(e["outcome"]).replace("_", " "))))
    meta += [badge(TAG_LABEL.get(t, str(t).replace("_", " ")), "caution") for t in e.get("tags") or []]
    if d.get("reread"):
        meta.append(badge("re-read"))
    dl = delivery_of(e)
    if dl:
        meta.append(tbadge(DELIVERY_LABEL[dl], DELIVERY_HINT[dl]))
    size = d.get("delivery_size") if isinstance(d.get("delivery_size"), int) else together + 1 if together else 0
    if size and size > 1:
        pos_ = f"item {d['position'] + 1} of {size}" if isinstance(d.get("position"), int) else f"{size} items"
        meta.append(tbadge(f"delivered together: {pos_}", "Pushed into its context in one delivery with the other "
                           "items listed under \u201cdelivered with\u201d (same time, same turn)"))
    if d.get("answered") is False:
        meta.append(tbadge("run ended before its next step", "No model step after this delivery was recorded"))
    kv = "".join(f"<dt>{esc(human(k))}</dt><dd>{esc(_shown(k, v))}</dd>"
                 for k, v in d.items() if v not in (None, "", [], {}) and k not in DELIVERY_HIDDEN)
    if size and size > 1 and isinstance(d.get("delivery_items"), list):
        kv += f"<dt>Delivered with</dt><dd>{esc(', '.join(event_label(x) for x in d['delivery_items']))}</dd>"
    k = "k-fake" if (e.get("fake") or typ == "P2") else "k-ov" if is_overseer(e) else ""
    kind = "sub" if typ == "S1" else "own" if typ in ("O1", "C1") else "exp"
    reacts = "".join(reaction_html(r, names, href) for r in reactions) if reactions else (
        reaction_html(None) if typ in ("L2", "B1", "D1", "P2", "R1") else "")
    head = event_label(e) if typ in ("L2", "B1", "D1", "S1", "P2", "O1") else type_name(typ)
    head = head[:1].upper() + head[1:]
    text = ev.get("text")
    if typ in ("L2", "S1", "P2", "L1", "P1"):
        text = tidy_times(text)  # extractor summaries (B1, D1, O1, C1, N1 and R1 texts are what the agent saw)
    icon = f'<svg class="hglyph" viewBox="-9 -9 18 18">{shape(typ, 0, 0, 5.2, glyph_class(ev), None, "")}</svg>'
    cite = [ev.get("i")] + [q.get("item") for r in reactions for q in r.get("quotes") or [] if isinstance(q, dict)]
    chain = chain_box(ev.get("turn"), cite, "Show what surrounded it: the agent's reasoning and tool calls",
                      before=0, after=1)
    return (f'<article class="item {k}" id="{key}" data-kind="{kind}" data-t="{_num(ev.get("t")):.1f}" '
            f'data-label="{esc(fmt_t(ev.get("t")) + " · " + head[:110])}">'
            f'<h3>{icon}{fmt_t(ev.get("t"))} &middot; {esc(head)}</h3>'
            f'<p>{"".join(meta)} <small>turn {esc(ev.get("turn"))} &middot; timeline item {esc(ev.get("i"))}</small></p>'
            f'<pre>{esc(cut(text))}</pre>{"<dl class=kv>" + kv + "</dl>" if kv else ""}{reacts}{chain}</article>')


DELIVERY_LABEL = {"push": "delivered unasked", "notice": "notice in its own tool result",
                  "pull": "read on request"}
DELIVERY_HINT = {"push": "The host appended it to the agent's context without the agent asking (push delivery)",
                 "notice": "The host added it to the result of one of the agent's own tool calls (it did not ask for it)",
                 "pull": "The agent called the tool that returned it"}
DELIVERY_HIDDEN = {"reread", "delivery", "delivery_id", "delivery_size", "delivery_items", "position", "answered",
                   "placement", "anchor_tool_use_id", "request_seq"}  # shown as badges, or bookkeeping


def js_span(item, start, end):
    """(item, start, end) in the browser's UTF-16 offsets of the embedded (capped, redacted) text, or None when the
    span is past the cap or redaction changed the text before its end (the page then falls back to the quote)."""
    if not isinstance(item, dict) or item.get("kind") not in CHAIN_CAPS:
        return None
    text = str(item.get("text") or "")
    cap = CHAIN_CAPS[item["kind"]]
    if end > min(len(text), cap) or redact(text[:end]) != text[:end]:
        return None
    u16 = lambda n: len(text[:n].encode("utf-16-le")) // 2  # noqa: E731
    return (item.get("i"), u16(start), u16(end))


def thought_article(th, names, related, key, items_by_i, href=""):
    code = str(th.get("code") or "?")
    group = code[:1]
    src = items_by_i.get(th.get("item"))
    ctx = ""
    if src is not None and src.get("text") and str(th.get("quote") or "") not in str(src.get("text")):
        ctx = '<p class="muted">Quote not found verbatim in the timeline item.</p>'
    rel = "".join(f'<p class="muted">Quoted as evidence in the stage-2 reaction to <a href="#sel={esc(k)}" '
                  f'data-k="{esc(k)}">{esc(event_label(eid))}</a>:</p>{reaction_html(r, names, href)}'
                  for k, eid, r in related)
    low = th.get("certainty") == "low"
    grp = (f' <small>(group {code_link(group, names, href)})</small>' if names.get(group)
           else f" <small>(group {esc(group)})</small>")
    icon = (f'<svg class="hglyph" viewBox="-9 -9 18 18">'
            f'{shape("TH", 0, 0, 6, "g-th-low" if low else "g-th", None, "")}</svg>')
    items, spans = evidence_refs(th)
    spans = [sp for sp in (js_span(items_by_i.get(i), a, b) for i, a, b in spans) if sp]
    turns = (th.get("evidence_chain") or {}).get("chain_turns")
    if isinstance(turns, list) and len(turns) == 2 and all(isinstance(x, int) for x in turns):
        lo, hi = turns  # the evidence file's chain: the previous model step and the thought's own
        chain = chain_box(hi, [th.get("item"), *items], "Show the full reasoning chain behind this thought",
                          before=max(hi - lo, 0), after=0, quote=str(th.get("quote") or ""), spans=spans)
    else:
        chain = chain_box(th.get("turn"), [th.get("item"), *items], "Show the full reasoning chain behind this thought",
                          before=1, after=0, quote=str(th.get("quote") or ""), spans=spans)
    label = f"{fmt_t(th.get('t'))} · thought {code_name(code, names, code)}"
    return (f'<article class="item k-th{" g-th-low" if low else ""}" id="{key}" data-kind="th" '
            f'data-t="{_num(th.get("t")):.1f}" data-label="{esc(label)}">'
            f'<h3>{icon}{fmt_t(th.get("t"))} &middot; thought {code_link(code, names, href)}{grp}</h3>'
            f'<p>{badge("reader certainty: " + str(th.get("certainty")))} <small>turn {esc(th.get("turn"))} &middot; '
            f'timeline item {esc(th.get("item"))} &middot; stage-1 chunk {esc(th.get("chunk"))}</small></p>'
            + (f'<p class="def">{esc(code_tip(code, DEFS, names))}</p>' if DEFS.get(code) else "")
            + f'<blockquote>{esc(th.get("quote"))}</blockquote>'
            f'{"<p>Reader note: " + esc(th.get("note")) + "</p>" if th.get("note") else ""}{ctx}{chain}{rel}</article>')


PANEL = ('<aside id="panel" aria-label="Detail"><div class="stepper"><button type="button" id="pprev" '
         'title="Previous (← or k)">← Prev</button><span id="pos" aria-live="polite"></span>'
         '<button type="button" id="pnext" title="Next (→ or j)">Next →</button>'
         '<label>Step through <select id="kind"><option value="">all items</option>'
         '<option value="exp">only what it read or saw</option><option value="th">only thoughts</option>'
         '<option value="sub">only submissions</option><option value="own">only own posts and restarts</option>'
         '<option value="tp">only turning points</option></select></label>'
         '<button type="button" id="pclose" title="Close (Esc)" aria-label="Close">×</button></div>'
         '<div id="pbody"></div><p class="muted keys">→ / j next, ← / k previous, Esc closes.</p></aside>')


def thought_lane_label(L, names):
    """(full, short, hint) of a thought lane: the group letter and its name (the one code scheme: letters are
    code groups only)."""
    nm = names.get(L)
    codes = sorted((c for c in names if c[:1] == L and len(c) > 1), key=lambda c: int(c[1:]))
    hint = (f"Thought codes {L}" + (f" ({nm})" if nm else "") + ", quoted from the agent's reasoning and coded by the "
            "stage-1 reader; hollow tick = low reader certainty" + "".join(f"\n{c} {names[c]}" for c in codes))
    return (nm or "thoughts"), (nm.split()[0] if nm else "thoughts"), hint


def agent_page(a, names, tmax, all_names, models="", nav="", href=""):
    name = a["name"]
    s3 = a["stage3"] or {}
    LH, R = LANE_PX, 6.5
    types_here = {ev["event"].get("type") for ev in a["events"]}
    ev_lanes = [ln for ln in LANES if ln[0] != "rem" or "R1" in types_here]
    lanes = ([("stance", "", POSITION, "Position", POSITION_HINT)]
             + [(f"th-{L}", L) + thought_lane_label(L, names) for L in LETTERS]
             + [(k, "", label, short, hint) for k, label, short, _, hint in ev_lanes])
    ypos = {ln[0]: LANE_TOP + n * LH for n, ln in enumerate(lanes)}
    bottom = LANE_TOP + len(lanes) * LH
    H = bottom + 18
    svg, labels = [], []
    for n, (k, letter, label, short, hint) in enumerate(lanes):
        y = ypos[k]
        if k != "stance":  # the stance bars sit on the card surface their colours were validated against
            svg.append(f'<rect class="lane-bg{" alt" if n % 2 else ""}" x="0" y="{y}" width="100%" height="{LH}"/>')
        lt = f"<b>{letter}</b>" if letter else ""
        labels.append(f'<div class="ll" title="{esc((letter + " " if letter else "") + label + ": " + hint)}">{lt}'
                      f'<span class="lf">{esc(label)}</span><span class="ls">{esc(short)}</span></div>')
    svg.append(axis_svg(tmax, LANE_TOP, len(lanes) * LH))
    svg.append(f'<line id="cursor" class="cursor" x1="0" x2="0" y1="{LANE_TOP}" y2="{bottom}" visibility="hidden"/>')

    articles = []  # (t, order, html)
    key_of_event = {}
    for ev in a["events"]:
        key_of_event.setdefault(str(ev["event"].get("id")), f"ev-{ev.get('i')}")
    # Stance lane.
    phases, phase_src = phases_of(a, tmax)
    y = ypos["stance"]
    for frm, to, st, label in phases:
        cls = st if st in STANCES else "unknown"
        w = max(pct(to, tmax) - pct(frm, tmax), 0.1)
        svg.append(f'<rect class="ph-{cls}" x="{pct(frm, tmax):.2f}%" y="{y + 5}" width="{w:.2f}%" '
                   f'height="{LH - 10}" rx="2"><title>{esc(stance_title(frm, to, st, label))}</title></rect>')
    for n, tp in enumerate(s3.get("turning_points") or []):
        key = f"tp-{n}"
        x = pct(tp.get("t"), tmax)
        tip = (f"Turning point · {fmt_t(tp.get('t'))} · position on cheating "
               f"{STANCE_LABEL.get(tp.get('from'), tp.get('from'))} → {STANCE_LABEL.get(tp.get('to'), tp.get('to'))}")
        svg.append(f'<line class="tp" data-k="{key}" data-p="{x:.2f}" x1="{x:.2f}%" x2="{x:.2f}%" y1="{y}" '
                   f'y2="{bottom}"><title>{esc(tip)}</title></line>')
        causes = ", ".join(cause_link(c, key_of_event) for c in tp.get("cause_event_ids") or []) or "none named"
        head = (f'turning point: position on cheating {STANCE_LABEL.get(tp.get("from"), tp.get("from"))} → '
                f'{STANCE_LABEL.get(tp.get("to"), tp.get("to"))}')
        articles.append((_num(tp.get("t")), 0, f'<article class="item k-tp" id="{key}" data-kind="tp" '
                         f'data-t="{_num(tp.get("t")):.1f}" data-label="{esc(fmt_t(tp.get("t")) + " · " + head)}">'
                         f'<h3><svg class="hglyph" viewBox="0 0 18 16"><line class="tp" x1="9" x2="9" y1="0" y2="16"/>'
                         f'</svg>{fmt_t(tp.get("t"))} &middot; {esc(head)}</h3>'
                         f'<p>Caused by (stage-3 reading): {causes}</p><p>{esc(plain_ids(tp.get("cause_note")))}</p></article>'))

    # Event lanes and reactions.
    cited = defaultdict(list)  # timeline item index -> [(event key, event id, reaction)]
    for eid, rs in a["by_event"].items():
        for r in rs:
            for q in r.get("quotes") or []:
                if isinstance(q, dict) and q.get("item") is not None:
                    cited[q["item"]].append((key_of_event.get(eid, ""), eid, r))
    lane_of = {t: k for k, _, _, types, _ in ev_lanes for t in types}
    deliveries = Counter(delivery_id(ev["event"]) for ev in a["events"] if delivery_id(ev["event"]))
    per_lane = defaultdict(list)
    for ev in a["events"]:
        e = ev["event"]
        key = f"ev-{ev.get('i')}"
        per_lane[lane_of.get(e.get("type"), "own")].append(
            {"t": _num(ev.get("t")), "order": _num(ev.get("i")), "prio": _event_prio(ev), "typ": e.get("type"),
             "cls": glyph_class(ev), "key": key, "title": event_title(ev)})
        together = deliveries.get(delivery_id(e), 1) - 1 if delivery_id(e) else 0
        articles.append((_num(ev.get("t")), 1, event_article(name, ev, a["by_event"].get(str(e.get("id")), []), key,
                                                             names, href, together)))
    seen_ids = set(key_of_event)
    orphans = [r for eid, rs in a["by_event"].items() if eid not in seen_ids for r in rs]

    # Thought lanes.
    items_by_i = {it.get("i"): it for it in a["items"]}
    for n, th in enumerate(a["thoughts"]):
        key = f"th-{n}"
        L = str(th.get("code") or "?")[:1].upper()
        lane = f"th-{L}" if L in LETTERS else "th-G"
        low = th.get("certainty") == "low"
        per_lane[lane].append(
            {"t": _num(th.get("t")), "order": _num(th.get("item")), "typ": "TH", "key": key,
             "prio": {"high": 0, "medium": 1}.get(th.get("certainty"), 2), "cls": "g-th-low" if low else "g-th",
             "title": (f"Thought {code_name(str(th.get('code')), names, str(th.get('code')))} · "
                       f"{fmt_t(th.get('t'))} · “{str(th.get('quote') or '')[:120]}”"
                       + (f"\n\n{code_tip(str(th.get('code')), DEFS, names)}" if DEFS.get(str(th.get('code'))) else ""))})
        related = [(k, eid, r) for k, eid, r in cited.get(th.get("item"), [])]
        ech = (a.get("evidence") or {}).get(th.get("ekey"))
        if ech and ech.get("item") == th.get("item"):
            th = dict(th, evidence_chain=ech)
        articles.append((_num(th.get("t")), 2, thought_article(th, names, related, key, items_by_i, href)))
    n_stacks = 0
    for lane, entries in per_lane.items():
        if lane not in ypos:
            continue
        for group in stack(entries, tmax):
            n_stacks += len(group) > 1
            svg.append(stack_glyph(group, tmax, ypos[lane] + LH / 2, 7 if group[0]["typ"] == "TH" else R))

    articles.sort(key=lambda x: (x[0], x[1]))
    f = s3.get("final") or {}
    final_kv = "".join(f"<dt>{esc(FINAL_LABEL.get(k, human(k)))}</dt><dd>{esc(_shown(k, v))}</dd>" for k, v in f.items())
    flags = "".join(f"<li>{esc(plain_ids(x))}</li>" for x in s3.get("consistency_flags") or [])

    def state_row(c, ch):
        s = ch.get("state") or {}
        span = (fmt_span(ch.get("from_t"), ch.get("to_t")) if ch.get("from_t") is not None and ch.get("to_t") is not None
                else "")
        return (f"<tr><td class=n>{c}</td><td>{esc(span)}</td><td>{esc(STANCE_LABEL.get(s.get('stance'), s.get('stance')))}</td>"
                f"<td>{esc(yesno(s.get('has_trick')))}</td><td>{esc(BELIEVES_LABEL.get(s.get('believes_judge'), s.get('believes_judge')))}</td>"
                f"<td>{esc(s.get('summary'))}</td></tr>")
    states = "".join(state_row(c, ch) for c, ch in enumerate(a["chunks"]))
    missing = [s for s, ok in (("the agent's timeline", a["items"]), ("stage-1 thoughts", a["chunks"]),
                               ("stage-2 reactions", a["reactions"]), ("a stage-3 summary", a["stage3"])) if not ok]
    idx = list(all_names)
    pos = idx.index(name)
    prev_link = f'<a href="{esc(idx[pos - 1])}.html">&larr; {esc(idx[pos - 1])}</a>' if pos > 0 else ""
    next_link = f'<a href="{esc(idx[pos + 1])}.html">{esc(idx[pos + 1])} &rarr;</a>' if pos + 1 < len(idx) else ""
    info = a["index"] or {}
    push = any(delivery_of(ev["event"]) in ("push", "notice") for ev in a["events"])
    has_chain = EVIDENCE["on"] and bool(a["items"])
    tl_json = f'<script type="application/json" id="tl">{timeline_json(a)}</script>' if has_chain else ""
    shown = {"R1"} if "R1" in types_here else set()
    # Timeline (the main view) with the detail column fixed beside it; story, items and readings below.
    body = (nav.replace("</nav>", f'<span class="sp"></span>{prev_link}{next_link}</nav>')
            + f'<header class="ah"><h1>{esc(name)}</h1> {final_labels(a["stage3"])}</header>{banner_line(models)}'
            f'{legend_bar("agent", shown, push)}<template id="pempty"><div class="lgbox pkey"><h4>What it thought: '
            f'code groups</h4>{groups_html(names, href, DEFS)}</div></template>'
            f'<div class="layout"><div class="main"><div class="tl"><div class="tl-labels">{"".join(labels)}</div>'
            f'<div class="tl-plot"><svg class="plot" height="{H}">{"".join(svg)}</svg></div></div>'
            f'<p class="tlcap">Minutes from run start (0–{int(tmax)}). '
            + (f"{plural(n_stacks, 'spot holds', 'spots hold')} several items (the number on the shape); "
               f"click one to list them. " if n_stacks else "")
            + 'Click a shape, or step with ← →.</p>'
            f'<h2>Summary (stage 3)</h2><p class="story">{esc(s3.get("story")) or "<span class=muted>No model-written summary for this agent.</span>"}</p>'
            + (f'<p class="muted">Not available for this agent: {", ".join(missing)}.</p>' if missing else "")
            + (f'<p class="muted">{len(a["malformed"])} model output(s) could not be read and are left out '
               f'({esc(", ".join(a["malformed"]))}).</p>' if a["malformed"] else "")
            + f'<p class="muted">{plural(len(a["events"]), "event")}, {plural(len(a["thoughts"]), "coded thought")}, '
            f'{plural(len(a["reactions"]), "stage-2 reaction")}. Position on cheating from the {esc(phase_src)}.'
            f'{" Role: " + esc(info.get("role")) + "." if info.get("role") else ""}</p>'
            f'<details class="all"><summary>All {plural(len(articles), "item")} in time order</summary><div id="items">'
            f'{"".join(h for _, _, h in articles) or "<div class=empty>Nothing recorded.</div>"}</div></details>'
            + ("<h2>Reactions to events not in the timeline</h2>" + "".join(reaction_html(r, names, href) for r in orphans)
               if orphans else "")
            + f'<h2>Final reading (stage 3)</h2>{"<dl class=kv>" + final_kv + "</dl>" if final_kv else "<p class=muted>No final reading.</p>"}'
            + (f'<h3>Consistency flags</h3><p class="muted">{esc(FLAG_HINT)}</p><ul>{flags}</ul>' if flags else "")
            + (f'<details class="more"><summary>Stage-1 reading, chunk by chunk</summary><p class="muted">The state the '
               f'stage-1 reader recorded at the end of each chunk of the timeline.</p><table><tr><th>Chunk</th><th>Span</th>'
               f"<th>Position on cheating</th><th>Knows the trick</th><th>Thinks the judge</th><th>Reader's summary</th>"
               f"</tr>{states}</table></details>" if states else "")
            + f'{legend_about("agent", names, href, DEFS, shown, push)}</div>{PANEL}</div>{tl_json}')
    return page(f"Story map {name}", body, LEGEND_JS + PANEL_JS + (CHAIN_JS if has_chain else ""), "agent")


# ------------------------------------------------------------------ events.html

def _group_key(e, ordinal=None):
    """Library files by path, integrity reminders by delivery number (``ordinal``: the agent's k-th reminder, so
    each group holds every solver's k-th), anything else by event id."""
    typ, eid, d = e.get("type"), str(e.get("id") or ""), e.get("detail") or {}
    if typ == "R1":
        return f"R1:no-{ordinal or 1}"
    if typ == "L2":
        path = d.get("path") or eid.split(":", 1)[-1].rsplit("@", 1)[0]
        return f"L2:{path}"
    return eid


EV_SECTIONS = (("ov", "Overseer posts and DMs"), ("board", "Other posts and DMs"),
               ("revert", "Reverts of own credit"), ("lib", "Library files"), ("rem", "Integrity reminders"))
OPTIONAL_SECTIONS = {"rem"}  # listed only when the run has them (only runs with the periodic integrity reminder)
EV_SECTION_HINT = {"ov": "Board posts and DMs written by an overseer",
                   "board": "Solvers' board posts and DMs that describe the trick, warn or rule about it, or drew a "
                            "stage-2 reaction",
                   "revert": "An agent seeing that an overseer removed its own credit",
                   "lib": "Shared-library files the agents read (all revisions of one path together)",
                   "rem": "The host's periodic integrity reminder, sent to every solver on a timer; grouped by "
                          "delivery number (each solver's first, second, ... reminder)"}


def _event_groups(agents):
    groups, skipped = {}, Counter()
    for name, a in agents.items():
        reminders = 0
        for ev in a["events"]:
            e = ev["event"]
            typ = e.get("type")
            if typ not in ("L2", "B1", "D1", "P2", "R1"):
                continue
            if typ == "R1":
                reminders += 1
            reacts = a["by_event"].get(str(e.get("id")), [])
            if typ in ("B1", "D1") and not (is_overseer(e) or set(e.get("tags") or []) & RELEVANT_TAGS or reacts):
                skipped[typ] += 1
                continue
            g = groups.setdefault(_group_key(e, reminders), {"type": typ, "ordinal": reminders, "first": ev,
                                                             "readers": {}, "fake": e.get("fake"),
                                                             "overseer": is_overseer(e), "ids": set()})
            g["ids"].add(str(e.get("id")))
            rd = g["readers"].setdefault(name, {"t": ev.get("t"), "key": f"ev-{ev.get('i')}", "n": 0, "reacts": []})
            rd["n"] += 1
            if not rd["reacts"]:
                rd["reacts"] = list(reacts)
    return groups, skipped


def reaction_counts(g):
    """Counts over the readers' first stage-2 reactions: registered (yes / no / unclear), change, direction (of a
    change), and readers without a reaction."""
    c = Counter()
    for rd in g["readers"].values():
        r = rd["reacts"][0] if rd["reacts"] else None
        if r is None:
            c["unjudged"] += 1
            continue
        c["reg-" + str(r.get("registered"))] += 1
        ch = r.get("change") if r.get("change") in CHANGES else "none"
        c["chg-" + ch] += 1
        if ch != "none":
            c["dir-" + str(r.get("direction") or "none")] += 1
    return c


def _sec_of(g):
    return ("revert" if g["type"] == "P2" else "lib" if g["type"] == "L2" else "rem" if g["type"] == "R1"
            else "ov" if g["overseer"] else "board")


def group_title(g):
    """A group's plain title: the event's label, or for integrity reminders the delivery number."""
    if g["type"] == "R1":
        return f"integrity reminder no. {g.get('ordinal') or 1} (each solver's {_ordinal(g.get('ordinal') or 1)})"
    return event_label(g["first"]["event"])


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def events_page(agents, tmax, models="", nav="", names=None):
    groups, skipped = _event_groups(agents)
    order = sorted(groups.items(), key=lambda kv: ([k for k, _ in EV_SECTIONS].index(_sec_of(kv[1])),
                                                   -len(kv[1]["readers"]), _num(kv[1]["first"].get("t"))))
    cards, rows = [], []
    for gk, g in order:
        cards.append(event_section(gk, g))
        rows.append(event_row(gk, g))
    count = Counter(_sec_of(g) for _, g in order)
    chips = "".join(f'<label class="chip" title="{esc(EV_SECTION_HINT[k])}"><input type="checkbox" name="sec" '
                    f'value="{k}" checked> {esc(lab)} ({count[k]})</label>' for k, lab in EV_SECTIONS
                    if k not in OPTIONAL_SECTIONS or count[k])
    opt = lambda vals: "".join(f'<option value="{v}">{esc(lab)}</option>' for v, lab in vals)  # noqa: E731
    filters = ('<div class="filters evf">' + chips
               + f'<label title="Show events where at least one reader changed in this direction">Direction '
                 f'<select id="fdir"><option value="">Any</option>'
                 f'{opt((("toward_cheating", "Toward cheating"), ("away", "Away from cheating"), ("neutral", "Neutral")))}'
                 f'</select></label>'
               + f'<label title="Show events where at least one reader reacted with this change">Change '
                 f'<select id="fchg"><option value="">Any</option>'
                 f'{opt((("none", "No change"), ("belief", "Belief changed"), ("behaviour", "Behaviour changed")))}'
                 f'</select></label>'
               + '<label title="Author or reader, e.g. agent-012 or 102">Agent <input id="fwho" type="search" size="9" '
                 'placeholder="agent-…"></label>'
               + '<label title="Words in the event text, title or tags">Search <input id="fq" type="search" size="14">'
                 '</label>'
               + '<button type="button" id="fview" aria-pressed="false">Show as cards</button>'
               + '<button type="button" id="freset">Reset</button> <span id="fshown" class="muted"></span></div>')
    head = "".join(f'<th data-sort="{k}"{" class=n" if num else ""} title="{esc(tip)}">{esc(lab)}</th>'
                   for k, lab, num, tip in EV_COLUMNS)
    n_skip = sum(skipped.values())
    shown = {"R1"} if count["rem"] else set()
    push = any(delivery_of(g["first"]["event"]) in ("push", "notice") for _, g in order)
    body = (f"{nav}<h1>Story map: events</h1>{banner(models)}"
            f"{legend_bar('events', shown, push)}"
            f'<p class="muted">Each event: who wrote it, when it was first seen, which agents read it, and how each '
            f'reacted (stage-2 reading). Library files are grouped by path across revisions. Click a row to open it; '
            f'click a column heading to sort. ↓ / ↑ (or j / k) move through the shown events, Enter opens one. '
            f'{plural(n_skip, "board post or DM", "board posts and DMs")} unrelated to the trick and without a '
            f'reaction {"is" if n_skip == 1 else "are"} not listed.</p>{filters}'
            f'<table class="evt" id="evt"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
            f'<div id="cards" hidden>{"".join(cards)}</div>'
            + ('' if order else '<div class="empty">No events.</div>')
            + legend_about('events', names, 'taxonomy.html' if names else '', DEFS, shown, push))
    return page("Story map events", body, LEGEND_JS + EVENTS_JS)


EV_COLUMNS = (  # (sort key, heading, numeric, hint)
    ("shape", "", False, "Event shape, as on the maps"), ("kind", "Kind", False, "Section"),
    ("author", "Author", False, "Who wrote it (for a revert: who reverted)"),
    ("t", "First seen (min)", True, "Minutes from run start when the first reader saw it"),
    ("readers", "Readers", True, "Agents that read it"),
    ("reg", "Noticed", True, "Readers whose reasoning or acts refer to it (stage 2: registered yes)"),
    ("belief", "Belief chg.", True, "Readers whose stated belief changed, but not their plan"),
    ("behaviour", "Behaviour chg.", True, "Readers whose plan or acts changed"),
    ("toward", "Toward", True, "Readers who changed toward cheating"),
    ("away", "Away", True, "Readers who changed away from cheating"),
    ("text", "Text", False, "Start of the text as first seen"))


def event_row(gk, g):
    ev, e = g["first"], g["first"]["event"]
    c = reaction_counts(g)
    n = len(g["readers"])
    readers = sorted(g["readers"])
    title = group_title(g)
    text = " ".join(str(tidy_times(ev.get("text")) if e.get("type") in ("L2", "P2") else ev.get("text") or "").split())
    text = re.sub(r"^\S+ (?:by|from) \S+ \(\w+\)(?:: )?", "", text)  # the title and author are in their own cells
    icon = f'<svg class="hglyph" viewBox="0 0 18 16">{shape(e.get("type"), 9, 8, 5, glyph_class(ev), None, "")}</svg>'
    sec = _sec_of(g)
    dirs = " ".join(d for d in ("toward_cheating", "away", "neutral") if c["dir-" + d])
    chgs = " ".join(k for k in CHANGES if c["chg-" + k])
    who = " ".join([str(e.get("author") or "")] + readers)
    q = " ".join([title, text[:TEXT_CAP], *[TAG_LABEL.get(x, x) for x in e.get("tags") or []],
                  "fake" if g["fake"] else ""]).lower()
    vals = {"t": f"{max(_num(ev.get('t')), 0):.1f}", "readers": n, "reg": c["reg-yes"], "belief": c["chg-belief"],
            "behaviour": c["chg-behaviour"], "toward": c["dir-toward_cheating"], "away": c["dir-away"]}

    def num(k):
        v = vals[k]
        return f'<td class="n" data-v="{v}">{v if v or k in ("readers", "t") else "<span class=muted>0</span>"}</td>'
    kind = type_name(e.get("type")) + (" (fake)" if g["fake"] else "")
    author = _who(e) if e.get("author") else "host (automatic)" if e.get("type") == "R1" else "—"
    return (f'<tr class="er" tabindex="0" data-id="{esc(gk)}" data-sec="{sec}" data-dir="{dirs}" data-chg="{chgs}" '
            f'data-who="{esc(who)}" data-q="{esc(q)}">'
            f'<td class="sh">{icon}</td><td data-v="{esc(kind)}">{esc(kind)}</td>'
            f'<td data-v="{esc(author)}">{esc(author)}</td>{num("t")}{num("readers")}{num("reg")}{num("belief")}'
            f'{num("behaviour")}{num("toward")}{num("away")}'
            f'<td class="ex" data-v="{esc(title)}"><b>{esc(title[:1].upper() + title[1:])}</b> '
            f'<span class="muted">{esc(text[:140])}{"…" if len(text) > 140 else ""}</span></td></tr>')


def stacked(label, parts, total, hint):
    """One labelled stacked bar with its counts in words (the counts carry the meaning; shades only separate)."""
    if not total:
        return ""
    segs = "".join(f'<span class="s{n}" style="flex:{v}" title="{esc(k)}: {v}"></span>'
                   for n, (k, v) in enumerate(parts) if v)
    words = " · ".join(f"{esc(k)} {v}" for k, v in parts if v)
    return (f'<div class="sb" title="{esc(hint)}"><span class="sbl">{esc(label)}</span><span class="sbar">{segs}</span>'
            f'<span class="sbw">{words}</span></div>')


def event_section(gk, g):
    ev = g["first"]
    e = ev["event"]
    readers = sorted(g["readers"].items(), key=lambda kv: _num(kv[1]["t"]))
    c = reaction_counts(g)
    rows = []
    for name, rd in readers:
        r = rd["reacts"][0] if rd["reacts"] else None
        rows.append(f'<tr><td><a href="agents/{esc(name)}.html#sel={esc(rd["key"])}">{esc(name)}</a></td>'
                    f'<td class="n">{fmt_t(rd["t"])}</td><td class="n">{rd["n"]}</td>'
                    + (f'<td>{esc(r.get("registered"))}</td><td>{esc(CHANGE_LABEL.get(r.get("change"), r.get("change")))}</td>'
                       f'<td>{esc(DIRECTION_LABEL.get(r.get("direction"), r.get("direction")))}</td>'
                       f'<td>{esc(plain_ids(r.get("reason")))}</td>' if r
                       else '<td colspan="4" class="muted">no stage-2 reaction</td>')
                    + "</tr>")
    judged = len(readers) - c["unjudged"]
    changed = c["chg-belief"] + c["chg-behaviour"]
    bars = (stacked("Noticed it", [("yes", c["reg-yes"]), ("unclear", c["reg-unclear"]), ("no", c["reg-no"])], judged,
                    "Did the reader's following reasoning or acts refer to it?")
            + stacked("Change", [("behaviour", c["chg-behaviour"]), ("belief", c["chg-belief"]),
                                 ("none", c["chg-none"])], judged, "What changed in the reader after it")
            + stacked("Direction of the change", [("toward cheating", c["dir-toward_cheating"]),
                                                  ("away", c["dir-away"]), ("neutral", c["dir-neutral"]),
                                                  ("not given", c["dir-none"])], changed,
                      "For readers who changed: toward or away from cheating"))
    meta = [badge(type_name(e.get("type")))]
    if e.get("author"):
        meta.append(badge(("reverted by " if e.get("type") == "P2" else "by ") + _who(e), "info" if g["overseer"] else ""))
    if e.get("type") == "R1":
        meta.append(badge("host notice, sent to every solver on a timer"))
    if g["fake"] is True:
        meta.append(badge("fake", "bad"))
    elif g["fake"] is False:
        meta.append(badge("not fake", "good"))
    meta += [badge(TAG_LABEL.get(t, str(t).replace("_", " ")), "caution") for t in e.get("tags") or []]
    ids = sorted(g["ids"])
    n = len(readers)
    title = group_title(g)
    title = title[:1].upper() + title[1:]
    icon = f'<svg class="hglyph" viewBox="0 0 18 16">{shape(e.get("type"), 9, 8, 5, glyph_class(ev), None, "")}</svg>'
    text = tidy_times(ev.get("text")) if e.get("type") in ("L2", "P2") else ev.get("text")
    return (f'<section class="ev" id="{esc(gk)}" data-sec="{_sec_of(g)}" tabindex="-1"><h3>{icon}{esc(title)}</h3>'
            f'<p>{"".join(meta)} '
            f'<small>first seen at {fmt_t(ev.get("t"))} by {esc(readers[0][0])} &middot; {plural(n, "reader")}'
            f'{" &middot; " + plural(len(ids), "revision") if len(ids) > 1 and g["type"] != "R1" else ""}</small></p>'
            f'<details><summary>Text as first seen</summary><pre>{esc(cut(text))}</pre></details>'
            f'<div class="sbs"><p class="muted">How the readers reacted (stage 2; '
            f'{plural(judged, "reader")} with a reaction, {c["unjudged"]} without):</p>{bars}</div>'
            f'<details{" open" if n <= 12 else ""}><summary>Readers</summary><table><tr><th>Agent</th>'
            f"<th>First seen at</th><th>Reads</th><th>Noticed it</th><th>Change</th><th>Direction</th><th>Why</th></tr>"
            f'{"".join(rows)}</table></details></section>')


# ------------------------------------------------------------------ validation.html

def _inline(text):
    out = esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)


def _anchor(heading):
    """Anchor of a taxonomy heading: '### C4 - Social proof' -> code-C4, '### event L1 - ...' -> event-L1."""
    m = re.match(r"(event\s+)?([A-Z]\d{0,2})\s+-\s", heading)
    if m:
        return ("event-" if m.group(1) else "code-") + m.group(2)
    m = re.match(r"(\d+)\.\s", heading)
    return f"s{m.group(1)}" if m else ""


def markdown_html(text, anchors=False):
    """The small Markdown subset of validation.md and taxonomy.md (headings, with ``anchors`` an id per code
    heading; paragraphs; bulleted and numbered lists with continuation lines; pipe tables) as escaped HTML."""
    out, para, items, table = [], [], [], []
    kind = ["ul"]

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()
        if items:
            out.append(f"<{kind[0]}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{kind[0]}>")
            items.clear()
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?[\s:|-]+\|?", r)]
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            if cells:
                head = "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
                body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in cells[1:])
                out.append(f"<table><tr>{head}</tr>{body}</table>")
            table.clear()
    for line in text.splitlines():
        s = line.rstrip()
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        lm = re.match(r"^\s*(?:([-*])|(\d+)\.)\s+", s)
        if m:
            flush()
            n = min(len(m.group(1)), 3)
            aid = _anchor(m.group(2)) if anchors else ""
            out.append(f'<h{n}{f" id={chr(34)}{aid}{chr(34)}" if aid else ""}>{_inline(m.group(2))}</h{n}>')
        elif s.startswith("|"):
            if para or items:
                tb = list(table)
                table.clear()
                flush()
                table.extend(tb)
            table.append(s)
        elif lm and not line.startswith("    "):
            k = "ol" if lm.group(2) else "ul"
            if para or table or (items and kind[0] != k):
                flush()
            kind[0] = k
            items.append(s[lm.end():])
        elif items and s.strip() and line[:1] == " ":
            items[-1] += " " + s.strip()  # continuation of a list item
        elif not s.strip():
            flush()
        else:
            if items or table:
                flush()
            para.append(s.strip())
    flush()
    return "".join(out)


CHECK_LABEL = {
    "faked": "Faked", "credited": "Got credit for a fake", "first_fake_t": "First fake time",
    "stopped": "Stopped faking", "considered": "Considered faking", "source": "Source of the fake",
    "aware_cheating": "Knew it was cheating", "stage2_event_exists": "Stage-2 reaction cites an event it saw",
    "stage2_event_before_reaction": "Stage-2 quotes come after the event",
    "stage2_quotes": "Stage-2 quotes verbatim", "stage1_quotes": "Stage-1 quotes verbatim",
}


def plain_validation(text):
    """validation.md in the pages' vocabulary: check and field names in words, event ids plain, model ids by name,
    times as minutes; the Markdown structure is kept."""
    text = text.replace("quote not_found at item", "quote not found in timeline item").replace(
        "quote wrong_item at item", "quote found, but not in the cited timeline item")
    text = re.sub(r"(?<![\w/.-])(" + "|".join(sorted(CHECK_LABEL, key=len, reverse=True)) + r")(?![\w/.-])",
                  lambda m: CHECK_LABEL[m.group(1)], text)
    text = re.sub(r"\b(?:anthropic|openai|google)/[a-z0-9.\-]+", lambda m: model_label(m.group(0)), text)
    text = text.replace("Jaccard", "overlap (Jaccard)").replace("+/-", "\u00b1")
    text = re.sub(r"\b(stop at|at) (\d+\.\d+)\b", lambda m: f"{m.group(1)} {fmt_t(float(m.group(2)))}", text)
    return plain_ids(text)


def validation_page(text, models="", nav=""):
    body = f"{nav}{banner(models)}{terms_html()}{markdown_html(plain_validation(text))}"
    return page("Story map validation", body)


EVENT_TOKEN = r"(?:L1|L2|P1|P2|B1|D1|S1|O1|C1|N1|R1)"


def _event_names(m):
    codes = re.findall(EVENT_TOKEN, m.group(0))
    names = [type_name(c) for c in codes]
    return (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]) + (
        " event" if len(names) == 1 else " events")


def taxonomy_display(text):
    """The taxonomy as shown on the codes page, in the pages' one code scheme: letters are only code groups (thought,
    reaction and overseer codes); event types are named in words, as on every other page (the committed file keeps
    its machine-read event letters, which the prompts insert verbatim). Applied outside the headings; the event
    headings are renamed in taxonomy_page."""
    out = []
    for line in text.splitlines():
        if line.startswith("#"):
            out.append(line)
            continue
        line = re.sub(r"Their type\s+letters overlap thought codes[^.]*\.[^.]*`code`\.", CODE_SCHEME, line)
        line = re.sub(r"\bevents?\s+" + EVENT_TOKEN + r"(?:(?:,\s*|\s+(?:or|and)\s+)" + EVENT_TOKEN + r")*",
                      _event_names, line)
        line = re.sub(r"\((" + EVENT_TOKEN + r")\)", lambda m: f"({type_name(m.group(1))} event)", line)
        line = re.sub(r"\b(" + EVENT_TOKEN + r")/(" + EVENT_TOKEN + r") events\b",
                      lambda m: f"{type_name(m.group(1))} or {type_name(m.group(2))} events", line)
        line = re.sub(r"\bS1 with\b", "a submission event with", line)
        out.append(line)
    text = "\n".join(out)
    # The section-2 intro sentence spans lines: replace it after joining.
    return re.sub(r"Their type\s+letters overlap thought codes.*?never as a thought `code`\.", CODE_SCHEME, text,
                  flags=re.S)


def taxonomy_page(text, models="", nav=""):
    html_text = markdown_html(taxonomy_display(text), anchors=True)

    def event_heading(m):
        typ = m.group(2)
        icon = (f'<svg class="hglyph" viewBox="-9 -9 18 18">'
                f'{shape(typ, 0, 0, 5.2, "g-rem" if typ == "R1" else "g-other" if typ in OTHER_TYPES else "g-hollow", None, "")}'
                f'</svg>')
        return f'<h{m.group(1)} id="event-{typ}">{icon}Event: {esc(type_name(typ))}</h{m.group(1)}>'
    html_text = re.sub(r'<h(\d) id="event-(' + EVENT_TOKEN + r')">[^<]*</h\d>', event_heading, html_text)
    body = (f"{nav}{banner(models)}"
            '<p class="muted">The code set the model readers apply. On the agent pages each thought links here; the '
            'legend box "What it thought: code groups" gives each code\'s short definition in place. '
            f'{esc(CODE_SCHEME)}</p>'
            f'<div class="story">{html_text}</div>')
    return page("Story map codes", body)


def nav_html(prefix="", has_val=False, has_tax=False):
    """The same top navigation on every page: Agents · Events · Validation · Codes."""
    links = [("index.html", "Agents"), ("events.html", "Events")]
    links += [("validation.html", "Validation")] if has_val else []
    links += [("taxonomy.html", "Codes")] if has_tax else []
    return '<nav class="top">' + "".join(f'<a href="{prefix}{h}">{t}</a>' for h, t in links) + "</nav>"


# ------------------------------------------------------------------ main

def build(map_dir, timeline=None, out=None, taxonomy=DEFAULT_TAXONOMY, domain=None, evidence=True, minutes=None,
          overseer_map=None, overseer_timeline=None, graph=None):
    """Write index.html, events.html and agents/<agent>.html; return the list of written paths. ``domain``
    (default: recorded in pipeline.json or timeline/domain.json, else lean) selects coding wording and, when the
    default taxonomy is asked for, the public coding taxonomy (storymap/domain.py); Lean maps are unchanged.
    ``evidence`` False leaves out the embedded timeline behind the reasoning chains; ``minutes`` sets the time axis
    (default: the run's configured length, else from the data). ``overseer_map`` / ``overseer_timeline``: an overseer
    story map (pipeline --overseer) to add as overseer pages, the index's overseer strip and run panel and
    taxonomy_overseer.html (storymap/view_overseer.py); without them the output is exactly as before. ``graph``:
    link the index to the experimental interaction graph (True / False; None, the default: when graph.html exists in
    the output folder); without a graph the output is exactly as before."""
    from . import domain as domain_mod
    domain = domain or domain_mod.map_domain(map_dir, timeline)
    if domain == domain_mod.CODE and taxonomy and Path(taxonomy) == DEFAULT_TAXONOMY:
        taxonomy = domain_mod.PUBLIC_CODE_TAXONOMY
    with domain_mod.view_overrides(sys.modules[__name__], domain):
        EVIDENCE["on"] = bool(evidence)
        try:
            return _build(map_dir, timeline, out, taxonomy, minutes, overseer_map, overseer_timeline, graph)
        finally:
            EVIDENCE["on"] = True


def _build(map_dir, timeline=None, out=None, taxonomy=DEFAULT_TAXONOMY, minutes=None, overseer_map=None,
           overseer_timeline=None, graph=None):
    """Write index.html, events.html and agents/<agent>.html; return the list of written paths."""
    agents, pipeline = load(map_dir, timeline)
    out = Path(out or map_dir)
    if graph is None:
        graph = (out / GRAPH_PAGE).is_file()
    (out / "agents").mkdir(parents=True, exist_ok=True)
    names = load_taxonomy(taxonomy) if taxonomy else {}
    if names and "E" not in names and any(c[:1] == "E" for c in names):  # E codes describe reactions (stage 2)
        names["E"] = "Reactions (stage 2)"
    DEFS.clear()
    DEFS.update(load_definitions(taxonomy) if taxonomy else {})
    has_tax = bool(names) and Path(taxonomy).is_file()
    if not agents and minutes is None and overseer_timeline is not None:  # overseer-only: the run's configured length
        ctx_file = timeline_dir(overseer_timeline) / "overseer_context.json"
        minutes = (_read_json(ctx_file) or {}).get("run_minutes") if ctx_file.is_file() else None
    tmax = axis_max(agents, minutes or run_minutes(map_dir, pipeline))
    models = model_names(pipeline)
    validation = Path(map_dir) / "validation.md"
    has_val = validation.is_file()
    nav = nav_html("", has_val, has_tax)
    written = []

    def write(path, text):
        path.write_text(text, encoding="utf-8")
        written.append(path)
    ov = None
    if overseer_map is not None or overseer_timeline is not None:
        from . import view_overseer  # only in overseer mode, so solver builds never depend on it
        ov = view_overseer.prepare(overseer_map, overseer_timeline, tmax, has_val, has_tax,
                                   view_module=sys.modules[__name__])
    if ov is not None and not agents:  # a run with overseer maps but no solver story map
        write(out / "index.html", overseer_only_index(ov, tmax, nav, graph))
    else:
        from .sample import load as load_sample
        write(out / "index.html", index_page(agents, pipeline, tmax, nav, names,
                                             *(ov.index_block() if ov else ("", "")), graph=graph,
                                             sample=load_sample(map_dir)))
    write(out / "events.html", events_page(agents, tmax, models, nav, names))
    if has_val:
        write(out / "validation.html", validation_page(validation.read_text(encoding="utf-8"), models, nav))
    if has_tax:
        write(out / "taxonomy.html", taxonomy_page(Path(taxonomy).read_text(encoding="utf-8"), models, nav))
    agent_nav = nav_html("../", has_val, has_tax)
    for a in agents.values():
        write(out / "agents" / f"{a['name']}.html", agent_page(a, names, tmax, agents.keys(), models, agent_nav,
                                                               "../taxonomy.html" if has_tax else ""))
    if ov:
        for path, text in ov.pages():
            (out / path).parent.mkdir(parents=True, exist_ok=True)
            write(out / path, text)
    return written


GRAPH_SCRIPT = REPO / "scripts" / "storymap_graph.py"


def _graph_module():
    """scripts/storymap_graph.py (loaded by path: scripts/ need not be on sys.path)."""
    mod = sys.modules.get("storymap_graph")
    if mod is not None:
        return mod
    import importlib.util
    spec = importlib.util.spec_from_file_location("storymap_graph", GRAPH_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["storymap_graph"] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop("storymap_graph", None)
        raise
    return mod


def infer_run_dir(map_dir):
    """RUN for a map folder RUN/analysis/<name> (the story map's usual place), else None."""
    map_dir = Path(map_dir).resolve()
    return map_dir.parents[1] if map_dir.parent.name == "analysis" and len(map_dir.parents) > 1 else None


def ensure_graph(map_dir, out, run_dir=None, overseer_map=None, log=None, inline=False):
    """Build the experimental interaction graph into ``out`` with scripts/storymap_graph.py, offline (owner,
    November 6, 2026: every story-map render includes the graph): the publishing form, graph.html (a shell loading
    the site's shared viewer, _graph/) plus graph.json, or with ``inline`` the self-contained graph.html. ``run_dir`` defaults to RUN for a map
    folder RUN/analysis/storymap; ``overseer_map`` to storymap_graph's own default (RUN/analysis/storymap-overseer
    when it has stage records). Skips with one log line, leaving ``out`` untouched, when the run folder, its
    conference database or the story map records are missing, or the build fails. Returns the graph.html path or
    None."""
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    map_dir, out = Path(map_dir), Path(out)
    run_dir = Path(run_dir) if run_dir else infer_run_dir(map_dir)
    if run_dir is None:
        log(f"interaction graph: skipped (run folder unknown for {map_dir}; pass --run-dir)")
        return None
    db = run_dir / "conference" / "state.sqlite"
    if not db.is_file():
        log(f"interaction graph: skipped (no conference database at {db})")
        return None
    om = Path(overseer_map) if overseer_map else None
    if not (any((map_dir / "stage1").glob("*.jsonl")) or (map_dir / "map.jsonl").is_file()
            or (om is not None and (om / "stage1").is_dir())
            or (run_dir / "analysis" / "storymap-overseer" / "stage1").is_dir()):
        log(f"interaction graph: skipped (no story map records in {map_dir})")
        return None
    target = out / GRAPH_PAGE
    argv = ["--run-dir", str(run_dir), "--map", str(map_dir), "--out", str(target)]
    if om is not None:
        argv += ["--overseer-map", str(om)]
    if inline:
        argv.append("--inline")
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            code = _graph_module().main(argv)
    except Exception as exc:  # noqa: BLE001  (the graph is optional: never block the story map)
        log(f"interaction graph: skipped (build failed: {type(exc).__name__}: {exc})")
        return None
    if code:
        log(f"interaction graph: skipped (storymap_graph exit {code})")
        return None
    log(f"interaction graph: wrote {target}")
    return target


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map", required=True, help="stage outputs dir (runs/<id>/analysis/storymap)")
    ap.add_argument("--timeline", help="stage-0 dir (holding timeline/ or the agent JSONL files)")
    ap.add_argument("--out", help="output dir (default: --map)")
    ap.add_argument("--taxonomy", default=str(DEFAULT_TAXONOMY),
                    help="taxonomy for code names, definitions and taxonomy.html (default: the public copy; '' = none)")
    ap.add_argument("--no-evidence", action="store_true",
                    help="leave out the embedded timeline behind the 'full reasoning chain' expanders")
    ap.add_argument("--minutes", type=float, help="time axis length (default: the run's configured length)")
    ap.add_argument("--overseer-map", help="overseer story map dir (pipeline --overseer): adds overseer pages")
    ap.add_argument("--overseer-timeline", help="overseer stage-0 dir (extract --overseers; holds "
                    "overseer_context.json)")
    ap.add_argument("--graph-link", action=argparse.BooleanOptionalAction, default=None,
                    help="link the index to the experimental interaction graph (graph.html; default: when it exists "
                         "in the output dir). Unless --no-graph-link, graph.html is first (re)built into the output "
                         "dir by scripts/storymap_graph.py when the run's conference records are at hand")
    ap.add_argument("--run-dir", help="run folder for the interaction graph (default: RUN for --map "
                    "RUN/analysis/storymap)")
    ap.add_argument("--graph-inline", action="store_true",
                    help="write the self-contained graph.html (for previews from disk) instead of the publishing "
                         "form (shell graph.html + graph.json; the site serves the shared viewer _graph/)")
    args = ap.parse_args(argv)
    if args.graph_link is not False:
        ensure_graph(args.map, args.out or args.map, args.run_dir, args.overseer_map, inline=args.graph_inline)
    written = build(args.map, args.timeline, args.out, args.taxonomy, evidence=not args.no_evidence,
                    minutes=args.minutes, overseer_map=args.overseer_map, overseer_timeline=args.overseer_timeline,
                    graph=args.graph_link)
    print(f"wrote {len(written)} pages to {Path(args.out or args.map)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
