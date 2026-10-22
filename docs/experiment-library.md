# Shared experiment library

The private library is https://huggingface.co/spaces/gdm-math-swarm/gdm-math-swarm-experiments.
The user authorized the initial private publication on October 15, 2026 and later
approved creating the `gdm-math-swarm` organization and transferring the library
there for collaboration. The organization uses the free plan. The transfer
preserved all 6204 files and revision `8e0bc9de448c8b878476c5ef2388c669209826c1`;
private visibility and authenticated SDK access were verified. The former
`jchang153/gdm-math-swarm-experiments` Hub URL redirects to this destination.
Use `gdm-math-swarm/gdm-math-swarm-experiments` as the publishing repository ID.

Organization members need Read access to browse and Write access to publish.
Write permits changes to every repository in this dedicated organization;
Admin is unnecessary for normal publishing. Each publisher uses their own HF
account and a fine-grained token with read/write permission for this Space.
Never share account passwords, provider keys, auth caches or local login tokens.
Pending collaborator invitations require the user's supplied account identities.

HF access covers the archive. Experiment execution separately requires GitHub
repository access, authorized GCP/IAP/SSH access and a configured host-only model
credential with an approved budget. A collaborator uses their own Google login;
never transfer GCP credentials. Follow `gcp-vm.md` and `validation.md`, preserve
the authoritative budget ledger and unresolved holds, and coordinate exclusive
use of the experiment VM before starting or stopping it. Membership alone does
not authorize a paid run. A collaborator decides their own budget for their own runs
and analyses and uses their own provider key, never the user's (docs/plan.md,
"Collaborator budgets and keys"); they record that decision in docs/plan.md before
any paid call, and the ledger enforces the cap they set.

The static app contains the experiment catalogue, per-run reports, the conference
view, native Inspect View (where agent transcripts are read), and original run records.
Runs published before October 21, 2026 also carry their custom HTML transcript pages;
the site no longer links them. Inspect View renders the saved
Inspect logs; it does not run or judge agents. The independent evaluator records
remain separate from conference acceptance. No VM, Docker, model key or inference
is required to browse. Local originals and host ledger backups are retained.

## Tools

All in `scripts/`; each is documented in its own `--help` and below.

- `build_results.py`: the facts-only run report (`results.json`, `results.html`).
- `prepare_hf_library.py` and `publish_hf_library.py`: prepare a bundle, then preview and `--commit` a
  first publication.
- `maintain_hf_run.py`: guarded updates of an already published run, `--scope judge` (default),
  `transcripts`, `evaluator`, `results`, `inspect`, `site` or `incident` (`--replace-judge` replaces an already
  published judge build; `site` republishes only regenerated page presentation, see "Site scope" below;
  `incident` publishes incident reports, see "Incident report outputs"; `prune` removes a run's large raw
  files to free storage, see "Storage limit and pruning"). `--scope unlist` (no `--bundle`) removes a run's catalogue row without deleting files,
  after the run is recorded in the tracked `data/library-unlisted.json`
  (run, date, reason). `build_experiment_index.py` and `publish_hf_library.py` leave recorded runs out
  of the local and hosted lists; `baseline100-20261015T191545Z` is unlisted (October 20, 2026).
  `--rerender-catalogue` (no `--maintain-run`, `--bundle` or `--scope`) rebuilds only `index.html`
  from the published `experiments.json` after a change to the catalogue page itself; the current
  generator must reproduce `experiments.json` byte for byte, apart from listing the same entries newest
  first (it reorders an older oldest-first catalogue), and no entry or run file changes.
- `monitor_run.py`: read-only live progress of a run on its VM (elapsed time, spending against the
  launch baseline, requests, cache, restarts, submissions and solved tasks).
- `live_mirror.py`: near-live mirror of running runs (Inspect View, conference view, live summary) to the
  Space's `live/` area and the catalogue's "Live now" list (owner choice "option 1", October 21, 2026);
  see "Near-live mirror" below.
- `verify_hosted.py`: read-only check, at a given Space revision, that a published run's pages are
  stored as regular files and served as HTML (not LFS downloads) with their exact stored bytes.
- `site_style.py`: the shared look of every generated page (catalogue, report, conference, live
  summary; also the local-only transcript export). Generators write colours as its `var(--...)` tokens and pass finished pages through
  `themed()`, which adds light/dark tokens following `prefers-color-scheme`, a corner Theme toggle
  (auto/light/dark, remembered per browser in `localStorage` only) and a phone layout (16px gutters,
  no page-wide horizontal scroll; wide tables scroll inside themselves). It loads nothing external
  and sends nothing. The behavior judge's script-free pages use the same token names and follow the
  system setting. Pages already published change look only when they are regenerated.

Any tool used to produce, publish or verify a run goes into `scripts/` (with a test), never into the
Git-ignored `runs/phase-b/` directories, which hold only that run's records and receipts.

## Preparing an archive

Install the optional publisher dependencies in your environment and authenticate
with **your own** HF account after accepting the organization invitation:

```bash
python -m pip install -e '.[publication]'
hf auth login
```

Use a fine-grained token with read/write access to
`gdm-math-swarm/gdm-math-swarm-experiments`. No model API key is needed to publish.
The export steps also require the pinned `runtime` dependencies. The script does
not create experiments, run proof evaluation, or invent a results report: first
complete the run and save its checked `results.json` and `results.html`, preserving
run-specific spending separately from the cumulative ledger and any uncertain holds.

Agent transcripts are read in Inspect View (owner, October 21, 2026). `scripts/export_transcripts.py`
still writes an offline `RUN/transcripts/` view for local use, but `prepare_hf_library.py` leaves that
folder out of every bundle and refuses a generated view that still links it (an older `results.html`
or conference view: regenerate it; archived pages such as `results.legacy.html` are copied as recorded). `--include-transcript-export` keeps the folder only to maintain the
transcript pages of a run published before then (`maintain_hf_run.py --scope transcripts`). Write the
report with `scripts/build_results.py`, which `prepare_hf_library.py` and `publish_hf_library.py` require
(they refuse a run without `results.json` and `results.html`). The publication steps are:

1. `python scripts/build_results.py --run-dir runs/RUN_ID [flags]` writes `results.json` and `results.html`
   in the library format (the layout of `gemini10-20261020T054602Z`).
2. `python3 scripts/prepare_hf_library.py --run "RUN_ID=VIEWER_BUNDLE" --output-dir NEW_BUNDLE` (below).
3. `python scripts/publish_hf_library.py --bundle NEW_BUNDLE` for the read-only preview, then `--commit`.

The report is a deterministic, facts-only review of the run's own records, with no model calls,
network or credentials: run timing and stop reason, the condition from `settings.json`, per-agent
requests, exits and restarts, spending and tokens, submissions per task and agent with times and
outcomes, and links to the existing record views (conference view with board, DMs, library
and private files, timeline, Inspect View with agent transcripts, and logs). It never summarizes those contents. The proof
checker section is filled only from `evaluations/` (verdicts and reasons as recorded, bound by
submission, task and source hash); otherwise the page states "Proof checker: not run" and accepted
submissions are `not_evaluated`. By user decision (October 20, 2026) behavior-judge findings and fields
stay out of `results.json`/`results.html`: they live only in the judge's own pages
(`analysis/judge/report.html` and the evidence browser, linked from the catalogue's "Behavior judge"
column). The report carries a single status line, "Behavior judge: see analysis/judge/report.html"
(`behavior_judge: {"status": "completed", "report": ...}`) or "Behavior judge: not run". Amounts are
written without exponent notation. Flags:
`--ledger LEDGER.sqlite` (a consistent ledger copy; exact run-only spending by this run's admitted request
IDs, otherwise derived from `runtime.jsonl` with uncertain holds still counted as held),
`--key-usage-log LOG.jsonl`, `--caveat TEXT` and `--caveat-for SUBJECT TEXT` (the only free text, stored
under `researcher_notes` and shown under "Researcher notes"), `--title`, `--stop-utc`/`--stop-reason` for a
run stopped without a `run_stop` event, and `--submission-ids serial` for older serial IDs. It refuses to
replace existing reports without `--overwrite`; use `--output-dir` for a scratch copy.

Run page layout (owner request, October 21, 2026): `results.html` opens with a summary card (model,
prompt variant, agents, duration, accepted and genuine tasks, cost, behavior-judge status only), then
tabs Report / Conference / Behavior judge / Inspect, each with a one-line explanation, and ends with
plain explanations of every status label it shows. Raw Inspect logs are not linked (site-nologs,
October 21, 2026: the link was only a download); the `.eval` files stay published because Inspect View
reads them. Pages are static, support
light and dark mode and fit phone width. Owner update (October 21, 2026): the site has no custom
transcript viewer, since agent transcripts are read in Inspect View; the run page has no Transcripts
tab or transcript links. `scripts/export_transcripts.py` stays for local, offline use only and its
pages are not published.

Create a native Inspect
bundle with the pinned Inspect installation, running outside the repository to
avoid automatic dotenv loading:

```bash
# From your own repository checkout; replace RUN_ID with the completed run ID.
REPO_DIR="$PWD"
RUN_ID=your-run-id
cd /tmp
"$REPO_DIR/.venv/bin/inspect" view bundle \
  --log-dir "$REPO_DIR/runs/$RUN_ID/inspect" \
  --output-dir "$REPO_DIR/runs/phase-b/inspect-$RUN_ID"
cd "$REPO_DIR"
```

Also run `python3 scripts/export_conference.py --run-dir runs/RUN_ID` to generate
`conference-view/index.html` and per-file captured-version pages before preparing
the upload bundle. This offline exporter reads the saved conference database,
cross-checks its events against JSONL, binds independent evaluations to exact
submission identity/source hashes and verifies library/snapshot hashes. Its header
links the run's Inspect View for agent transcripts; records carry no links to the
custom transcript pages. It uses no model calls or running containers.

The conference tabs show board posts, participant-grouped private DMs, shared
library revisions, submissions with conference and independent judgments,
organizer feedback, captured workspace working files/notes, and the saved activity
timeline. Search, agent/task/type filters and pagination operate locally in the
page. Empty sections are explicit. Board posts have no recorded reply relationship;
the viewer does not invent threads. Untimed events remain unknown. File captures
are observations rather than a complete edit history, and were not automatically
available to peers. SDK sessions are read in Inspect View;
task templates and build manifests are omitted from the private working-file list.

Conference exports compress their embedded JSON records with gzip/base64 and
decompress them locally in the browser. This preserves the records and keeps the
self-contained page below 8 MB; larger exports fail with an explicit request for
a split-data export. A browser with `DecompressionStream` support is required.
Large HTML files tracked through HF LFS can be served with
`Content-Disposition: attachment`, causing Conference to download instead of open.
Check inline HTML headers and the actual catalogue button after publishing;
HTTP 200 and matching file hashes alone do not establish browser behavior.

Hosted HTML also needs private-session continuity across page navigation.
`space_navigation.py` is applied to the hosted catalogue and every derived HTML
page during archive preparation. At browser runtime it carries the visitor's
existing temporary HF signed session only to the exact same Space origin, including
Inspect module imports, stylesheet resources and range fetches. It embeds no host
token, stores/logs no session and disables outgoing referrers. External destinations
never receive the session. Local exports are inert. Reopen the Hub's Space App page
to obtain a fresh session if it expires; do not share signed inner-page URLs.

From the repository, prepare a fresh, non-existing output directory:

```bash
python3 scripts/prepare_hf_library.py \
  --run "$RUN_ID=runs/phase-b/inspect-$RUN_ID" \
  --output-dir runs/phase-b/hf-library-preparation/NEW_BUNDLE
```

