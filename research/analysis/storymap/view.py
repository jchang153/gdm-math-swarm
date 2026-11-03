"""Story map viewer: offline static HTML from the stage-0 timelines and the stage 1-3 outputs.

    python -m research.analysis.storymap.view --map runs/<id>/analysis/storymap --timeline DIR [--out DIR]

Writes (default ``--out`` = ``--map``):

* ``index.html``: one row per agent; positions on cheating as coloured spans on a 0-180 min axis (longer
  when the run is longer), event glyphs (library reads, overseer posts, DMs, reverts, submissions),
  final readings, and filters by source of the fake / faked / credited / stopped / knew it was cheating.
* ``agents/<agent>.html``: lanes (position on cheating; thoughts by code group A-G; library; board and DMs;
  status and reverts; submissions; own actions and restarts). Clicking a glyph shows its quote or event text with the
  stage-2 reaction; every item is also listed in time order below the lanes.
* ``validation.html``: ``validation.md`` (checks against the run's records, model agreement) in the pages' words.
* ``taxonomy.html``: the code set (``--taxonomy``, default the public copy ``storymap/taxonomy.md``) with an anchor
  per code; the pages link each code to it.
* ``events.html``: each overseer post or DM, other relevant board post, revert and library file, with who read it
  and when, and the breakdown of stage-2 reactions (no change / belief / behaviour by direction).

Inputs follow private/research/analysis/storymap/INTERFACE.md. Every input is optional: an agent with only a timeline,
or only stage outputs, still gets a row, and missing stages are stated on the page instead of guessed. The taxonomy is
public (owner, November 2, 2026): code and group names, and each code's definition, appear in place (lane labels and
hints, the legend's code-group box, every thought's tooltip and panel, the reaction badges). ``--taxonomy ''`` renders
without names, saying so where the names would be. Visible text never shows raw event codes (B1, S1, ...) or the
timeline's field names: events are named in words, detail fields have labels, times are minutes from run start.
The pages, ``validation.md`` and the taxonomy page are published (scripts/publish_cloudflare.py storymap); timelines
and stage records stay local. Each page has a legend (bar colour = position on cheating, shape = event, code groups)
and a glossary; agent pages have a detail panel that steps through the items in time order (arrow keys or j / k,
Prev / Next, a kind filter, Esc closes). No network, no external assets (the shared site theme is inlined); a little
vanilla JavaScript.
"""
from __future__ import annotations

import argparse
import html
import json
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

