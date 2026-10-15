# Instructions for GDM Math Swarm

Read docs/plan.md before implementing or running experiments. It is the
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
Do not start paid calls or provision resources before model integration and a
user-approved spending cap with validated enforcement. Analysis spending is separate.

Preserve the weak operational judge and a separate observational research evaluator.
Operational acceptance is not mathematical validity or intent. Keep unknown and
unresolved outcomes explicit. Validate source evidence before behavioral claims.

Keep completed experiment artifacts together under runs/<run-id>/: raw Inspect logs,
conference records, settings, budget, evaluator records, report and transcripts.
Use scripts/export_transcripts.py --run-dir runs/<run-id> for the offline HTML
transcript view and scripts/build_experiment_index.py after saving results.json and
results.html. The local experiment library is runs/index.html. Preserve raw records;
exports are derived views. Raw runs are Git-ignored and require separate backup.

Freeze the 14 tasks for all 20-agent experiments. Deliver their exact names, pinned
Lean declarations, and verified online descriptions/known solutions after selection.
Keep reference solutions, analysis rubrics, researcher reports, and observer logs
inaccessible to experimental agents. Standard Mathlib reuse remains permitted.

Render mathematical formulas in LaTeX unless referring specifically to code.
