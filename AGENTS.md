# Instructions for GDM Math Swarm

Read docs/HANDOFF.md first: it is the living record of current state (latest runs, features,
website, in-flight work, next steps). Keep it current: update and push it whenever a run,
feature, site change, owner decision or background job starts or finishes, so a fresh agent
can take over at any moment. Read docs/plan.md before implementing or running experiments. It is the
authoritative record of user decisions. Keep proposals distinct from approvals.

Use terminal Git commands and an SSH GitHub remote for future commits and pushes,
as requested by the user. Do not use GitHub Desktop to push; report any SSH
authentication blocker instead.

Before GCP provisioning, SSH, file transfer, remote execution, or shutdown, read
docs/gcp-vm.md and docs/validation.md. They preserve the user's supplied setup and
run procedure. Stop the VM after authorized work completes or fails, once results
and the budget ledger are preserved and no other active work needs it; verify
TERMINATED. Closing SSH or detaching tmux does not stop the VM. These instructions
are researcher-only and must not enter experimental agent workspaces.

The current pilot is baseline-only: 20 independent asynchronous Claude Code
agents, one fixed random 14-problem subset, spontaneous discovery, no mitigations.
Seeded contagion is to be supported but disabled initially. Never plant development
exploit fixtures in experimental workspaces or shared library contents.

Use development subagents for bounded implementation workstreams in docs/plan.md.
These are separate from experimental agents; agree interfaces and file ownership.

Do not read, print, copy, mount, or commit real credential values. A future .env is
host-only and Git-ignored. Inspect only credential presence, permissions, and ignore
status when needed. Containers must never receive provider keys or the host .env.
Do not start paid calls or provision resources without a per-run budget specified
before the run. Spending has exactly one control, repo-wide (docs/plan.md, "One per-run
budget, no manual approvals (October 20, 2026)"): every paid invocation (a pilot run on
any track, the live compatibility check, the behavior judge) has its own budget, the
config's `run_budget_usd` (default $100; the launch prints it) or the judge's run limit
(`--run-limit`, else `behavior_judge_max_usd`, else $50), and the ledger admits a request
only while that run's committed spend plus the reservation fits. Never add cumulative
caps, top-ups, extra spending checkers, approval notes or flags, typed confirmations,
interactive-terminal requirements, or any step that makes the owner approve commands by
hand or paste them into terminals; refresh stale records and handle leftover holds
automatically. This applies to every collaborator's track too. A collaborator sets their
own run budgets and uses their own provider key (docs/plan.md, "Collaborator budgets and
keys"); never use another person's key.

Preserve the weak operational judge and a separate observational research evaluator.
Operational acceptance is not mathematical validity or intent. Keep unknown and
unresolved outcomes explicit. Validate source evidence before behavioral claims.

Read docs/prompt-caching.md for the paid diagnosis and `counter-user-v1` correction.
Keep exact historical token counters as chronological user context in provider
requests; preserve genuine system instructions and record the normalization version.
Provider prompt caching is separate from Inspect's whole-response `cache=False`.

Keep completed experiment artifacts together under runs/<run-id>/: raw Inspect logs,
conference records, settings, budget, evaluator records, report and transcripts.
Use scripts/export_transcripts.py --run-dir runs/<run-id> for the offline HTML
transcript view and scripts/build_experiment_index.py after saving results.json and
results.html. The local experiment library is runs/index.html. Preserve raw records;
exports are derived views. Raw runs are Git-ignored and require separate backup.
The user also authorized a private shared HF library at
https://huggingface.co/spaces/gdm-math-swarm/gdm-math-swarm-experiments.
Follow docs/experiment-library.md for preparation, privacy and guarded updates;
preserve other collaborators' runs and never upload host credentials or auth caches.
Use scripts/publish_hf_library.py --bundle PREPARED_DIRECTORY for a read-only
publication preview; --commit merges and publishes using the operator's own HF
login, guarded revisions and immutable run records. Install the publication extra.
Use scripts/export_conference.py --run-dir runs/<run-id> to browse saved board
posts, DMs, library revisions, judging, organizer feedback, captured private files
and the existing activity timeline. Keep empty sections and capture limits explicit.

Freeze the 14 tasks for all 20-agent experiments. Deliver their exact names, pinned
Lean declarations, and verified online descriptions/known solutions after selection.
Keep reference solutions, analysis rubrics, researcher reports, and observer logs
inaccessible to experimental agents. Standard Mathlib reuse remains permitted.

Render mathematical formulas in LaTeX unless referring specifically to code.