Repeat `--run` for multiple locally available completed experiments. The helper
copies only selected run folders and supplied viewer bundles, excludes hidden files,
refuses symlinks and obvious credential filenames, generates a hosted catalogue,
and records file sizes and SHA-256 digests. This is not a general secret scanner:
review new data sources before publishing. Never point it at a host home directory,
repository root, credential directory, or launch phase directory. All generated
records remain Git-ignored; commit only code and researcher documentation.

## Behavior judge outputs

The post-hoc transcript judge (docs/plan.md, October 17, 2026; since the evening decision the
misbehavior-focused judge, rubric `gdm-judge-rubric/2` for the published cache100 outputs and,
from judge revision v3 of October 19, 2026, rubric `gdm-judge-rubric/3`, amended on October 20, 2026 to
`gdm-judge-rubric/3.1` with plain-language explanations) writes researcher-only
records under `RUN/analysis/judge/`. There is no human marking step: the judge's adversarially
verified labels are the reported labels (docs/plan.md, superseding decision "no human marks"). The
pipeline's report stage builds both outputs; to rebuild them from saved records without any
model call, run `python -m research.analysis.judge.review build --run-dir RUN` and then
`python -m research.analysis.judge.report --run-dir RUN` (all `PYTHONPATH=src:.`).
`report.html` is the five-minute read: a short run summary with cohort counts beside the
paper projection, the run-level spread and whistleblowing-reach summary, one row per agent
(cohort, flags, one-sentence note), short evidence cards only for agents with a verified
finding, the statement-defect footnote, the unresolved and ungraded list, spend by stage and
model, and the rubric hash with the post-hoc disclosure. `review/` is the read-only evidence
browser behind it: `review/index.html` is a compact index of every agent's cohort, flags and
category levels plus the full run-level record; `review/agents/<agent>.html` exists only for
agents with at least one judge finding (verified, refuted or unverified, each labelled) and
shows the verifier's verdict and model and the full evidence passages. It has no form
controls, scripts or browser storage. `review/manifest.json` (schema
`gdm-judge-review-manifest/3`) records the SHA-256 of every input and page. The judge's report
module can add a marked section to `results.html` and a `behavior_judge` key to `results.json`; reports
regenerated with `scripts/build_results.py` replace those with a single status and link line (user
decision, October 20, 2026), and the index builder links `analysis/judge/report.html`.

Rule applied by `prepare_hf_library.py`: judge outputs are published only from a `paid`
run whose evidence browser and report still match the judge outputs. It refuses
`rehearsal`/unknown-mode outputs, outputs of the superseded 12-dimension judge (a
`report.json` schema other than `gdm-judge-report/3` or a browser manifest other than
`gdm-judge-review-manifest/3`), a missing evidence browser or report, a judge input or
browser page whose SHA-256 differs from `review/manifest.json`, a judge output added after
the browser was built, an agent page the current build did not produce (remove pages left by
an earlier build; nothing is deleted automatically), a `report.json` not rebuilt after the
current browser, and a `results.html` judge section without its judge directory. Within
`analysis/judge/` only an allowlist is copied: `report.html`, `report.json`, `labels.jsonl`,
`pipeline.json`, `run_level.json`, `review/index.html`, `review/manifest.json`,
`review/agents/`, `agents/` and `packages/`. Everything else stays local and is listed in `archive-manifest.json` under
`behavior_judge`, notably `raw/` provider request/response archives, `units/`, any ledger
copy and any other file under `review/` (for example marks files left by the withdrawn
spot-check design). The library already publishes each run's complete raw records (Inspect
logs with returned reasoning, private-file snapshots, conference database), so judge input
packages add no new transcript exposure; their host-only evaluator and statement-defect
context is researcher material for this private Space. Raw provider archives duplicate the
packages and add provider request metadata, which the library does not publish. Credentials
and auth caches are never included.

Adding judge outputs to an already published run changes its `results.html`,
`results.json` and catalogue entry. `publish_hf_library.py` refuses changes to existing
run files by design, so that needs a separately reviewed maintenance update like the
October 16 repairs, after a user decision. For `cache100-20261016T020259Z` the user
decided on such a narrow update (docs/plan.md, October 17, 2026): it replaces only that
run's `results.html`, `results.json` and catalogue entry and adds the judge files, and it
no longer waits for any spot-check. Other runs need their own decision.

`scripts/maintain_hf_run.py --maintain-run RUN_ID` performs that update (`--scope judge`, the
default; `--scope transcripts` below republishes regenerated transcript exports). Prepare a fresh
bundle of only that run with the Inspect View bundle used for the original publication,
then run the read-only dry run and review its plan before committing:

```bash
RUN_ID=cache100-20261016T020259Z
BUNDLE=runs/phase-b/hf-library-preparation/judge-$RUN_ID
OUT=runs/phase-b/judge-publication-$RUN_ID
python3 scripts/prepare_hf_library.py --run "$RUN_ID=runs/phase-b/$RUN_ID/inspect-view" \
  --output-dir "$BUNDLE"
python scripts/maintain_hf_run.py --maintain-run "$RUN_ID" --bundle "$BUNDLE"   # plan only
python scripts/maintain_hf_run.py --maintain-run "$RUN_ID" --bundle "$BUNDLE" --commit \
  --backup-dir "$OUT/attempt-1" --receipt "$OUT/attempt-1/maintenance-receipt.json"
# Only after a failed or interrupted attempt (always resume from attempt-1):
python scripts/maintain_hf_run.py --maintain-run "$RUN_ID" --bundle "$BUNDLE" --commit \
  --resume-from "$OUT/attempt-1" --backup-dir "$OUT/attempt-2" \
  --receipt "$OUT/attempt-2/maintenance-receipt.json"
```

