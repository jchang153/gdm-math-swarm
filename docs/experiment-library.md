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
/Users/jchang153/Documents/GitHub/gdm-math-swarm/.venv/bin/inspect view bundle \
  --log-dir /Users/jchang153/Documents/GitHub/gdm-math-swarm/runs/RUN_ID/inspect \
  --output-dir /private/tmp/inspect-RUN_ID
```

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
