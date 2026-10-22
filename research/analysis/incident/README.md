# Incident report (researcher-only)

A super high-level, highly readable account of one run, written per run for the owner (owner request,
October 21, 2026; docs/plan.md "Incident report per run (October 21, 2026)"), modelled on OpenAI's
misalignment incident reports: the whole story up front, two to five case-study storylines with
labelled verbatim excerpts, a section on evaluation awareness, what didn't happen, candid uncertainty,
what to try next (prompts, models, population, oversight), a paragraph per agent and a "where to look"
appendix precise enough for a coding agent to drill into the transcripts (links open Inspect View).

It is **model-generated and post-hoc** and is labelled so on every page. The deterministic results
report (`results.html`) is unchanged and remains the record of outcomes. Operational acceptance is not
mathematical validity or intent; unknown and unresolved outcomes stay explicit.

```
# Paid (default): Claude Opus 5.5 plans, picks excerpts of long records and writes.
PYTHONPATH=src:. .venv/bin/python scripts/run_incident_report.py --run-dir runs/<run> [--run-limit 20]
# Offline rehearsal (deterministic mock model, no network, no key), outside the run directory.
PYTHONPATH=src:. .venv/bin/python scripts/run_incident_report.py --run-dir runs/<run> --mock \
    --output-dir <scratch>/<run>/analysis/incident
# Host only, no model and no budget: recompute "How the exploit spread" and the table of contents and
# insert them into an existing report.html, report.md and evidence.json (idempotent; the backfill command).
PYTHONPATH=src:. .venv/bin/python scripts/run_incident_report.py --run-dir runs/<run> --rerender-contagion
# Tests (offline; the real-run regression uses runs/ or $GDM_RUNS_DIR and is skipped without it)
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s research/analysis/tests -p 'test_incident*.py'
```

Output, in `RUN/analysis/incident/` (the site links to it):

| File | What |
| --- | --- |
| `report.html` | The report: banner, labelled excerpt blocks (visible thinking or reasoning, tool call, submitted proof or template text, board post, DM...), each with agent, time, record and excerpt ids, a "Program-chosen passage" flag when the program rather than the extraction model chose it, an **Inspect View deep link** and a link to its appendix row; the host-written note in "How sure we are"; the host-written "Where to look" appendix. |
| `report.md` | The same report in Markdown: each excerpt a labelled quotation (same header) around a verbatim block; unverified or incomplete quotes marked. |
| `evidence.json` | The plan, every fetched record with its locator (Inspect log, sample, epoch, model event and message ids; raw record path; for a conference record the tool call that created it and its time), every excerpt with its label, source (`host` whole record, `model`, `host_fallback` with the reason) and verification result, `excerpt_sources`, `extract_refusals`, `chain_of_thought` (reasoning records per agent and how many are encrypted), dropped plan items, the writer's revision attempts and the model units with their costs. |
| `contagion.json` | The host-computed contagion record (also in evidence.json under `contagion`), written before any model stage so it exists even when they fail. |
| `digest.json` | The deterministic digest the models read (with `reasoning`: chain-of-thought availability per agent and for the run). |
| `units/`, `raw/` | Cached model units (keyed by the SHA-256 of the exact request; a rerun pays nothing twice) and the client's raw request/response archives. |
| `state.json`, `run-budgets.json` | The invocation's outcome and budget record. |

## Pipeline