The plan is pinned to the current Space revision. `replaced` (only `RUN/results.html` and
`RUN/results.json`) and `added` (the allowlisted bundle files under `RUN/analysis/judge/`)
are relative to the original pre-maintenance revision `origin_revision`; `to_replace`,
`to_add` and `catalogue.files` are what this attempt still uploads (identical copies are
skipped). It also lists the catalogue entry diff and the `archive-manifest.json` digests that
change (only digests and `total_bytes`; the list of locally excluded judge records stays in
the bundle's own manifest). Every other local difference, for example transcripts
regenerated by a newer exporter, appears under `skipped_local_differences` and is never
uploaded by this scope; other remote run files missing from the bundle are preserved. It refuses a bundle
with more or other runs, with judge files outside the allowlist or not matching its
preparation record (`behavior_judge.included`), a public Space, a run that is not already
published or listed in the remote catalogue and manifest, a missing remote report or
catalogue file, a download that does not match its listed metadata, catalogue or manifest
warnings, a remote catalogue that the current generator does not reproduce byte for byte
(regenerating it would alter another entry), a catalogue link to a file that would not exist
remotely (for example a skipped bundle-only file), an existing remote judge file that
differs, and any remote file under `RUN/analysis/judge/` that the bundle lacks (for example
output of another judge build). It lists those files for the operator to decide and never
deletes them.

`--commit` requires a new or empty backup directory and a new receipt path, both outside the
bundle (and outside any resumed attempt's directory). The receipt may not be the backup
directory itself, its `before-inventory.json` or `plan.json`, or a path under its `original/`.
It writes the receipt first, so an unwritable receipt path fails before any upload, then
`before-inventory.json` (remote path to blob id, LFS SHA-256 and size at this attempt's base
revision, with the original revision), `original/` copies of the replaced and catalogue files
(`results.html`, `results.json`, `experiments.json`, `index.html`, `archive-manifest.json`,
`.gitattributes`) and `plan.json`. It uploads judge files first, then the two reports, and the
regenerated catalogue and manifest last, in guarded batches that refuse a revision change or
loss of privacy, and issues no deletions. Verification compares the final state with the
original pre-maintenance inventory: it must be the original inventory plus the added judge
files, every other file must keep its original blob id, every planned file must have exactly
its planned content, and `.gitattributes` may gain only exact LFS rules for added judge
files (not for the replaced reports). The receipt records the original, base and final
revisions, the cumulative replaced and added lists, the skipped list and the verification
counts; `committed` says whether this attempt itself made a commit. A failure is recorded as
`status: failed: ...`; if the receipt cannot be written then, the original error is still
raised.

A commit without `--resume-from` refuses a Space where the update is already partly applied
(a judge file or the bundle's catalogue entry already published), because that attempt's
starting revision is not the original state. After a failed or interrupted attempt, rerun the
dry run and commit with a new `attempt-N` directory and `--resume-from` set to the first
attempt's directory. Its before-inventory and `original/` copies describe the pre-maintenance
state; they are checked against each other, and a resumed attempt's own records are refused
as a resume source. Before planning, the resume refuses any difference of the current Space
from that original state beyond the maintenance targets (an unrelated file change, deletion
or addition, a broader LFS rule, another catalogue entry or manifest record), so earlier
commits are verified rather than accepted as the new starting point. Identical uploaded files
are skipped, and the final verification is again against the original revision. Check the
hosted report and judge links afterwards.

To replace a run's already published judge outputs with another judge build (for example a
re-run under a newer rubric), after a user decision, add `--replace-judge` to the judge-scope dry
run and commit. Keep the earlier build locally under another name inside the run (for example
`analysis/judge-v3-first-run/`; `prepare_hf_library.py` publishes only `analysis/judge/`, and the
renamed directory appears among the skipped local differences, never uploaded) and put the new
build in `analysis/judge/`. Published judge files that differ from the bundle are then replaced
(listed in `replaced`, their originals saved under `original/`), new ones are added, identical ones
kept (`judge_already_present`). A published judge file that the bundle lacks is still refused and
nothing is deleted. Before any write it refuses a replacement at or above 9.5 MiB that no LFS rule
already names (HF would newly store it as LFS). The manifest is uploaded last, so a published
target whose bytes differ from the published manifest's record shows an interrupted attempt
(`already_applied`, `resume_required`). Resume as above, with `--replace-judge` again, since records
written in the other mode are refused. The mode refuses a run with no published judge files.

`--scope transcripts` republishes regenerated transcript exports of an already published run,
for example the full-history exports regenerated on October 18; use it only after a user
decision recorded in docs/plan.md. It replaces only existing `RUN/transcripts/` files that
differ from the bundle and changes nothing else. The bundle's transcript file set must equal
the published one at the base revision: an extra or a missing local page is refused with the
differing names, and nothing is added or deleted. The bundle's catalogue entry must equal the
published entry (otherwise it is refused with the entry diff); `index.html` and
`experiments.json` are neither regenerated nor uploaded and stay byte for byte.
`archive-manifest.json` is the only root upload: new digests for exactly the replaced
transcripts and a recomputed `total_bytes`, everything else unchanged (a replaced transcript
without a manifest record is refused). Reports, judge files and every other local difference
appear under `skipped_local_differences` and are never uploaded. Prepare a fresh bundle of only
that run as above, then dry run, review and commit each run separately:

```bash
RUN_ID=pilot-20261015T170231Z   # likewise baseline100-, baseline8g100- and cache100- runs
BUNDLE=runs/phase-b/hf-library-preparation/transcripts-$RUN_ID
OUT=runs/phase-b/transcripts-publication-$RUN_ID
VIEWER=...   # the Inspect View bundle used for this run's original publication
python3 scripts/prepare_hf_library.py --run "$RUN_ID=$VIEWER" --output-dir "$BUNDLE"
python scripts/maintain_hf_run.py --scope transcripts --maintain-run "$RUN_ID" --bundle "$BUNDLE"  # plan only
python scripts/maintain_hf_run.py --scope transcripts --maintain-run "$RUN_ID" --bundle "$BUNDLE" --commit \
  --backup-dir "$OUT/attempt-1" --receipt "$OUT/attempt-1/maintenance-receipt.json"
# Only after a failed or interrupted attempt (always resume from attempt-1):
python scripts/maintain_hf_run.py --scope transcripts --maintain-run "$RUN_ID" --bundle "$BUNDLE" --commit \
  --resume-from "$OUT/attempt-1" --backup-dir "$OUT/attempt-2" \
  --receipt "$OUT/attempt-2/maintenance-receipt.json"
```

The plan shows `scope: transcripts`, `replaced` with `replaced_sizes` (old and new bytes,
relative to the original revision), `to_replace` (what this attempt still uploads),
`replacement_identical` (pages already equal to the bundle, for example ones the exporter left
unchanged), `transcripts` (file count, `file_sets_equal: true`, `largest_new_bytes` and
`lfs_safe_limit_bytes`), `catalogue_unchanged: true` with an empty `catalogue.files` and entry
diff, and the `archive_manifest` digest changes and whether it is uploaded.

This scope never stores a transcript as LFS (HF serves LFS-tracked HTML as a download). HF stores
uploads of about 10 MB or more as LFS, so both the dry run and `--commit` refuse, before any
write, a transcript to upload at or above `lfs_safe_limit_bytes` (9.5 MiB, 9,961,472 bytes),
naming each page and its size. They also refuse any of the run's transcripts that is stored as
LFS at the base or in the original state, or that an LFS rule in `.gitattributes` at the base
matches (a pattern without a slash matches the basename at any depth). Such a state, for example
one left by an earlier failed attempt, can continue neither afresh nor with `--resume-from`; it
needs a manual repair like the October 16 conference fix below.

`--commit` uses the same output-path rules, receipt handling, guarded batches and privacy and
revision checks as the judge scope. Before uploading it writes `before-inventory.json` (with
`scope: transcripts`), `original/` copies of every transcript it replaces, of
`archive-manifest.json` and of `.gitattributes`, and `plan.json`; it uploads the transcripts and
the manifest last. Verification against the original revision requires the same file inventory
(nothing added or removed), the original blob id for every other file including the catalogue,
no transcript stored as LFS (whatever the original revision held), an unchanged `.gitattributes`
(any new LFS rule fails with a clear message) and exactly the planned bytes for every transcript
and the manifest; the receipt's verification records `gitattributes: unchanged` and
`catalogue: unchanged`. After each transcript batch the new revision is checked the same way
(without the byte comparison), so if HF stores a transcript as LFS, adds a rule or anything else
changes, the attempt stops before uploading the manifest and the Space stays detectably partly
applied.

Because the manifest is uploaded last, a published transcript whose bytes differ from the
published manifest's record for it shows an earlier, interrupted attempt, whatever the current
bundle holds (for example after the exporter was rerun and the bundle re-prepared). The dry run
therefore downloads every published transcript that differs from the bundle and compares its
digest with the manifest. It lists each such page under `already_applied` with
`resume_required: true`, and a commit without `--resume-from` is refused with that reason. A page
the exporter left unchanged keeps its recorded digest and is no such sign. The resume works as for the judge scope, against the first
attempt's original state; its records must belong to the same scope (records written before
scopes existed are judge records). When every transcript already equals the bundle and the
manifest records it, the dry run reports `uploads: 0` and `--commit` without `--resume-from`
makes no commit and refuses with `Nothing to upload`, writing no records; with `--resume-from`
set to the first attempt's directory it verifies the result against the original state without
committing (`status: verified`, `committed: false`).

`--scope evaluator` publishes independent-evaluator results of an already published run that was
published before it was evaluated; use it only after a user decision for that run. It replaces the
run's `results.html` and `results.json`, adds the bundle's `RUN/evaluations/` records that are not
yet published and replaces published ones that differ, and replaces the existing
`RUN/conference-view/` pages that the rebuilt conference view changed. The bundle's conference file
set must equal the published one (an extra or missing page is refused; nothing is added or deleted
there), and a published evaluator record the bundle lacks is refused for review. The catalogue is
regenerated as in the judge scope (the remote catalogue must be reproduced byte for byte and only
this run's entry may change, typically `distinct_verified_tasks`), and `archive-manifest.json`
changes only the digests of those files and `total_bytes`. As in the transcripts scope, no planned
file may be stored as LFS or matched by an LFS rule, no run upload may reach 9.5 MiB, and
`.gitattributes` must stay unchanged; the check also runs after every batch, before the catalogue
and manifest batch. A published target whose bytes differ from the published manifest's record
shows an earlier, interrupted attempt (`already_applied`, `resume_required`); resume as for the other
scopes. Everything else (transcripts, raw records) is reported under `skipped_local_differences`
and never uploaded. Regenerate the reports and the conference view first
(`scripts/export_conference.py --run-dir runs/RUN`), then:

```bash
RUN_ID=gemini10-20261020T054602Z
BUNDLE=runs/phase-b/hf-library-preparation/evaluator-$RUN_ID
OUT=runs/phase-b/evaluator-publication-$RUN_ID
python3 scripts/prepare_hf_library.py --run "$RUN_ID=runs/phase-b/inspect-$RUN_ID" --output-dir "$BUNDLE"
python scripts/maintain_hf_run.py --scope evaluator --maintain-run "$RUN_ID" --bundle "$BUNDLE"  # plan only
python scripts/maintain_hf_run.py --scope evaluator --maintain-run "$RUN_ID" --bundle "$BUNDLE" --commit \
  --backup-dir "$OUT/attempt-1" --receipt "$OUT/attempt-1/maintenance-receipt.json"
```

The plan's `evaluator` block gives the evaluator record count, how many are added, the conference
page count with `file_sets_equal: true` and how many pages are replaced, and the largest upload
against `lfs_safe_limit_bytes`. Backups under `original/` hold both reports, every replaced page or
record, the catalogue, the manifest and `.gitattributes`.

`--scope results` replaces only a published run's `results.html` and `results.json`, for example
reports regenerated with `scripts/build_results.py --overwrite`, and regenerates the catalogue
(only this run's entry may change; review `catalogue.entry_diff` in the plan, which should be empty
when only the report layout changes). It is the evaluator scope without evaluator records or
conference pages: no LFS, `.gitattributes` unchanged, guarded batches with the catalogue and
manifest last, resume and verification as above; every other local difference is skipped. Keep the
previous reports locally (for example as `results.legacy.json`/`results.legacy.html`; they are not
published) and prepare a single-run bundle as for the other scopes, then run
`python scripts/maintain_hf_run.py --scope results --maintain-run "$RUN_ID" --bundle "$BUNDLE"` (plan)
and the same with `--commit --backup-dir "$OUT/attempt-1" --receipt "$OUT/attempt-1/maintenance-receipt.json"`.

### Site scope

`--scope site` rolls a new site look out to an already published run (owner request, October 21, 2026: run
page summary card and tabs, shared light/dark theme, phone layout, no custom transcript links). It replaces
only generated views that differ from the bundle: `RUN/results.html`, the existing `RUN/conference-view/`
pages (with its `manifest.json`) and the existing HTML pages of the published behavior-judge build. It never
changes data or adds or deletes files: `RUN/results.json` and every non-HTML judge file must equal the
published ones, the conference and judge page sets must equal the published ones, every replaced page must
carry the shared site theme, and the catalogue entry stays the published one (the catalogue page reads newer
fields from the run's own records). Raw records, Inspect logs, Inspect View, evaluator records and the
transcript pages of runs published before October 21 are untouched (the catalogue and run pages no longer
link those transcript pages). Otherwise it follows the results scope: no LFS, `.gitattributes` unchanged,
guarded batches with the manifest last, backups under `original/`, receipt, resume and verification.

Rollout (all listed runs; nothing in `runs/RUN/` except the derived `results.html` and `conference-view/`
changes, and the previous versions are first copied to a backup folder):

```bash
OUT=runs/phase-b/site-publication-YYYYMMDD
BUNDLE=runs/phase-b/hf-library-preparation/site-$RUN_ID
VIEWER=...   # the Inspect View bundle used for this run's publication
# 1. Re-render the catalogue page (entries byte for byte), plan first.
python scripts/maintain_hf_run.py --rerender-catalogue
python scripts/maintain_hf_run.py --rerender-catalogue --commit --backup-dir "$OUT/catalogue" \
  --receipt "$OUT/catalogue/receipt.json"
# 2. Per run: re-render the report page from its existing results.json (no data flags needed),
#    rebuild the conference view, prepare a single-run bundle with the viewer of its publication.
python scripts/build_results.py --run-dir "runs/$RUN_ID" --rerender-html --overwrite
python scripts/export_conference.py --run-dir "runs/$RUN_ID"
python3 scripts/prepare_hf_library.py --run "$RUN_ID=$VIEWER" --output-dir "$BUNDLE"
python scripts/maintain_hf_run.py --scope site --maintain-run "$RUN_ID" --bundle "$BUNDLE"   # plan only
python scripts/maintain_hf_run.py --scope site --maintain-run "$RUN_ID" --bundle "$BUNDLE" --commit \
  --backup-dir "$OUT/$RUN_ID/attempt-1" --receipt "$OUT/$RUN_ID/attempt-1/maintenance-receipt.json"
# 3. Check the hosted pages at the final revision.
python scripts/verify_hosted.py --run "$RUN_ID" --revision "$REVISION" --bundle "$BUNDLE"
```

The plan's `evaluator` block adds `judge_pages` (count, `file_sets_equal`, replaced) and
`results_json_unchanged: true`; `catalogue.files` is only `archive-manifest.json` and `entry_diff` is empty.

Hosted Inspect links (October 20, 2026). HF stores each run's `.eval` logs (25 to 60 MB) as LFS/Xet
and serves them through a same-origin redirect to its CDN (which allows CORS and byte ranges). Inspect
View fetches logs with `redirect: "error"`, so the hosted viewer showed "Failed to fetch". The Space
navigation added to Inspect View pages now follows redirects of same-origin GET/HEAD requests only;
credentials stay same-origin, no referrer is sent and the session is never added to the CDN URL.
Static hosting also cannot list a directory (`RUN/inspect/` redirected off-site), so the catalogue's
"Inspect logs" link names the `.eval` file directly (numbered links for several logs). A 206 range
check with a redirect-following HTTP client does not detect this; open the hosted Inspect View.
`--scope inspect` repairs a published run: it replaces only `RUN/inspect-view/index.html` (the bundle's
page must contain the redirect fix and every other Inspect View file must equal the published one) and
changes only the Inspect log link(s) of the published catalogue entry, never the bundle's own entry;
otherwise it follows the results scope (no LFS, `.gitattributes` unchanged, catalogue and manifest last,
resume and verification). Prepare each run's bundle with the viewer of its original publication and run
it per run, plan first, as for the other scopes. Published `results.html` pages still link `inspect/`
until their reports are regenerated and republished.

No raw log links (site-nologs, October 21, 2026). The "Inspect logs" / "Inspect log N" links were only
downloads, so no page shows them any more: the catalogue rows and compare view hide them
(`catalogue_page.hidden_link`), and `results.html` no longer links `RUN/inspect/*.eval` from its tabs or
Records line. Catalogue entries keep the links as data, so published entries survive regeneration
unchanged; every `.eval` file and all of Inspect View stay published. To update live pages, re-render the
catalogue with `maintain_hf_run.py --rerender-catalogue --commit` (index.html only, which also carries the
compare view), then regenerate each run's `results.html` and replace it per run with the site scope (or
`--scope results`). `verify_hosted.py` fails a served page that still links a raw `RUN/inspect/*.eval`.

## Incident report outputs

The model-written incident report (docs/plan.md, "Incident report per run (October 21, 2026)") writes
`RUN/analysis/incident/`. Only `report.html`, `report.md` and `evidence.json` are published
(`scripts/incident_site.py`); `raw/`, `units/`, `route-pricing/`, `digest.json`, `run-budgets.json`,
`state.json` and lock files stay local. `prepare_hf_library.py` copies only those three files, screens
them for credential-like content, refuses a report page that links the local-only files, records them
under `incident_report` in `archive-manifest.json` (with the count of excluded local files) and adds an
"Incident report" tab (after Behavior judge, before Inspect) to the bundle's `results.html`; a run page
without the tab bar is refused (regenerate it with `build_results.py`, which adds the tab itself when the
report exists). `publish_hf_library.py` refuses any other file under `RUN/analysis/incident/`. The
local and hosted catalogues link "Incident report" (before the raw Inspect log links). The report's own
page keeps its light/dark styles and gains the site theme toggle; nothing in the facts of `results.html`
changes.

For already published runs, `maintain_hf_run.py --scope incident` adds or replaces exactly those three
files, adds the tab to the **published** `results.html` (never the bundle's, which may have drifted) and
the link to the **published** catalogue entry (only that entry changes; the current generator must
reproduce the remote catalogue byte for byte), and updates the manifest digests of exactly those files,
in one guarded commit pinned to the planned revision, verified afterwards. A published incident file the
bundle lacks is refused (nothing is deleted); no planned file may be or become LFS. After a failure, run
it again; it continues from the current state (no `--resume-from`). Per run:

```bash
RUN_ID=owner-opus55-20261021T183459Z
BUNDLE=runs/phase-b/hf-library-preparation/incident-$RUN_ID
OUT=runs/phase-b/incident-publication-$RUN_ID
python3 scripts/prepare_hf_library.py --run "$RUN_ID=runs/phase-b/inspect-$RUN_ID" --output-dir "$BUNDLE"
python scripts/maintain_hf_run.py --maintain-run "$RUN_ID" --scope incident --bundle "$BUNDLE"   # plan only
python scripts/maintain_hf_run.py --maintain-run "$RUN_ID" --scope incident --bundle "$BUNDLE" --commit \
  --backup-dir "$OUT/attempt-1" --receipt "$OUT/attempt-1/maintenance-receipt.json"
```

## Snapshot archives (HF file limit)

HF limits a repository to 20,000 files, and the Space reached it (19,937 files at revision
`8c79494`), mostly runtime file snapshots (`RUN/snapshots/AGENT/SHA256`, thousands of small
content-addressed blobs per run). The owner decided on October 21, 2026: "just go ahead and merge
files". `prepare_hf_library.py` therefore publishes each run's `snapshots/` folder as one
deterministic `RUN/snapshots.tar.gz` (sorted members, zero times and owners, fixed modes, no gzip
name or time: preparing it again gives the same bytes) plus `RUN/snapshots.manifest.json`
(schema `gdm-snapshot-archive/1`: the archive's size and SHA-256 and every member's path, size and
SHA-256). No page loads snapshot files at runtime: the conference view embeds each captured file in
its own pages when it is exported, and Inspect View reads only its logs. Preparation refuses a page
that links or fetches a loose `snapshots/` path (link the conference view's captured-file pages
instead), and publication refuses a bundle with loose snapshot files, checks that each manifest
describes its archive exactly and screens every archive member for credential patterns. The archive
matches the Space's existing `*.tar.*` LFS rule, so it is stored as LFS (a download) with
`.gitattributes` unchanged.

`maintain_hf_run.py --maintain-run RUN_ID --scope snapshots` (no `--bundle`; `--runs-dir`, default
`runs/`) converts an already-published run. It builds the same archive from the local records and,
before any write, requires every published loose snapshot file to be an archive member with identical
bytes (Git blob or LFS hash) and, for a run recorded in `archive-manifest.json`, the recorded snapshot
digests to equal the archive manifest exactly; a partly published run (not yet recorded) may hold a
subset. With `--commit --receipt FILE --backup-dir DIR` it writes the before-inventory, the original
`archive-manifest.json` and `.gitattributes`, the archive and its manifest (which hold every deleted
file's bytes), `deleted.json` and the plan, then commits, each pinned to the expected parent revision:
the two archive files (verified before anything is deleted), the deletions in batches of 1,000, and
last, for a recorded run, `archive-manifest.json` with the loose records replaced by the two archive
files. The catalogue and every other file stay unchanged; the final state is verified against the base
inventory. An interrupted update continues by running the scope again (the remaining loose files are
verified the same way); a completed one reports `already_applied`.

```bash
OUT=runs/phase-b/hf-library-preparation/snapshot-archives-YYYYMMDD
python scripts/maintain_hf_run.py --maintain-run RUN_ID --scope snapshots          # plan only
python scripts/maintain_hf_run.py --maintain-run RUN_ID --scope snapshots --commit \
  --receipt "$OUT/RUN_ID.receipt.json" --backup-dir "$OUT/RUN_ID"
```

## Storage limit and pruning

The organization's free plan caps the Space at 1 GB of storage, and HF counts LFS objects across the whole
history. On October 22, 2026 the Space reached it (`used_storage` 981.9 MB at revision `6d3350b`; 1,110.9 MB of
LFS files at the head, duplicates counted once by HF), and publishing glm100-repeat2/3 failed with "403 Storage
limit reached". The owner decided: "Let's start by first just pruning old runs": only the old DeepSeek runs
(pilot, baseline100, baseline8g100, cache100), keeping every listed run with its page, results, judge and
report, removing the least useful material first.

`maintain_hf_run.py --maintain-run RUN_ID --scope prune` (no `--bundle`; `--runs-dir`, default `runs/`) removes
a published run's LFS-stored raw files: by default its `.jsonl` records and `snapshots.tar.gz`
(`--prune-kind jsonl|snapshots|eval`, repeatable; `eval` also removes the Inspect logs, after which Inspect View
cannot load that run). Before any write each file must match the local run records byte for byte (the snapshot
archive is rebuilt deterministically from `runs/RUN/snapshots/`); the local copies are only read. With
`--commit --receipt FILE --backup-dir DIR` it writes the before-inventory, the original `results.html`,
`archive-manifest.json` and `.gitattributes`, `deleted.json` and the plan, then makes one commit pinned to the
planned revision that deletes the files, replaces `RUN/results.html` with the published page plus a "Raw records
kept offline" notice listing them, and moves their records in `archive-manifest.json` from `files` to
`offline_files` (bytes, SHA-256, removal date, description). The result is verified (only those files changed;
catalogue and `.gitattributes` unchanged). Deleting at the head frees nothing by itself, so it then permanently
deletes the run's offline LFS objects that no file of the new head references (`permanently_delete_lfs_files`,
`rewrite_history=False`; `--no-purge` keeps them). Revisions keep their IDs, but earlier revisions can no longer
serve those files. Rerunning continues from the current state and re-adds the notice from `offline_files`, so run
it again after any later re-render of such a run's `results.html` (for example `--scope site`).

October 22, 2026 (records: `runs/phase-b/hf-library-preparation/prune-20261022/`, including the pre-prune head
`6d3350b` and its full file listing with sizes): removed 292.7 MB, namely cache100 171.8 MB (`analysis/timeline.jsonl`
82.2, `snapshots.tar.gz` 79.0, `runtime.jsonl` 10.5), baseline8g100 78.0 MB (`timeline.jsonl` 40.6,
`snapshots.tar.gz` 37.4), baseline100 36.7 MB (`timeline.jsonl`) and pilot 6.3 MB (`snapshots.tar.gz`); by
type, `.jsonl` 170.0 MB and `.tar.gz` 122.7 MB (exact bytes in the receipts). Their `.eval` logs, conference
databases and every page stay published. `used_storage` fell from 981.9 MB to 689.1 MB, so no history squash was
needed and every revision ID cited in this document still resolves. glm100-repeat2 and glm100-repeat3 were then
published (head `0a4ba2fc`, `used_storage` 918.7 MB). The next run of about 120 MB will not fit: prune further
(for example the old runs' `.eval` logs, 117 MB, with `--prune-kind eval`) or, as a last resort the owner accepted
on October 22, 2026, squash the Space history (`HfApi.super_squash_history`), which makes every earlier revision
ID cited here stop resolving; write a receipt of the head and its full listing first and record it here.

The behavior judge of glm100-repeat2 (published without it) was then added with `--scope judge` from a fresh
single-run bundle (`prepare_hf_library.py --run RUN=<the original bundle's inspect-view>`), from `0a4ba2fc` to
`831c0f59576f8ceb199e4ce35fecd9f37bbc782e`: 240 allowlisted judge files (38,175,746 bytes, all regular Git files,
none LFS), `results.html`/`results.json` and the catalogue replaced, 14,381 files preserved, receipt `verified`.
`used_storage` read 918.7 MB right after (at most about 957 MB once the added bytes count). `verify_hosted.py`
passed at that revision for the catalogue, run page, conference view, Inspect View, incident report, judge report,
evidence browser and a 206 range on the `.eval` (with `--bundle` only the merged catalogue `index.html` differs, as
expected). Records: `runs/phase-b/hf-library-preparation/judge-glm100-repeat2-20261022T055342Z-20261022*`.

## Logs and raw records in the dataset repo

Status (October 22, 2026): **code implemented; Inspect logs copied to the dataset and verified; the `logs-test/`
deployment and the move of the download-only files are pending (see below); deleting the Space's `.eval` logs
awaits the owner's separate approval.** Owner decisions of October 22, 2026: move the
Inspect `.eval` logs from the Space into the private dataset `HF-ORG/LOGS-DATASET`, read by the
static Space with the viewer's own "Sign in with HF" token (OAuth `read-repos`, no stored secret); then "go ahead
and move everything to dataset that's not necessary for current functionality of the website", with deletion
from the Space authorized for those download-only files only (not the `.eval` logs, which Inspect View still
reads from the Space until the owner confirms the sign-in test).

Resume bundle (owner, October 22, 2026): a run's `RUN/resume/` (docs/runtime.md, "Resume bundle": agent
session transcripts, workspace archives, the resume manifest) is the download-only dataset category `resume`.
It goes only to the dataset, at `RUN/resume/...`, and never to the Space. Each agent archive is screened member
by member (`prepare_hf_library.resume_archive_members`).

State on October 22, 2026 (records: `runs/phase-b/hf-library-preparation/logs-migration-20261022/`):

- **Space metadata**: `hf_oauth: true` and `hf_oauth_scopes: [read-repos]` were added to the Space README at
  `882dd1ff338cba6b5e7d3faad0f54bf0484d3d52` (guarded commit from `8982306`; before/after copies and receipt in
  the records). HF now injects `OAUTH_CLIENT_ID`, `OAUTH_SCOPES` and `OPENID_PROVIDER_URL` into served pages;
  the pages are otherwise unchanged. `/oauth/authorize` accepts any path on the Space host as `redirect_uri`.
- **Inspect logs copied and verified** (`--scope migrate-logs --commit`, receipt `eval-receipt.json`, status
  `verified`): the 34 `.eval` paths of the 17 published runs (two Space copies of each log) became 17 dataset
  files, 275,288,498 bytes, at dataset commit `b4f10b1082dbae32d81d6bd52b78f2f433450c99` (one commit per run).
  - Each matched its Space copy (LFS SHA-256 and size) and its dataset record.
  - 15 also matched the local `runs/RUN/inspect/` copy byte for byte. The local copies of cache100 and
    glm10-baseline-2h are iCloud placeholders that `brctl download` did not fetch; they were verified against
    the Space copy only.
  - The first and last 64 bytes of all 17 were read through resolve with HTTP 206.
  - A CORS preflight from the Space origin allows `authorization, range`. Anonymous resolve gives 401; with a
    bearer token it gives a 302 to `us.aws.cdn.hf.co`, which answers Range with 206,
    `Access-Control-Allow-Origin: *` and `Access-Control-Expose-Headers: *`.
  - The Space is unchanged; its `.eval` files stay until stage 4 is approved. The live run's log under
    `live/glm100-baseline-90m-20261022T183812Z/` was left alone.
- **Pending**: the copy and Space deletion of the download-only categories, and the `logs-test/` deployment.
  The agent's tool-permission check stopped those HF writes; they need the operator's go-ahead in their own
  session. At the Space head `882dd1ff` the download-only categories are:
  - `.jsonl`: 113 files, 380.5 MB (267.6 MB unique LFS);
  - snapshots: 1,856 files, 290.3 MB (166.1 MB LFS; the loose files are baseline100's);
  - databases: 50 files, 110.6 MB (110.0 MB LFS);
  - transcripts: 100 files, 205.9 MB (regular Git files, which HF's `used_storage` does not count).

  Deleting and purging them would free about 543.7 MB of the 920.0 MB `used_storage`. Deleting the `.eval` logs
  afterwards (stage 4) would free another 275.3 MB.

### What lives where

- **Dataset** (`scripts/dataset_logs.py`, `category()`): Inspect logs at `RUN/inspect/<name>.eval` (live runs:
  `live/RUN/inspect/<name>.eval`); every other download-only run file at its own `RUN/<path>`: raw `.jsonl`
  records, `snapshots.tar.gz`, `snapshots.manifest.json` and loose `snapshots/` files, `.db`/`.sqlite` databases
  (and `-shm`/`-wal`), and the old custom transcript pages `RUN/transcripts/`. Dataset files are immutable: a
  commit refuses a path that already holds other bytes.
- **Space**: every page and what pages load: the catalogue, run pages, `results.json`, conference views, judge
  report and evidence browser (with its JSON), incident reports, Inspect View (app, `logs/listing.json`), plus
  `signin.html` at the root once switched. `archive-manifest.json` records moved files under `dataset_files`
  (`{repo, path, commit, bytes, sha256, category, moved}`); files pruned earlier stay under `offline_files`.
- **Links**: a page link to a moved file points to the dataset file page
  (`huggingface.co/datasets/HF-ORG/LOGS-DATASET/blob/<commit>/RUN/<path>`, new tab) with the note
  "(in the private logs dataset)"; a link to a pruned file without a dataset copy points to the run page's
  "Raw records kept offline" notice. `verify_hosted.py` fails a page that still links a moved or absent file.

### Sign-in and the fetch shim

- `hf_site/evallog-fetch.js` is inlined first into each Inspect View `index.html` (`data-dataset`,
  `data-commit` pinned to the dataset commit of that run's logs, `data-path` `RUN/inspect`, `data-signin`). It
  rewrites same-origin GET/HEAD requests for `logs/<name>.eval` to
  `huggingface.co/datasets/<dataset>/resolve/<commit>/RUN/inspect/<name>` with `Authorization: Bearer <viewer's
  OAuth token>`, `redirect: 'follow'` and `credentials: 'omit'` (HF answers with a 302 to its CDN, which serves
  Range requests with 206). Without a token it returns 401 and shows a yellow "Sign in with HF" bar.
- `hf_site/signin.html` (no external libraries) runs PKCE against the `window.huggingface.variables` HF injects
  (`OAUTH_CLIENT_ID`, `OAUTH_SCOPES`, `OPENID_PROVIDER_URL`), asks for `openid profile read-repos` with
  `orgIds` of gdm-math-swarm, exchanges the code at `/oauth/token` (CORS allowed, public PKCE client) and stores
  `{accessToken, expiresAt}` in `localStorage` (`evallog-oauth`); then it returns to the page it came from.
- **It runs in its own tab, not in the Space frame.** HF's login page cannot be framed, and it sends
  `Cross-Origin-Opener-Policy: same-origin`, which severs a popup's opener, so the token cannot be posted back
  to the frame (the shim still accepts such a message if a browser keeps the opener), and Chrome partitions the
  frame's storage. The sign-in link carries the frame's `__sign`; that first top-level visit sets the static
  host's own `hf_jwt` cookie (24 h, `SameSite=Lax`), so HF's redirect back to `signin.html` and the following
  pages load in that tab without `__sign`. Read the logs in that tab.
- **Space metadata**: OAuth needs `hf_oauth: true` and `hf_oauth_scopes: [read-repos]` in the Space README
  (committed at `882dd1ff`, the only production change for OAuth; pages unchanged). HF then injects the OAuth
  variables into every served page's `window.huggingface` script, which `verify_hosted.py` strips before
  comparing bytes.
- **Deep link**: the catalogue opens `?page=PATH.html` or `#page=PATH.html` (same-site `.html` paths only),
  keeping the session, so `huggingface.co/spaces/gdm-math-swarm/gdm-math-swarm-experiments?page=logs-test/index.html`
  reaches a page the catalogue does not link.

### Commands

- New runs: `prepare_hf_library.py` writes dataset-bound files to `OUTPUT/dataset-files/` (with
  `manifest.json`), injects the shim and adds `signin.html`; `publish_hf_library.py --commit` commits the dataset
  first (verified per file), pins the dataset commit into the pages, then commits the Space and refuses any
  dataset category bound for the Space (`--records-in-space` only reproduces legacy bundles for maintenance).
- Live runs: `live_mirror.py` commits live logs to `live/RUN/inspect/` in the dataset, then the pages-only Space
  `live/` commit; superseded live logs are purged from the dataset (`repo_type="dataset"`).
- Existing runs: `maintain_hf_run.py --all-published-runs --scope migrate-logs [--migrate-category eval|jsonl|
  snapshots|database|transcripts ...] [--commit --receipt FILE --backup-dir DIR [--delete-moved]]`
  (`scripts/migrate_dataset.py`): plan (pinned Space head, every file downloaded, checked against its record,
  credential-screened and compared with `runs/RUN/` when readable; iCloud placeholders get `brctl download`,
  and after two that do not download the rest are left to the Space copy), copy (verified dataset commits, one
  per run; size and LFS SHA-256 or Git blob id per path; first and last 64 bytes of each log through resolve,
  206), and with `--delete-moved` (never `eval`) one Space commit that deletes the moved files, repoints links,
  moves their manifest records to `dataset_files`, is verified, and then purges the freed LFS objects without
  rewriting history. Other maintenance scopes refuse to re-add a file recorded in `dataset_files`.
- Test area: `python scripts/migrate_dataset.py deploy-test --run RUN --dataset-commit SHA --receipt FILE` adds
  only `logs-test/` (landing page, `signin.html`, `evallog-fetch.js`, the run's Inspect View without `.eval`).
- Verify: `verify_hosted.py --run RUN --revision SHA` (dataset checks run automatically when the run's Inspect
  View has a shim: `dataset_private`, `inspect_shim`, `range_206` per log via the dataset, `preflight`,
  `no_moved_links`, `no_broken_links`, `dataset_links_resolve`; `--expect-no-space-logs` after stage 4).

### Proposal record (October 22, 2026)

The owner asked to move the Inspect `.eval` logs out of the Space (at its 1 GB cap) into a private HF dataset,
with the site reading them from there. The feasibility tests below used throwaway private repos, all deleted
afterwards. They were created under the operator's user namespace because the operator's fine-grained token is
scoped to the Space and `create_repo` in the organization returned 403.

#### Findings

- **Direct fetch with the browser's HF session: not possible.** `huggingface.co/datasets/.../resolve/...`
  answers CORS preflights for any `Origin`, including the Space's `*.static.hf.space` origin:
  - it echoes the origin and the requested headers (`range`, `authorization`) and allows GET/HEAD
    (`max-age` 86400);
  - it exposes `Content-Range`, `Accept-Ranges`, `ETag` and `X-Linked-Size`;
  - it never sends `Access-Control-Allow-Credentials`, so a `credentials: 'include'` fetch fails in Chromium
    ("Failed to fetch");
  - the session cookie would be cross-site anyway, and an anonymous request gets 401.
- **Direct fetch with a bearer token: works.**
  - With `Authorization: Bearer`, resolve returns a 302 to a signed `us.aws.cdn.hf.co` URL
    (`Access-Control-Allow-Origin: *`, everything exposed), which answers Range with 206.
  - Tested in Chromium against a local endpoint that copies HF's CORS headers and returns HF's real 302 to the
    real CDN (the test browser had no HF login, and no token was put into a browser):
    - GET with Range gave 206 with a readable `Content-Range`;
    - HEAD gave 200 with `Content-Length`.
  - Inspect View 0.3.279 then loaded the glm-baseline log and opened a sample through a 20-line fetch shim.
  - Inspect View requests logs with HEAD and then Range GETs, passing `redirect: 'error'` and
    `credentials: 'same-origin'`. The shim must override them to `redirect: 'follow'` and `credentials: 'omit'`;
    otherwise HF's 302 fails.
- **OAuth on a private static Space: provisioned.**
  - With `hf_oauth: true` and `hf_oauth_scopes: [read-repos]`, HF injects
    `window.huggingface.variables = {OAUTH_CLIENT_ID, OAUTH_SCOPES: "openid profile read-repos", OPENID_PROVIDER_URL}`
    into served pages.
  - `/oauth/authorize` accepts the Space host as `redirect_uri` (302 to login) and rejects a foreign one (400).
  - A full sign-in was not exercised, because there was no owner login in the test browser.
  - Per HF docs, `read-repos` covers organization repos only if the user grants that organization at
    consent; request it with `orgIds=<org id>`.
- **Server proxy: works, but cannot be hosted on the free plan.**
  - The prototype is a stdlib Python server. It serves the static pages unchanged. It proxies
    `RUN/inspect/*.eval` and `RUN/inspect-view/logs/*.eval` to the dataset, passing Range and returning 206,
    and runs the HF OAuth code flow, keeping the viewer's token only in memory.
  - Locally it returned identical bytes (full-file SHA-256 equal), and Inspect View loaded and opened a sample.
  - Creating the Docker Space failed with "402: hosting Gradio and Docker Spaces on free cpu-basic requires a PRO
    subscription", so its cold start could not be measured.
  - Static Spaces are free and have no cold start: the test Space read RUNNING 0.1 s after upload.
- **Quotas.**
  - **Space:** a 1 GB repository cap, which HF staff say on the forum cannot be raised. `used_storage` is
    920,036,510 bytes.
  - **Inspect logs in the Space:** 276.7 MB of unique `.eval` content (18 LFS objects). Counting both the
    `inspect/` and `inspect-view/logs/` paths it is 552.0 MB, but the duplicates are stored once.
  - **Datasets:** no per-repository cap. They count against the account's private storage, which is 100 GB for a
    free user or organization, 1 TB for PRO and 1 TB per seat for Team (docs/hub/storage-limits). The dataset's
    `used_storage` equalled its file bytes.

#### Recommended design: static site, viewer's own OAuth token, fetch shim

No server, no secret and no cold start, on the free plan.

- **Dataset.** Private `HF-ORG/LOGS-DATASET`, holding one copy per log at `RUN/inspect/<name>.eval`
  (Inspect View's `logs/<name>` maps to it) and live logs at `live/RUN/inspect/<name>.eval`. `listing.json` and
  every page stay in the Space.
- **Sign-in.**
  - A small `signin.html` at the site root runs PKCE against `window.huggingface.variables`, written by us so the
    site still loads nothing external. It requests `openid profile read-repos` with the organization's
    `orgIds`.
  - It stores `{accessToken, expiresAt}` in `localStorage` (`evallog-oauth`); the default lifetime is 8 h
    (`hf_oauth_expiration_minutes`).
  - The catalogue and the run pages show "Sign in with HF" when no valid token is stored.
  - HF's login page cannot be framed, and Chrome partitions an iframe's storage. Sign-in therefore opens a popup
    that `postMessage`s the token back to the opener iframe. The alternative is to browse at the Space's direct
    URL.
  - This must be verified once with a real login.
- **Fetch shim.**
  - `evallog-fetch.js` at the site root is injected first into every `inspect-view/index.html`, next to the
    existing private-space-navigation script.
  - It carries the dataset id and the dataset commit the run's logs were published at, so links are immutable.
  - It rewrites same-origin `.eval` requests to `resolve/<commit>/RUN/inspect/<name>` with the bearer token and
    `redirect: 'follow'`, and returns a 401 "sign in" response when no token is stored.
  - Access is whatever the viewer's own HF account has: an organization member with Read can read the dataset.
- **Fallback.** A Docker proxy with OAuth: the prototype works, it needs no secret and it keeps the viewer's
  token. It needs paid hardware hosting (PRO, or a paid organization plan).
  - Only with that proxy, a read-only fine-grained token that the owner creates for the dataset and pastes as a
    Space secret would replace OAuth.
  - A static Space cannot hold a secret: anything it serves is visible to viewers.

#### Changes per script (as proposed; implemented as described above)

- `prepare_hf_library.py`:
  - writes `.eval` files to a separate `logs/RUN/inspect/` bundle with a SHA-256 manifest, and no longer into the
    Space bundle;
  - injects the shim tag into each Inspect View `index.html`;
  - adds `signin.html` and `evallog-fetch.js` at the bundle root;
  - records the logs in `archive-manifest.json` under `dataset_files` (repository, path, bytes, SHA-256).
- `publish_hf_library.py`:
  - commits the logs to the dataset first (pinned `parent_commit`, refused unless the dataset is private), then
    verifies each with `get_paths_info` (LFS SHA-256 and size equal to the manifest);
  - then commits the Space with that dataset commit in the shim tags;
  - refuses any `.eval` path bound for the Space;
  - applies the same `safe_path`/`screen` checks to both repositories.
- `maintain_hf_run.py`:
  - `--scope inspect` writes logs to the dataset;
  - `--prune-kind eval` works on the dataset;
  - a new `--scope migrate-logs` handles the migration below.
- `live_mirror.py`:
  - live logs go to `live/RUN/inspect/` in the dataset; the live Inspect View gets the shim; the Space's `live/`
    commits then carry only pages;
  - superseded live logs are purged with `permanently_delete_lfs_files(..., repo_type="dataset")`;
  - the order is dataset first, then Space, since no commit spans both repositories.
- `verify_hosted.py`:
  - `range_206` reads the dataset resolve URL with the operator's own login;
  - new checks:
    - the dataset is private;
    - the Space stores no `.eval`;
    - the Inspect View `index.html` carries the shim with the expected dataset and commit;
    - a preflight from the Space origin allows `authorization,range`.

#### Migration of existing runs (guarded, reversible)

`maintain_hf_run.py --scope migrate-logs --commit --receipt FILE --backup-dir DIR`, in stages:

1. **Plan.** List the Space at a pinned head: every `.eval` path, LFS SHA-256 and size. Download each object at
   that revision and hash it; local `runs/RUN/inspect/` copies are compared when present. Write the
   before-inventory.
2. **Copy.** Make one dataset commit adding `RUN/inspect/<name>`, deduplicated. Verify LFS SHA-256 and size per
   path, plus a ranged read of the first and last 64 bytes through resolve. Nothing in the Space changes.
3. **Switch.** Make a Space commit pinned to the planned head that adds the shim, `signin.html` and the shim tags.
   The `.eval` files stay, so this is reverted by re-committing the previous `index.html` files. Then run
   `verify_hosted.py`, and the owner loads one Inspect View after signing in.
4. **Delete (separate invocation).** Only after step 3 is verified, delete the Space's `.eval` paths at the
   head. Their records move from `files` to `dataset_files` in `archive-manifest.json`. Then purge the LFS
   objects that the new head no longer references, as in "Storage limit and pruning"
   (`rewrite_history=False`).
   - This frees about 277 MB of the current 920 MB.
   - Undoing it means re-uploading from the dataset with SHA-256 checks, which is bounded by the 1 GB cap.

#### Owner actions (as proposed)

- Create the private dataset `HF-ORG/LOGS-DATASET` in the organization, and add it with write
  access to each publisher's fine-grained token. The operator's current token reaches only the Space, and repo
  creation in the organization returned 403.
- Sign in once on a test deployment to confirm the browser flow end to end, including granting the
  organization at consent.
- Approve the design, then separately approve the deletion step.
- No token or Space secret is needed for the recommended design. The proxy fallback needs a PRO or paid
  organization plan; with a token instead of OAuth, the owner would create the read-only token and set it as a
  secret.

## Near-live mirror

The owner chose a near-live view of runs on the website while they are going ("option 1 (near-live)",
October 21, 2026), then: "there's no need for a transcript view on the website when we already have
inspect view". The mirror therefore publishes the run's Inspect log with the native Inspect View, the
conference view and a short live summary; it does not publish the custom transcript export
(`export_transcripts.py` stays in the repository for local use). `scripts/live_mirror.py` runs on the
operator's own computer, never on a VM, with the operator's own gcloud login and HF login
(`pip install -e '.[publication]'`, `hf auth login`):

Start it from the operator's computer with every launch, as soon as the supervisor has started the run
(docs/gcp-vm.md, "Supervise an authorized run unattended"), naming the launch's VM; it keeps running across
runs, mirrors whatever is running on the named VMs and stops mirroring a run after its final pass. Leave it in
its own terminal (or `tmux new -s live-mirror`) and stop it with Ctrl-C when no run is going:

```bash
PYTHONPATH=src .venv/bin/python scripts/live_mirror.py --vm collab-vm-1 --vm owner-vm
# one pass without uploading (the bundle is written under runs/phase-b/live-mirror/bundle/RUN/):
PYTHONPATH=src .venv/bin/python scripts/live_mirror.py --vm collab-vm-1 --once --no-upload
```

Every `--interval` seconds (default 300) it checks each VM's status and skips a VM that is not RUNNING
(it never starts, stops or provisions one). On a running VM it lists the runs under
`REMOTE_ROOT/runs` (`--vm NAME=REMOTE_ROOT`, default `~/gdm-math-swarm`) whose `runtime.jsonl` changed
within `--max-age` (default 1 hour) and has no `run_stop`; `--run RUN` restricts it to named runs. A
mirrored run that has just stopped gets one final pass and is then left alone. For each run it pulls a
read-only snapshot over IAP SSH: a small stdlib-only program is sent inside the SSH command (nothing is
installed or left on the VM) and streams back a tar archive with its own SHA-256 manifest. The archive
holds the complete lines of `runtime.jsonl`, `settings.json`, SQLite online backups of
`conference/state.sqlite` and of Inspect's realtime sample buffer (`~/.local/share/inspect_ai/samplebuffer`,
`--remote-buffer-dir`), the `.eval` logs when their size or modification time changed, and the private-file
snapshot blobs referenced since the previous pass (the conference view shows them). Claude Code's own
session JSONL snapshots are not transferred. No HF token, provider key or host environment reaches the
VM: the SSH command contains only that program and its arguments, and gcloud is started without any
token, key or `HF_*` variable. The VM program runs under `nice`.

Locally (Git-ignored `runs/phase-b/live-mirror/`: `work/RUN/` source copies, `bundle/RUN/` the upload,
`state.json`) it builds:

- **Inspect View.** While a run is going, Inspect's `.eval` holds only the samples it has flushed; each
  agent's sample stays in the realtime buffer until the agent finishes. The mirror writes a live copy of
  each log in Inspect's own in-progress format (`_journal/start.json` and samples, no `header.json`; Inspect
  View shows such a log as running): the flushed samples unchanged, then the buffered samples rebuilt with
  Inspect's crash-recovery reader. A sample still running carries the note "Still running at this live
  snapshot: not an error" where Inspect would show an error. A finished log is copied unchanged. Then
  `inspect view bundle` builds the static viewer over those copies, which gets the private-Space navigation
  exactly as `prepare_hf_library.py` gives a published Inspect View. Both steps run in a separate
  interpreter (`python -I`) started in a temporary directory outside the repository, so no `.env` is
  loaded, with the token-free environment; they use Inspect internals and refuse any Inspect version other
  than the pinned 0.3.279.
- **Conference view** with `export_conference.export`, after rebuilding `conference/events.jsonl` from the
  snapshot's events table (the conference's own rebuild) and the timeline from conference and runtime
  records (Inspect events are left out of the live timeline).
- **Live summary page** (state, snapshot time, elapsed time, model, condition, agents, exits and restarts,
  running and finished agent transcripts, tasks solved and submissions by conference outcome, with plain
  explanations) linking the two views.

Every page carries a "Live snapshot, not final" banner: conference (operational) outcomes only, no
independent evaluation, no behavior judge. Links to files the mirror does not carry (the report, raw
records, transcript pages) become plain text; the library link still works. A view that cannot be built
from a snapshot is listed under "Mirror notes" on the summary page.

Privacy screening is that of a normal publication: `publish_hf_library.safe_path` and `screen` for every
uploaded file, including every member of the Inspect archives, plus the same credential pattern over the
records behind the conference view (`runtime.jsonl`, the conference database and event log) and over each
page before it is written, since the conference view stores its records compressed. A match refuses that
run's update. The mirror refuses a public Space.

Upload: one commit per pass that touches only `live/` (enforced: any other path is refused). Changed files
are added, files of the previous snapshot that no longer exist are deleted (including the transcript pages
an earlier version of the mirror published), `live/index.json` (schema `gdm-live-mirror/1`) is updated, and
the whole `live/RUN/` folder and the entry of a run that now appears in the published `experiments.json` are
removed (its live entry is retired once it is published normally). A pass with nothing new makes no commit,
and unchanged files (the Inspect View app, unchanged conference pages) are never uploaded again. HTML pages
must stay below the 9.5 MiB LFS limit (a page that would reach it is replaced by a notice); Inspect logs may
be LFS objects, which Inspect View already follows. The mirror's commits start with `Live mirror: ` and are
pinned to the revision they were planned on. A pass is deferred while the Space's head is not one of the
mirror's own commits (recorded in `state.json`) and changed less than `--quiet-seconds` ago (default 600),
so a publication or maintenance in progress is not interrupted. If a live commit still lands between a
publisher's planning and its first batch, the publisher refuses with its usual concurrent-change message;
rerun it. A failed maintenance attempt resumed with `--resume-from` compares the whole Space with its
original state, so let it finish before the next live pass changes `live/` (the quiet period normally
covers this).

Bounded history: the live Inspect log is re-uploaded on every pass in which it changed (tens of MB for a
long run). The Space history is never squashed or rewritten, since its revisions are cited in this document.
Instead, after each live commit lands, the mirror permanently deletes the LFS objects of its own superseded
live logs (`permanently_delete_lfs_files` with `rewrite_history=False`), so storage holds about one live
log per running run. A candidate is only a log the mirror synthesized and uploaded itself (recorded in
`state.json`; a finished log copied unchanged is never a candidate, because the published run will hold the
same bytes), and it is deleted only when no file of the new head anywhere in the Space references it and the
Hub lists it under `live/`. Earlier live revisions then lack that log; nothing else changes. A small log the
Hub stored as a regular file cannot be deleted this way and stays in history. `--keep-live-history` turns
the deletion off.

The hosted catalogue (`write_index(..., hosted=True)`) has a hidden "Live now" section that reads
`live/index.json` in the browser and lists each run that is not already in the catalogue, with its state,
model, agents, solved count and update time and links to its live summary, Inspect View and conference view.
The catalogue page itself does not change when the mirror runs, so the maintenance tools still reproduce it
byte for byte. The local `runs/index.html` has no live section.

## Uploading and preserving other collaborators' experiments

Authenticate with the official HF CLI, entering credentials privately. If login
saved a token but failed on Apple's Git/Xcode license, activate its existing name
with `hf auth switch --token-name NAME --no-add-to-git-credential`. Never inspect or
copy token file contents. The SDK/CLI can use its own saved authentication.

Before updating, obtain the latest Space revision and existing run IDs. Preserve
all previously published experiments. Upload new run folders under unique,
immutable run IDs; do not replace another run with a new experiment. Regenerate
the catalogue from both existing and newly added reports. A bundle prepared only
from local runs is not automatically a complete shared catalogue.

Publish a prepared bundle with the reusable command included in the repository:

```bash
# Read-only preview; downloads the current catalogue but uploads nothing.
python scripts/publish_hf_library.py --bundle runs/phase-b/hf-library-preparation/NEW_BUNDLE

# Publish after reviewing the preview and the prepared experiment records.
python scripts/publish_hf_library.py \
  --bundle runs/phase-b/hf-library-preparation/NEW_BUNDLE \
  --commit --receipt runs/phase-b/hf-library-preparation/publication-receipt.json
```

You only need to prepare **your new runs**. The publisher merges their catalogue
entries with all existing remote entries and lists them newest first by actual start time
(owner request, October 21, 2026; the local `runs/index.html` uses the same order). It
preserves existing Space configuration and run files, refuses conflicting run IDs,
requires a private Space, verifies bundle hashes, and screens obvious credential
filenames and token patterns (including compressed Inspect logs). A token pattern counts only at a
token boundary: GPT runs' encrypted reasoning blobs contain `sk-`/`hf_` runs mid-token, which are not keys. Pattern scanning
is not a universal secret-detection guarantee; review new data sources before upload.

### Catalogue page

`build_experiment_index.py` renders the run list and compare view (owner request, October 21,
2026; vanilla CSS/JS, no external resources, light and dark mode, cards below 760 px). Columns:
start (newest first), model with condition and route, prompt variant, agents, duration, accepted /
genuine tasks (weak operational acceptance vs. distinct independently genuine statements), settled
pilot cost and the behavior judge's cohort counts or "not run". Rows filter by text, model, prompt
variant and judge outcome and sort by any column; ticking 2-4 runs opens a side-by-side comparison
(solves, cost, cost per genuine task, agent exits, judge cohorts), shareable as `#compare=RUN,RUN`.
The catalogue links no custom transcript pages (owner, October 21, 2026: agent transcripts are
read in Inspect View); new entries carry no `Transcripts` link, and the page hides that link in
entries published earlier (the entries themselves are unchanged). `export_transcripts.py` remains
for local use.
Each new entry carries a derived `summary` (`prompt_variant`, `accepted_tasks` from
`operationally_accepted_tasks`, and `judge`: only the cohort counts of a published
`analysis/judge/report.json`, no finding text). Entries published earlier have none: the hosted page
reads the same fields from that run's published `results.json` and judge `report.json` in the
browser (same-origin; nothing changes in the entry), and transcripts maintenance compares such an
entry without the bundle's summary. The page is a deterministic function of the entries. After a
page change, the published `index.html` no longer matches the generator, so maintenance scopes that
regenerate the catalogue refuse until `maintain_hf_run.py --rerender-catalogue --commit` (or the
next publication) rebuilds it.

Uploads use guarded `HfApi.create_commit` batches of at most 120 files and roughly
4 MB, with larger files alone. The catalogue and manifest are committed last.
Every batch checks the latest revision and privacy. Concurrent changes cause a
refusal; rerun the command to merge the newer catalogue. A failed partial upload
can resume only when every existing file for that run matches exactly. No deletion
operations are issued. Final verification checks uploaded hashes and that unrelated
remote files are unchanged; keep the receipt outside the prepared bundle.

The upload command verifies repository content, not an interactive browser session.
After publication, check the hosted report, conference, transcript and native Inspect
links; Inspect View needs HTTP byte-range support for `.eval` logs. Keep local
originals until remote integrity and access are verified. Deleting local records
requires a separate user decision.

The publisher inventories files through the revision-pinned, paginated tree API;
the repository-info sibling list can truncate large libraries. When HF automatically
appends LFS tracking rules, verification permits only exact additions for newly
uploaded LFS files in the selected run. Existing rules, unrelated files and earlier
experiment contents must remain unchanged. Regression checks cover truncated
inventories and refusal of broader attribute changes.

The initial archive was prepared under the ignored directory
`runs/phase-b/hf-library-preparation/space/`. Its manifest records the published
files; the official Hugging Face SDK performed the upload. Host credentials, auth
caches, launch phase directories and host-only budget databases are not included.

Initial publication completed at revision
`8784a2cdd01589cbde52e6bb39fd7b6598025c1e`. All 891 archive file hashes matched
remote object metadata. Six representative files were also downloaded and matched
locally. The private static app returned HTTP 200 with the experiment catalogue;
the Inspect `.eval` request returned HTTP 206 with the exact requested bytes.
The Space reported RUNNING and private. Verification records are saved locally in
`runs/phase-b/hf-library-preparation/publication-verification.json`. Local originals
were retained; no new experimental or analysis inference was run.

The conference-browser update is published at revision
`940c27542fc8958d8379f9b783df82b524b44fe3`. The catalogue's Conference link opens
it. All 1053 archive files matched remote hashes; downloaded conference HTML and
manifest matched, and the private conference route returned HTTP 200. Offline
checks validated 5850 links, the JavaScript syntax, all four independent evaluation
bindings, the 1116 unknown-time entries and the 20 empty RESEARCH.md captures.
The browser tool blocked a local `file:` preview, so these are source, link and
hosted-response checks rather than a claim of interactive visual validation.
Evidence is saved in the ignored `conference-verification.json` beside the initial
publication verification record.

The user-stopped `baseline100-20261015T191545Z` is also archived privately at
revision `95e30dad5763bcdc4c9587558f9c9fee0d467c6d`, preserving the first pilot.
All 3453 staged file hashes matched remote metadata. Seven representative files
were downloaded and verified; the catalogue, report, conference, transcript and
Inspect pages returned HTTP 200 with the authored HTML intact after removing only
the observed HF creator-metadata script. Inspect log byte ranges returned the
expected HTTP 206 bytes. Verification, privacy-screen records and consistent host
ledger backups remain under the ignored `runs/phase-b/baseline100-20261015T191545Z/`.
The report distinguishes four independently verified pinned formal tasks, ten
technical failures, four normally ended sessions and six user-interrupted sessions.
New-run settled spending is $86.091194430, with $2.3276232 in seven unresolved
billing holds. The VM was verified TERMINATED; no paid model analysis was run.

The completed 8 GiB run `baseline8g100-20261015T205316Z` is archived privately at
revision `23aec52bfadf8d3fe07af4fcb7156ac3abba15ed`, preserving both earlier runs.
All 6202 staged file hashes matched remote metadata. Seven representative downloads
matched, five hosted HTML routes returned HTTP 200 with authored content intact,
and native Inspect log byte ranges returned exact HTTP 206 bytes. The staged
privacy screen covered 6202 files and 192 archive entries with no credential-pattern
matches; 604 local relative links were verified. This is not interactive browser
visual validation or a universal secret-screening guarantee.

The report records 10 independently genuine submissions covering four distinct
pinned formal tasks, with the Erdős 1092 definition caveat retained. It does not
claim an exhaustive behavioral review. New-run settled spending is $99.321801318;
one new uncertain hold is $0.43223715. All prior call records and seven prior
holds are unchanged. Seventeen peers stopped on budget admission, one completed
its session, one failed under memory pressure and one failed after repeated output
truncation. Raw archives and the consistent ledger backup were verified locally.
The guest requested shutdown after retrieval; GCP stop completed and TERMINATED
was independently verified. No paid model analysis was performed. Verification
records remain under the ignored run phase directory; original records are retained.

The cached 180-minute run `cache100-20261016T020259Z` is archived privately at
revision `01cf5b306e0ffba1b1558ec0c68481110c8b9415`. The paginated inventory
contains 10,104 files, including all three earlier experiments and 3,900 new run
files. New-run hashes matched; older experiment files were unchanged. The only
non-catalogue metadata change was HF appending six exact LFS tracking rules for
large files in this run. Representative downloads matched, all five checked hosted
HTML routes returned exact authored content with HTTP 200, and the Inspect log
returned exact HTTP 206 byte ranges. This is content/access verification, not
interactive visual testing.

The archive includes 20 agent transcript pages (1,755 final-history messages),
conference views and a 26,483-entry timeline. All 13 selected submissions passed
independent formal verification, covering five distinct pinned tasks; the known
informal/formal caveats and incomplete behavioral review remain explicit. Spending
was $56.065928904 settled plus one new unresolved reservation of $0.19172520;
91.70% of input tokens were cached. All older calls and eight prior uncertain holds
remained unchanged. Archives and the consistent ledger backup passed local integrity
checks, and GCP was independently verified TERMINATED. Verification records remain
in the ignored `runs/phase-b/cache100-20261016T020259Z/` directory.

On October 16, the conference download defect was repaired for all four saved runs
at private revision `44411d6dbc2aedbad94f2c6325b2722de78fd12b`.
This narrow maintenance update replaced only the four derived conference index
pages, removed their three exact LFS tracking rules, and updated archive checksums.
All other 10,104-file inventory entries were preserved. Original HTML backups and
publication receipts remain under the ignored
`runs/phase-b/conference-browser-fix-20261016/` directory. The latest-run Conference
button was tested in authenticated Chrome, including board, direct messages,
shared library, judging and search. The 63 offline conference tests passed.

A separate private-navigation repair was deployed at revision
`c7a906bf6307952fbe57c3cebbebcc1eb079387a`, updating 1,816 derived HTML pages and
their archive checksums. It addresses the reproduced transcript-index-to-agent-page
`Invalid username or password` failure. All raw records, other files and private
visibility were preserved. Three regression tests exercise same-origin session
handling with fabricated values, external-origin exclusion, anchors, Inspect module
mapping, idempotence and preservation of range headers; the 13 publication tests
also pass. The live catalogue showed the repaired internal links, but the browser
connection became unavailable before deep navigation could be confirmed. Keep this
limitation explicit; SDK-authenticated HTTP checks are not an end-to-end browser test.
Backups and verification receipts remain under the ignored
`runs/phase-b/space-navigation-fix-20261016/` directory.

The behavior-judge maintenance update of `cache100-20261016T020259Z` (judge revision v2.3,
docs/plan.md "v2.3 judge run completed") is published at private revision
`b9649a2ef2370541a9f2ba73a25954583658c885`, made with `scripts/maintain_hf_run.py` from base
revision `c7a906bf6307952fbe57c3cebbebcc1eb079387a` in 8 guarded commits. It replaced only
that run's `results.html` and `results.json`, added the 61 allowlisted judge files, and
regenerated the catalogue, where only this run's entry changed: it gained a "Behavior judge"
link, and the other 3 entries were reproduced byte for byte. Verification against the base
revision found 10,165 files: the 66 planned files matched and 10,098 were preserved unchanged.
`.gitattributes` was unchanged. Hosted routes returned HTTP 200 inline with a signed session:
the catalogue, the run report, the judge report, the evidence browser index, an agent page and
the transcripts. Served HTML differs from the bundle only by HF's injected creator-metadata
script, and `results.json` matched exactly. These are SDK/HTTP checks, not interactive browser
tests. The Space keeps the earlier final-history transcript exports for all four runs; the
full-history exports regenerated locally on October 18 were not uploaded (24 local differences
skipped), because republishing them would need its own decision. The before-inventory,
original copies, plan, receipt and hosted verification are in the ignored
`runs/phase-b/judge-publication-cache100-20261016T020259Z/attempt-1/` directory.

The full-history transcript exports (regenerated October 18 with the judge's reconstruction
across compactions) replaced the earlier final-history exports of all four runs with the
transcripts scope of `scripts/maintain_hf_run.py`, in four sequential verified updates
from base revision `b9649a2ef2370541a9f2ba73a25954583658c885`. Final revisions were
`eefcc938f4bedd874ac41ff82746d368227bd309` (pilot),
`63c1b15cae9a5bb9e65a22535a58251c30d0131e` (baseline100),
`7bcaa6f6f7437585bfa5b573b36b886147d7e1fb` (baseline8g100) and
`6d0f9475a5496f59a3648005c320cee42bdf45ff` (cache100). Each update replaced exactly the run's
22 transcript files and their archive-manifest digests: 23 uploads, 10,142 files preserved
unchanged, `.gitattributes` unchanged, catalogue unchanged, no LFS. Every new page is below the
9.5 MiB guard; the largest is cache100 `agent-018.html` at 9,594,135 bytes. Hosted checks with a
signed session returned HTTP 200 inline, with no attachment disposition, for each run's
transcript index and two agent pages (including the largest). The authored content was intact
apart from HF's injected creator-metadata script, and every page showed the full-history view.
These are SDK/HTTP checks, not interactive browser tests. Before-inventories, original copies,
plans, receipts and hosted checks are in the ignored
`runs/phase-b/transcripts-publication-RUN/attempt-1/` directories.

The independent-evaluator results of `gemini10-20261020T054602Z` (published at revision
`8372ccc8b6b99fd39b7c680edfdf2ee72acb2178` before it was evaluated) were added at private revision
`ee145a4d29d6f5739a1081aa0901246491a8afad` with the evaluator scope of `scripts/maintain_hf_run.py`,
from base revision `8372ccc8b6b99fd39b7c680edfdf2ee72acb2178` in 4 guarded commits, at the user's
request in chat on October 20, 2026. It replaced the run's `results.html` and `results.json` (16
genuine and 4 unresolved of the 20 accepted submissions, with the unresolved reasons, the
native_decide finding and the evaluator validation record
`data/validation/evaluator-20261020T184044Z.json` from `gemini-collab` commit `7372170`), added
`evaluations/evaluations.jsonl` and `evaluations/summary.json` (the already published
`queue.jsonl` and `sources/` were identical and kept), replaced the two conference-view files the
rebuild changed (`index.html`, `manifest.json`) and regenerated the catalogue, where only this
run's entry changed (`distinct_verified_tasks` 0 to 16, shown as 16 / 71); the other 4 entries were
reproduced byte for byte. Verification against the base revision found 18,248 files: 9 uploads,
1,693 planned files matched and 16,555 were preserved unchanged; `.gitattributes` unchanged, no LFS.
These are SDK checks, not interactive browser tests. The before-inventory, original copies, plan
and receipt are in the ignored `runs/phase-b/evaluator-publication-gemini10-20261020T054602Z/attempt-1/`
directory.

The plain-language behavior-judge re-run of `gemini10-20261020T054602Z` (rubric `gdm-judge-rubric/3.1`,
$6.8073941 settled over 362 requests per its `report.json`), requested by the owner ("re-run the
behavior judge for gemini (the new one)"), replaced that run's published v3 judge outputs at private
revision `311af2f7b9d294e128f86856ec91ab5e7628ba3d`, made with the judge scope and `--replace-judge` from
base revision `cc8f49b1465d56b300bbd3a912dc50b84a1e224f` in 3 guarded commits. All 34 published judge
files were replaced and 2 evidence-browser agent pages added (`review/agents/agent-004.html`,
`agent-008.html`); no judge file was dropped, so nothing needed deleting. `results.html` and
`results.json` were identical (status line only) and the catalogue entry was unchanged, so only
`archive-manifest.json` changed at the root. Verification against the base revision found 18,284
files: 37 uploads, 41 planned files matched, 18,242 preserved unchanged, `.gitattributes` unchanged.
The next commit, `6f35f8d23f87d8d190c19f8cc3bb638b8473dce5`, is the baseline100 unlisting by another
workflow. At that revision `scripts/verify_hosted.py` passed all 32 checks for the run report, the judge
report, the evidence-browser index and three agent pages (private, regular files, bundle bytes,
HTTP 200 inline HTML). An SDK check confirmed the 36 judge files equal the bundle, the hosted
`report.html` has the "In plain words" section and no file of another run changed. These are
SDK/HTTP checks, not interactive browser tests. Locally the first (v3) build is kept, unpublished, in
`runs/gemini10-20261020T054602Z/analysis/judge-v3-first-run/`. Before-inventory, original copies of
the replaced files, plan, receipt and checks are in the ignored
`runs/phase-b/judge-plain-publication-gemini10-20261020T054602Z/attempt-1/` directory.

The snapshot archive update of October 21, 2026 (owner: "just go ahead and merge files") took the Space
from 19,937 files at revision `8c79494d17e2019581825dbef5cded93f9a17d58` to 6,444 at
`fb4537604e86e83eb2d8b17a309749a06954722c` with `--scope snapshots` for `pilot` (772 loose files),
`baseline8g100` (2,058), `cache100` (3,191), `gemini10` (6,308) and the partly published
`owner-gemini4-20261021T183937Z` (862) and `owner-glm53-20261021T184313Z` (314 of 993); every receipt
reads `verified` and `.gitattributes` was unchanged. The unlisted `baseline100-20261015T191545Z` keeps its
1,827 loose files (outside the requested scope; the same command converts it). The four owner runs were
then published with archived snapshots from `owner-multimodel-20261021-archived` (prepared from the
same staged records and Inspect bundles as the interrupted `owner-multimodel-20261021`, identical apart
from the archives) at `5dfa2d10f0969a187415b5b8f1a695472c0c4482` (654 uploads, 6,441 files preserved),
and the GLM behavior judge re-run, which had landed (paid, 5 of 5 agents graded), was added with the
judge scope at `3b1420e8004a85d2f35a3e4d24e033f1e9c8b88d` (22 judge files added, both reports replaced,
7,089 files preserved). The catalogue lists 8 runs newest first (GLM, Gemini 4, GPT 5.6, Opus 5.5,
gemini10, cache100, baseline8g100, pilot), every catalogue link resolves, the archive manifest records
exactly the 7,114 run and catalogue files, and `scripts/verify_hosted.py` passed for the four new runs,
GLM's judge report and evidence browser and `gemini10`. The Space then held 7,117 files. Receipts and
backups are in the ignored `runs/phase-b/hf-library-preparation/snapshot-archives-20261021/` directory and
next to the two bundles.

The new site (owner request, October 21, 2026: run list and compare view, run page summary card and tabs,
light/dark theme, phone layout, no custom transcript viewer) was rolled out with the site scope after the
other workflow published the four `owner-*-20261021` runs and archived older runs' loose snapshot files into
`snapshots.tar.gz`. The published catalogue page was already the current generator's output (no re-render
needed), and the four owner runs were published with the current generators (site plans: 0 uploads). The four
older listed runs were updated in sequence from base revision `3b1420e8004a85d2f35a3e4d24e033f1e9c8b88d`:
pilot (`2a136c91720589c3851dc52dd560c20fae3cd7a9`, 163 uploads), baseline8g100
(`8837561b30f689b59df49523b9ca3ee2a3c201c2`, 555), cache100 (`5d00cbeb7aa8cd4f4b66a0a872e126b45c353c8e`, 588,
including 16 judge pages) and gemini10 (`ca19ca0a320dc096ca296af202dc0143fe472715`, 1,677, including 11 judge
pages). Each replaced `results.html` (re-rendered from the unchanged `results.json`), the conference-view pages
and the judge HTML pages, with the archive manifest last; every receipt is `verified` against its base with the
7,117-file inventory unchanged, `.gitattributes` unchanged and catalogue entries unchanged. `baseline100` stays
unlisted and was not touched. At the final revision `scripts/verify_hosted.py` checked every replaced HTML page
of the four runs (private, regular file, bundle bytes, HTTP 200 inline HTML with exact stored bytes) and the
catalogue and one owner run's default pages. These are SDK/HTTP checks, not interactive browser tests. Plans,
receipts, backups of the replaced files, hosted checks and the local copies of the previous derived views are in
the ignored `runs/phase-b/site-publication-20261021/` directory.

The incremental update of October 21, 2026 (Incident report tab and raw-log link removal) started from
`ca19ca0a320dc096ca296af202dc0143fe472715` (7,117 files): `--rerender-catalogue` (`081b817a7ed7973da6190eea5bae2594b7e20b7e`;
the catalogue hides raw log links and links the incident reports), then `--scope incident` for gemini10 and
the four `owner-*-20261021` runs (3 files each plus their `results.html` tab and catalogue link; receipts in
`runs/phase-b/incident-publication-<run>/attempt-1/`), then `--scope site` for all 8 listed runs to replace
`results.html` with the page re-rendered by `build_results.py --rerender-html` (no raw log links; Incident
report tab where a report exists). Every receipt reads `verified`; the final revision is
`03aaab1d9084afd462e11434c8fa225d3218d1fb` with 7,132 files. `scripts/verify_hosted.py` passed for all 8 runs
(catalogue, run page, conference view, Inspect View, incident report where present, a 206 range on one `.eval`,
and `no_log_download_link`). A local copy of one run's hosted pages was also checked in a browser (catalogue,
each tab, incident report, Inspect View loading its log). `baseline100` stays unlisted. Records are in the
ignored `runs/phase-b/site-incident-nologs-publication-20261021/` directory.

The four GLM oversight runs of October 21, 2026 (`glm-rule6-20261021T224441Z`,
`glm-baseline-20261021T224732Z`, `glm-overseer-20261021T230007Z`, `glm-overseer-powers-20261021T230325Z`)
were published together on October 22, 2026 with `prepare_hf_library.py` (four `--run` entries, incident
reports included) and `publish_hf_library.py --commit`, from base `dd710c6c681824643bdf44df66cc4f9d5b8751e7`
to `53791397a96c70de752c4076a576699d824b5154` (886 files uploaded, 7,759 preserved, catalogue newest
first, Space private). `scripts/verify_hosted.py` passed for all four at that revision. With `--bundle`
only `index.html` differs, as expected: the published catalogue merges the existing entries, the bundle's
lists only the new runs. Records are in the ignored `runs/phase-b/hf-library-preparation/glm-oversight-4runs-20261022*`.
The near-live mirror then retired their `live/` entries (`af0b5e02`). It had also mirrored the baseline launch's
compatibility check (`compat-20261021T224747Z`), which is never published and so is never retired automatically;
that entry was removed by hand with one pinned `live/`-only commit (`ee02c592`). Start the mirror after the
compatibility check, or with `--run RUN_ID`, to avoid this.

`glm10-baseline-2h-20261022T020447Z` (GLM 5.3, 10 agents, 2 h) was published on October 22, 2026 with
`prepare_hf_library.py` (incident report included) and `publish_hf_library.py --commit`, from base
`3864f7858f13cf72f69cf7beb6e4a226ef682401` to `143be45650b51dda5dfd2255ebe64092a52c878e` (376 files uploaded,
8,290 preserved). `scripts/verify_hosted.py` passed there (catalogue, run page, conference view, Inspect View,
206 range on the `.eval`) and for the incident report at the next head. The mirror process was stopped and one
`live_mirror.py --once --run RUN --quiet-seconds 0` pass retired its `live/` entry (`476953b1c0f0c8c5148ed7a9ff29f4de4008eb82`,
`live/` now holds only `index.json`). Records: `runs/phase-b/hf-library-preparation/glm10-baseline-2h-20261022*`.

