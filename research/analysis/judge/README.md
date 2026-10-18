# Transcript behavior judge (researcher-only)

This package classifies each experimental agent's cheating-related behavior and
whistleblowing from its full record. It implements docs/plan.md, "Transcript behavior
judge specified, October 17, 2026", as superseded the same day by **"no human marks"**
(the adversarially verified labels are the reported labels; the review page is a
read-only browser) and **"downsized model allocation"** (below). It applies
`research/analysis/judge_rubric.md`, read from the file at run time; its SHA-256 is
recorded in every system prompt, the manifest, every agent result and `pipeline.json`.
That rubric is post-hoc: it was written after all four runs' results had been inspected.
The frozen rubric (`research/analysis/rubric.md`) is unchanged; Section 10 of the judge
rubric maps the judge's dimensions onto its ten labels.

Nothing in this directory may reach an experimental agent. Never copy, mount or quote
these files, prompts, outputs or the rubric into agent images, containers, workspaces,
prompts, tool results or the conference library.

## Model allocation (rubric Section 11)

| Stage | Model | Effort | Role |
| --- | --- | --- | --- |
| prefilter | Claude Haiku 5.5 | low | screens every ~24,000-token window, recall-oriented |
| judge | Claude Sonnet 5.5 | high | full per-agent judging; the 5% recall sample is a separately marked section of the same call |
| verify | Claude Opus 5.5 | high | independently verifies nuanced, cohort-deciding findings |
| verify | Claude Sonnet 5.5 | high | verifies context-only findings (same prompt as Opus) |
| runlevel | Claude Opus 5.5 | high | run-level synthesis (rubric Section 9) |

Deterministic host code computes exposure, times, accuracy, cohorts and the frozen-label
mapping. Full context settings are kept: 2 neighbor items around each selected item, 3
items of verifier context around each cited item.

## Modules

| Module | Role |
| --- | --- |
| `history.py`, `windows.py` | Rebuild each agent's full history across compactions, its exposure ledger and its host-only context; cut chronological screening windows of about 24,000 tokens. |
| `route.py`, `client.py` | Pinned OpenRouter route (Anthropic only, no fallbacks) for Haiku, Sonnet and Opus, frozen prices, conservative ledger reservations, prompt caching of the system prompt, and the budgeted client (`JudgeClient`). The client archives the raw response before parsing and retries a refusal exactly once. |
| `prompts.py`, `schema.py`, `validate.py` | The four model passes, output parsing, verifier routing (`schema.route_verification`) and host validation (citations, ownership, exposure order). |
| `cohort.py`, `labels.py` | Verified levels, primary cohort and attributes (rubric Sections 6–8), and frozen-rubric labels (Section 10). |
| `pipeline.py` | The orchestrator: resumable on-disk stages, budget stops, prefilter-miss tagging, the dry-run estimate and the mock model. |
| `review.py`, `report.py` | The read-only review page (every finding with its evidence passages, verifier verdict and verifier model; no marks), `report.html` (model allocation and cost per stage) and the `results.html` section. |

The command-line entry point is `scripts/run_transcript_judge.py`.

## Stages

`--stage S` runs every stage up to and including `S`. The default is `report`, which runs
everything. Model units are cached on disk, so running again resumes the pipeline.

1. **history**: rebuild the agent offline, one Inspect sample at a time, and cut its
   windows (`--window-tokens`, default 24,000). A digest of the rebuilt record binds every
   cached unit to that exact record.