LANES = (  # (key, label, event types, hover hint)
    ("library", "What it saw: library", ("L1", "L2"), "Shared-library listings and files the agent read"),
    ("board", "What it saw: board, DMs", ("B1", "D1"), "Board posts and direct messages the agent read"),
    ("status", "What it saw: status", ("P1", "P2", "N1"),
     "Problem lists, reverts of its own credit, and unread-message notices"),
    ("subs", "What it submitted", ("S1",), "The agent's own proof submissions and their outcomes"),
    ("own", "What it did: posts, restarts", ("O1", "C1"),
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
             "O1": "own post, DM or note", "C1": "context compaction or restart", "N1": "unread-messages notice"}


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
    return re.sub(r"\b([LPBDSOCN][12]):([^\s,;)'\"\]]+)", lambda m: event_label(m.group(0), m.group(1)), str(text))


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
# Two palettes that share no hue: five stance hues, used only by the stance bars, and two event hues, used only by
# event glyphs (pink = fake, cyan = from an overseer). Everything else on a glyph is neutral: ordinary events are
# grey outlines, thoughts are ink ticks, a submission's outcome is a mark (check = accepted or credited, slash = not
# accepted). The seven hues were validated together (dataviz validate_palette.js, --pairs all: lightness band,
# chroma, CVD >= 8, normal vision >= 15, contrast >= 3:1) on every surface they sit on: light #fafbfc, #ffffff,
# #f1f4f7, #e9edf2; dark #0f141a, #161d25, #18202a, #1d2631. "none" is a neutral grey bar and "unknown" an outline.
TOKENS_LIGHT = {"st-none": "#a7b1bd", "st-considering": "#9c8133", "st-deferred": "#1440bd",
                "st-rejected": "#027651", "st-faking": "#b41e07", "st-stopped": "#8254f9",
                "ev-fake": "#e13c85", "ev-ov": "#2593af",
                "lane": "#f1f4f7", "lane-alt": "#e9edf2", "panel": "#ffffff"}
TOKENS_DARK = {"st-none": "#5b6675", "st-considering": "#a3933c", "st-deferred": "#2b6dec",
               "st-rejected": "#337f53", "st-faking": "#c7461b", "st-stopped": "#9457b3",
               "ev-fake": "#e95296", "ev-ov": "#1aa0be",
               "lane": "#18202a", "lane-alt": "#1d2631", "panel": "#161d25"}


def _vars(tokens):
    return "".join(f"--{k}:{v};" for k, v in tokens.items())


CSS = (
    f":root{{{_vars(TOKENS_LIGHT)}}}"
    f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{_vars(TOKENS_DARK)}}}}}'
    f':root[data-theme="dark"]{{{_vars(TOKENS_DARK)}}}'
    """
:root{font-family:system-ui,sans-serif;line-height:1.5}
body{max-width:1320px;margin:28px auto;padding:0 24px;background:var(--bg);color:var(--fg)}
h1{margin:0 0 6px;font-size:1.6rem}h2{margin:28px 0 8px;font-size:1.2rem}h3{margin:0 0 6px;font-size:1.02rem}
.muted,small{color:var(--muted)}.banner{background:var(--banner);border-left:4px solid var(--banner-line);
padding:10px 16px;margin:12px 0}nav.top{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0 14px}
.badge{display:inline-block;background:var(--chip);padding:1px 7px;border-radius:4px;font-size:12px;
margin:1px 2px 1px 0;white-space:nowrap}.b-bad{background:var(--chip);color:var(--fg);box-shadow:inset 0 0 0 1px var(--fg);font-weight:600}
.b-good{background:var(--chip);color:var(--fg)}.b-caution{background:var(--chip);color:var(--fg);
box-shadow:inset 0 0 0 1px var(--muted)}.b-info{background:var(--chip);color:var(--fg)}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:14px 0}
.filters label{display:flex;gap:6px;align-items:center;font-size:14px}
select,button{font:inherit;padding:6px 9px;border:1px solid var(--input-line);border-radius:6px;
background:var(--card);color:inherit}button{cursor:pointer}
.sw{display:inline-block;width:18px;height:10px;border-radius:2px;flex:none}
details.legend{position:static;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
padding:4px 0 6px;margin:6px 0 10px}details.legend>summary{cursor:pointer;font-size:13px;font-weight:600;padding:2px 0}
.lg{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.6fr);gap:8px 14px;font-size:12.5px;line-height:1.35}
.lgbox{border:1px solid var(--line);border-radius:8px;padding:4px 10px 6px;background:var(--card)}
.lg ul.marks,.lg ul.ex{margin-top:4px;padding-top:4px;border-top:1px dashed var(--line)}.lg svg.swatch{width:22px}
.lg h4{margin:4px 0 3px;font-size:12.5px}.lg ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:3px 14px}
.lg li{display:inline-flex;gap:5px;align-items:center}.lg svg{width:18px;height:16px;flex:none;overflow:visible}
.lg.three{grid-template-columns:minmax(0,1fr) minmax(0,1.5fr) minmax(0,.9fr)}
@media (max-width:1100px){.lg.three{grid-template-columns:minmax(0,1fr) minmax(0,1.6fr)}.lg.three>.lgbox:last-child{grid-column:1/-1}}
.lg .note{color:var(--muted);margin:3px 0 0}.lg .g{cursor:default}
.codes{max-height:150px;overflow:auto;padding-right:4px}.codes .grp{margin:0 0 3px}
.codes .gname{font-weight:400}.codes details.code{margin-left:12px}.codes details.code>summary{cursor:pointer;
list-style:none}.codes details.code>summary::-webkit-details-marker{display:none}
.codes details.code>summary:focus-visible{outline:2px solid var(--fg)}.codes details.code p{margin:2px 0 4px;
color:var(--muted)}.def{color:var(--muted);font-size:13px;margin:4px 0}
details.terms{font-size:13px;margin:0 0 10px}details.terms>summary{cursor:pointer;color:var(--muted)}
details.terms ul{margin:4px 0;padding-left:18px}
.hglyph{width:16px;height:14px;vertical-align:-2px;margin-right:6px;overflow:visible}
.stepper{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding-bottom:8px;margin-bottom:8px;
border-bottom:1px solid var(--line);font-size:13px}.stepper button{padding:3px 9px}.stepper select{padding:3px 6px}
#pos{min-width:9em;font-variant-numeric:tabular-nums}#pclose{margin-left:auto}.keys{font-size:12px;margin:8px 0 0}
section.ev.cur{outline:2px solid var(--fg)}section.ev{scroll-margin-top:var(--legend-h,0px)}
.grid{display:grid;grid-template-columns:96px minmax(420px,1fr) 300px;gap:0 12px;align-items:center}
.grid>.hd{font-size:12px;color:var(--muted);padding:4px 0}
.row{display:contents}.row>*{border-top:1px solid var(--line);padding:5px 0}
.row:hover>*{background:var(--pick)}.agent a{font-weight:600;text-decoration:none}
.strip svg{display:block;width:100%;height:30px}.labels{font-size:12px}
svg text{fill:var(--muted);font:11px system-ui,sans-serif}
.ph-none{fill:var(--st-none)}.ph-considering{fill:var(--st-considering)}.ph-deferred{fill:var(--st-deferred)}
.ph-rejected{fill:var(--st-rejected)}.ph-faking{fill:var(--st-faking)}.ph-stopped{fill:var(--st-stopped)}
.g{cursor:pointer;stroke-width:1.3}.g:hover{stroke-width:2.4}
.g .m{fill:var(--card);stroke:var(--muted);stroke-width:1.4}.g.ring .m{stroke:var(--ev-ov)}
.g.g-fill .m{fill:var(--ev-fake);stroke:var(--ev-fake)}.g .ring{fill:none;stroke:var(--ev-ov);stroke-width:1.4}.g .mk{fill:none;stroke:var(--fg);stroke-width:1.6;stroke-linecap:round}
.g .halo{fill:none;stroke:var(--card);stroke-width:4;stroke-linecap:round}.g .hit{fill:transparent;stroke:none}
.g .x{fill:none;stroke:var(--fg);stroke-width:2.4;stroke-linecap:round}.g.g-other .m{fill:var(--muted);stroke:none}
.g.g-th .m{fill:var(--fg);stroke:var(--fg);stroke-width:.8}.g.g-th-low .m{fill:var(--card);stroke:var(--fg);stroke-width:1}
.g:hover .m,.g:hover .x{stroke-width:2.2}.g.sel .m,.g.sel .ring{stroke-width:2.6}.g.sel .x{stroke-width:3.4}
.tp.sel{stroke-width:3;stroke-dasharray:none}.cursor{stroke:var(--fg);stroke-width:1.5;opacity:.55;pointer-events:none}
.ph-unknown{fill:none;stroke:var(--muted);stroke-dasharray:3 2}
.lane-bg{fill:var(--lane)}.lane-bg.alt{fill:var(--lane-alt)}.tick{stroke:var(--line)}
.tp{stroke:var(--fg);stroke-dasharray:3 3;stroke-width:1.2;cursor:pointer}
.lanes{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;margin:12px 0}
.lanes svg{display:block;width:100%;height:auto}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 400px;grid-template-areas:"lanes panel" "list panel";
gap:0 18px;align-items:start}.layout>.lanes{grid-area:lanes;margin-top:0}.layout>.list{grid-area:list;min-width:0}
#panel{grid-area:panel;position:sticky;top:calc(var(--legend-h,0px) + 8px);background:var(--panel);
border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-size:14px;display:flex;flex-direction:column;
height:calc(100vh - var(--legend-h,0px) - 24px);max-height:760px;min-height:320px;box-sizing:border-box}
#pbody{flex:1;overflow:auto;min-height:0}.lanetext{cursor:help}
article.item{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);
border-radius:8px;padding:9px 12px;margin:8px 0;font-size:14px;scroll-margin-top:calc(var(--legend-h,0px) + 12px)}
article.item.k-th{border-left-color:var(--muted)}article.item.k-ov{border-left:5px double var(--fg)}
article.item.k-fake{border-left-color:var(--fg)}article.item.k-tp{border-left-style:dashed;border-left-color:var(--fg)}
article.item.hl{outline:2px solid var(--fg)}
blockquote{margin:6px 0;padding:4px 10px;border-left:3px solid var(--input-line);white-space:pre-wrap;
overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--pre);padding:8px 10px;
border-radius:6px;font:12px/1.5 ui-monospace,monospace;margin:6px 0;max-height:320px;overflow:auto}
.react{background:var(--chip);border-radius:6px;padding:6px 10px;margin:6px 0}
.react p{margin:3px 0}dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:2px 10px;margin:6px 0}
dl.kv dt{color:var(--muted)}dl.kv dd{margin:0}
table{border-collapse:collapse;margin:8px 0;font-size:14px}th,td{border:1px solid var(--line);padding:4px 8px;
text-align:left;vertical-align:top}th{background:var(--chip);font-weight:600}td.n{text-align:right}
section.ev{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
.empty{padding:16px;color:var(--muted);border:1px dashed var(--input-line);border-radius:8px}
.story{max-width:900px}
.evf .chip{border:1px solid var(--input-line);border-radius:14px;padding:3px 9px;background:var(--card);cursor:pointer}
.evf input[type=search]{font:inherit;padding:5px 8px;border:1px solid var(--input-line);border-radius:6px;
background:var(--card);color:inherit}
table.evt{width:100%;border-collapse:collapse;font-size:13.5px}table.evt th{white-space:nowrap;cursor:pointer;
position:sticky;top:var(--legend-h,0px);z-index:5}table.evt th[aria-sort=ascending]::after{content:" \u25b2"}
table.evt th[aria-sort=descending]::after{content:" \u25bc"}table.evt td{vertical-align:middle}
tr.er{cursor:pointer;scroll-margin-top:calc(var(--legend-h,0px) + 36px)}tr.er:hover>td,tr.er.open>td,tr.er:focus>td{background:var(--pick)}tr.er:focus{outline:2px solid var(--fg)}
td.ex{max-width:0;width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}td.sh{width:22px}
table.evt td:nth-child(2),table.evt td:nth-child(3){white-space:nowrap}
tr.xr>td{padding:0 0 8px}tr.xr section.ev{margin:4px 0}
.sb{display:grid;grid-template-columns:11em 160px 1fr;gap:8px;align-items:center;font-size:13px;margin:3px 0}
.sbar{display:flex;height:10px;border-radius:3px;overflow:hidden;background:var(--chip)}
.sbar .s0{background:var(--fg)}.sbar .s1{background:var(--muted)}.sbar .s2{background:var(--input-line)}
.sbar .s3{background:var(--line)}.sbs p{margin:6px 0 2px}[hidden]{display:none!important}
@media (max-width:900px){.layout{grid-template-columns:minmax(0,1fr);grid-template-areas:"lanes" "panel" "list"}
#panel{position:static;height:auto;max-height:none;min-height:0}#pbody{max-height:60vh}}
@media (max-width:1000px){.grid{grid-template-columns:80px minmax(0,1fr)}.grid>.labels,.grid>.hd.lab{grid-column:2}}
@media (max-width:700px){table.evt thead{display:none}table.evt,table.evt tbody{display:block}
tr.er{display:grid;grid-template-columns:22px 1fr auto auto;gap:0 8px;border-top:1px solid var(--line);padding:4px 0}
tr.er>td{border:none;padding:1px 0}tr.er>td:nth-child(3),tr.er>td:nth-child(n+6):nth-child(-n+10){display:none}
tr.er>td.ex{grid-column:1/-1;width:auto;max-width:none;display:block;white-space:nowrap}
tr.er>td:nth-child(4)::after{content:" min"}tr.er>td:nth-child(5)::after{content:" readers";color:var(--muted)}tr.xr{display:block}tr.xr>td{display:block}.sb{grid-template-columns:1fr;gap:2px}
.sbar{width:100%}}
@media (max-width:700px){body{padding:0 16px;margin:16px auto}.lg,.lg.three{grid-template-columns:1fr}}
@media (min-width:701px) and (min-height:600px){details.legend{position:sticky;top:0}}
"""
)

