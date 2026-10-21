# Incident report (researcher-only)

A super high-level, highly readable account of one run, written per run for the owner (owner request,
October 21, 2026), modelled on OpenAI's misalignment incident reports: the whole story up front, two to
five case-study storylines with labelled verbatim excerpts, what didn't happen, candid uncertainty,
what to try next (prompts, models, population, oversight), a paragraph per agent and a "where to look"
appendix precise enough for a coding agent to drill into the transcripts.

It is **model-generated and post-hoc** and is labelled so on every page. The deterministic results
report (`results.html`) is unchanged and remains the record of outcomes. Operational acceptance is not
mathematical validity or intent; unknown and unresolved outcomes stay explicit.

```
# Paid (default): Claude Opus 5.5 plans and writes, Claude Sonnet 5.5 extracts excerpts.
PYTHONPATH=src:. .venv/bin/python scripts/run_incident_report.py --run-dir runs/<run> [--run-limit 20]
# Offline rehearsal (deterministic mock model, no network, no key), outside the run directory.
PYTHONPATH=src:. .venv/bin/python scripts/run_incident_report.py --run-dir runs/<run> --mock \
    --output-dir <scratch>/<run>/analysis/incident
# Tests (offline)
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s research/analysis/tests -p test_incident_report.py
```

Output, in `RUN/analysis/incident/` (the site links to it):

| File | What |
| --- | --- |
| `report.html` | The report: banner, labelled excerpt blocks (reasoning, message, tool call, submission, board post, DM...), each with agent, time, record id, an **Inspect View deep link** and a link to its appendix row; the host-written "Where to look" appendix. |
| `report.md` | The same report in Markdown (unverified quotes marked inline). |
| `evidence.json` | The plan, every fetched record with its locator (Inspect log, sample, epoch, model event and message ids; raw record path), every excerpt with its verification result, dropped plan items, the writer's revision attempts and the model units with their costs. |
| `digest.json` | The deterministic digest the models read. |
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
3. **Evidence**: exact record texts by id. A record of at most 1,800 characters is offered whole; a
   longer one goes to Sonnet (one request per storyline), which copies one to three short verbatim
   passages; the host checks each passage and discards an unverified one (recorded in evidence.json).
4. **Write** (Opus): the Markdown report in the requested structure. Quotes only through excerpt
   blocks (`:::excerpt <id>` ... `:::`) or `"words" [<id>]`; labels, agent, time and links are added
   by the host, never by the model.
5. **Verify** (host, `verify.py`): every quote piece (pieces separated by `[...]`) must be a verbatim
   substring of the cited record's full text after **whitespace normalization only**; every cited id
   must exist; the required sections must be present. With problems, Opus revises once; the version
   with fewer problems is kept and any remaining unverified quote is marked in report.html and
   report.md and listed in evidence.json.
6. **Render** (host, `render.py`): report.html (clean serif reading typography, light and dark),
   report.md, evidence.json. Inspect View links point at the run's bundle
   (`RUN/inspect-view/index.html#/logs/<log>/samples/sample/<agent>/<epoch>/transcript?event=<model
   event uuid>`), which opens the agent's transcript at the turn that produced the record. A run
   directory without a local `inspect-view/` bundle is noted in the appendix (the links work where the
   bundle is published beside the run).

## Budget and models

One spending control (docs/plan.md, "One per-run budget, no manual approvals (October 20, 2026)"):
`--run-limit` USD, default $20, is this run's incident-report budget. Every invocation into the same
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
