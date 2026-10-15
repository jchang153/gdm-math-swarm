# Shared experiment library

The private library is https://huggingface.co/spaces/jchang153/gdm-math-swarm-experiments.
The user authorized uploading the completed pilot there on October 15, 2026,
after choosing private visibility. Check that it remains private before updates.
An HF login with access to this Space is required to view it. Personal-namespace
ownership does not automatically give collaborators access; configure access on
Hugging Face or move to a shared organization when needed.

The static app contains the experiment catalogue, per-run reports, HTML transcripts,
native Inspect View, and original run records. Inspect View renders the saved
Inspect logs; it does not run or judge agents. The independent evaluator records
remain separate from conference acceptance. No VM, Docker, model key or inference
is required to browse. Local originals and host ledger backups are retained.

## Preparing an archive

Export the completed run with `scripts/export_transcripts.py`. Save the report's
`results.json` and `results.html` alongside the raw records. Create a native Inspect
bundle with the pinned Inspect installation, running outside the repository to
avoid automatic dotenv loading:

```bash
cd /private/tmp
/Users/owner/Documents/GitHub/gdm-math-swarm/.venv/bin/inspect view bundle \
  --log-dir /Users/owner/Documents/GitHub/gdm-math-swarm/runs/RUN_ID/inspect \
  --output-dir /private/tmp/inspect-RUN_ID
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

From the repository, prepare a fresh, non-existing output directory:

```bash
python3 scripts/prepare_hf_library.py \
  --run RUN_ID=/private/tmp/inspect-RUN_ID \
  --output-dir runs/phase-b/hf-library-preparation/NEW_BUNDLE
```

Repeat `--run` for multiple locally available completed experiments. The helper
copies only selected run folders and supplied viewer bundles, excludes hidden files,
refuses symlinks and obvious credential filenames, generates a hosted catalogue,
and records file sizes and SHA-256 digests. This is not a general secret scanner:
review new data sources before publishing. Never point it at a host home directory,
repository root, credential directory, or launch phase directory. All generated
records remain Git-ignored; commit only code and researcher documentation.

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

Use guarded `HfApi.create_commit` batches with `repo_type='space'` and
`parent_commit` set to the revision you inspected. The initial single-folder upload
timed out with HTTP 408; retrying in 20 sequential batches succeeded. Group at most
120 files and approximately 4 MB per commit (larger individual files go alone).
Publish run records before the root catalogue and manifest. If the revision changes,
fetch the new records and rebuild
the catalogue before retrying. Do not use deletion patterns or overwrite a newer
catalogue blindly. Verify private visibility, remote file hashes, and report/viewer
links after upload. Inspect View needs HTTP byte-range support for `.eval` files.
Keep local originals until remote integrity and access are verified; deleting local
records requires a separate user decision.

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