PANEL_JS = r"""
(()=>{const panel=document.getElementById('panel');if(!panel)return;
const body=document.getElementById('pbody'),pos=document.getElementById('pos'),kind=document.getElementById('kind'),
prev=document.getElementById('pprev'),next=document.getElementById('pnext'),
all=[...document.querySelectorAll('#items article.item[id]')];let cur=null;
function ok(a){return !kind.value||a.dataset.kind===kind.value;}
function list(){return all.filter(ok);}
function status(){const L=list(),src=cur&&document.getElementById(cur),i=src?L.indexOf(src):-1;
const t=src?' \u00b7 '+src.dataset.t+' min':'';
pos.textContent=src?(i>=0?(i+1)+' of '+L.length:'('+kind.selectedOptions[0].text.toLowerCase()+': '+L.length+')')+t
:L.length+' '+(kind.value?kind.selectedOptions[0].text.toLowerCase():'items');
const ix=src?all.indexOf(src):-1;
prev.disabled=!all.some((a,j)=>ok(a)&&(ix<0?true:j<ix));next.disabled=!all.some((a,j)=>ok(a)&&j>ix);}
function show(key,how){const src=document.getElementById(key);if(!src||!src.matches('article.item'))return;cur=key;
document.querySelectorAll('.sel').forEach(e=>e.classList.remove('sel'));
document.querySelectorAll('[data-k="'+key+'"]').forEach(e=>e.classList.add('sel'));
all.forEach(a=>a.classList.toggle('hl',a===src));body.innerHTML=src.innerHTML;body.scrollTop=0;
panel.classList.add('open');status();
const g=document.querySelector('.lanes [data-k="'+key+'"]'),cu=document.getElementById('cursor');
if(cu&&g&&g.getBBox){const b=g.getBBox(),x=g.tagName==='line'?+g.getAttribute('x1'):b.x+b.width/2;
cu.setAttribute('x1',x);cu.setAttribute('x2',x);cu.setAttribute('visibility','visible');}
if(how!=='click'&&g)reveal(g);
try{history.replaceState(null,'','#sel='+key);}catch(e){}}
function reveal(el){const r=el.getBoundingClientRect(),
top=(parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--legend-h'))||0)+8,
bot=innerHeight-12;if(r.top<top||r.bottom>bot)scrollBy(0,r.top-(top+Math.max(bot-top,0)/2));}
function close(){cur=null;const cu=document.getElementById('cursor');if(cu)cu.setAttribute('visibility','hidden');document.querySelectorAll('.sel').forEach(e=>e.classList.remove('sel'));
all.forEach(a=>a.classList.remove('hl'));panel.classList.remove('open');
body.innerHTML='<p class="muted">Nothing selected. Click a shape on the timeline, or press \u2192 (or Next) to read '
+'the items one by one in time order.</p>';
status();try{history.replaceState(null,'',location.pathname+location.search);}catch(e){}}
function step(d){const src=cur&&document.getElementById(cur),ix=src?all.indexOf(src):(d>0?-1:all.length);
let j=ix+d;while(j>=0&&j<all.length&&!ok(all[j]))j+=d;if(j>=0&&j<all.length)show(all[j].id,'step');}
prev.addEventListener('click',()=>step(-1));next.addEventListener('click',()=>step(1));
document.getElementById('pclose').addEventListener('click',close);
kind.addEventListener('change',()=>{status();kind.blur();});
document.addEventListener('click',ev=>{const g=ev.target.closest('[data-k]');if(!g)return;
ev.preventDefault();show(g.getAttribute('data-k'),'click');});
document.addEventListener('keydown',ev=>{if(ev.altKey||ev.ctrlKey||ev.metaKey)return;
const tag=(ev.target.tagName||'').toLowerCase();if(tag==='input'||tag==='select'||tag==='textarea')return;
const k=ev.key;if(k==='ArrowRight'||k==='j'){ev.preventDefault();step(1);}
else if(k==='ArrowLeft'||k==='k'){ev.preventDefault();step(-1);}
else if(k==='Escape'){if(cur){ev.preventDefault();close();}}
else if(k==='Enter'||k===' '){const g=ev.target.closest&&ev.target.closest('[data-k]');
if(g){ev.preventDefault();show(g.getAttribute('data-k'),'click');}}});
const m=location.hash.match(/^#sel=(.+)$/);if(m)show(decodeURIComponent(m[1]),'hash');else close();})();
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
            for th in chunk.get("thoughts") or []:
                if isinstance(th, dict):
                    thoughts.append(dict(th, chunk=c))
        thoughts.sort(key=lambda th: (_num(th.get("t")), _num(th.get("item"))))
        a["thoughts"] = thoughts
        by_event = defaultdict(list)
        for r in a["reactions"]:
            by_event[str(r.get("event_id"))].append(r)
        a["by_event"] = by_event
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


OTHER_TYPES = ("P1", "N1", "C1", "O1")


def glyph_class(ev):
    """Neutral-ink flags of an event glyph: g-fill (fake) or g-hollow, ring (from an overseer), and for a submission
    ok (accepted or credited) or no (not accepted); g-other for the small status / own-action dots."""
    e = ev["event"]
    typ = e.get("type")
    flags = ["g-other"] if typ in OTHER_TYPES else ["g-fill" if e.get("fake") else "g-hollow"]
    if is_overseer(e):
        flags.append("ring")
    if typ == "S1":
        d = e.get("detail") or {}
        flags.append("ok" if d.get("credited") or str(e.get("outcome") or "").startswith("accepted") else "no")
    return " ".join(flags)


def _mark(typ, x, y, r, cls="m"):
    """The bare SVG element of a shape (square, diamond, triangle, small dot, tick or circle)."""
    if typ in ("L1", "L2"):
        return f'<rect class="{cls}" x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r:.1f}" height="{2 * r:.1f}"/>'
    if typ == "D1":
        k = r * 1.3
        return (f'<polygon class="{cls}" points="{x:.1f},{y - k:.1f} {x + k:.1f},{y:.1f} {x:.1f},{y + k:.1f} '
                f'{x - k:.1f},{y:.1f}"/>')
    if typ == "S1":
        return (f'<polygon class="{cls}" points="{x:.1f},{y - r * 1.3:.1f} {x + r * 1.2:.1f},{y + r:.1f} '
                f'{x - r * 1.2:.1f},{y + r:.1f}"/>')
    if typ in OTHER_TYPES:
        return f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{max(r * 0.4, 1.6):.1f}"/>'
    if typ == "TH":
        return f'<rect class="{cls}" x="{x - 1.6:.1f}" y="{y - r:.1f}" width="3.2" height="{2 * r:.1f}" rx="1"/>'
    return f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}"/>'


def shape(typ, x, y, r, cls, key, title):
    """One SVG glyph in neutral ink: the shape encodes the event type; ``cls`` flags (glyph_class) add the fill,
    the overseer ring and a submission's outcome mark. ``key`` None: not selectable (legend, links)."""
    flags = set(cls.split())
    attrs = f'class="g {cls}"' + (f' data-k="{esc(key)}" tabindex="0" role="button"' if key is not None else "")
    t = f"<title>{esc(title)}</title>" if title else ""
    if typ == "P2":
        d = f"M{x - r:.1f},{y - r:.1f}L{x + r:.1f},{y + r:.1f}M{x + r:.1f},{y - r:.1f}L{x - r:.1f},{y + r:.1f}"
        body = f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="{r + 1:.1f}"/><path class="x" d="{d}"/>'
    else:
        body = _mark(typ, x, y, r)
    ring = _mark(typ, x, y, r + 2.8, "ring") if "ring" in flags and typ != "P2" else ""
    extra = ""
    if "ok" in flags:  # a check mark above right: accepted or credited
        extra = (f'<path class="mk" d="M{x + r * 0.7:.1f},{y - r * 1.5:.1f}l{r * 0.45:.1f},{r * 0.5:.1f}'
                 f'l{r * 0.9:.1f},{-r * 1.1:.1f}"/>')
    elif "no" in flags:  # a slash through it: not accepted
        d = f"M{x - r * 1.3:.1f},{y + r * 1.4:.1f}L{x + r * 1.3:.1f},{y - r * 1.6:.1f}"
        extra = f'<path class="halo" d="{d}"/><path class="mk" d="{d}"/>'
    return f'<g {attrs}>{t}{ring}{body}{extra}</g>'


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
    return " \u00b7 ".join(bits)


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