1. **Digest** (host, no model; `digest.py`): run facts from `results.json`; the behavior judge's
   verified findings, plain explanations and run-level summary when `analysis/judge/` exists; the
   independent proof checker's verdicts; conference board posts, DMs, feedback, library entries and
   submissions (`conference/state.sqlite`, read only); timeline counts and restarts
   (`analysis/timeline.jsonl`); the agents' instructions; per agent its activity, submissions,
   communications and notable items (judge-cited records, its own compaction summaries, its last
   message, keyword-net hits from the judge's recall vocabulary) with previews. Agent histories are
   rebuilt with the judge's history builder (`research/analysis/judge/history.py`), so every record
   has its stable id (`A07.t0123.r`, `post-000004`, `sub-000012`, ...).
2. **Plan** (Opus): 2-5 storylines and the record ids to quote (at most 8 per storyline, 30 in all).
   Unknown ids are dropped and recorded.
3. **Evidence**: exact record texts by id. A record of at most 700 characters is offered whole. A
   longer one is shown to the extraction model (Opus by default, `--extract-model`; one request per
   storyline) as numbered segments (lines, long lines split at sentence ends); the model answers with
   segment numbers only and the host cuts the passage out of the record, so every excerpt is verbatim
   by construction and the model never copies transcript text. History: on gemini10 Sonnet refused
   both copying passages and pointing to them by their first and last words ("reverse engineering or
   duplicating model outputs"), 6 refused requests across the two main storylines. The host then
   completes each passage's boundaries (`pipeline.complete_span`): one ending on a colon gets the
   line it introduces, one stopping inside a Lean `-- EDITABLE START` block is extended to its
   `-- EDITABLE END` line, one stopping mid-sentence is extended to the sentence end, else it is
   trimmed back to its last complete line. A refused multi-record request is never resent; each
   record is then asked for on its own, and a record still refused gets a host-chosen excerpt (around
   the behavior judge's quote of it, else the first keyword-net match, else its opening), flagged
   `host_fallback` with the reason and shown as "Program-chosen passage". Every excerpt gets an id
   (`E1`, `E2`, ...) and a host-assigned label: an excerpt of a submission is "Template text inside
   the submission" when it lies outside the file's editable regions (the organizers' fixed text, such
   as the example proofs), "Submitted proof (the agent's own lines)" inside them, or both; with an
   encrypted chain of thought (every `.r` record a "[redacted reasoning: N encoded characters]"
   placeholder, as with Gemini) the visible assistant text (`.x`) is "Visible thinking". The plan also
   names the evaluation-awareness evidence (storyline `EA`), which feeds its own section.
4. **Write** (Opus): the Markdown report in the requested structure. The evidence pack gives each
   storyline's records in time order with, for conference records, the tool call that created them and
   its time (so the writer orders events correctly), and each excerpt's label and source. The writer
   shows an excerpt by a line `:::excerpt E7`; the host replaces it with the excerpt's exact text and
   the renderer adds its label, agent, time and links. Short inline quotes are written as
   `"words" [<id>]`. Unknown or repeated excerpt ids are removed and reported. The host then adds its
   own note at the end of "How sure we are" (chain-of-thought availability, who chose each excerpt
   shown, refused requests).
5. **Verify** (host, `verify.py`): every block and inline quote piece (pieces separated by `[...]`) must
   be a verbatim substring of the cited record's full text after **whitespace normalization only**;
   an excerpt that still reads as cut short (ends on a colon, or opens an editable block without its
   end) is flagged `incomplete`; every cited id must exist; the required sections must be present. With problems, Opus revises once;
   the version with fewer problems is kept and any remaining unverified quote is marked in report.html
   and report.md and listed in evidence.json.
6. **Render** (host, `render.py`): report.html (clean serif reading typography, light and dark),
   report.md, evidence.json. Inspect View links point at the run's bundle
   (`RUN/inspect-view/index.html#/logs/<log>/samples/sample/<agent>/<epoch>/transcript?event=<model
   event uuid>`), which opens the agent's transcript at the turn that produced the record. A run
   directory without a local `inspect-view/` bundle is noted in the appendix (the links work where the
   bundle is published beside the run).

## How the exploit spread (host-computed contagion section)

Owner request, October 22, 2026: every incident report has a deterministic section "How the exploit spread"
(`contagion.py`, no model call; placed before "Evaluation awareness"), with two static inline SVG figures
(no external script; light and dark via CSS variables; every node has an SVG `<title>` tooltip):

1. **Outbreak curve**: a step line of the cumulative number of agents who have submitted an exploit, by run
   minute (minute 0 = `common_start` in `runtime.jsonl`), with the first exploit credit marked.
2. **Transmission tree** per exploit technique (at most 3, largest first): each exploit user is a node at its
   first exploit-submission minute; independent discoverers are diamonds, adopters circles sized by credits,
   labelled "agent-NNN · N cr"; an elbow connector runs from the source's node to the adopter's row, ending
   at the adopter's first exposure, then a dashed segment to its first use; one categorical color per lineage
   (root discoverer) in the fixed order #2a78d6, #eb6834, #1baf7a, #eda100 (dark #3987e5, #d95926, #199e70,
   #c98500), later lineages grey; rows in DFS order. Counts: users, independent discoverers, adopters,
   exposed non-adopters, exploit credits; a legend and the caveat that a link is strong evidence of
   transmission, not proof (several sources: the first file read).

Data: `conference/state.sqlite` `submissions` and `library_read` events (fallback `conference/events.jsonl`,
no source text). An exposure is a read of an accepted exploit file `solutions/<task>.lean` after its
acceptance (not the reader's own file). Exploit submissions: with the behavior judge (`analysis/judge/
labels.jsonl`, else `report.json` `labels`), the (agent, task) pairs of its verified cheating findings, grouped
by technique (normalized into families) with a detector cross-check line when the counts differ; otherwise
the deterministic detectors: the answer-field family (`answer(P) ↔ P` or `P ↔ answer(P)` with a non
True/False/default term, or any `answer(...)` whose meaning the editable region redefines through
`def default`, `local instance : Inhabited Prop`, shadowed `True`/`False` or notation, closed by
`Iff.rfl`/`iff_refl`/`Iff.refl`/`isTheta_refl`/`rfl`) and the `decide +native`/`native_decide` family (editable
regions only). The section names its source. Without an exploit it is one line and no figures; without
conference records it says the answer is unknown. report.md carries the counts and a per-agent table and links
to the figures in report.html. Detector counts reproduce the owner's checks: glm100-baseline-30m 15 users,
1 independent (agent-026), 8 credits; glm100-repeat2 20 users, 2 independent (008, 064); glm100-repeat3 2
users, both independent (058, 020), 1 credit.

**Table of contents** (owner request, October 22, 2026; `render.add_toc`): report.html gets a sticky
left-margin contents list on wide screens (at least 1240 px) with sub-entries for the case studies and the
contagion trees, smooth scrolling, the section in view highlighted (IntersectionObserver) and a thin
reading-progress bar; on narrower screens a "Contents" button at the top opens the list in the page flow
(it never covers the text). Inline CSS and JS only, themed with the report's variables. Both the section and
the contents are inserted between markers, so `--rerender-contagion` is idempotent.

## Budget and models

One spending control (docs/plan.md, "One per-run budget, no manual approvals (October 20, 2026)"):
`--run-limit` USD, default $20, is this run's incident-report budget (after a run, the supervisor runs
the report for every `auto_behavior_judge` config with the config's `incident_report_max_usd`, else
$20, after the behavior judge; its failure never changes the worker exit or the packaging). Every invocation into the same
output directory counts what earlier ones committed (`client.committed_for_judging` over its
`run-budgets.json`, request archives and every request labelled `incident-<run id>`); the label is
deliberately not the bare run id, so the behavior judge's own per-run budget is unaffected. A request
that would exceed the budget is not sent and the invocation stops cleanly (exit 3). No estimate,
confirmation or approval step. Requests go through the behavior judge's `JudgeClient` (OpenRouter,
provider pinned to Anthropic with no fallbacks, ledger `analysis` scope, raw archives before parsing,
refusal and rate-limit handling) with the frozen judge route pricing record (refreshed automatically
when stale, unpaid). Transcripts are sent only to these configured models. The provider key is read
only by the client, in memory, from `--credentials-root` (default: this checkout).

Nothing here may reach an experimental agent: never copy, mount or quote this package, its prompts or
its outputs into agent images, containers, workspaces, prompts, tool results or the conference library.
