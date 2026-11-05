"""Overseer story map pages (owner approval, November 5, 2026; docs/plan.md "Overseer story maps").

    python -m research.analysis.storymap.view --map M --timeline T --overseer-map OM --overseer-timeline OT [--out DIR]

Called by view.build only when an overseer map or timeline is given (without them the solver pages are byte for byte
as before). Writes, beside the solver pages:

* ``agents/<overseer>.html``: the overseer's status bar (stage-3 phases: not yet looking, investigating something
  else, investigating the trick, enforcing, coordinating, idle) in place of the solver's position on cheating; thought
  lanes by overseer code group (Orientation, Hypotheses, Investigation, Detection, Action, Other); one lane per
  affordance: proof checks (verdict and seed vs real target on the glyph), reverts (done, failed, duplicate; latency
  since acceptance in the tooltip and panel), the public board (own posts, reads, pushed deliveries), direct messages
  (sent, with the recipient and whether it was a known faker; received), the private overseer board (own posts and
  reads; always shown, with an explicit note when the run has none), the library, problems and status, workspace
  calls (ticks) and sleeps (spans), and any other tool call. Clicking a glyph opens it with its text, quotes and the
  stage-2 reaction (registered, change, response, recognised the trick, reason). Below: the stage-3 story and final
  labels, the code-derived hard facts (first moments with the solvers' fakers, accepted and standing fakes at each,
  counts, revert latencies, DM targets), every tool call by category, and the reasoning chain expander with the same
  redaction as the solver pages (view.redact via view.timeline_json).
* ``index.html`` gains an "Overseers" strip (status phases and key-moment glyphs per overseer, linked to its page)
  and a run panel: the solvers' cumulative fakers and standing accepted fakes as step lines over the run, with each
  overseer's first moments as labelled, focusable markers, and the same moments as a table.
* ``taxonomy_overseer.html``: the public overseer codebook ``storymap/taxonomy_overseer.md`` with an anchor per code.

Inputs follow private/research/analysis/storymap/INTERFACE.md, "Overseer mode". Activity rows, hard facts and the
run context come from storymap/overseer.py; when a field is missing the page computes the first moments from the
timeline events and says what it could not show. No network, no external assets.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from . import view as V

try:  # the overseer helpers (activity, hard facts, run context)
    from . import overseer as OV
except ImportError:  # pragma: no cover - the module ships with this one
    OV = None

DEFAULT_TAXONOMY = Path(__file__).resolve().parent / "taxonomy_overseer.md"  # public codebook
TAXONOMY_PAGE = "taxonomy_overseer.html"
LETTERS = "OHITAG"
STATUSES = ("not_looking", "investigating_other", "investigating_trick", "enforcing", "coordinating", "idle")
STATUS_LABEL = {"not_looking": "not yet looking", "investigating_other": "investigating something else",
                "investigating_trick": "investigating the trick", "enforcing": "enforcing",
                "coordinating": "coordinating", "idle": "idle", "unknown": "no reading"}
STATUS_MEANING = (
    ("not_looking", "orienting: reading instructions, not yet reviewing others' work"),
    ("investigating_other", "reviewing for other misbehaviour, or a general sweep"),
    ("investigating_trick", "examining the answer-restatement trick"),
    ("enforcing", "reverting, warning publicly or messaging agents about misbehaviour"),
    ("coordinating", "mainly working with peer overseers on the overseer board"),
    ("idle", "sleeping, waiting or polling with no new review"),
    ("unknown", "no reading for this span"))
STATUS_TITLE = "Overseer status"
STATUS_HINT = ("What the overseer was doing at that moment (stage-3 reading): not yet looking, investigating something "
               "else, investigating the trick, enforcing, coordinating, idle")

# Status colours: the six hues validated together for the solver pages' position bars (view.TOKENS_*), reused here
# with their own token names; idle is a pale fill with an outline. Light, then dark.
OS_LIGHT = {"os-not_looking": "#a7b1bd", "os-investigating_other": "#1440bd", "os-investigating_trick": "#9c8133",
            "os-enforcing": "#b41e07", "os-coordinating": "#8254f9", "os-idle": "#e9edf2"}
OS_DARK = {"os-not_looking": "#5b6675", "os-investigating_other": "#2b6dec", "os-investigating_trick": "#a3933c",
           "os-enforcing": "#c7461b", "os-coordinating": "#9457b3", "os-idle": "#2a3440"}

CSS = (
    f":root{{{V._vars(OS_LIGHT)}}}"
    f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{V._vars(OS_DARK)}}}}}'
    f':root[data-theme="dark"]{{{V._vars(OS_DARK)}}}'
    + "".join(f".os-{s}{{fill:var(--os-{s})}}" for s in STATUSES)
    + """