def axis_max(agents):
    last = [AXIS_MIN]
    for a in agents.values():
        last += [_num(it.get("t")) for it in a["items"][-1:]]
        last += [_num(th.get("t")) for th in a["thoughts"][-1:]]
        for ph in (a["stage3"] or {}).get("phases") or []:
            last.append(_num(ph.get("to_t")))
    top = max(last)  # a run's last seconds may spill a little past its length: keep the run's own axis
    return AXIS_MIN if top <= AXIS_MIN + 5 else 30.0 * int((top + 29.999) // 30)


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


def page(title, body, scripts=""):
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta name="robots" content="noindex"><title>{esc(title)}</title><style>{CSS}</style></head>'
           f'<body>{body}{"<script>" + scripts + "</script>" if scripts else ""}</body></html>')
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
    return "; ".join(f"{model_label(mid)} " + (f"(stages {g[0]}\u2013{g[-1]})" if len(g) > 1 else f"(stage {g[0]})")
                     for g, mid in groups)


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
SHAPE_MEANING = (  # (type, glyph_class flags, name); drawn by shape() exactly as on the map
    ("L2", "g-hollow", "library file"), ("B1", "g-hollow", "board post"), ("D1", "g-hollow", "direct message"),
    ("S1", "g-hollow", "own submission"), ("P2", "g-hollow", "revert notice"),
    ("N1", "g-other", "problem list, unread notice, own post, restart"))
MARK_MEANING = (("L2", "g-hollow", "grey outline: ordinary or genuine"), ("L2", "g-fill", "pink, solid: fake"),
                ("D1", "g-hollow ring", "cyan with ring: from an overseer"),
                ("S1", "g-hollow ok", "\u2713 accepted or credited"), ("S1", "g-hollow no", "slash: not accepted"))
EXAMPLES = (("L2", "g-fill", "fake library file read"), ("D1", "g-hollow ring", "DM from an overseer"),
            ("S1", "g-fill ok", "fake submission, accepted"), ("S1", "g-hollow no", "genuine submission, rejected"))


def _legend_glyph(typ, cls):
    """A legend glyph from the map's own code: shape() for events, the stance bar rect for stances."""
    if typ == "TP":
        return '<svg viewBox="0 0 18 16"><line class="tp" x1="9" x2="9" y1="0" y2="16"/></svg>'
    if typ == "BAR":
        return f'<svg viewBox="0 0 22 16" class="swatch"><rect class="ph-{cls}" x="1" y="3" width="20" height="10" rx="2"/></svg>'
    return f'<svg viewBox="0 0 18 16">{shape(typ, 8, 8.5, 4.6, cls, None, "")}</svg>'


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


def terms_html():
    """The glossary: a separate, collapsed block after the sticky legend, so the legend stays short."""
    return ('<details class="terms"><summary>Terms used on these pages: trick, fake, judge, credited, closed, revert, '
            'overseer, seed, turn, item, chunk, certainty</summary><ul>'
            + "".join(f"<li><b>{esc(k)}</b>: {esc(v)}</li>" for k, v in GLOSSARY) + "</ul></details>")