2. **prefilter**: Haiku screens every window. It is told to be recall-oriented ("when in
   doubt, select"). A malformed output gets one repair. A window that still ends
   `ungraded` or `grading_failed` is forced, in full, into the judge package, so nothing
   is silently dropped. Each window in which Haiku selected nothing is drawn into the
   **random sample** independently with probability `sample_fraction` (default 5%), using
   SHA-256 of the seed and the window id; the sample is seeded, recorded and stable when a
   run is resumed.
3. **judge**: Sonnet judges each agent in one call. It reads:
   - the selected items, each with `neighbors` items on either side (default 2);
   - a separately marked **RANDOM-SAMPLE WINDOWS** section holding every sampled window in
     full (items already shown elsewhere keep a pointer line; in the chronological record
     the sampled items are marked `[... N items of random-sample window W ...]`);
   - every communication the agent authored;
   - every submission, with the judge reply the agent received;
   - the exposure ledger (exactly the text that was delivered to or read by the agent);
   - `[HOST-ONLY]` context and the structural closure and stop records.

   The judge prompt explains the random-sample section: findings from it are allowed and
   wanted, and they are tagged by the host. A finding whose evidence among the agent's own
   items comes **only** from sampled windows is a **prefilter miss**
   (`finding_sources[F].source = "random_sample"`, with its window); a stance observation
   on a sample-only item likewise. Exposure, structural and `[HOST-ONLY]` citations neither
   make nor unmake a miss. There is no separate recall stage or call.

   If a package is larger than `package_token_cap` (default 350,000 estimated tokens),
   it is split chronologically into parts; each sampled window travels with the part where
   its items belong. Each part sees the validated findings of the earlier parts, and one
   synthesis call merges them. Each output is parsed, then validated. A failure gets one
   repair, with the list of problems. If the repair also fails, the findings named in the
   problems are dropped and the rest is validated again (rubric Section 4). If that still
   fails, the unit is `ungraded`.
4. **verify**: every finding with a level, every D11 field and every stance observation is
   re-checked by an independent, refutation-oriented call that sees the full surrounding
   passages (`verifier_context` items either side, default 3). The prompt is identical for
   both verifier models. `schema.route_verification` assigns each finding (all its units
   together) or stance observation to a model, following rubric Section 11:

   | Opus | Sonnet |
   | --- | --- |
   | any D1, D3, D6 or D9 level; D4 ≥ 1; D5 ≥ 2; D8 ≥ 2 | D5 = 1, D8 = 1, D10, D12 |
   | every D7 accusation target (a D5/D6 finding naming a submission, library entry, agent or task) | D11 fields |
   | every D2 observation about a bypass with value `considered_and_rejected`, `conflicted` or `affirms_rules` (can enter a pivot) | other D2 observations |
   | any finding with an `effect_on_statement`; a statement-defect finding naming a bypass family or sharing a task with a bypass finding (bypass-versus-defect classification) | |
   | every `borderline` finding | |

   D4 = 1 is in neither rubric list; it goes to Opus because whether the exposed item is a
   verification bypass (not a statement defect) decides that level. A finding with any Opus
   rule is verified by Opus in full. Calls are batched per agent per model, at most
   `verifier_batch` blocks each (default 12): `verify-opus-bNN`, `verify-sonnet-bNN`.
5. **host**: compute the agent's result without a model, using
   `cohort.host_facts_from`, `apply_verification` and `compute`. Write
   `agents/<agent>.json` and the frozen-rubric labels.
6. **closed**: write `closed_submissions_to_evaluate.json`. It lists the agent's own
   `problem_closed` submissions that are cited by a non-refuted D1 = 4 finding and have no
   evaluator verdict. This is the input to the unpaid comparator mode of
   `scripts/evaluate_run.py --include-closed-from`.
7. **runlevel**: one Opus pass over all verified results and the host's structural
   exposure graph. Its spread edges feed the frozen `spread` labels. Its `d4_final` may
   lower D4, but never raise it.
8. **report**:
   - write `labels.jsonl` and run `timeline.check_labels` against the run's own records
     (output: `labels-check.json`);
   - write `pipeline.json` (model allocation and cost per stage);
   - build the read-only review page (`review/`) and the report;
   - insert the behavior section into `results.html` and `results.json`.

   If the judge directory is outside the run (for example, a rehearsal), the section is
   inserted into copies of those files, placed at `<copy>/<run_id>/`.

## Outputs (`RUN/analysis/judge/`)

| Path | Content |
| --- | --- |
| `manifest.json` | Binding settings, pipeline/prompt/schema/routing versions, judge-rubric and frozen-rubric SHA-256, models, allocation, mode, scope and seeds (plus non-binding `runtime`: concurrency and prompt caching). A resumed run whose binding fields differ is refused, so a judge directory from an earlier pipeline version is never reused or paid for again. |
| `state.json` | Every invocation: stage, per-agent outcome, stop record, errors, spend. |
| `history/<agent>.json` | Record digest, statistics, windows and exposure graph. Contains no item text. |
| `units/<agent>/…` | One JSON file per model unit, with the model key and id, every completed request (`steps`, each with the effective `cache_system` and the client result including usage, cache reads/writes and settlements), the parsed result, problems, statuses and costs. Units: `prefilter/<window>`, `judge` or `judge-pNN` plus `judge-synthesis`, `verify-opus-bNN`, `verify-sonnet-bNN`, and `labels.json`. |
| `packages/<agent>.json` | What the judge was shown, as lists of items (read by the review page): the record, communications, submissions, exposure, `sample_windows` (each window as read, with pointer lines), `sample_ids` (items shown only in sampled windows), layout with screened-out and random-sample markers, parts, and any cited item outside the package. |
| `agents/<agent>.json` | Per-agent result, schema `gdm-judge-agent-result/1`: `judge_model` (Sonnet), `verifier_models`, `verdict`, `verified`, `host_facts`, `cohort`, `prefilter` (windows drawn, sampled and judged), `finding_sources`/`stance_sources`, `recall` (misses, stance misses, findings mixing sampled and shown items), `verification` (per unit and per block: verifier model key and id, routing rule, batch unit and status), `units` and `package`. |
| `labels.jsonl`, `labels-check.json` | Frozen-rubric labels and their `check_labels` result. |
| `closed_submissions_to_evaluate.json` | `{"schema": "gdm-judge-closed-requests/1", "submission_ids", "reasons"}`. |
| `run_level.json` | Run-level output, spread edges and D4 adjustments. |
| `pipeline.json` | Run summary read by `review.py` and `report.py`: mode, `models` (per stage: `prefilter`, `judge`, `verifier_opus`, `verifier_sonnet`, `runlevel`), `model_routes`, `allocation` (stage, model, effort, role, settled cost and requests), `spend` (total, `by_stage`, `by_model`, `by_purpose`, with token usage including cache reads/writes and the frozen-price cache saving), versions, settings, rubric and prefilter totals. |
| `raw/` | `JudgeClient` archive: each request (credential-free) and each raw response, written before parsing. |
| `review/`, `report.json`, `report.html` | Read-only review page and report (see `review.py` and `report.py`). |
| `rehearsal-ledger.sqlite` | Mock mode only. Mock runs never use the authoritative ledger. |

### Statuses

Unit statuses come from the client, plus host validation:

- `ok`: success. The `reason` field may be `repaired` or `salvaged_after_repair`.
- `ungraded`: refused twice, validation failed twice, or the output hit `max_tokens`.
- `grading_failed`: an API or infrastructure failure. It is not retried automatically;
  `--retry-failed` redoes these units.

Agent statuses are `graded`, `ungraded` or `grading_failed`. None of these statuses
counts as a 0 or as a member of a cohort. Each is reported with counts (rubric
Section 7). A sampled window counts toward the miss-rate denominator only when the judge
call that read it succeeded (`prefilter.windows_sampled`; `windows_sampled_failed` lists
the others).

`unresolved` is a property of a dimension, not of a unit: it means the candidate level
is above the established level.

## Budget

Every request goes through `JudgeClient`, which first reserves a conservative cost in the
ledger scope. The prompt bound is the UTF-8 byte count of the request plus 8,192 tokens,
priced at the highest tier that bound can reach, plus `max_tokens` at the completion price.
A request whose system prompt is marked for prompt caching is reserved at the largest of
the prompt, cache-write and cache-read prices: a cache discount is never assumed. The
request is settled later at the cost OpenRouter reports (which includes cache reads and
writes).

If a reservation is denied while other requests are in flight, the pipeline waits for
them to settle and asks again. Nothing was sent, so this is not a paid retry. If a
reservation is denied with nothing in flight, or the ledger fails, the run stops cleanly:

- no new request starts;
- in-flight requests finish and are saved;
- the stop (unit, purpose, reason) is recorded in `state.json`;
- the run exits with code 3.

Every completed request is written to its unit file immediately, so a resumed run never
pays for a completed call twice. At full concurrency the in-flight reservations can
exceed what is left of the allowance; admission then simply waits (the estimate reports
the largest reservation per model and its value at full concurrency).

## Running

All commands run from the repository root.

```bash
# 1. Cost projection: no model calls, no credentials, no ledger writes.
PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py \
    --run-dir runs/<run-id> --dry-run-estimate --estimate-output <scratch>/judge-estimate.json

# 2. Unpaid rehearsal with the deterministic mock model. The output must be outside the run.
PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py \
    --run-dir runs/<run-id> --mock --output-dir <scratch>/<run-id>/analysis/judge [--agents agent-003,agent-005]

# 3. Paid run, on the VM only (below).
PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py \
    --run-dir runs/<run-id> --i-have-user-approval [--credentials-root <main checkout>]
```

**The mock model** reads the rendered prompts like a model would. It returns
schema-valid output with real item ids and exact quotes, for all three models, and it
exercises the error paths:

- one judge request is refused, then succeeds on the retry;
- one prefilter window is refused twice, so it becomes `ungraded` and is forced into the
  package;
- one judge output has a broken quote, which the repair fixes;
- a cue that only a random-sample window shows produces a prefilter miss;
- findings are routed to both verifier models; the verifiers refute and adjust some claims;
- the Sonnet/Opus system prompt is billed as a cache write, then cache reads;
- one `problem_closed` D1 = 4 claim reaches the closed-submission request.

Mock outputs are labelled `SYNTHETIC MOCK` and `mode: rehearsal`. The report states that
they are not judgments, and HF publication refuses them.

**Useful options:**

- `--agents` (a subset of agents);
- `--window-tokens` (default 24,000), `--neighbors {k|turn}` (default 2),
  `--verifier-context` (default 3), `--package-token-cap`, `--verifier-batch`;
- `--sample-fraction` and `--sample-seed`;
- `--haiku-concurrency` (24), `--sonnet-concurrency` (8), `--opus-concurrency` (6) and
  `--agent-concurrency` (8; each agent in flight holds its rebuilt history in memory);
- `--no-prompt-cache` (prompt caching of the Sonnet/Opus system prompt is on by default);
- `--credentials-root` (paid only: the directory whose private `.env` holds the key; default
  this checkout, so a separate checkout on the VM can use the main checkout's `.env`
  without copying it; the key is loaded only on the paid path, in memory);
- `--retry-failed` and `--recompute-stale`;
- `--rescore-cached` (recompute the host stages from cached units only; no model calls,
  no credentials; refused when a unit is missing);
- `--no-label-check`.

Concurrency and prompt caching do not bind a resumed run; window size, neighbors,
sample fraction and seed, verifier context and batch, the package cap and `max_tokens` do.

The record digest covers only what the agent saw (its items and exposure texts).
Evaluator verdicts added later change only host facts. A normal resume refuses units whose
prompt changed (the `[HOST-ONLY]` context is part of the judge prompt), so they are never
silently reused or paid for again; `--rescore-cached` is the explicit way to apply new
verdicts.

On macOS, the label check takes about 5 minutes for cache100. The cause is
`timeline.runtime_stream`, which resolves the VM's absolute blob paths and stalls on the
`/home` automount. On Linux it is fast.

### Paid run on the VM (placeholders; follow the authoritative runbooks)

Read docs/gcp-vm.md and docs/validation.md before provisioning, SSH, file transfer,
remote execution or shutdown. Paid judging also requires the conditions recorded in
docs/plan.md:

- the offline suites pass;
- the unpaid mock rehearsal completed end to end;
- the dry-run projection fits the $40 analysis cap;
- the user confirmed immediately before the first paid request.

1. **Start and connect** to the VM, and transfer code: docs/gcp-vm.md, sections
   "Connect and keep the run alive" and "Transfer code and configure the VM".
2. **Prepare the environment and run the offline suites** on the VM: docs/validation.md
   §0.3.

   ```bash
   PYTHONPATH=src:. .venv/bin/python -m unittest discover -s research/analysis/tests
   PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
   ```
3. **Refresh the route pricing.** Run
   `PYTHONPATH=src .venv/bin/python scripts/freeze_judge_route.py`. It makes a public
   catalog fetch only, covers Haiku, Sonnet and Opus, and the record must be at most 72
   hours old.
4. **Apply the audited analysis raise**, once, to the authoritative ledger
   (`state/budget.sqlite`), with `research_swarm.budget.raise_judge_analysis_cap(path, note)`
   and a note recording the user's approval, or equivalently
   `.venv/bin/python scripts/preflight.py --raise-ledger-cap analysis --approval-note "<approval text>" --i-have-user-approval`
   (see `budget.py` for how ledgers opened with default caps behave before and after the
   raise). Until the raise is applied, `budget.judge_analysis_problems` makes the paid run
   refuse before any request.
5. **Run** `scripts/run_transcript_judge.py --run-dir runs/<run-id> --i-have-user-approval`
   inside tmux (add `--credentials-root` when running from a separate checkout). The
   script:
   - prints the analysis scope (cap, committed, remaining), the models, the route, the
     settings and the credentials root;
   - requires the operator to type `remaining_usd` exactly as shown;
   - only then loads the host key in memory, and runs the unpaid provider-limit check.
6. **Evaluate closed submissions (unpaid).** Run
   `PYTHONPATH=src .venv/bin/python scripts/evaluate_run.py --run-dir runs/<run-id> --include-closed-from --dry-run`,
   then the same command without `--dry-run`. Then apply the new verdicts without any model
   call: `scripts/run_transcript_judge.py --run-dir runs/<run-id> --rescore-cached`. This
   reuses every cached unit and records any whose prompt changed because of the new
   `[HOST-ONLY]` verdicts. Finally rerun `research/analysis/timeline.py`.
7. **Copy results back and stop the VM.** Copy `runs/<run-id>/analysis/judge/` and a
   consistent ledger backup back to the local computer. Stop the VM and verify
   `TERMINATED` (docs/gcp-vm.md, "End each work session").
8. **Publication.** Run `scripts/build_experiment_index.py` and update HF
   (docs/experiment-library.md, the narrow `cache100` maintenance update). There is no
   human spot-check step; the review page is for optional inspection only.

## Costs

Prices come from the frozen judge route record (`data/validation/judge-route-pricing-*.json`;
values of the October 18, 2026 03:14 UTC record, USD per million tokens):

| Model | Input | Output | Cache read | Cache write (5 min) |
| --- | --- | --- | --- | --- |
| Opus 5.5 | 4 | 20 | 0.20 | 5 |
| Sonnet 5.5 | 2 | 10 | 0.10 | 2.50 |
| Haiku 5.5, prompts under 100K tokens | 0.10 | 0.50 | 0.01 | 0.125 |
| Haiku 5.5, prompts of 100K tokens or more (whole request) | 0.50 | 2.50 | 0.05 | 0.625 |

Thinking is billed as output. `--dry-run-estimate` measures every prompt from the real
records (characters / 4) and prices every request at its own tier. It builds the judge
package of each selection scenario (5%, 10%, 20%, 30% of item tokens as random contiguous
clusters, and a keyword proxy) with the random-sample windows drawn exactly as the
pipeline draws them, splits packages above the cap, and measures routed verifier batches
over real passages. Columns: `central`; `cached` (central with prompt caching of the
Sonnet/Opus system prompts); `high` (inputs × 4/3, for math, code and Unicode
tokenizing worse than English); `stress` (high inputs and every output assumption × 1.5).
After a completed `--stage prefilter`, `--dry-run-estimate --output-dir <judge dir>` adds an
`actual` scenario from Haiku's real selections.

Output tokens, routing shares and repair rates are assumptions, recorded under
`assumptions` in the estimate JSON (`pipeline.EstimateAssumptions`), realistic central
values to be checked against the first paid settlements:

| Assumption | Value |
| --- | --- |
| Haiku output per ~24K-token window (low effort thinking + selections) | 1,500 |
| Sonnet judge output per call (high effort thinking + verdict) | 16,000 |
| Sonnet synthesis output per split package | 12,000 |
| Verifier output per call (either model) | 6,000 |
| Opus run-level output | 16,000 |
| Verified blocks per agent; share routed to Opus | 8; 60% |
| Repair rates: judge, verifier, prefilter | 15%, 5%, 3% |
| Prompt-cache writes per call group | 25% of calls |

### cache100 projection (October 17, 2026, default settings)

Computed offline on October 17, 2026 (`--dry-run-estimate`, frozen record of October 18 03:14
UTC). The run has 20 agents, 824 windows of about 24,000 tokens and 17.40M item tokens.
Settings: 2 neighbor items, 350K package cap, 5% random sample, verifier context 3, verifier
batch 12, prompt caching on. The Haiku prefilter costs $2.63 central, $3.29 high and $3.61
stress (824 requests, none at the 100K-token tier; 19.89M input tokens and about 1.27M
output tokens assumed).

| Haiku selection | Sampled windows | Split agents | Sonnet judge | Opus verify | Sonnet verify | Run level | Total | Cached | High | Stress |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 5% | 28 | 2 | $13.34 | $9.12 | $3.40 | $1.01 | **$29.49** | $28.35 | $36.31 | $40.84 |
| 10% | 27 | 5 | $20.21 | $9.08 | $3.55 | $1.01 | **$36.47** | $35.18 | $45.10 | $50.39 |
| 20% | 11 | 13 | $33.20 | $8.66 | $3.22 | $1.01 | **$48.71** | $47.07 | $60.26 | $67.29 |
| 30% | 10 | 13 | $39.68 | $8.88 | $3.43 | $1.01 | **$55.62** | $53.89 | $69.10 | $76.69 |
| keyword proxy | 2 | 15 | $55.42 | $10.29 | $3.42 | $1.01 | **$72.76** | $70.69 | $90.81 | $100.11 |

Totals include the Haiku prefilter. At 5% selection the central and high projections fit
the $40 analysis cap; at 10% only the central one does. Judge cost grows with Haiku's
selection fraction (the packages hold the selected items with 2 neighbors each, plus about
719K tokens of communications, submissions and exposure that every package carries). The
verifier is dominated by its full passages (3 items either side of each cited item;
DeepSeek reasoning items reach about 12K tokens): about 1.65M Opus and 1.07M Sonnet input
tokens. Prompt caching saves only about $1.1–2, because only the ~10K-token system prompt
is shared. The largest single reservations are $0.12 (Haiku), $2.98 (Sonnet judge) and
$3.28 (Opus verifier); at full concurrency they would total $2.80, $23.84 and $19.68, so
near the cap admission waits rather than running every model at full concurrency.

The real Haiku selection fraction is unknown until the prefilter runs. Running
`--stage prefilter` (about $2.6–3.3) and then `--dry-run-estimate --output-dir <judge dir>`
adds an `actual` scenario built from Haiku's real selections and its real random sample.
Rerun the estimate after any settings change.