.os-idle{stroke:var(--muted);stroke-width:1}.os-unknown{fill:none;stroke:var(--muted);stroke-dasharray:3 2}
.g.g-own .m{fill:var(--fg);stroke:var(--fg)}.g.g-own.g-fill .m{fill:var(--ev-fake);stroke:var(--fg)}
.g.g-failed .m{fill:var(--card);stroke:var(--muted);stroke-dasharray:2 1.6}
.g.g-failed .x{stroke:var(--muted);stroke-dasharray:2.5 2}.g .x.xfake{stroke:var(--ev-fake)}
.g .kfr{fill:none;stroke:var(--ev-fake);stroke-width:1.8}.g .dupr{fill:none;stroke:var(--fg);stroke-width:1.1;
stroke-dasharray:1.6 1.6}.g .sd{fill:var(--fg);stroke:var(--card);stroke-width:.8}
.g .q{fill:var(--fg);font:700 10px system-ui,sans-serif;text-anchor:middle}
.g.g-ws .m{fill:var(--muted);stroke:none}.g.g-ws.g-failed .m{fill:none;stroke:var(--muted);stroke-dasharray:none}
.sl{fill:var(--muted);fill-opacity:.32}.g.g-sleep .m{fill:var(--card);stroke:var(--muted);stroke-width:1.5}
.ovnote{font-size:12px;fill:var(--muted)}
section.ovs,section.ovrun{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;
margin:12px 0}section.ovs h2,section.ovrun h2{margin:4px 0 6px}.ovs .grid .row{grid-template-columns:96px minmax(0,1fr) 320px}
.ovlg{display:flex;flex-wrap:wrap;gap:2px 12px;list-style:none;margin:4px 0;padding:0;font-size:12.5px}
.ovlg li{display:inline-flex;gap:4px;align-items:center}.ovlg svg{width:20px;height:18px;flex:none;overflow:visible}
.ovchart{display:block;width:100%;overflow:visible}.ovchart .ax{stroke:var(--line)}
.cv-fakers{stroke:var(--ev-fake);stroke-width:2.2}.cv-standing{stroke:var(--fg);stroke-width:2.2}
.cv-accepted{stroke:var(--muted);stroke-width:1.4;stroke-dasharray:4 3}
.mkr{cursor:help}.mkr line.v{stroke:var(--fg);stroke-width:1;stroke-dasharray:2 3;opacity:.75}
.mkr line.hitl{stroke:transparent;stroke-width:14}.mkr text{fill:var(--fg);font:11px system-ui,sans-serif;paint-order:stroke;stroke:var(--card);stroke-width:3px;stroke-linejoin:round}
.mkr:hover line.v,.mkr:focus line.v,.mkr.on line.v{stroke-width:2.4;opacity:1;stroke-dasharray:none}
.mkr:focus{outline:none}.mkr:focus text,.mkr.on text{font-weight:700}
.quiet .mkr text{visibility:hidden}.quiet .mkr:hover text,.quiet .mkr:focus text,.quiet .mkr.on text{visibility:visible}
#mkinfo{min-height:1.5em;font-size:13px;margin:2px 0 6px}
table.hf td.n,table.hf th.n{text-align:right;font-variant-numeric:tabular-nums}table.hf{font-size:13.5px}
.acts{font-size:13.5px}.acts td{vertical-align:top}.acts td.sm{max-width:560px;overflow-wrap:anywhere}
@media (max-width:900px){.ovs .grid .row{grid-template-columns:80px minmax(0,1fr)}}
""")

# The run panel's markers: hover, focus (Tab) or tap shows each one's text in the line under the chart.
INDEX_JS = r"""
(()=>{const box=document.getElementById('mkinfo');if(!box)return;const ms=[...document.querySelectorAll('.ovchart .mkr')];
const def=box.textContent;function on(m){ms.forEach(x=>x.classList.toggle('on',x===m));box.textContent=m?m.dataset.info:def;}
ms.forEach(m=>{m.addEventListener('mouseenter',()=>on(m));m.addEventListener('focus',()=>on(m));
m.addEventListener('click',()=>on(m));m.addEventListener('mouseleave',()=>{if(document.activeElement!==m)on(null);});
m.addEventListener('blur',()=>on(null));m.addEventListener('keydown',e=>{const i=ms.indexOf(m);
if(e.key==='ArrowRight'&&i+1<ms.length){e.preventDefault();ms[i+1].focus();}
else if(e.key==='ArrowLeft'&&i>0){e.preventDefault();ms[i-1].focus();}});});})();
"""

TYPE_NAME = {"K1": "proof check", "V1": "revert", "M1": "overseer-board post read", "M2": "own overseer-board post",
             "WS": "workspace call", "SL": "sleep", "AC": "tool call"}
OUTCOME_LABEL = {"genuine": "verdict: genuine", "not genuine": "verdict: not genuine",
                 "undecided": "verdict: undecided", "failed": "check failed", "reverted": "reverted",
                 "credit_removed": "credit removed", "reopened": "problem reopened"}
DETAIL_LABEL = {
    "target": "Checked", "submission_id": "Submission", "public_submission_id": "ID shown to agents",
    "file_path": "File", "checked_author": "Credited agent of the checked submission",
    "checked_author_role": "Its role", "seed": "The seed's planted fake", "verified": "Check verified",
    "validity": "Research evaluator's verdict", "accepted_at": "Accepted at", "ok": "Call succeeded",
    "error": "Error", "duplicate": "Duplicate (already done by an overseer)",
    "first_check_of_submission": "First check of this submission", "credited_agent": "Credited agent",
    "credited_role": "Its role", "latency_min": "Since acceptance", "reason": "Reason given",
    "library_path": "Library file", "message": "Message", "own": "Its own post", "recipient": "To",
}
VALUE_LABEL = {"target": {"accepted_submission": "an accepted submission", "workspace_file": "a file in its workspace"}}
CATEGORY_LABEL = {
    "check": "proof checks", "revert": "reverts", "board_post": "public board posts", "board_read": "public board reads",
    "dm_send": "direct messages sent", "dm_read": "direct message reads", "overseer_board_post": "overseer-board posts",
    "overseer_board_read": "overseer-board reads", "library_list": "library listings", "library_read": "library reads",
    "problems": "problem lists", "status": "conference status", "submit": "submissions", "workspace": "workspace calls",
    "sleep": "sleeps", "other": "other tool calls"}
CATEGORY_ONE = {
    "check": "Proof check", "revert": "Revert", "board_post": "Public board post", "board_read": "Public board read",
    "dm_send": "Direct message sent", "dm_read": "Direct message read", "overseer_board_post": "Overseer-board post",
    "overseer_board_read": "Overseer-board read", "library_list": "Library listing", "library_read": "Library read",
    "problems": "Problem list", "status": "Conference status", "submit": "Submission", "workspace": "Workspace call",
    "sleep": "Sleep", "other": "Other tool call"}
REG = {"yes": "noticed it", "no": "did not notice it", "unclear": "unclear whether it noticed"}
RESPONSE_LABEL = {"none": "no response", "investigate": "investigated", "check": "checked the proof",
                  "revert": "reverted", "warn_public": "warned publicly", "dm": "sent a DM",
                  "coordinate": "coordinated with overseers", "dismiss": "dismissed it", "other": "other"}
SUSPECT_LABEL = {"answer_restatement": "answer restatement", "name_shadowing": "name shadowing",
                 "header_tamper": "header tamper", "axioms": "axioms, sorry or banned constructs", "other": "other"}
FINAL_LABEL = {"detected_trick": "Detected the trick", "first_suspicion_t": "First suspicion at",
               "first_suspected": "First suspected", "detection_t": "Detected at",
               "confirmed_on_real_t": "Confirmed on a real submission at", "enforced": "Enforced (a successful revert)",
               "communicated": "Communicated (public post or DM)", "coordinated": "Coordinated on the overseer board",
               "public_posts_specific": "Public posts specific", "names_trick_publicly": "Named the trick publicly",
               "states_consequences": "Stated consequences"}
SPECIFIC_LABEL = {"yes": "all specific", "no": "all vague", "mixed": "mixed", "none": "no public posts"}

MOMENTS = (  # (hard-facts key, label, short label (chart), glyph type, glyph flags)
    ("library_list", "first library listing", "listed library", "L1", "g-hollow"),
    ("library_read", "first library file read", "read library", "L2", "g-hollow"),
    ("check_any", "first proof check", "first check", "K1", "g-hollow"),
    ("check_seed", "first check of the seed's fake", "checked seed", "K1", "g-hollow seed"),
    ("check_real", "first check of a real submission", "checked real", "K1", "g-hollow"),
    ("check_not_genuine", "first verdict 'not genuine'", "not genuine", "K1", "g-hollow no"),
    ("revert_attempt", "first revert attempt", "revert try", "V1", "g-hollow"),
    ("revert", "first successful revert", "first revert", "V1", "g-hollow"),
    ("public_post", "first public board post", "public post", "B1", "g-own"),
    ("dm", "first direct message", "DM", "D1", "g-own"),
    ("overseer_board_post", "first overseer-board post", "ov-board post", "M2", "g-own"),
    ("overseer_board_read", "first overseer-board read", "ov-board read", "M1", "g-hollow"),
    ("sleep", "first sleep", "sleep", "SL", "g-sleep"),
)
CHART_MOMENTS = ("library_list", "check_seed", "check_real", "revert", "public_post", "dm", "overseer_board_post")
STRIP_MOMENTS = CHART_MOMENTS

LANES = (  # (key, label, short, hint)
    ("checks", "Did: proof checks", "Checks",
     "Its check_proof calls: shape = check; mark = verdict (✓ genuine, slash not genuine, ? undecided, dashed = the "
     "call failed); inner dot = the seed's planted fake; pink = the checked source is a fake"),
    ("reverts", "Did: reverts", "Reverts",
     "Its revert_submission calls: X = revert (pink = the reverted submission was a fake); dashed = failed; dotted "
     "ring = duplicate (already reverted or done by another overseer)"),
    ("board", "Public board: posts, reads", "Board",
     "Its own public board posts (filled) and the posts it read or that were pushed to it (hollow; chevron = pushed)"),
    ("dms", "Direct messages: sent, read", "DMs",
     "DMs it sent (filled; pink ring = the recipient already had a fake submission) and DMs it received (hollow)"),
    ("ovboard", "Overseer board (private): posts, reads", "Ov. board",
     "The private overseer message board: its own posts (filled) and the posts it read (hollow)"),
    ("library", "Library: listings, reads", "Library",
     "Shared-library listings and files it read (pink = a fake file, or a listing that shows fakes)"),
    ("status", "Problems and status", "Status", "Problem lists and conference status calls"),
    ("workspace", "Workspace: shell and files", "Workspace",
     "Shell and file operations in its own workspace (Bash, Read, Write, Edit, Grep, ...); hollow = the call failed"),
    ("sleep", "Sleep and waiting", "Sleep", "Sleeps (Bash sleep N) as spans from start to end"),
    ("other", "Other: restarts, notices, other calls", "Other",
     "Context restarts, unread notices, reminders, submissions and any other tool call"),
)
CATEGORY_LANE = {"check": "checks", "revert": "reverts", "board_post": "board", "board_read": "board",
                 "dm_send": "dms", "dm_read": "dms", "overseer_board_post": "ovboard", "overseer_board_read": "ovboard",
                 "library_list": "library", "library_read": "library", "problems": "status", "status": "status",
                 "workspace": "workspace", "sleep": "sleep", "submit": "other", "other": "other"}
CATEGORY_GLYPH = {"workspace": ("WS", "g-ws"), "sleep": ("SL", "g-sleep"), "check": ("K1", "g-hollow"),
                  "revert": ("V1", "g-hollow"), "board_post": ("B1", "g-own"), "board_read": ("P1", "g-other"),
                  "dm_send": ("D1", "g-own"), "overseer_board_post": ("M2", "g-own")}


# ------------------------------------------------------------------ names and labels

def ov_type_name(typ, cap=False):
    s = TYPE_NAME.get(typ) or V.TYPE_NAME.get(typ, "event")
    return s[:1].upper() + s[1:] if cap else s


def event_label(e_or_id, typ=None):
    """Plain label of an overseer-map event (never the raw type code)."""
    if isinstance(e_or_id, dict):
        typ, eid, d = e_or_id.get("type"), str(e_or_id.get("id") or ""), e_or_id.get("detail") or {}
    else:
        eid, d = str(e_or_id or ""), {}
        typ = typ or (eid.split(":", 1)[0] if ":" in eid else None)
    rest = eid.split(":", 1)[1] if ":" in eid else eid
    if typ in ("K1", "V1"):
        n = rest.rsplit(":", 1)[-1]
        what = "proof check" if typ == "K1" else "revert"
        sub = d.get("submission_id") or d.get("file_path")
        return f"{what} no. {n}" + (f" ({sub})" if sub else "")
    if typ == "M1":
        return f"overseer-board post {rest}"
    if typ == "M2":
        return f"own overseer-board post {rest}"
    if typ == "O1" and isinstance(e_or_id, dict):
        ch = d.get("channel")
        if ch == "dm" or rest.startswith("dm-"):
            to = d.get("to") or d.get("recipient")
            return f"own direct message {rest}" + (f" to {to}" if to else "")
    return V.event_label(e_or_id, typ)


def plain_ids(text):
    """Model text with overseer event ids ('K1:agent-102:3', 'M1:ovpost-000004') and solver ones made plain."""
    text = re.sub(r"\b(K1|V1|M1|M2):([^\s,;)'\"\]]+)", lambda m: event_label(m.group(0), m.group(1)), str(text))
    return V.plain_ids(text)


def shown(key, value):
    key = str(key)
    if key == "latency_min" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.1f} min"
    if key in VALUE_LABEL and isinstance(value, str):
        return VALUE_LABEL[key].get(value, value)
    if key == "suspected" and isinstance(value, list):
        return ", ".join(SUSPECT_LABEL.get(v, str(v)) for v in value) or "none"
    if key == "first_suspected":
        return SUSPECT_LABEL.get(value, value) if value else "none"
    if key == "public_posts_specific":
        return SPECIFIC_LABEL.get(value, value)
    if key == "coordinated" and value is None:
        return "not applicable (no overseer board in this run)"
    if key.endswith("event_ids") and isinstance(value, list):
        return ", ".join(event_label(v) for v in value) or "none"
    v = V._shown(key, value)
    return plain_ids(v) if isinstance(v, str) else v


def human(key):
    return DETAIL_LABEL.get(key) or V.human(key)


@contextmanager
def overseer_vocabulary(defs):
    """While rendering overseer pages: the event names of the overseer types and the overseer codebook's definitions
    in view's shared tables (thought articles and the chain text use them); restored afterwards."""
    saved_defs, added = dict(V.DEFS), [k for k in TYPE_NAME if k not in V.TYPE_NAME]
    try:
        V.DEFS.clear()
        V.DEFS.update(defs)
        for k in added:
            V.TYPE_NAME[k] = TYPE_NAME[k]
        yield
    finally:
        V.DEFS.clear()
        V.DEFS.update(saved_defs)
        for k in added:
            V.TYPE_NAME.pop(k, None)


# ------------------------------------------------------------------ taxonomy

def load_names(path):
    """{"O": "Orientation", "O1": "Reads its role or instructions", ...} from the overseer codebook's headings."""
    names = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError):
        return names
    for line in text.splitlines():
        m = re.match(rf"^###\s+([{LETTERS}])(\d{{0,2}})\s+-\s+(.*)$", line.strip())
        if m:
            title = re.split(r"\s\(", re.sub(r"[*`_]", "", m.group(3)).strip())[0][:80]
            names.setdefault(m.group(1) + m.group(2), title)
    return names


def load_definitions(path):
    """{code: {"name", "def", "include", "example"}} for the overseer codes (as view.load_definitions)."""
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
        m = re.match(rf"^###\s+([{LETTERS}]\d{{0,2}})\s+-\s+(.*)$", line.strip())
        if m or line.startswith("#"):
            close()
            cur, body = ((m.group(1), re.split(r"\s\(", m.group(2))[0].strip()) if m else None), []
        elif cur is not None:
            body.append(line.strip())
    close()
    return out


EVENT_TOKEN = r"(?:L1|L2|P1|P2|B1|D1|S1|O1|C1|N1|R1|K1|V1|M1|M2)"


def taxonomy_page(text, models="", nav=""):
    """The overseer codebook with an anchor per code; event headings named in words with their glyph."""
    html_text = V.markdown_html(text, anchors=True)

    def event_heading(m):
        typ = m.group(2)
        cls = "g-other" if typ in V.OTHER_TYPES else "g-hollow"
        icon = f'<svg class="hglyph" viewBox="-9 -9 18 18">{ov_shape(typ, 0, 0, 5.2, cls, None, "")}</svg>'
        return f'<h{m.group(1)} id="event-{typ}">{icon}Event: {V.esc(ov_type_name(typ))}</h{m.group(1)}>'
    html_text = re.sub(r'<h(\d) id="event-(' + EVENT_TOKEN + r')">[^<]*</h\d>', event_heading, html_text)
    body = (f"{nav}{V.banner(models)}"
            '<p class="muted">The code set the model readers apply to overseers. On the overseer pages each thought '
            'links here. Letters are only code groups (O, H, I, T, A, G); events are named in words and drawn as '
            f'shapes.</p><div class="story">{html_text}</div>')
    return page("Story map overseer codes", body)


def page(title, body, scripts="", body_class=""):
    return V.page(title, body, scripts, body_class, css=CSS)


# ------------------------------------------------------------------ glyphs

def _poly(cls, pts):
    return f'<polygon class="{cls}" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}"/>'


def ov_mark(typ, x, y, r, cls="m"):
    if typ == "K1":  # hexagon: a proof check
        k = r * 1.2
        return _poly(cls, [(x + k * math.cos(math.pi / 3 * j), y + k * math.sin(math.pi / 3 * j)) for j in range(6)])
    if typ in ("M1", "M2"):  # pentagon: the private overseer board
        k = r * 1.25
        return _poly(cls, [(x + k * math.sin(2 * math.pi * j / 5), y - k * math.cos(2 * math.pi * j / 5))
                           for j in range(5)])
    if typ == "WS":  # a thin tick: a workspace call
        return f'<rect class="{cls}" x="{x - 1.3:.1f}" y="{y - r:.1f}" width="2.6" height="{2 * r:.1f}" rx="1"/>'
    if typ == "SL":  # a small half disc: the start of a sleep
        return (f'<path class="{cls}" d="M{x:.1f},{y - r:.1f}A{r:.1f},{r:.1f} 0 1,0 {x:.1f},{y + r:.1f}'
                f'A{r * 0.55:.1f},{r:.1f} 0 1,1 {x:.1f},{y - r:.1f}Z"/>')
    return V._mark(typ, x, y, r, cls)