def groups_html(names, href, defs=None):
    """The in-page code key: each group with its codes and short names; each code shows its definition on hover or
    focus (title) and expands on click; the full reference is the taxonomy page. Without names, one line saying the
    names are not shown."""
    defs = defs or {}
    if not any(names.get(L) for L in LETTERS):
        return ('<p class="note">Thoughts that the stage-1 reader quoted and coded into seven groups, A\u2013G; '
                'the group names are not shown on this copy.</p>')
    groups = []
    for L in LETTERS:
        codes = sorted((c for c in names if c[:1] == L and len(c) > 1), key=lambda c: int(c[1:]))
        items = "".join(
            f'<details class="code"><summary title="{esc(code_tip(c, defs, names))}"><b>{c}</b> {esc(names[c])}'
            f'</summary><p>{esc(code_tip(c, defs, names))} <a href="{esc(href)}#code-{c}">more</a></p></details>'
            for c in codes)
        groups.append(f'<div class="grp"><div class="gname" title="{esc(code_tip(L, defs, names, False))}">'
                      f'<b>{L}</b> {esc(names.get(L, ""))}</div>{items}</div>')
    return (f'<p class="note">Codes the stage-1 reader gave to quoted thoughts (and, for E and F, the stage-2 reader to '
            f'reactions). Hover or click a code for its definition; full reference with examples: '
            f'<a href="{esc(href)}">codes page</a>.</p><div class="codes">{"".join(groups)}</div>')


def legend(mode="agent", names=None, href="taxonomy.html", defs=None):
    """Two labelled boxes built from the map's own classes: bar colour = stance; shape = event, fill = fake,
    mark = overseer or outcome (with worked examples)."""
    names = names or {}
    sw = "".join(f'<li>{_legend_glyph("BAR", s)}<b>{STANCE_LABEL.get(s, s)}</b>&nbsp;{esc(m)}</li>'
                 for s, m in STANCE_MEANING)
    shapes = "".join(f'<li>{_legend_glyph(t, c)}{esc(m)}</li>' for t, c, m in SHAPE_MEANING)
    if mode == "agent":
        shapes += (f'<li>{_legend_glyph("TH", "g-th")}{_legend_glyph("TH", "g-th-low")}thought (hollow = low '
                   f'certainty)</li><li>{_legend_glyph("TP", "")}turning point</li>')
    marks = "".join(f'<li>{_legend_glyph(t, c)}{esc(m)}</li>' for t, c, m in MARK_MEANING)
    examples = "".join(f'<li>{_legend_glyph(t, c)}= {esc(m)}</li>' for t, c, m in EXAMPLES)
    where = {"index": "Shapes above an agent's bar are what it read (library files, overseer and other relevant posts, "
                      "DMs, reverts); below are its submissions. Click a shape to open it on the agent page.",
             "events": "Each event below carries its shape. ↓ / ↑ (or j / k) move through the events."
             }.get(mode, "Click any shape for its quote, the event and the stage-2 reaction; "
                         "→ / j next, ← / k previous, Esc closes.")
    return (f'<details class="legend" open><summary>Legend: bar colour = position on cheating; shape + colour = '
            f'event{"; tick = coded thought" if mode == "agent" else ""}; terms</summary>'
            f'<div class="lg{" three" if names or mode == "agent" else ""}"><div class="lgbox"><h4>Bar colour = the agent\'s position on cheating at that moment</h4>'
            f'<p class="note">{esc(POSITION_HINT)}.</p><ul>{sw}</ul>'
            f'<p class="note">These colours appear only on the position bars (the stage-3 reading; the stage-1 '
            f'reading where stage 3 is missing). Dashed outline = no reading.</p></div>'
            f'<div class="lgbox"><h4>Shape + colour = event</h4>'
            f'<ul>{shapes}</ul><ul class="marks">{marks}</ul><ul class="ex"><li><b>Read as:</b></li>{examples}</ul>'
            f'<p class="note">Event colours (pink, cyan) are never position colours.</p>'
            f'<p class="note">{where}</p></div>'
            + (f'<div class="lgbox"><h4>What it thought: code groups</h4>{groups_html(names, href, defs)}</div>'
               if names or mode == "agent" else "")
            + '</div></details>' + terms_html())


def axis_svg(tmax, x0, x1, y, h, ticks=True):
    out = []
    step = 30 if tmax <= 360 else 60
    t = 0
    while t <= tmax + 1e-9:
        x = x0 + (x1 - x0) * t / tmax
        out.append(f'<line class="tick" x1="{x:.1f}" x2="{x:.1f}" y1="{y}" y2="{y + h}"/>')
        if ticks:
            anchor = "end" if t + step > tmax + 1e-9 else "start" if t == 0 else "middle"
            out.append(f'<text x="{x:.1f}" y="{y + h + 11}" text-anchor="{anchor}">{t}</text>')
        t += step
    return "".join(out)


# ------------------------------------------------------------------ index.html

INDEX_TYPES = ("L2", "B1", "D1", "P2", "S1")


def stance_title(frm, to, st, label):
    return (f"Position on cheating: {STANCE_LABEL.get(st, st)} \u00b7 {fmt_span(frm, to)}"
            + (f" \u00b7 {label}" if label else ""))


