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
not authorize a paid run or a spending-cap increase.

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

Export the completed run with `python scripts/export_transcripts.py --run-dir runs/RUN_ID`. Save the report's
`results.json` and `results.html` alongside the raw records. Create a native Inspect
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

The post-hoc transcript judge (docs/plan.md, October 17, 2026) writes researcher-only
records under `RUN/analysis/judge/`. There is no human marking step: the judge's
adversarially verified labels are the reported labels (docs/plan.md, superseding decision
"no human marks"). The pipeline's report stage builds both outputs; to rebuild them from
saved records without any model call, run
`python -m research.analysis.judge.review build --run-dir RUN` and then
`python -m research.analysis.judge.report --run-dir RUN` (all `PYTHONPATH=src:.`).
`review/` is a read-only findings browser: `review/index.html` lists every finding,
refuted ones included and labelled, with the verifier's verdict and the model that
verified it, every agent's dimensions and cohort, and the run-level results;
`review/agents/<agent>.html` shows each agent's full evidence passages. It has no form
controls, scripts or browser storage. `review/manifest.json` records the SHA-256 of every
input and page. The report adds a marked section to `results.html`, a `behavior_judge`
key to `results.json` (including the model allocation and cost per stage), and the index
builder links `analysis/judge/report.html`.

Rule applied by `prepare_hf_library.py`: judge outputs are published only from a `paid`
run whose findings browser and report still match the judge outputs. It refuses
`rehearsal`/unknown-mode outputs, a missing findings browser or report, a judge input or
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