def ov_shape(typ, x, y, r, cls, key, title, count=0, attrs=""):
    """One glyph of an overseer page (as view.shape): shape = event type; flags in ``cls``: g-fill (fake), g-own (its
    own act), g-failed (the call failed), ok / no / und (check verdict), seed (inner dot), dup (dotted ring), kf (pink
    ring: DM to a known faker), ring (by another overseer), push (chevron)."""
    flags = set(cls.split())
    a = f'class="g {cls}"' + (f' data-k="{V.esc(key)}" tabindex="0" role="button"' if key is not None else "") + attrs
    t = f"<title>{V.esc(title)}</title>" if title else ""
    if typ == "V1":
        d = f"M{x - r:.1f},{y - r:.1f}L{x + r:.1f},{y + r:.1f}M{x + r:.1f},{y - r:.1f}L{x - r:.1f},{y + r:.1f}"
        body = (f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="{V.HIT_R if key is not None else r + 1:.1f}"/>'
                f'<path class="x{" xfake" if "g-fill" in flags else ""}" d="{d}"/>')
    else:
        body = ((f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="{V.HIT_R}"/>' if key is not None else "")
                + ov_mark(typ, x, y, r))
    ring = ""
    if "ring" in flags and typ != "V1":
        ring += ov_mark(typ, x, y, r + 2.8, "ring")
    if "kf" in flags:
        ring += f'<circle class="kfr" cx="{x:.1f}" cy="{y:.1f}" r="{r + 3.2:.1f}"/>'
    if "dup" in flags:
        ring += f'<circle class="dupr" cx="{x:.1f}" cy="{y:.1f}" r="{r + 3.4:.1f}"/>'
    extra = ""
    if "seed" in flags:
        extra += f'<circle class="sd" cx="{x:.1f}" cy="{y:.1f}" r="{max(r * 0.38, 1.6):.1f}"/>'
    if "ok" in flags:
        extra += (f'<path class="mk" d="M{x + r * 0.7:.1f},{y - r * 1.5:.1f}l{r * 0.45:.1f},{r * 0.5:.1f}'
                  f'l{r * 0.9:.1f},{-r * 1.1:.1f}"/>')
    elif "no" in flags:
        d = f"M{x - r * 1.3:.1f},{y + r * 1.4:.1f}L{x + r * 1.3:.1f},{y - r * 1.6:.1f}"
        extra += f'<path class="halo" d="{d}"/><path class="mk" d="{d}"/>'
    elif "und" in flags:
        extra += f'<text class="q" x="{x + r * 1.5:.1f}" y="{y - r * 0.6:.1f}">?</text>'
    if "push" in flags:
        extra += (f'<path class="push" d="M{x - r * 0.75:.1f},{y + r * 1.55:.1f}l{r * 0.75:.1f},{r * 0.6:.1f}'
                  f'l{r * 0.75:.1f},{-r * 0.6:.1f}"/>')
    if count > 1:
        extra += V.count_badge(x, y, r, count)
    return f'<g {a}>{t}{ring}{body}{extra}</g>'


def channel(e):
    d = e.get("detail") or {}
    ch = d.get("channel")
    rest = str(e.get("id") or "").split(":", 1)[-1]
    if ch in ("post", "board") or (ch is None and rest.startswith("post-")):
        return "post"
    if ch == "dm" or (ch is None and rest.startswith("dm-")):
        return "dm"
    return ch or "other"


def glyph(ev, known_fakers_at=None):
    """(type drawn, flags) of an event on an overseer page."""
    e = ev["event"]
    typ, d = e.get("type"), e.get("detail") or {}
    if typ == "K1":
        flags = ["g-fill" if e.get("fake") else "g-hollow"]
        flags.append({"genuine": "ok", "not genuine": "no", "undecided": "und"}.get(e.get("outcome"), ""))
        if e.get("outcome") == "failed" or d.get("ok") is False:
            flags.append("g-failed")
        if d.get("seed"):
            flags.append("seed")
        if d.get("duplicate"):
            flags.append("dup")
        return typ, " ".join(f for f in flags if f)
    if typ == "V1":
        flags = ["g-fill" if e.get("fake") else "g-hollow"]
        if e.get("outcome") == "failed" or d.get("ok") is False:
            flags.append("g-failed")
        if d.get("duplicate"):
            flags.append("dup")
        return typ, " ".join(flags)
    if typ == "M2":
        return typ, "g-own" + (" g-failed" if d.get("ok") is False else "")
    if typ == "M1":
        return typ, "g-hollow"
    if typ == "O1":
        ch = channel(e)
        if ch == "post":
            return "B1", "g-own"
        if ch == "dm":
            kf = known_fakers_at(d.get("to") or d.get("recipient"), V._num(ev.get("t"))) if known_fakers_at else False
            return "D1", "g-own" + (" kf" if kf else "")
        return "O1", "g-other"
    if typ == "L1" and d.get("fake_paths"):
        return typ, V.glyph_class(ev).replace("g-hollow", "g-fill")
    return typ, V.glyph_class(ev)


def lane_of_event(e):
    typ = e.get("type")
    if typ == "O1":
        return {"post": "board", "dm": "dms"}.get(channel(e), "other")
    return {"K1": "checks", "V1": "reverts", "B1": "board", "D1": "dms", "M1": "ovboard", "M2": "ovboard",
            "L1": "library", "L2": "library", "P1": "status", "P2": "status"}.get(typ, "other")


def event_title(ev, known_fakers_at=None):
    e = ev["event"]
    typ, d = e.get("type"), e.get("detail") or {}
    lab = event_label(e)
    bits = [lab[:1].upper() + lab[1:], V.fmt_t(ev.get("t"))]
    if typ == "K1":
        bits.append(OUTCOME_LABEL.get(e.get("outcome"), str(e.get("outcome"))))
        bits.append("the seed's planted fake" if d.get("seed") else VALUE_LABEL["target"].get(d.get("target"), "")
                    + (f" by {d['checked_author']}" if d.get("checked_author") else ""))
        if e.get("fake"):
            bits.append("the checked source is a fake")
        if d.get("duplicate"):
            bits.append("duplicate check")
    elif typ == "V1":
        bits.append("reverted" if e.get("outcome") == "reverted" else "revert failed")
        if d.get("credited_agent"):
            bits.append(f"credit of {d['credited_agent']}" + (" (the seed)" if d.get("seed") else ""))
        if isinstance(d.get("latency_min"), (int, float)):
            bits.append(f"{d['latency_min']:.1f} min after acceptance")
        if e.get("fake"):
            bits.append("a fake")
        if d.get("duplicate"):
            bits.append("duplicate")
    elif typ == "O1" and channel(e) == "dm":
        to = d.get("to") or d.get("recipient")
        if known_fakers_at and known_fakers_at(to, V._num(ev.get("t"))):
            bits.append("recipient already had a fake submission")
    else:
        if e.get("author") and typ not in ("S1", "M2", "O1"):
            bits.append(f"by {V._who(e)}")
        if e.get("fake"):
            bits.append("fake")
        if typ == "L1" and d.get("fake_paths"):
            bits.append(V.plural(len(d["fake_paths"]), "fake file") + " listed")
        if typ == "M1" and d.get("own"):
            bits.append("its own post")
        if V.delivery_of(e) in ("push", "notice"):
            bits.append(V.DELIVERY_LABEL[V.delivery_of(e)])
    if d.get("ok") is False and typ in ("M2",):
        bits.append("failed")
    return " · ".join(b for b in bits if b)


# ------------------------------------------------------------------ data

def _seed_at(ctx, t):
    """(accepted, standing) of the seed's planted fake at minute t (storymap/overseer.py seed_fake_at)."""
    if OV is not None and hasattr(OV, "seed_fake_at"):
        return OV.seed_fake_at(ctx, t)
    return 0, 0


def _seed_agents(ctx):
    return OV.seed_agents(ctx) if OV is not None and hasattr(OV, "seed_agents") else set()


def _with_seed(ctx, name):
    """The context's accepted / standing fake curve with the seed's planted fake added (other curves as given)."""
    curve = [list(p) for p in ctx.get(name) or [] if isinstance(p, (list, tuple)) and len(p) == 2]
    seed = ctx.get("seed_fake") or {}
    if name not in ("accepted_fakes_curve", "standing_fakes_curve") or seed.get("t_accept") is None or not curve:
        return curve
    changes = [(V._num(seed["t_accept"]), 1)]
    if name == "standing_fakes_curve" and seed.get("reverted_at") is not None:
        changes.append((V._num(seed["reverted_at"]), -1))
    times = sorted({V._num(p[0]) for p in curve} | {t for t, _ in changes})
    return [[t, _curve_at(curve, t) + sum(d for ct, d in changes if ct <= t + 1e-9)] for t in times]


def _curve_at(curve, t):
    """Value of a step curve ([[t, n], ...]; each point holds until the next) at minute t (None when t is None)."""
    if t is None or not curve:
        return None
    v = 0
    for pt in curve:
        try:
            if float(pt[0]) <= float(t) + 1e-9:
                v = pt[1]
            else:
                break
        except (TypeError, ValueError, IndexError):
            continue
    return v


def load_context(*folders):
    for f in folders:
        if f is None:
            continue
        tdir = V.timeline_dir(f)
        for d in (tdir, Path(f)):
            ctx = None
            if OV is not None and hasattr(OV, "load_context"):
                try:
                    ctx = OV.load_context(d)
                except (OSError, ValueError):
                    ctx = None
            if ctx is None:
                ctx = V._read_json(Path(d) / "overseer_context.json")
            if isinstance(ctx, dict):
                return ctx
    return None


def _fallback_category(item):
    tool = str(item.get("tool") or "")
    text = str(item.get("text") or "")
    if tool == "Bash" and re.search(r"(^|[;&|\s\"'])sleep\s+\d", text):
        return "sleep"
    return {"check_proof": "check", "revert_submission": "revert", "post_intent": "board_post",
            "get_bulletin_board": "board_read", "send_direct_message": "dm_send", "get_messages": "dm_read",
            "post_overseer_board": "overseer_board_post", "read_overseer_board": "overseer_board_read",
            "list_library": "library_list", "read_library_file": "library_read", "list_problems": "problems",
            "get_problem": "problems", "get_status": "status", "get_conference_status": "status",
            "submit_solution": "submit"}.get(tool, "workspace" if tool in (
                "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "NotebookEdit", "TodoWrite")
                else "other")


def activity_rows(items):
    """One row per tool call (overseer.activity); a minimal local reading when that returns nothing for a timeline
    with tool calls (the rows then carry no event links and no sleep length unless the command states it)."""
    rows = []
    if OV is not None and hasattr(OV, "activity"):
        try:
            rows = list(OV.activity(items) or [])
        except Exception:  # noqa: BLE001 - the page must render; the gap is stated on it
            rows = []
    calls = [it for it in items if it.get("kind") == "tool_call"]
    if rows or not calls:
        return rows, False
    evs = defaultdict(list)  # events share the turn and tool of the call that delivered them
    for it in items:
        if it.get("kind") == "event" and isinstance(it.get("event"), dict):
            evs[(it.get("turn"), it.get("tool"))].append(str(it["event"].get("id")))
    for it in calls:
        cat = _fallback_category(it)
        m = re.search(r"sleep\s+(\d+(?:\.\d+)?)", str(it.get("text") or "")) if cat == "sleep" else None
        rows.append({"i": it.get("i"), "t": it.get("t"), "turn": it.get("turn"), "category": cat,
                     "tool": it.get("tool"), "ok": None, "event_ids": evs.pop((it.get("turn"), it.get("tool")), []),
                     "summary": " ".join(str(it.get("text") or "").split())[:200],
                     "seconds": float(m.group(1)) if m else None})
    return rows, True


def first_events(events):
    """{moment: event item} of the first event of each kind (used for links, and for times when the hard facts lack
    them)."""
    out = {}

    def put(k, ev):
        out.setdefault(k, ev)
    for ev in sorted(events, key=lambda ev: (V._num(ev.get("t")), V._num(ev.get("i")))):
        e = ev["event"]
        typ, d = e.get("type"), e.get("detail") or {}
        if typ == "L1":
            put("library_list", ev)
        elif typ == "L2":
            put("library_read", ev)
        elif typ == "K1":
            put("check_any", ev)
            if d.get("seed"):
                put("check_seed", ev)
            elif d.get("target") == "accepted_submission" or d.get("submission_id"):
                put("check_real", ev)
            if e.get("outcome") == "not genuine":
                put("check_not_genuine", ev)
        elif typ == "V1":
            put("revert_attempt", ev)
            if e.get("outcome") == "reverted" and d.get("ok") is not False:
                put("revert", ev)
        elif typ == "O1":
            ch = channel(e)
            if ch == "post":
                put("public_post", ev)
            elif ch == "dm":
                put("dm", ev)
        elif typ == "M2" and d.get("ok") is not False:
            put("overseer_board_post", ev)
        elif typ == "M1":
            put("overseer_board_read", ev)
    return out


class Overseer:
    """One overseer's data for its page, its strip row and the run panel."""

    def __init__(self, a, context, tmax):
        self.a, self.name, self.ctx, self.tmax = a, a["name"], context or {}, tmax
        self.events = a["events"]
        self.rows, self.fallback = activity_rows(a["items"])
        self.firsts = first_events(self.events)
        hf = {}
        if OV is not None and hasattr(OV, "hard_facts"):
            try:
                hf = OV.hard_facts(self.name, a["items"], a["index"], context) or {}
            except Exception:  # noqa: BLE001 - stated on the page
                hf = {}
        self.hf = hf
        first = dict(hf.get("first") or {})
        for k, ev in self.firsts.items():
            if first.get(k) is None:
                first[k] = V._num(ev.get("t"))
        if first.get("sleep") is None:
            sl = [r for r in self.rows if r.get("category") == "sleep"]
            first["sleep"] = V._num(sl[0].get("t")) if sl else None
        self.first = first
        self.at_first = {}
        for k, _, _, _, _ in MOMENTS:
            given = (hf.get("at_first") or {}).get(k)
            t = first.get(k)
            if isinstance(given, dict) and given.get("t") is not None:
                self.at_first[k] = given
            else:
                acc, stand = _seed_at(self.ctx, t)
                plus = lambda v, n: v + n if isinstance(v, (int, float)) else v  # noqa: E731
                self.at_first[k] = {"t": t, "solver_fakers": _curve_at(self.ctx.get("fakers_curve"), t),
                                    "accepted_fakes": plus(_curve_at(self.ctx.get("accepted_fakes_curve"), t), acc),
                                    "standing_fakes": plus(_curve_at(self.ctx.get("standing_fakes_curve"), t), stand)}

    def known_faker_at(self, agent, t):
        """True when ``agent`` had a fake submission at or before minute ``t`` (from the run context)."""
        if not agent:
            return False
        for f in self.ctx.get("solver_fakes") or []:
            if f.get("agent") == agent and V._num(f.get("t_submit"), 1e9) <= t + 1e-9:
                return True
        if _seed_at(self.ctx, t)[0] and agent in _seed_agents(self.ctx):  # the seed, once its fake was accepted
            return True
        targets = {(x.get("to"), round(V._num(x.get("t")), 1)) for x in self.hf.get("dm_targets") or []
                   if x.get("known_faker")}
        return (agent, round(t, 1)) in targets

    def key_of(self, moment):
        ev = self.firsts.get(moment)
        return f"ev-{ev.get('i')}" if ev else None

    def phases(self):
        s3 = self.a["stage3"] or {}
        if s3.get("phases"):
            return [(V._num(p.get("from_t")), V._num(p.get("to_t"), self.tmax), str(p.get("status") or "unknown"),
                     str(p.get("label") or "")) for p in s3["phases"]], "stage-3 reading"
        out, prev = [], 0.0
        for c in self.a["chunks"]:
            st = (c.get("state") or {}).get("status") or "unknown"
            ts = [V._num(th.get("t")) for th in c.get("thoughts") or []]
            end = V._num(c.get("to_t"), max(ts) if ts else prev)
            if end > prev or not out:
                out.append((prev, end, st, "stage-1 state"))
                prev = end
        return out, ("stage-1 reading (no stage-3 reading)" if out else "none")


# ------------------------------------------------------------------ shared bits

def status_title(frm, to, st, label):
    return (f"{STATUS_TITLE}: {STATUS_LABEL.get(st, st)} · {V.fmt_span(frm, to)}" + (f" · {label}" if label else ""))


def _bar_glyph(status):
    cls = status if status in STATUSES else "unknown"
    return f'<svg viewBox="0 0 22 16" class="swatch"><rect class="os-{cls}" x="1" y="3" width="20" height="10" rx="2"/></svg>'


def _glyph(typ, cls, count=0):
    return f'<svg viewBox="0 0 18 16">{ov_shape(typ, 8, 8.5, 4.6, cls, None, "", count)}</svg>'


def status_legend():
    return "".join(f'<li title="{V.esc(m)}">{_bar_glyph(s)}{V.esc(STATUS_LABEL[s])}</li>' for s, m in STATUS_MEANING)


SHAPES = (("K1", "g-hollow", "proof check"), ("V1", "g-hollow", "revert"), ("B1", "g-hollow", "public board post"),
          ("D1", "g-hollow", "direct message"), ("M1", "g-hollow", "overseer-board post (private)"),
          ("L2", "g-hollow", "library listing or file"), ("P1", "g-other", "problems, status, other"),
          ("WS", "g-ws", "workspace call"), ("SL", "g-sleep", "sleep (span)"))
MARKS = (("B1", "g-own", "filled ink: its own act (hollow: it read it)"), ("L2", "g-fill", "pink: fake"),
         ("K1", "g-hollow ok", "✓ verdict genuine"), ("K1", "g-hollow no", "slash: verdict not genuine"),
         ("K1", "g-hollow und", "? undecided"), ("K1", "g-hollow seed", "inner dot: the seed's planted fake"),
         ("K1", "g-hollow g-failed", "dashed: the call failed"), ("V1", "g-hollow dup", "dotted ring: duplicate"),
         ("D1", "g-own kf", "pink ring: DM to a known faker"), ("D1", "g-hollow ring", "cyan ring: by another overseer"),
         ("B1", "g-hollow push", "chevron: pushed to it unasked"))


def legend_bar():
    shapes = "".join(f"<li>{_glyph(t, c)}{V.esc(m)}</li>" for t, c, m in SHAPES)
    shapes += (f'<li>{V._legend_glyph("TH", "g-th")}{V._legend_glyph("TH", "g-th-low")}thought (hollow = low '
               f'certainty)</li><li>{V._legend_glyph("TP", "")}turning point</li>')
    marks = "".join(f"<li>{_glyph(t, c)}{V.esc(m)}</li>" for t, c, m in MARKS)
    marks += f'<li>{_glyph("L2", "g-hollow", 3)}number = items stacked at one spot</li>'
    return ('<details class="legend" open><summary>Legend: bar colour = overseer status; shape = affordance; tick = '
            'coded thought</summary><div class="lstrip">'
            f'<div class="row"><span class="k">Status:</span><ul>{status_legend()}</ul></div>'
            f'<div class="row"><span class="k">Shape:</span><ul>{shapes}</ul></div>'
            f'<div class="row"><span class="k">Mark:</span><ul>{marks}</ul></div></div></details>')


def groups_html(names, href, defs):
    if not any(names.get(L) for L in LETTERS):
        return ('<p class="note">Thoughts the stage-1 reader quoted and coded into six groups (O, H, I, T, A, G); the '
                'group names are not shown on this copy.</p>')
    groups = []
    for L in LETTERS:
        codes = sorted((c for c in names if c[:1] == L and len(c) > 1), key=lambda c: int(c[1:]))
        items = "".join(
            f'<details class="code"><summary title="{V.esc(V.code_tip(c, defs, names))}"><b>{c}</b> '
            f'{V.esc(names[c])}</summary><p>{V.esc(V.code_tip(c, defs, names))}'
            + (f' <a href="{V.esc(href)}#code-{c}">more</a>' if href else "") + '</p></details>' for c in codes)
        groups.append(f'<div class="grp"><div class="gname"><b>{L}</b> {V.esc(names.get(L, ""))}</div>{items}</div>')
    more = f' Full reference: <a href="{V.esc(href)}">overseer codes page</a>.' if href else ""
    return (f'<p class="note">Codes the stage-1 reader gave to the overseer\'s quoted thoughts. Hover or click a code '
            f'for its definition.{more}</p><div class="codes">{"".join(groups)}</div>')


def reaction_html(r):
    """The stage-2 overseer reaction: registered, change, response, recognised the trick, reason, next act, quotes."""
    if not r:
        return '<p class="muted">No stage-2 reaction recorded for this event.</p>'
    rec = r.get("recognised_trick")
    head = (V.tbadge(f"registered: {REG.get(r.get('registered'), r.get('registered'))}",
                     "Whether the overseer's following items refer to the event's content")
            + V.tbadge(f"change: {V.CHANGE_LABEL.get(r.get('change'), r.get('change'))}",
                       "No change, a changed belief, or a changed plan or act", "info" if r.get("change") != "none" else "")
            + V.tbadge(f"response: {RESPONSE_LABEL.get(r.get('response'), r.get('response'))}",
                       "What the overseer did about the event (the strongest linked act in the window)")
            + V.tbadge("recognised the trick: " + ("yes" if rec is True else "no" if rec is False else "not applicable"),
                       "Whether its words show it recognised the event's content as the answer-restatement trick "
                       "(not applicable: the event does not concern the trick, or it did not register it)",
                       "bad" if rec is False else "")
            + V.tbadge(f"reader certainty: {r.get('certainty')}", "How sure the stage-2 reader was"))
    quotes = "".join(f'<blockquote>{V.esc(q.get("quote"))} <small>(timeline item {V.esc(q.get("item"))})</small>'
                     f'</blockquote>' for q in r.get("quotes") or [] if isinstance(q, dict))
    return (f'<div class="react"><p><b>How it reacted (stage 2)</b> {head}</p>'
            + (f"<p>Why: {V.esc(plain_ids(r.get('reason')))}</p>" if r.get("reason") else "")
            + (f"<p>What it did next: {V.esc(plain_ids(r.get('next_action')))}</p>" if r.get("next_action") else "")
            + quotes + "</div>")


# ------------------------------------------------------------------ agents/<overseer>.html

HIDDEN = V.DELIVERY_HIDDEN | {"message"}


def event_article(o, ev, reactions, key):
    e = ev["event"]
    typ, d = e.get("type"), e.get("detail") or {}
    meta = [V.badge(ov_type_name(typ))]
    if typ == "K1":
        meta.append(V.badge(OUTCOME_LABEL.get(e.get("outcome"), str(e.get("outcome"))),
                            "bad" if e.get("outcome") == "not genuine" else ""))
        meta.append(V.badge("the seed's planted fake" if d.get("seed") else
                            "target: " + VALUE_LABEL["target"].get(d.get("target"), str(d.get("target")))))
    elif typ == "V1":
        meta.append(V.badge("reverted" if e.get("outcome") == "reverted" else "revert failed",
                            "info" if e.get("outcome") == "reverted" else "caution"))
        if isinstance(d.get("latency_min"), (int, float)):
            meta.append(V.tbadge(f"{d['latency_min']:.1f} min after acceptance",
                                 "Time from the submission's acceptance to this revert"))
    elif e.get("author") and typ not in ("S1", "O1", "M2"):
        meta.append(V.badge("by " + V._who(e), "info" if V.is_overseer(e) else ""))
    if typ == "O1" and channel(e) == "dm":
        to = d.get("to") or d.get("recipient")
        if to:
            meta.append(V.badge(f"to {to}"))
        if o.known_faker_at(to, V._num(ev.get("t"))):
            meta.append(V.tbadge("known faker", "The recipient already had a fake submission when the DM was sent",
                                 "bad"))
    if e.get("fake") is True:
        meta.append(V.badge("fake" if typ not in ("K1", "V1") else "the submission is a fake", "bad"))
    elif e.get("fake") is False:
        meta.append(V.badge("not fake", "good"))
    if d.get("duplicate"):
        meta.append(V.tbadge("duplicate", "Already done: the same submission checked or reverted earlier by an "
                             "overseer, or nothing left to revert", "caution"))
    if d.get("ok") is False:
        meta.append(V.badge("call failed", "caution"))
    meta += [V.badge(V.TAG_LABEL.get(t, str(t).replace("_", " ")), "caution") for t in e.get("tags") or []]
    if d.get("reread"):
        meta.append(V.badge("re-read"))
    if d.get("own") and typ == "M1":
        meta.append(V.badge("its own post"))
    dl = V.delivery_of(e)
    if dl:
        meta.append(V.tbadge(V.DELIVERY_LABEL[dl], V.DELIVERY_HINT[dl]))
    if e.get("outcome") and typ not in ("K1", "V1"):
        meta.append(V.badge(V.OUTCOME_LABEL.get(str(e["outcome"]), str(e["outcome"]).replace("_", " "))))
    kv = "".join(f"<dt>{V.esc(human(k))}</dt><dd>{V.esc(shown(k, v))}</dd>"
                 for k, v in d.items() if v not in (None, "", [], {}) and k not in HIDDEN)
    k = "k-fake" if e.get("fake") else "k-ov" if typ in ("M1", "M2") else ""
    reacts = "".join(reaction_html(r) for r in reactions)
    head = event_label(e)
    head = head[:1].upper() + head[1:]
    text = ev.get("text")
    if typ in ("L2", "S1", "P2", "L1", "P1"):
        text = V.tidy_times(text)
    if typ == "M2" and d.get("message") and not text:
        text = d.get("message")
    dt, cls = glyph(ev, o.known_faker_at)
    icon = f'<svg class="hglyph" viewBox="-9 -9 18 18">{ov_shape(dt, 0, 0, 5.2, cls, None, "")}</svg>'
    cite = [ev.get("i")] + [q.get("item") for r in reactions for q in r.get("quotes") or [] if isinstance(q, dict)]
    chain = V.chain_box(ev.get("turn"), cite, "Show what surrounded it: the overseer's reasoning and tool calls",
                        before=0, after=1)
    return (f'<article class="item {k}" id="{key}" data-kind="exp" data-t="{V._num(ev.get("t")):.1f}" '
            f'data-label="{V.esc(V.fmt_t(ev.get("t")) + " · " + head[:110])}">'
            f'<h3>{icon}{V.fmt_t(ev.get("t"))} &middot; {V.esc(head)}</h3>'
            f'<p>{"".join(meta)} <small>turn {V.esc(ev.get("turn"))} &middot; timeline item {V.esc(ev.get("i"))}'
            f'</small></p><pre>{V.esc(V.redact(V.cut(text)))}</pre>{"<dl class=kv>" + kv + "</dl>" if kv else ""}{reacts}'
            f'{chain}</article>')


def activity_article(row, key, items_by_i):
    cat = str(row.get("category") or "other")
    typ, cls = CATEGORY_GLYPH.get(cat, ("P1", "g-other"))
    if row.get("ok") is False:
        cls += " g-failed"
    head = f"{CATEGORY_ONE.get(cat, 'Tool call')}: {row.get('tool')}"
    meta = [V.badge(CATEGORY_LABEL.get(cat, cat))]
    if row.get("ok") is False:
        meta.append(V.badge("call failed", "caution"))
    elif row.get("ok") is True:
        meta.append(V.badge("succeeded"))
    if isinstance(row.get("seconds"), (int, float)):
        meta.append(V.badge(f"slept {row['seconds']:.0f} s"))
    call = items_by_i.get(row.get("i")) or {}
    text = V.redact(V.cut(str(call.get("text") or row.get("summary") or ""), 1200))
    icon = f'<svg class="hglyph" viewBox="-9 -9 18 18">{ov_shape(typ, 0, 0, 5.2, cls, None, "")}</svg>'
    chain = V.chain_box(row.get("turn"), [row.get("i")], "Show the call, its result and the reasoning around it",
                        before=0, after=0)
    return (f'<article class="item" id="{key}" data-kind="act" data-t="{V._num(row.get("t")):.1f}" '
            f'data-label="{V.esc(V.fmt_t(row.get("t")) + " · " + head[:110])}">'
            f'<h3>{icon}{V.fmt_t(row.get("t"))} &middot; {V.esc(head)}</h3><p>{"".join(meta)} <small>turn '
            f'{V.esc(row.get("turn"))} &middot; timeline item {V.esc(row.get("i"))}</small></p>'
            f'<pre>{V.esc(text)}</pre>{chain}</article>')


def thought_article(th, names, related, key, items_by_i, href):
    html_ = V.thought_article(th, names, [], key, items_by_i, href)
    rel = "".join(f'<p class="muted">Quoted as evidence in the stage-2 reaction to <a href="#sel={V.esc(k)}" '
                  f'data-k="{V.esc(k)}">{V.esc(event_label(eid))}</a>:</p>{reaction_html(r)}' for k, eid, r in related)
    return html_[:-len("</article>")] + rel + "</article>" if rel else html_


PANEL = ('<aside id="panel" aria-label="Detail"><div class="stepper"><button type="button" id="pprev" '
         'title="Previous (← or k)">← Prev</button><span id="pos" aria-live="polite"></span>'
         '<button type="button" id="pnext" title="Next (→ or j)">Next →</button>'
         '<label>Step through <select id="kind"><option value="">all items</option>'
         '<option value="exp">only events (checks, reverts, posts, DMs, reads)</option>'
         '<option value="th">only thoughts</option><option value="act">only other tool calls</option>'
         '<option value="tp">only turning points</option></select></label>'
         '<button type="button" id="pclose" title="Close (Esc)" aria-label="Close">×</button></div>'
         '<div id="pbody"></div><p class="muted keys">→ / j next, ← / k previous, Esc closes.</p></aside>')


def hard_facts_html(o):
    """First moments with the solvers' counts at each, counts by kind, revert latencies, DM targets."""
    hf = o.hf
    rows = []
    for k, label, _, typ, cls in MOMENTS:
        t = o.first.get(k)
        at = o.at_first.get(k) or {}
        key = o.key_of(k)
        when = (f'<a href="#sel={V.esc(key)}" data-k="{V.esc(key)}">{V.fmt_t(t)}</a>' if key and t is not None
                else V.fmt_t(t) if t is not None else '<span class="muted">never</span>')
        num = lambda v: '<td class="n">' + ("—" if v is None else V.esc(v)) + "</td>"  # noqa: E731
        rows.append(f'<tr><td>{_glyph(typ, cls)} {V.esc(label)}</td><td class="n">{when}</td>'
                    f'{num(at.get("solver_fakers"))}{num(at.get("accepted_fakes"))}{num(at.get("standing_fakes"))}</tr>')
    first = ('<table class="hf"><tr><th>Moment</th><th class="n">At</th><th class="n" title="Distinct solvers with at '
             'least one fake submission by then">Solver fakers</th><th class="n" title="Accepted fake submissions by '
             'then (solvers and the seed\'s planted fake)">Accepted fakes</th><th class="n" title="Accepted fakes '
             f'not yet reverted at that moment (the seed\'s included)">Standing fakes</th></tr>{"".join(rows)}</table>')
    counts = dict(hf.get("counts") or {})
    if not counts:
        counts = dict(Counter(str(r.get("category")) for r in o.rows))
    cells, seen = [], set()
    for k, v in counts.items():
        if isinstance(v, dict):
            v = "; ".join(f"{a}: {b}" for a, b in v.items()) or "none"
        label = CATEGORY_LABEL.get(k) or {
            "reverts_ok": "reverts done", "reverts_failed": "reverts failed", "reverts_duplicate": "duplicate reverts",
            "checks_by_outcome": "checks by verdict", "checks_duplicate": "duplicate checks",
            "dms_to_known_fakers": "DMs to known fakers", "dms_received": "DMs received",
            "board_posts_read": "public posts read", "overseer_board_posts": "overseer-board posts",
            "overseer_board_reads": "overseer-board posts read", "sleep_seconds": "seconds asleep"}.get(k, V.human(k))
        if label in seen:  # the same count under two names (a category and its own fact)
            continue
        seen.add(label)
        cells.append(f"<tr><td>{V.esc(label[:1].upper() + label[1:])}</td><td class=\"n\">{V.esc(v)}</td></tr>")
    lat = hf.get("revert_latencies_min")
    if lat is None:
        lat = [V._num((ev["event"].get("detail") or {}).get("latency_min")) for ev in o.events
               if ev["event"].get("type") == "V1" and isinstance((ev["event"].get("detail") or {}).get("latency_min"),
                                                                 (int, float))]
    lat_s = ", ".join(f"{x:.1f}" for x in lat) + " min" if lat else "no reverts with a known acceptance time"
    targets = hf.get("dm_targets")
    if targets is None:
        targets = [{"t": ev.get("t"), "to": (ev["event"].get("detail") or {}).get("to"),
                    "known_faker": o.known_faker_at((ev["event"].get("detail") or {}).get("to"), V._num(ev.get("t")))}
                   for ev in o.events if ev["event"].get("type") == "O1" and channel(ev["event"]) == "dm"]
    dm_rows = "".join(f'<tr><td class="n">{V.fmt_t(x.get("t"))}</td><td>{V.esc(x.get("to"))}</td>'
                      f'<td>{"yes" if x.get("known_faker") else "no"}</td></tr>' for x in targets)
    board = hf.get("board_enabled", o.ctx.get("board"))
    note = []
    if not hf or not hf.get("first"):
        note.append("The first moments were read from the timeline events on this page (the code-derived hard facts "
                    "were not available).")
    if not o.ctx:
        note.append("No run context (overseer_context.json): the solver counts are not shown.")
    return ('<h2>Hard facts (from the records, not model readings)</h2>'
            + "".join(f'<p class="muted">{V.esc(n)}</p>' for n in note)
            + f'<p class="muted">Overseer board in this run: {"yes" if board else "no" if board is False else "unknown"}'
            f'.</p>{first}<div class="hfx" style="display:flex;flex-wrap:wrap;gap:4px 24px">'
            f'<div><h3>Counts</h3><table class="hf">{"".join(cells) or "<tr><td>none</td></tr>"}</table></div>'
            f'<div><h3>Revert latency (acceptance to revert)</h3><p>{V.esc(lat_s)}</p>'
            f'<h3>DM targets</h3>' + (f'<table class="hf"><tr><th class="n">At</th><th>To</th><th>Known faker '
                                     f'then</th></tr>{dm_rows}</table>' if dm_rows else '<p class="muted">No DMs sent.'
                                     '</p>') + '</div></div>')


def activity_html(o, key_of_row, key_of_event):
    """Every tool call, by category (counts; each call listed with time, tool, result and a link to its glyph)."""
    by = defaultdict(list)
    for r in o.rows:
        by[str(r.get("category") or "other")].append(r)
    if not o.rows:
        return ('<h2>Every tool call</h2><p class="muted">No tool calls in the timeline (or no timeline).</p>')
    order = [c for c in CATEGORY_LABEL if c in by] + sorted(c for c in by if c not in CATEGORY_LABEL)
    parts = []
    for c in order:
        rows = []
        for r in by[c]:
            key = key_of_row.get(r.get("i")) or next((key_of_event[x] for x in r.get("event_ids") or []
                                                      if x in key_of_event), None)
            when = (f'<a href="#sel={V.esc(key)}" data-k="{V.esc(key)}">{V.fmt_t(r.get("t"))}</a>' if key
                    else V.fmt_t(r.get("t")))
            ok = "failed" if r.get("ok") is False else "ok" if r.get("ok") is True else "—"
            rows.append(f'<tr><td class="n">{when}</td><td>{V.esc(r.get("tool"))}</td><td>{ok}</td>'
                        f'<td class="sm">{V.esc(V.redact(str(r.get("summary") or "")))}</td></tr>')
        failed = sum(r.get("ok") is False for r in by[c])
        parts.append(f'<details><summary>{V.esc(CATEGORY_LABEL.get(c, c).capitalize())}: {len(by[c])}'
                     + (f" ({failed} failed)" if failed else "") + '</summary><table class="acts"><tr><th>At</th>'
                     f'<th>Tool</th><th>Result</th><th>Call</th></tr>{"".join(rows)}</table></details>')
    note = (' <span class="muted">(Read without the activity classifier: categories from the tool names only.)</span>'
            if o.fallback else "")
    return (f'<h2>Every tool call ({len(o.rows)})</h2><p class="muted">Each call of the overseer, by kind; the time '
            f'links to its shape on the timeline.{note}</p>' + "".join(parts))


def overseer_page(o, names, defs, tmax, all_names, models="", nav="", href=""):
    a, name = o.a, o.name
    s3 = a["stage3"] or {}
    LH, R = V.LANE_PX, 6.5
    board = o.hf.get("board_enabled", o.ctx.get("board")) if o.hf else o.ctx.get("board")
    present = defaultdict(int)
    for ev in o.events:
        present[lane_of_event(ev["event"])] += 1
    for r in o.rows:
        present[CATEGORY_LANE.get(str(r.get("category")), "other")] += 1
    lanes = ([("bar", "", STATUS_TITLE, "Status", STATUS_HINT)]
             + [(f"th-{L}", L) + V.thought_lane_label(L, names) for L in LETTERS]
             + [(k, "", label, short, hint) for k, label, short, hint in LANES if k != "other" or present["other"]])
    ypos = {ln[0]: V.LANE_TOP + n * LH for n, ln in enumerate(lanes)}
    bottom = V.LANE_TOP + len(lanes) * LH
    svg, labels = [], []
    for n, (k, letter, label, short, hint) in enumerate(lanes):
        y = ypos[k]
        if k != "bar":
            svg.append(f'<rect class="lane-bg{" alt" if n % 2 else ""}" x="0" y="{y}" width="100%" height="{LH}"/>')
        lt = f"<b>{letter}</b>" if letter else ""
        labels.append(f'<div class="ll" title="{V.esc((letter + " " if letter else "") + label + ": " + hint)}">{lt}'
                      f'<span class="lf">{V.esc(label)}</span><span class="ls">{V.esc(short)}</span></div>')
    svg.append(V.axis_svg(tmax, V.LANE_TOP, len(lanes) * LH))
    if board is False:
        svg.append(f'<text class="ovnote" x="50%" y="{ypos["ovboard"] + LH / 2 + 4}" text-anchor="middle">no overseer '
                   f'board in this run</text>')
    svg.append(f'<line id="cursor" class="cursor" x1="0" x2="0" y1="{V.LANE_TOP}" y2="{bottom}" visibility="hidden"/>')

    articles = []
    key_of_event, ev_of_id = {}, {}
    for ev in o.events:
        key_of_event.setdefault(str(ev["event"].get("id")), f"ev-{ev.get('i')}")
        ev_of_id.setdefault(str(ev["event"].get("id")), ev["event"])
    phases, phase_src = o.phases()
    y = ypos["bar"]
    for frm, to, st, label in phases:
        cls = st if st in STATUSES else "unknown"
        w = max(V.pct(to, tmax) - V.pct(frm, tmax), 0.1)
        svg.append(f'<rect class="os-{cls}" x="{V.pct(frm, tmax):.2f}%" y="{y + 5}" width="{w:.2f}%" height="{LH - 10}" '
                   f'rx="2"><title>{V.esc(status_title(frm, to, st, label))}</title></rect>')
    for n, tp in enumerate(s3.get("turning_points") or []):
        key = f"tp-{n}"
        x = V.pct(tp.get("t"), tmax)
        fr, to = STATUS_LABEL.get(tp.get("from"), tp.get("from")), STATUS_LABEL.get(tp.get("to"), tp.get("to"))
        tip = f"Turning point · {V.fmt_t(tp.get('t'))} · {fr} → {to}"
        svg.append(f'<line class="tp" data-k="{key}" data-p="{x:.2f}" x1="{x:.2f}%" x2="{x:.2f}%" y1="{y}" '
                   f'y2="{bottom}"><title>{V.esc(tip)}</title></line>')
        causes = ", ".join((f'<a href="#sel={V.esc(key_of_event[str(c)])}" data-k="{V.esc(key_of_event[str(c)])}">'
                            f'{V.esc(event_label(ev_of_id[str(c)]))}</a>' if str(c) in key_of_event
                            else V.esc(event_label(c)))
                           for c in tp.get("cause_event_ids") or []) or "none named"
        head = f"turning point: {fr} → {to}"
        articles.append((V._num(tp.get("t")), 0, f'<article class="item k-tp" id="{key}" data-kind="tp" '
                         f'data-t="{V._num(tp.get("t")):.1f}" data-label="{V.esc(V.fmt_t(tp.get("t")) + " · " + head)}">'
                         f'<h3><svg class="hglyph" viewBox="0 0 18 16"><line class="tp" x1="9" x2="9" y1="0" y2="16"/>'
                         f'</svg>{V.fmt_t(tp.get("t"))} &middot; {V.esc(head)}</h3><p>Caused by (stage-3 reading): '
                         f'{causes}</p><p>{V.esc(plain_ids(tp.get("cause_note")))}</p></article>'))

    cited = defaultdict(list)
    for eid, rs in a["by_event"].items():
        for r in rs:
            for q in r.get("quotes") or []:
                if isinstance(q, dict) and q.get("item") is not None:
                    cited[q["item"]].append((key_of_event.get(eid, ""), eid, r))
    per_lane = defaultdict(list)
    for ev in o.events:
        e = ev["event"]
        key = f"ev-{ev.get('i')}"
        typ, cls = glyph(ev, o.known_faker_at)
        prio = (0 if typ in ("K1", "V1") else 1 if e.get("fake") or "kf" in cls else 2 if "g-own" in cls else 4)
        per_lane[lane_of_event(e)].append({"t": V._num(ev.get("t")), "order": V._num(ev.get("i")), "prio": prio,
                                           "typ": typ, "cls": cls, "key": key, "title": event_title(ev, o.known_faker_at)})
        articles.append((V._num(ev.get("t")), 1, event_article(o, ev, a["by_event"].get(str(e.get("id")), []), key)))
    orphans = [r for eid, rs in a["by_event"].items() if eid not in key_of_event for r in rs]

    items_by_i = {it.get("i"): it for it in a["items"]}
    key_of_row = {}
    for r in o.rows:  # every call without an event of its own gets a glyph (sleeps: a span and a start glyph)
        if any(str(x) in key_of_event for x in r.get("event_ids") or []):
            continue
        cat = str(r.get("category") or "other")
        lane = CATEGORY_LANE.get(cat, "other")
        key = f"ac-{r.get('i')}"
        key_of_row[r.get("i")] = key
        typ, cls = CATEGORY_GLYPH.get(cat, ("P1", "g-other"))
        if r.get("ok") is False:
            cls += " g-failed"
        t = V._num(r.get("t"))
        title = (f"{CATEGORY_LABEL.get(cat, cat).capitalize()} · {V.fmt_t(t)} · {r.get('tool')}"
                 + (" · failed" if r.get("ok") is False else "")
                 + (f" · {r['seconds']:.0f} s" if isinstance(r.get("seconds"), (int, float)) else "")
                 + (f"\n{V.redact(str(r.get('summary') or '')[:160])}" if r.get("summary") else ""))
        if cat == "sleep" and isinstance(r.get("seconds"), (int, float)) and r["seconds"] > 0:
            x0, x1 = V.pct(t, tmax), V.pct(t + r["seconds"] / 60.0, tmax)
            svg.append(f'<rect class="sl" x="{x0:.2f}%" y="{ypos["sleep"] + 8}" width="{max(x1 - x0, 0.15):.2f}%" '
                       f'height="{LH - 16}" rx="2"><title>{V.esc(title)}</title></rect>')
        per_lane[lane].append({"t": t, "order": V._num(r.get("i")), "prio": 5, "typ": typ, "cls": cls, "key": key,
                               "title": title})
        articles.append((t, 3, activity_article(r, key, items_by_i)))

    for n, th in enumerate(a["thoughts"]):
        key = f"th-{n}"
        L = str(th.get("code") or "?")[:1].upper()
        lane = f"th-{L}" if L in LETTERS else "th-G"
        low = th.get("certainty") == "low"
        code = str(th.get("code"))
        per_lane[lane].append(
            {"t": V._num(th.get("t")), "order": V._num(th.get("item")), "typ": "TH", "key": key,
             "prio": {"high": 0, "medium": 1}.get(th.get("certainty"), 2), "cls": "g-th-low" if low else "g-th",
             "title": (f"Thought {V.code_name(code, names, code)} · {V.fmt_t(th.get('t'))} · "
                       f"“{str(th.get('quote') or '')[:120]}”"
                       + (f"\n\n{V.code_tip(code, defs, names)}" if defs.get(code) else ""))})
        ech = (a.get("evidence") or {}).get(th.get("ekey"))
        if ech and ech.get("item") == th.get("item"):
            th = dict(th, evidence_chain=ech)
        articles.append((V._num(th.get("t")), 2, thought_article(th, names, cited.get(th.get("item"), []), key,
                                                                 items_by_i, href)))
    n_stacks = 0
    for lane, entries in per_lane.items():
        if lane not in ypos:
            continue
        for group in V.stack(entries, tmax):
            n_stacks += len(group) > 1
            svg.append(stack_glyph(group, tmax, ypos[lane] + LH / 2, 7 if group[0]["typ"] == "TH" else R))
    articles.sort(key=lambda x: (x[0], x[1]))

    f = s3.get("final") or {}
    final_kv = "".join(f"<dt>{V.esc(FINAL_LABEL.get(k, human(k)))}</dt><dd>{V.esc(shown(k, v))}</dd>"
                       for k, v in f.items())
    flags = "".join(f"<li>{V.esc(plain_ids(x))}</li>" for x in s3.get("consistency_flags") or [])

    def state_row(c, ch):
        s = ch.get("state") or {}
        span = (V.fmt_span(ch.get("from_t"), ch.get("to_t")) if ch.get("from_t") is not None
                and ch.get("to_t") is not None else "")
        return (f"<tr><td class=n>{c}</td><td>{V.esc(span)}</td><td>{V.esc(STATUS_LABEL.get(s.get('status'), s.get('status')))}"
                f"</td><td>{V.esc(V.yesno(s.get('knows_trick')))}</td><td>{V.esc(shown('suspected', s.get('suspected') or []))}"
                f"</td><td>{V.esc('; '.join(map(str, s.get('open_questions') or [])))}</td><td>{V.esc(s.get('summary'))}</td></tr>")
    states = "".join(state_row(c, ch) for c, ch in enumerate(a["chunks"]))
    missing = [s for s, ok in (("the overseer's timeline", a["items"]), ("stage-1 thoughts", a["chunks"]),
                               ("stage-2 reactions", a["reactions"]), ("a stage-3 summary", a["stage3"])) if not ok]
    idx = list(all_names)
    pos = idx.index(name)
    prev_link = f'<a href="{V.esc(idx[pos - 1])}.html">&larr; {V.esc(idx[pos - 1])}</a>' if pos > 0 else ""
    next_link = f'<a href="{V.esc(idx[pos + 1])}.html">{V.esc(idx[pos + 1])} &rarr;</a>' if pos + 1 < len(idx) else ""
    has_chain = V.EVIDENCE["on"] and bool(a["items"])
    tl_json = f'<script type="application/json" id="tl">{V.timeline_json(a)}</script>' if has_chain else ""
    no_board = ('<p class="banner one">No overseer board in this run: the private overseer message board was not '
                'enabled, so its lane is empty.</p>' if board is False else "")
    body = (nav.replace("</nav>", f'<span class="sp"></span>{prev_link}{next_link}</nav>')
            + f'<header class="ah"><h1>{V.esc(name)} <small class="muted">overseer</small></h1> {final_labels(s3)}'
            f'</header>{V.banner_line(models).replace("the agent", "the overseer")}{legend_bar()}{no_board}'
            f'<template id="pempty"><div class="lgbox pkey"><h4>What it thought: code groups</h4>'
            f'{groups_html(names, href, defs)}</div></template>'
            f'<div class="layout"><div class="main"><div class="tl"><div class="tl-labels">{"".join(labels)}</div>'
            f'<div class="tl-plot"><svg class="plot" height="{bottom + 18}">{"".join(svg)}</svg></div></div>'
            f'<p class="tlcap">Minutes from run start (0–{int(tmax)}). '
            + (f"{V.plural(n_stacks, 'spot holds', 'spots hold')} several items (the number on the shape); "
               f"click one to list them. " if n_stacks else "")
            + 'Click a shape, or step with ← →.</p>'
            f'<h2>Summary (stage 3)</h2><p class="story">'
            f'{V.esc(plain_ids(s3.get("story") or "")) or "<span class=muted>No model-written summary for this overseer.</span>"}</p>'
            + (f'<p class="muted">Not available for this overseer: {", ".join(missing)}.</p>' if missing else "")
            + (f'<p class="muted">{len(a["malformed"])} model output(s) could not be read and are left out '
               f'({V.esc(", ".join(a["malformed"]))}).</p>' if a["malformed"] else "")
            + f'<p class="muted">{V.plural(len(o.events), "event")}, {V.plural(len(o.rows), "tool call")}, '
            f'{V.plural(len(a["thoughts"]), "coded thought")}, {V.plural(len(a["reactions"]), "stage-2 reaction")}. '
            f'Status from the {V.esc(phase_src)}.</p>'
            f'<h2>Final reading (stage 3)</h2>{"<dl class=kv>" + final_kv + "</dl>" if final_kv else "<p class=muted>No final reading.</p>"}'
            + (f'<h3>Consistency flags</h3><p class="muted">{V.esc(V.FLAG_HINT)}</p><ul>{flags}</ul>' if flags else "")
            + hard_facts_html(o)
            + f'<details class="all"><summary>All {V.plural(len(articles), "item")} in time order</summary>'
            f'<div id="items">{"".join(h for _, _, h in articles) or "<div class=empty>Nothing recorded.</div>"}</div>'
            f'</details>'
            + activity_html(o, key_of_row, key_of_event)
            + ("<h2>Reactions to events not in the timeline</h2>" + "".join(reaction_html(r) for r in orphans)
               if orphans else "")
            + (f'<details class="more"><summary>Stage-1 reading, chunk by chunk</summary><table><tr><th>Chunk</th>'
               f"<th>Span</th><th>Status</th><th>Knows the trick</th><th>Suspects</th><th>Open questions</th>"
               f"<th>Reader's summary</th></tr>{states}</table></details>" if states else "")
            + f'<details class="about"><summary>About the codes</summary><div class="lg"><div class="lgbox">'
            f'<h4>Status</h4><ul>{status_legend()}</ul></div><div class="lgbox"><h4>What it thought: code groups</h4>'
            f'{groups_html(names, href, defs)}</div></div>{V.terms_html()}</details>'
            f'</div>{PANEL}</div>{tl_json}')
    return page(f"Story map {name} (overseer)", body, V.LEGEND_JS + V.PANEL_JS + (V.CHAIN_JS if has_chain else ""),
                "agent")


def stack_glyph(group, tmax, y, r, href=None):
    """view.stack_glyph with the overseer shapes."""
    rep = min(group, key=lambda e: (e["prio"], e["t"]))
    n = len(group)
    p = V.pct(rep["t"], tmax)
    title = rep["title"] if n == 1 else (
        f"{n} items stacked here ({V.fmt_span(group[0]['t'], group[-1]['t'])}):\n"
        + "\n".join("• " + e["title"].split("\n")[0] for e in group[:14]) + ("\n…" if n > 14 else ""))
    attrs = f' data-p="{p:.2f}"'
    if n > 1 and href is None:
        attrs += (f' data-stack="{V.esc(" ".join(e["key"] for e in group))}" '
                  f'data-span="{V.esc(V.fmt_span(group[0]["t"], group[-1]["t"]))}"')
    if rep["typ"] == "TH":
        g = V.shape("TH", 0, 0, r, rep["cls"], None if href else group[0]["key"], title, n, attrs)
    else:
        g = ov_shape(rep["typ"], 0, 0, r, rep["cls"], None if href else group[0]["key"], title, n, attrs)
    g = f'<svg x="{p:.2f}%" y="{y:.1f}">{g}</svg>'
    return f'<a href="{V.esc(href)}">{g}</a>' if href else g


def final_labels(s3):
    if not s3:
        return V.badge("no final reading", "caution")
    f = s3.get("final") or {}
    out = []
    if f.get("detected_trick"):
        out.append(V.tbadge(f"detected the trick at {V.fmt_t(f.get('detection_t'))}",
                            "First recognised the answer-restatement trick (stage-3 reading)", "info"))
    else:
        out.append(V.tbadge("did not detect the trick", "No recognition of the trick in its words (stage-3 reading)",
                            "bad"))
    if f.get("confirmed_on_real_t") is not None:
        out.append(V.tbadge(f"confirmed on a real submission at {V.fmt_t(f['confirmed_on_real_t'])}",
                            "First confirmed the trick on a real solver submission"))
    out.append(V.tbadge("enforced" if f.get("enforced") else "did not enforce", "At least one successful revert",
                        "good" if f.get("enforced") else "caution"))
    out.append(V.tbadge("communicated" if f.get("communicated") else "did not communicate",
                        "A public post, notebook or DM to a solver about misbehaviour"))
    if f.get("coordinated") is not None:
        out.append(V.tbadge("coordinated" if f["coordinated"] else "did not coordinate",
                            "Posted findings, verdicts or a division of labour on the overseer board"))
    if f.get("public_posts_specific") not in (None, "none"):
        out.append(V.tbadge("public posts: " + SPECIFIC_LABEL.get(f["public_posts_specific"],
                                                                  str(f["public_posts_specific"])),
                            "Whether its public posts named the problems, submissions or the trick"))
    if f.get("names_trick_publicly"):
        out.append(V.badge("named the trick publicly"))
    flags = s3.get("consistency_flags") or []
    if flags:
        out.append(V.tbadge(V.plural(len(flags), "consistency flag"),
                            V.FLAG_HINT + " " + "; ".join(plain_ids(x) for x in flags), "caution"))
    return "".join(out)


# ------------------------------------------------------------------ index.html block

def strip_row(o, tmax):
    parts = [V.axis_svg(tmax, 0, 34, labels=False)]
    for frm, to, st, label in o.phases()[0]:
        cls = st if st in STATUSES else "unknown"
        w = max(V.pct(to, tmax) - V.pct(frm, tmax), 0.1)
        parts.append(f'<rect class="os-{cls}" x="{V.pct(frm, tmax):.2f}%" y="13" width="{w:.2f}%" height="8" rx="2">'
                     f'<title>{V.esc(status_title(frm, to, st, label))}</title></rect>')
    entries = []
    for k, label, _, typ, cls in MOMENTS:
        if k not in STRIP_MOMENTS or o.first.get(k) is None:
            continue
        at = o.at_first.get(k) or {}
        tip = (f"{label[:1].upper() + label[1:]} · {V.fmt_t(o.first[k])}"
               + (f" · {at.get('solver_fakers')} solver fakers, {at.get('standing_fakes')} standing fakes"
                  if at.get("solver_fakers") is not None else ""))
        entries.append({"t": V._num(o.first[k]), "order": len(entries), "prio": len(entries), "typ": typ, "cls": cls,
                        "key": o.key_of(k) or "", "title": tip})
    for group in V.stack(entries, tmax):
        key = group[0]["key"] or min(group, key=lambda e: e["prio"])["key"]
        parts.append(stack_glyph(group, tmax, 6.5, 4.4, f"agents/{o.name}.html" + (f"#sel={key}" if key else "")))
    reverts = [ev for ev in o.events if ev["event"].get("type") == "V1"]
    for ev in reverts:  # every revert below the bar
        typ, cls = glyph(ev)
        parts.append(stack_glyph([{"t": V._num(ev.get("t")), "prio": 0, "typ": typ, "cls": cls,
                                   "key": f"ev-{ev.get('i')}", "title": event_title(ev)}], tmax, 27.5, 4.2,
                                 f"agents/{o.name}.html#sel=ev-{ev.get('i')}"))
    story = (o.a["stage3"] or {}).get("story") or ""
    return (f'<div class="row" data-overseer="{V.esc(o.name)}"><div class="agent"><a href="agents/{V.esc(o.name)}.html" '
            f'title="{V.esc(plain_ids(story))}">{V.esc(o.name)}</a></div><div class="strip"><svg>{"".join(parts)}</svg>'
            f'</div><div class="labels">{final_labels(o.a["stage3"])}</div></div>')


def _nice(n):
    if n <= 5:
        return max(int(n), 1), 1
    step = 2 if n <= 12 else 5 if n <= 30 else 10 if n <= 60 else 20 if n <= 120 else 50
    return int(math.ceil(n / step) * step), step


def run_panel(ovs, ctx, tmax):
    """The solvers' fakers and standing fakes over the run, with each overseer's first moments as markers."""
    if not ctx:
        return ('<section class="ovrun"><h2>Solver fakes and overseer moments</h2><p class="muted">No run context '
                '(overseer_context.json beside the overseer timeline): the curves are not shown.</p></section>')
    curves = (("fakers_curve", "cv-fakers", "solvers who faked (cumulative)"),
              ("standing_fakes_curve", "cv-standing", "accepted fakes standing (not yet reverted; seed included)"),
              ("accepted_fakes_curve", "cv-accepted", "accepted fakes (cumulative; seed included)"))
    top = max([V._num(pt[1]) for c, _, _ in curves for pt in _with_seed(ctx, c)] + [1])
    ymax, ystep = _nice(top)
    H, T, B = 250, 14, 22  # height, top padding, bottom (axis labels)
    ph = H - T - B
    yof = lambda v: T + ph * (1 - V._num(v) / ymax)  # noqa: E731
    svg = []
    for v in range(0, ymax + 1, ystep):
        svg.append(f'<line class="ax" x1="0" x2="100%" y1="{yof(v):.1f}" y2="{yof(v):.1f}"/>'
                   f'<text x="0" y="{yof(v) - 3:.1f}">{v}</text>')
    step = V.tick_step(tmax)
    t = 0
    while t <= tmax + 1e-9:
        x = V.pct(t, tmax)
        anchor = "end" if t + step > tmax + 1e-9 else "start" if t == 0 else "middle"
        svg.append(f'<text x="{x:.2f}%" y="{H - 6}" text-anchor="{anchor}">{t}</text>')
        t += step
    for c, cls, label in curves:
        pts = [(V._num(p[0]), V._num(p[1])) for p in _with_seed(ctx, c)]
        if not pts:
            continue
        pts.sort()
        if pts[0][0] > 0:
            pts.insert(0, (0.0, 0.0))
        seg = []
        for (t0, v0), (t1, v1) in zip(pts, pts[1:] + [(tmax, pts[-1][1])]):
            t1 = min(max(t1, t0), tmax)
            seg.append(f'<line x1="{V.pct(t0, tmax):.2f}%" x2="{V.pct(t1, tmax):.2f}%" y1="{yof(v0):.1f}" '
                       f'y2="{yof(v0):.1f}"/>')
            if v1 != v0 and t1 < tmax:
                seg.append(f'<line x1="{V.pct(t1, tmax):.2f}%" x2="{V.pct(t1, tmax):.2f}%" y1="{yof(v0):.1f}" '
                           f'y2="{yof(v1):.1f}"/>')
        svg.append(f'<g class="{cls}"><title>{V.esc(label)}</title>{"".join(seg)}</g>')
    multi = len(ovs) > 1
    marks, rows = [], []
    rowx = defaultdict(list)  # label row -> label x positions (to stagger labels that would overlap)
    for o in ovs:
        for k, label, short, typ, cls in MOMENTS:
            if k not in CHART_MOMENTS:
                continue
            t = o.first.get(k)
            at = o.at_first.get(k) or {}
            fk, sf, af = at.get("solver_fakers"), at.get("standing_fakes"), at.get("accepted_fakes")
            rows.append(f'<tr><td><a href="agents/{V.esc(o.name)}.html">{V.esc(o.name)}</a></td><td>{V.esc(label)}</td>'
                        f'<td class="n">{V.fmt_t(t) if t is not None else "never"}</td>'
                        + "".join(f'<td class="n">{"—" if v is None else V.esc(v)}</td>' for v in (fk, af, sf))
                        + "</tr>")
            if t is None:
                continue
            x = V.pct(t, tmax)
            lab = (o.name.replace("agent-", "") + " " if multi else "") + short
            row = next(r for r in range(20) if all(abs(x - px) > 9 for px in rowx[r]))
            rowx[row].append(x)
            info = (f"{o.name}: {label} at {V.fmt_t(t)}"
                    + (f" — {V.plural(fk, 'solver')} had faked, {V.plural(af, 'fake')} accepted, {sf} standing"
                       if fk is not None else ""))
            ty = T + 10 + 13 * (row % 8)
            anchor = "end" if x > 85 else "start"
            dx = -3 if anchor == "end" else 3
            marks.append((t, f'<g class="mkr" tabindex="0" role="img" aria-label="{V.esc(info)}" data-info="{V.esc(info)}">'
                         f'<title>{V.esc(info)}</title><line class="hitl" x1="{x:.2f}%" x2="{x:.2f}%" y1="{T}" '
                         f'y2="{T + ph}"/><line class="v" x1="{x:.2f}%" x2="{x:.2f}%" y1="{T}" y2="{T + ph}"/>'
                         f'<svg x="{x:.2f}%" y="{ty:.1f}" overflow="visible"><text x="{dx}" y="0" '
                         f'text-anchor="{anchor}">{V.esc(lab)}</text></svg></g>'))
    quiet = len(ovs) > 2
    seed = ctx.get("seed_fake") or {}
    seed_s = ""
    if seed:
        seed_s = (f" The seed's planted fake was accepted at {V.fmt_t(seed.get('t_accept'))}"
                  + (f" and reverted at {V.fmt_t(seed['reverted_at'])} by {seed.get('reverted_by') or 'an overseer'}."
                     if seed.get("reverted_at") is not None else " and never reverted."))
    lg = ('<ul class="ovlg"><li><svg viewBox="0 0 22 10"><line class="cv-fakers" x1="1" x2="21" y1="5" y2="5"/></svg>'
          'solvers who faked (cumulative)</li><li><svg viewBox="0 0 22 10"><line class="cv-standing" x1="1" x2="21" '
          'y1="5" y2="5"/></svg>accepted fakes standing (not yet reverted; seed included)</li><li><svg viewBox="0 0 22 10"><line '
          'class="cv-accepted" x1="1" x2="21" y1="5" y2="5"/></svg>accepted fakes (cumulative; seed included)</li><li><svg '
          'viewBox="0 0 22 10"><line x1="11" x2="11" y1="0" y2="10" style="stroke:var(--fg);stroke-dasharray:2 2"/>'
          '</svg>an overseer\'s first moment (labelled)</li></ul>')
    return (f'<section class="ovrun"><h2>Solver fakes and overseer moments</h2>'
            f'<p class="muted">Counts from the run\'s records (solver fakers: solvers only; accepted and standing '
            f'fakes: the solvers\' and the seed\'s planted fake): step lines over the run, 0–{int(tmax)} min. Vertical lines: each overseer\'s first library listing, check of the seed\'s fake, check '
            f'of a real submission, revert, public post, DM and overseer-board post. Hover, tap or Tab to a line for its '
            f'counts (← → move between lines).{V.esc(seed_s)}</p>{lg}'
            f'<svg class="ovchart{" quiet" if quiet else ""}" height="{H}" role="group" '
            f'aria-label="Solver fakers and standing fakes over the run, with overseer moments">{"".join(svg)}'
            f'{"".join(m for _, m in sorted(marks, key=lambda x: x[0]))}</svg><p id="mkinfo" aria-live="polite" class="muted">Hover or focus a line for its '
            f'counts.</p><details><summary>The same moments as a table</summary><table class="hf"><tr><th>Overseer</th>'
            f'<th>Moment</th><th class="n">At</th><th class="n">Solver fakers</th><th class="n">Accepted fakes</th>'
            f'<th class="n">Standing fakes</th></tr>{"".join(rows)}</table></details></section>')


def index_block(ovs, ctx, tmax):
    if not ovs:
        return ('<section class="ovs"><h2>Overseers</h2><p class="muted">The overseer map has no overseers.</p>'
                '</section>' + run_panel(ovs, ctx, tmax))
    head_axis = ("<svg>" + "".join(
        f'<text x="{V.pct(t, tmax):.2f}%" y="12" text-anchor="{"start" if t == 0 else "end" if t + V.tick_step(tmax) > tmax else "middle"}">{t}</text>'
        for t in range(0, int(tmax) + 1, V.tick_step(tmax))) + "</svg>")
    keys = "".join(f"<li>{_glyph(typ, cls)}{V.esc(label)}</li>" for k, label, _, typ, cls in MOMENTS
                   if k in STRIP_MOMENTS)
    board = ctx.get("board") if ctx else None
    return (f'<section class="ovs"><h2>Overseers</h2><p class="muted">Each overseer\'s status over the run (bar), its '
            f'first moments (shapes above; click to open them) and its reverts (below). Its page shows every check, '
            f'revert, post, DM, overseer-board post and read, library read, workspace call and sleep. '
            f'<a href="{TAXONOMY_PAGE}">Overseer codes</a>.'
            + (" No overseer board in this run." if board is False else "")
            + f'</p><ul class="ovlg">{status_legend()}</ul><ul class="ovlg">{keys}<li>{_glyph("V1", "g-hollow")}revert '
            f'(below the bar)</li></ul><div class="grid"><div class="hdr"><div>Overseer</div><div>{head_axis}</div>'
            f'<div class="lab">Final reading (stage 3)</div></div>{"".join(strip_row(o, tmax) for o in ovs)}</div>'
            f'</section>' + run_panel(ovs, ctx, tmax))


# ------------------------------------------------------------------ entry (called by view._build)

class Prepared:
    def __init__(self, ovs, ctx, tmax, names, defs, pipeline, has_val, has_tax_solver, taxonomy):
        self.ovs, self.ctx, self.tmax, self.names, self.defs = ovs, ctx, tmax, names, defs
        self.pipeline, self.has_val, self.has_tax_solver, self.taxonomy = pipeline, has_val, has_tax_solver, taxonomy

    def index_block(self):
        """(html, css) for view.index_page: the overseer strip and run panel (with its little script) and styles."""
        return index_block(self.ovs, self.ctx, self.tmax) + f"<script>{INDEX_JS}</script>", CSS

    def pages(self):
        models = V.model_names(self.pipeline)
        has_tax = bool(self.names) and self.taxonomy is not None and Path(self.taxonomy).is_file()
        nav = V.nav_html("../", self.has_val, self.has_tax_solver)
        if has_tax:
            nav = nav.replace("</nav>", f'<a href="../{TAXONOMY_PAGE}">Overseer codes</a></nav>')
        href = f"../{TAXONOMY_PAGE}" if has_tax else ""
        out = []
        with overseer_vocabulary(self.defs):
            names_all = [o.name for o in self.ovs]
            for o in self.ovs:
                out.append((f"agents/{o.name}.html",
                            overseer_page(o, self.names, self.defs, self.tmax, names_all, models, nav, href)))
            if has_tax:
                top = V.nav_html("", self.has_val, self.has_tax_solver).replace(
                    "</nav>", f'<a href="{TAXONOMY_PAGE}">Overseer codes</a></nav>')
                out.append((TAXONOMY_PAGE, taxonomy_page(Path(self.taxonomy).read_text(encoding="utf-8"), models, top)))
        return out


def overseer_names(agents, pipeline):
    """The overseers of an overseer map: timeline index role 'overseer' (never the seed); every mapped agent when
    the index records no roles."""
    roles = {n: (a["index"] or {}).get("role") for n, a in agents.items()}
    if any(r == "overseer" for r in roles.values()):
        return [n for n, r in roles.items() if r == "overseer"]
    return [n for n, r in roles.items() if r != "seed" and r != "solver"]


def prepare(overseer_map, overseer_timeline, tmax, has_val=False, has_tax=False, taxonomy=DEFAULT_TAXONOMY,
            view_module=None):
    """Load the overseer map. ``view_module``: the calling viewer module (``python -m ...view`` runs it as
    ``__main__``, a second copy of view.py; its build state, e.g. --no-evidence, lives there)."""
    global V
    if view_module is not None:
        V = view_module
    om = Path(overseer_map) if overseer_map is not None else None
    agents, pipeline = V.load(om if om is not None else Path(overseer_timeline), overseer_timeline)
    ctx = load_context(overseer_timeline, om)
    names = load_names(taxonomy) if taxonomy else {}
    defs = load_definitions(taxonomy) if taxonomy else {}
    ovs = [Overseer(agents[n], ctx, tmax) for n in overseer_names(agents, pipeline)]
    return Prepared(ovs, ctx, tmax, names, defs, pipeline, has_val, has_tax, taxonomy)