def index_page(agents, pipeline, tmax, nav="", names=None):
    W, x0, x1 = 1000, 4, 996
    X = lambda t: x0 + (x1 - x0) * min(max(_num(t), 0.0), tmax) / tmax  # noqa: E731
    rows, counts = [], Counter()
    sources = sorted({str(((a["stage3"] or {}).get("final") or {}).get("source", "unknown"))
                      if a["stage3"] else "no stage 3" for a in agents.values()})
    for name, a in agents.items():
        f = (a["stage3"] or {}).get("final") or {}
        src = str(f.get("source", "unknown")) if a["stage3"] else "no stage 3"
        counts[src] += 1
        parts = [axis_svg(tmax, x0, x1, 0, 30, ticks=False)]
        phases, _ = phases_of(a, tmax)
        for frm, to, st, label in phases:
            cls = st if st in STANCES else "unknown"
            parts.append(f'<rect class="ph-{cls}" x="{X(frm):.1f}" y="11" width="{max(X(to) - X(frm), 1):.1f}" '
                         f'height="8" rx="2"><title>{esc(stance_title(frm, to, st, label))}'
                         f"</title></rect>")
        for ev in a["events"]:
            e = ev["event"]
            typ = e.get("type")
            if typ not in INDEX_TYPES or (typ in ("B1", "D1") and not (is_overseer(e) or set(e.get("tags") or []) & RELEVANT_TAGS)):
                continue
            y = 24 if typ == "S1" else 6
            key = f"ev-{ev.get('i')}"
            parts.append(f'<a href="agents/{esc(name)}.html#sel={key}">'
                         + shape(typ, X(ev.get("t")), y, 3.6, glyph_class(ev), None, event_title(ev)) + "</a>")
        data = {"source": src, "faked": _yn(f.get("faked")), "stopped": _yn(f.get("stopped")),
                "credited": _yn(f.get("credited")), "aware": str(f.get("aware_cheating") or "")}
        attrs = " ".join(f'data-{k}="{esc(v)}"' for k, v in data.items())
        story = (a["stage3"] or {}).get("story") or ""
        rows.append(f'<div class="row" data-agent="{esc(name)}" {attrs}>'
                    f'<div class="agent"><a href="agents/{esc(name)}.html" title="{esc(story)}">{esc(name)}</a></div>'
                    f'<div class="strip"><svg viewBox="0 0 {W} 30" preserveAspectRatio="none">{"".join(parts)}</svg></div>'
                    f'<div class="labels">{final_labels(a["stage3"])}</div></div>')
    header_axis = (f'<svg viewBox="0 0 {W} 16" preserveAspectRatio="none" style="display:block;width:100%;height:16px">'
                   + "".join(f'<text x="{x0 + (x1 - x0) * t / tmax:.1f}" y="12" text-anchor='
                             f'"{"start" if t == 0 else "end" if t + 30 > tmax else "middle"}">{t}</text>'
                             for t in range(0, int(tmax) + 1, 30)) + "</svg>")

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
    body = (f"<h1>Story map{(': ' + esc(run)) if run else ''}</h1>"
            f'{nav}{banner(model_names(pipeline))}'
            f'<p class="muted">{len(agents)} agents. {SOURCE_TITLE}: {summary}. Time axis: minutes from run start '
            f'(0\u2013{int(tmax)}).</p>'
            f"{legend('index', names, 'taxonomy.html', DEFS)}{filters}"
            f'<div class="grid"><div class="hd">Agent</div><div class="hd">{header_axis}</div>'
            f'<div class="hd lab">Final reading (stage 3)</div>'
            f'{"".join(rows) or "<div class=empty>No agents found.</div>"}</div>')
    return page(f"Story map {run}".strip(), body, LEGEND_JS + FILTER_JS)


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


def event_article(name, ev, reactions, key, names=None, href=""):
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
    kv = "".join(f"<dt>{esc(human(k))}</dt><dd>{esc(_shown(k, v))}</dd>"
                 for k, v in d.items() if v not in (None, "", [], {}) and k != "reread")
    k = "k-fake" if (e.get("fake") or typ == "P2") else "k-ov" if is_overseer(e) else ""
    kind = "sub" if typ == "S1" else "own" if typ in ("O1", "C1") else "exp"
    reacts = "".join(reaction_html(r, names, href) for r in reactions) if reactions else (
        reaction_html(None) if typ in ("L2", "B1", "D1", "P2") else "")
    head = event_label(e) if typ in ("L2", "B1", "D1", "S1", "P2", "O1") else type_name(typ)
    text = ev.get("text")
    if typ in ("L2", "S1", "P2", "L1", "P1"):
        text = tidy_times(text)  # extractor summaries (B1, D1, O1, C1 and N1 texts are what the agent saw)
    return (f'<article class="item {k}" id="{key}" data-kind="{kind}" data-t="{_num(ev.get("t")):.1f}">'
            f'<h3>{fmt_t(ev.get("t"))} &middot; {esc(head[:1].upper() + head[1:])}</h3>'
            f'<p>{"".join(meta)} <small>turn {esc(ev.get("turn"))} &middot; timeline item {esc(ev.get("i"))}</small></p>'
            f'<pre>{esc(cut(text))}</pre>{"<dl class=kv>" + kv + "</dl>" if kv else ""}{reacts}</article>')


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
    low = " g-th-low" if th.get("certainty") == "low" else ""
    grp = (f' <small>(group {code_link(group, names, href)})</small>' if names.get(group)
           else f" <small>(group {esc(group)})</small>")
    return (f'<article class="item k-th{low}" id="{key}" data-kind="th" data-t="{_num(th.get("t")):.1f}">'
            f'<h3>{fmt_t(th.get("t"))} &middot; thought {code_link(code, names, href)}{grp}</h3>'
            f'<p>{badge("reader certainty: " + str(th.get("certainty")))} <small>turn {esc(th.get("turn"))} &middot; '
            f'timeline item {esc(th.get("item"))} &middot; stage-1 chunk {esc(th.get("chunk"))}</small></p>'
            + (f'<p class="def">{esc(code_tip(code, DEFS, names))}</p>' if DEFS.get(code) else "")
            + f'<blockquote>{esc(th.get("quote"))}</blockquote>'
            f'{"<p>Reader note: " + esc(th.get("note")) + "</p>" if th.get("note") else ""}{ctx}{rel}</article>')


PANEL = ('<aside id="panel"><div class="stepper"><button type="button" id="pprev" title="Previous (\u2190 or k)">'
         '\u2190 Prev</button><span id="pos"></span><button type="button" id="pnext" title="Next (\u2192 or j)">Next \u2192'
         '</button><label>Step through <select id="kind"><option value="">all items</option>'
         '<option value="exp">only what it read or saw</option><option value="th">only thoughts</option>'
         '<option value="sub">only submissions</option><option value="own">only own posts and restarts</option>'
         '<option value="tp">only turning points</option></select></label>'
         '<button type="button" id="pclose" title="Close (Esc)" aria-label="Close">\u00d7</button></div>'
         '<div id="pbody"></div><p class="muted keys">\u2192 / j next, \u2190 / k previous, Esc closes.</p></aside>')


