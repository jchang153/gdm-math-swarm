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

The static app contains the experiment catalogue, per-run reports, HTML transcripts,
native Inspect View, and original run records. Inspect View renders the saved
Inspect logs; it does not run or judge agents. The independent evaluator records
remain separate from conference acceptance. No VM, Docker, model key or inference
is required to browse. Local originals and host ledger backups are retained.

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

Export the completed run with `python scripts/export_transcripts.py --run-dir runs/RUN_ID`. Then write the
report with `scripts/build_results.py`, which `prepare_hf_library.py` and `publish_hf_library.py` require
(they refuse a run without `results.json` and `results.html`). The publication steps are:

1. `python scripts/build_results.py --run-dir runs/RUN_ID [flags]` writes `results.json` and `results.html`
   in the library format (the layout of `gemini10-20261020T054602Z`).
2. `python3 scripts/prepare_hf_library.py --run "RUN_ID=VIEWER_BUNDLE" --output-dir NEW_BUNDLE` (below).
3. `python scripts/publish_hf_library.py --bundle NEW_BUNDLE` for the read-only preview, then `--commit`.

The report is a deterministic, facts-only review of the run's own records, with no model calls,
network or credentials: run timing and stop reason, the condition from `settings.json`, per-agent
requests, exits and restarts, spending and tokens, submissions per task and agent with times and
outcomes, and links to the existing record views (transcripts, conference view with board, DMs, library
and private files, timeline, Inspect View and logs). It never summarizes those contents. The proof
checker section is filled only from `evaluations/` (verdicts and reasons as recorded, bound by
submission, task and source hash) and the behavior judge section only from `analysis/judge/report.json`
(counts and links, labelled model-generated post-hoc judging); otherwise the page states "Proof checker:
not run" / "Behavior judge: not run" and accepted submissions are `not_evaluated`. Flags:
`--ledger LEDGER.sqlite` (a consistent ledger copy; exact run-only spending by this run's admitted request
IDs, otherwise derived from `runtime.jsonl` with uncertain holds still counted as held),
`--key-usage-log LOG.jsonl`, `--caveat TEXT` and `--caveat-for SUBJECT TEXT` (the only free text, stored
under `researcher_notes` and shown under "Researcher notes"), `--title`, `--stop-utc`/`--stop-reason` for a
run stopped without a `run_stop` event, and `--submission-ids serial` for older serial IDs. It refuses to
replace existing reports without `--overwrite`; use `--output-dir` for a scratch copy. Create a native Inspect
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
submission identity/source hashes, verifies library/snapshot hashes, and links
timeline messages to existing transcript anchors where available. It uses no
model calls or running containers.

The conference tabs show board posts, participant-grouped private DMs, shared
library revisions, submissions with conference and independent judgments,
organizer feedback, captured workspace working files/notes, and the saved activity
timeline. Search, agent/task/type filters and pagination operate locally in the
page. Empty sections are explicit. Board posts have no recorded reply relationship;
the viewer does not invent threads. Untimed events remain unknown. File captures
are observations rather than a complete edit history, and were not automatically
available to peers. SDK session JSONL is covered by the agent transcript viewer;
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
from judge revision v3 of October 19, 2026, rubric `gdm-judge-rubric/3`) writes researcher-only
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
`gdm-judge-review-manifest/3`) records the SHA-256 of every input and page. The report adds a
marked section to `results.html`, a `behavior_judge` key to `results.json` (including the model
allocation and cost per stage), and the index builder links `analysis/judge/report.html`.

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
entries with all existing remote entries and sorts by actual start time. It
preserves existing Space configuration and run files, refuses conflicting run IDs,
requires a private Space, verifies bundle hashes, and screens obvious credential
filenames and token patterns (including compressed Inspect logs). Pattern scanning
is not a universal secret-detection guarantee; review new data sources before upload.

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