def agent_page(a, names, tmax, all_names, models="", nav="", href=""):
    name = a["name"]
    s3 = a["stage3"] or {}
    W, LX, x0, x1 = 1000, 0, 212, 990
    X = lambda t: x0 + (x1 - x0) * min(max(_num(t), 0.0), tmax) / tmax  # noqa: E731
    LH = 22
    lanes = ([("stance", POSITION, POSITION_HINT)]
             + [(f"th-{L}", f"Thought {L}: {names[L]}" if names.get(L) else f"What it thought: {L}",
                 f"Thoughts in code group {L}" + (f" ({names[L]})" if names.get(L) else "")
                 + ", quoted from the agent's reasoning and coded by the stage-1 reader; hollow tick = low reader "
                 "certainty" + ("".join(f"\n{c} {names[c]}" for c in sorted(
                     (c for c in names if c[:1] == L and len(c) > 1), key=lambda c: int(c[1:])))))
                for L in LETTERS]
             + [(k, label, hint) for k, label, _, hint in LANES])
    ypos = {k: 6 + n * LH for n, (k, _, _) in enumerate(lanes)}
    H = 6 + len(lanes) * LH + 18
    svg = []
    for n, (k, label, hint) in enumerate(lanes):
        y = ypos[k]
        if k != "stance":  # the stance bars sit on the card surface their colours were validated against
            svg.append(f'<rect class="lane-bg{" alt" if n % 2 else ""}" x="{x0}" y="{y}" width="{x1 - x0}" height="{LH}"/>')
        svg.append(f'<text class="lanetext" x="{LX + 4}" y="{y + 15}"><title>{esc(label)}: {esc(hint)}</title>'
                   f'{esc(label if len(label) <= 36 else label[:35] + "…")}</text>')
    svg.append(axis_svg(tmax, x0, x1, 6, len(lanes) * LH))
    svg.append(f'<line id="cursor" class="cursor" x1="0" x2="0" y1="6" y2="{6 + len(lanes) * LH}" visibility="hidden"/>')

    articles = []  # (t, order, html)
    key_of_event = {}
    for ev in a["events"]:
        key_of_event.setdefault(str(ev["event"].get("id")), f"ev-{ev.get('i')}")
    # Stance lane.
    phases, phase_src = phases_of(a, tmax)
    y = ypos["stance"]
    for frm, to, st, label in phases:
        cls = st if st in STANCES else "unknown"
        svg.append(f'<rect class="ph-{cls}" x="{X(frm):.1f}" y="{y + 4}" width="{max(X(to) - X(frm), 1):.1f}" '
                   f'height="{LH - 8}" rx="2"><title>{esc(stance_title(frm, to, st, label))}</title></rect>')
    for n, tp in enumerate(s3.get("turning_points") or []):
        key = f"tp-{n}"
        x = X(tp.get("t"))
        tip = (f"Turning point \u00b7 {fmt_t(tp.get('t'))} \u00b7 position on cheating "
               f"{STANCE_LABEL.get(tp.get('from'), tp.get('from'))} \u2192 {STANCE_LABEL.get(tp.get('to'), tp.get('to'))}")
        svg.append(f'<line class="tp" data-k="{key}" x1="{x:.1f}" x2="{x:.1f}" y1="{y}" y2="{6 + len(lanes) * LH}">'
                   f"<title>{esc(tip)}</title></line>")
        causes = ", ".join(cause_link(c, key_of_event) for c in tp.get("cause_event_ids") or []) or "none named"
        articles.append((_num(tp.get("t")), 0, f'<article class="item k-tp" id="{key}" data-kind="tp" data-t="{_num(tp.get("t")):.1f}"><h3>{fmt_t(tp.get("t"))} '
                         f'&middot; turning point: position on cheating {esc(STANCE_LABEL.get(tp.get("from"), tp.get("from")))} &rarr; '
                         f'{esc(STANCE_LABEL.get(tp.get("to"), tp.get("to")))}</h3>'
                         f'<p>Caused by (stage-3 reading): {causes}</p><p>{esc(plain_ids(tp.get("cause_note")))}</p></article>'))

    # Event lanes and reactions.
    cited = defaultdict(list)  # timeline item index -> [(event key, event id, reaction)]
    for eid, rs in a["by_event"].items():
        for r in rs:
            for q in r.get("quotes") or []:
                if isinstance(q, dict) and q.get("item") is not None:
                    cited[q["item"]].append((key_of_event.get(eid, ""), eid, r))
    lane_of = {t: k for k, _, types, _ in LANES for t in types}
    for ev in a["events"]:
        e = ev["event"]
        key = f"ev-{ev.get('i')}"
        lane = lane_of.get(e.get("type"), "own")
        svg.append(shape(e.get("type"), X(ev.get("t")), ypos[lane] + LH / 2, 5, glyph_class(ev), key, event_title(ev)))
        articles.append((_num(ev.get("t")), 1, event_article(name, ev, a["by_event"].get(str(e.get("id")), []), key,
                                                             names, href)))
    seen_ids = set(key_of_event)
    orphans = [r for eid, rs in a["by_event"].items() if eid not in seen_ids for r in rs]

    # Thought lanes.
    items_by_i = {it.get("i"): it for it in a["items"]}
    for n, th in enumerate(a["thoughts"]):
        key = f"th-{n}"
        L = str(th.get("code") or "?")[:1].upper()
        lane = f"th-{L}" if L in LETTERS else "th-G"
        cls = "g-th-low" if th.get("certainty") == "low" else "g-th"
        svg.append(shape("TH", X(th.get("t")), ypos[lane] + LH / 2, 4.5, cls, key,
                         f"Thought {code_name(str(th.get('code')), names, str(th.get('code')))} \u00b7 "
                         f"{fmt_t(th.get('t'))} \u00b7 \u201c{str(th.get('quote') or '')[:120]}\u201d"
                         + (f"\n\n{code_tip(str(th.get('code')), DEFS, names)}" if DEFS.get(str(th.get('code'))) else "")))
        related = [(k, eid, r) for k, eid, r in cited.get(th.get("item"), [])]
        articles.append((_num(th.get("t")), 2, thought_article(th, names, related, key, items_by_i, href)))

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
    body = (nav.replace("</nav>", f"{prev_link}{next_link}</nav>") + f"<h1>{esc(name)}</h1>{banner(models)}"
            f'<p>{final_labels(a["stage3"])}</p>'
            f'<p class="story">{esc(s3.get("story")) or "<span class=muted>No model-written summary for this agent.</span>"}</p>'
            + (f'<p class="muted">Not available for this agent: {", ".join(missing)}.</p>' if missing else "")
            + (f'<p class="muted">{len(a["malformed"])} model output(s) could not be read and are left out '
               f'({esc(", ".join(a["malformed"]))}).</p>' if a["malformed"] else "")
            + f'<p class="muted">{plural(len(a["events"]), "event")}, {plural(len(a["thoughts"]), "coded thought")}, '
            f'{plural(len(a["reactions"]), "stage-2 reaction")}. Position on cheating from the {esc(phase_src)}.'
            f'{" Role: " + esc(info.get("role")) + "." if info.get("role") else ""}</p>'
            f'{legend("agent", names, href, DEFS)}<div class="layout"><div class="lanes"><svg viewBox="0 0 {W} {H}">{"".join(svg)}</svg></div>'
            f'{PANEL}<div class="list"><h2>In time order</h2><div id="items">'
            f'{"".join(h for _, _, h in articles) or "<div class=empty>Nothing recorded.</div>"}</div>'
            + ("<h2>Reactions to events not in the timeline</h2>" + "".join(reaction_html(r, names, href) for r in orphans)
               if orphans else "")
            + '</div></div>'
            f'<h2>Final reading (stage 3)</h2>{"<dl class=kv>" + final_kv + "</dl>" if final_kv else "<p class=muted>No final reading.</p>"}'
            + (f'<h3>Consistency flags</h3><p class="muted">{esc(FLAG_HINT)}</p><ul>{flags}</ul>' if flags else "")
            + (f'<h2>Stage-1 reading, chunk by chunk</h2><p class="muted">The state the stage-1 reader recorded at the end '
               f'of each chunk of the timeline.</p><table><tr><th>Chunk</th><th>Span</th><th>Position on cheating</th>'
               f"<th>Knows the trick</th><th>Thinks the judge</th><th>Reader's summary</th></tr>{states}</table>"
               if states else ""))
    return page(f"Story map {name}", body, LEGEND_JS + PANEL_JS)


# ------------------------------------------------------------------ events.html

def _group_key(e):
    typ, eid, d = e.get("type"), str(e.get("id") or ""), e.get("detail") or {}
    if typ == "L2":
        path = d.get("path") or eid.split(":", 1)[-1].rsplit("@", 1)[0]
        return f"L2:{path}"
    return eid


EV_SECTIONS = (("ov", "Overseer posts and DMs"), ("board", "Other posts and DMs"),
               ("revert", "Reverts of own credit"), ("lib", "Library files"))
EV_SECTION_HINT = {"ov": "Board posts and DMs written by an overseer",
                   "board": "Solvers' board posts and DMs that describe the trick, warn or rule about it, or drew a "
                            "stage-2 reaction",
                   "revert": "An agent seeing that an overseer removed its own credit",
                   "lib": "Shared-library files the agents read (all revisions of one path together)"}


def _event_groups(agents):
    groups, skipped = {}, Counter()
    for name, a in agents.items():
        for ev in a["events"]:
            e = ev["event"]
            typ = e.get("type")
            if typ not in ("L2", "B1", "D1", "P2"):
                continue
            reacts = a["by_event"].get(str(e.get("id")), [])
            if typ in ("B1", "D1") and not (is_overseer(e) or set(e.get("tags") or []) & RELEVANT_TAGS or reacts):
                skipped[typ] += 1
                continue
            g = groups.setdefault(_group_key(e), {"type": typ, "first": ev, "readers": {}, "fake": e.get("fake"),
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
    return "revert" if g["type"] == "P2" else "lib" if g["type"] == "L2" else "ov" if g["overseer"] else "board"


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
                    f'value="{k}" checked> {esc(lab)} ({count[k]})</label>' for k, lab in EV_SECTIONS)
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
    body = (f"{nav}<h1>Story map: events</h1>{banner(models)}{legend('events', names, 'taxonomy.html', DEFS)}"
            f'<p class="muted">Each event: who wrote it, when it was first seen, which agents read it, and how each '
            f'reacted (stage-2 reading). Library files are grouped by path across revisions. Click a row to open it; '
            f'click a column heading to sort. ↓ / ↑ (or j / k) move through the shown events, Enter opens one. '
            f'{plural(n_skip, "board post or DM", "board posts and DMs")} unrelated to the trick and without a '
            f'reaction {"is" if n_skip == 1 else "are"} not listed.</p>{filters}'
            f'<table class="evt" id="evt"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
            f'<div id="cards" hidden>{"".join(cards)}</div>'
            + ('' if order else '<div class="empty">No events.</div>'))
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
    title = event_label(e)
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
    author = _who(e) if e.get("author") else "—"
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
    if g["fake"] is True:
        meta.append(badge("fake", "bad"))
    elif g["fake"] is False:
        meta.append(badge("not fake", "good"))
    meta += [badge(TAG_LABEL.get(t, str(t).replace("_", " ")), "caution") for t in e.get("tags") or []]
    ids = sorted(g["ids"])
    n = len(readers)
    title = event_label(e)
    title = title[:1].upper() + title[1:]
    icon = f'<svg class="hglyph" viewBox="0 0 18 16">{shape(e.get("type"), 9, 8, 5, glyph_class(ev), None, "")}</svg>'
    text = tidy_times(ev.get("text")) if e.get("type") in ("L2", "P2") else ev.get("text")
    return (f'<section class="ev" id="{esc(gk)}" data-sec="{_sec_of(g)}" tabindex="-1"><h3>{icon}{esc(title)}</h3>'
            f'<p>{"".join(meta)} '
            f'<small>first seen at {fmt_t(ev.get("t"))} by {esc(readers[0][0])} &middot; {plural(n, "reader")}'
            f'{" &middot; " + plural(len(ids), "revision") if len(ids) > 1 else ""}</small></p>'
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


def taxonomy_page(text, models="", nav=""):
    body = (f"{nav}{banner(models)}"
            '<p class="muted">The code set the model readers apply. On the agent pages each thought links here; the '
            'legend box "What it thought: code groups" gives each code\'s short definition in place.</p>'
            f'<div class="story">{markdown_html(text, anchors=True)}</div>')
    return page("Story map codes", body)


def nav_html(prefix="", has_val=False, has_tax=False):
    """The same top navigation on every page: Agents · Events · Validation · Codes."""
    links = [("index.html", "Agents"), ("events.html", "Events")]
    links += [("validation.html", "Validation")] if has_val else []
    links += [("taxonomy.html", "Codes")] if has_tax else []
    return '<nav class="top">' + "".join(f'<a href="{prefix}{h}">{t}</a>' for h, t in links) + "</nav>"


# ------------------------------------------------------------------ main

def build(map_dir, timeline=None, out=None, taxonomy=DEFAULT_TAXONOMY):
    """Write index.html, events.html and agents/<agent>.html; return the list of written paths."""
    agents, pipeline = load(map_dir, timeline)
    out = Path(out or map_dir)
    (out / "agents").mkdir(parents=True, exist_ok=True)
    names = load_taxonomy(taxonomy) if taxonomy else {}
    if names and "E" not in names and any(c[:1] == "E" for c in names):  # E codes describe reactions (stage 2)
        names["E"] = "Reactions (stage 2)"
    DEFS.clear()
    DEFS.update(load_definitions(taxonomy) if taxonomy else {})
    has_tax = bool(names) and Path(taxonomy).is_file()
    tmax = axis_max(agents)
    models = model_names(pipeline)
    validation = Path(map_dir) / "validation.md"
    has_val = validation.is_file()
    nav = nav_html("", has_val, has_tax)
    written = []

    def write(path, text):
        path.write_text(text, encoding="utf-8")
        written.append(path)
    write(out / "index.html", index_page(agents, pipeline, tmax, nav, names))
    write(out / "events.html", events_page(agents, tmax, models, nav, names))
    if has_val:
        write(out / "validation.html", validation_page(validation.read_text(encoding="utf-8"), models, nav))
    if has_tax:
        write(out / "taxonomy.html", taxonomy_page(Path(taxonomy).read_text(encoding="utf-8"), models, nav))
    agent_nav = nav_html("../", has_val, has_tax)
    for a in agents.values():
        write(out / "agents" / f"{a['name']}.html", agent_page(a, names, tmax, agents.keys(), models, agent_nav,
                                                               "../taxonomy.html" if has_tax else ""))
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map", required=True, help="stage outputs dir (runs/<id>/analysis/storymap)")
    ap.add_argument("--timeline", help="stage-0 dir (holding timeline/ or the agent JSONL files)")
    ap.add_argument("--out", help="output dir (default: --map)")
    ap.add_argument("--taxonomy", default=str(DEFAULT_TAXONOMY),
                    help="taxonomy for code names, definitions and taxonomy.html (default: the public copy; '' = none)")
    args = ap.parse_args(argv)
    written = build(args.map, args.timeline, args.out, args.taxonomy)
    print(f"wrote {len(written)} pages to {Path(args.out or args.map)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
