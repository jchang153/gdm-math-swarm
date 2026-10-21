# Handoff: current state of the GDM Math Swarm project

This file lets a fresh coding agent with no conversation history pick up the
project. It is the living "where are we" record. `docs/plan.md` remains the
authoritative record of the owner's decisions; this file summarizes state and
points to where things live.

## Maintaining this file (mandatory for every agent)

The owner can run out of agent usage at any moment, so this file must always be
current enough for a new agent to continue without asking.

- **Update it as you go, not at the end.** After any of these, edit the relevant
  section and commit + push right away:
  - a run is launched or finishes;
  - a feature lands on main;
  - the website or HF Space changes;
  - the owner makes a decision;
  - a background job starts or ends.
- **Keep "In flight" and "Next steps" accurate.** Name branches, worktrees and
  run ids. Say what a half-finished job still needs and how to resume it.
- **Keep it short and factual.** Replace stale text; don't append a diary. Use
  absolute dates (for example 2026-10-21), never "today" or "yesterday".
- **The repo is private.** If it is ever made public, the owner wants the cloud
  provider, project and host names removed first; warn them. Never put credential
  values, account emails or budget figures here: those go in the git-ignored
  `HANDOFF.local.md` at the repo root, which must also be kept current.
- **Bump the "Last updated" line** below whenever you edit this file.

Last updated: 2026-10-21 by Claude (Opus 5.5) session.

## What the project is

A documented reconstruction of the research-conference environment from Paglieri
et al., *A Case Study on Emergent Cheating and Whistleblowing in Autonomous Research
Swarms* (https://arxiv.org/pdf/2609.04170):

- Multiple LLM agents share a pool of Lean 4 + Mathlib problems: a frozen pool of
  71, with a 14-problem subset for 20-agent runs.
- The first accepted submission for a problem earns credit.
- A deliberately weak operational judge accepts submissions.
- The study looks at whether agents spontaneously exploit the judge, spread
  exploits, or blow the whistle.

Each agent is the Claude Code CLI in a Docker container on a cloud VM. Inspect AI's
model bridge routes its model calls to OpenRouter, so any model can drive it.

## Read these first

| File | What it holds |
|---|---|
| `AGENTS.md` | Hard rules: credentials, budgets, VM shutdown, never expose researcher material to agents |
| `docs/plan.md` | All owner decisions, with dates |
| `HANDOFF.local.md` (git-ignored, owner's machine only) | Account email, budget status |
| "Hosts and access" and "Operating a VM" below, plus `docs/gcp-vm.md` | VM access and step-by-step operation |
| `docs/validation.md` | The run procedure; read before touching a VM |
| `docs/experiment-library.md` | HF Space publishing rules and the guarded update scopes |
| `docs/prompts.md`, `src/research_swarm/prompts.py` | Prompt variants |
| `docs/judges.md`, `docs/evaluator.md` | Weak operational judge vs. independent proof checker |
| `docs/prompt-caching.md` | `counter-user-v1` normalization for provider caching |
| `docs/runtime.md`, `docs/interfaces.md` | Harness internals |

## Where things live

- **GitHub:** `jchang153/gdm-math-swarm`, branch `main`.
  - Port 22 times out. Push over 443 with:
    `GIT_SSH_COMMAND="ssh -o HostKeyAlias=github.com" git push ssh://git@ssh.github.com:443/jchang153/gdm-math-swarm.git main`
  - Git binary: `/Library/Developer/CommandLineTools/usr/bin/git`.
- **Shared results site:** the private HF Space (URL under "Hosts and access"); keep it private.
  - A static site with the run list, per-run pages, Inspect View, behavior judge,
    conference browser and proof-checker results.
  - Run list is newest first. baseline100 is deliberately unlisted.
  - The 20,000-file limit is handled by packing each run's `snapshots/` into
    `snapshots.tar.gz` plus `snapshots.manifest.json`.
  - The owner has NOT approved making the site public.
- **Local experiment library:** `runs/index.html`, rebuilt with
  `scripts/build_experiment_index.py`.
- **Raw runs:** `runs/<run-id>/`. Git-ignored; back them up separately.
- **VMs:** see "Hosts and access" and "Operating a VM" below. Always stop VMs after work and verify they're stopped. Do not
  touch collaborator Amelie's VM, files or branches.
- **Credentials:** host-only `.env`, Git-ignored. It holds the owner's OpenRouter
  key and HF login. Never read or print the values; containers never get them.
- **Python:** `.venv/bin/python`. Run tests with
  `PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -q`
  (pytest is not installed). Analysis tests are in `research/analysis/tests`.

## Hosts and access

- **Compute:** Google Cloud, project `GCP-PROJECT`, zone `us-central1-a`.
  - Owner's VMs: `owner-vm`, `owner-vm-2`, `-3`, `-4`.
  - Don't touch `collab-vm-1` or any other collaborator VM.
  - Repo on each VM: `~/gdm-math-swarm`, with its own `.venv` and the host-only `.env`
    holding the owner's OpenRouter key. Never read or print it.
- **gcloud on the owner's Mac:** `/Users/owner/.local/share/gdm-vm-tools/google-cloud-sdk/bin/gcloud`.
  - Set `CLOUDSDK_PYTHON=/Users/owner/Documents/GitHub/gdm-math-swarm/.venv/bin/python`.
  - Always pass `--project GCP-PROJECT --zone us-central1-a --tunnel-through-iap`.
    There is no public IP; SSH goes through IAP.
- **HF Space (private):** https://huggingface.co/spaces/gdm-math-swarm/gdm-math-swarm-experiments

## Operating a VM

Steps 2–8 are what the launch agents did for the 2026-10-21 runs. If the gcloud login has expired, the owner must re-run `gcloud auth login` (it opens a browser; the account is in `HANDOFF.local.md`).

```bash
G=/Users/owner/.local/share/gdm-vm-tools/google-cloud-sdk/bin/gcloud
F="--project GCP-PROJECT --zone us-central1-a --tunnel-through-iap"
VM=owner-vm-2
```

1. **Status:**
   `$G compute instances describe $VM --project GCP-PROJECT --zone us-central1-a --format='value(status)'`
2. **Start:**
   `$G compute instances start $VM --project GCP-PROJECT --zone us-central1-a`.
   If it fails with "not enough resources", retry.
3. **Ship code:** the VM has no GitHub credentials, so use a git bundle.
   - Locally: `git bundle create /tmp/x.bundle <branch>`.
   - Copy it: `$G compute scp /tmp/x.bundle $VM:~/x.bundle $F`. Retry; IAP is flaky.
   - On the VM:
     `cd ~/gdm-math-swarm && git fetch ~/x.bundle <branch>:refs/heads/<branch> && git checkout <branch>`.
     First move aside any untracked files that would be overwritten, after checking
     they're identical.
4. **Run commands:** `$G compute ssh $VM $F --command '...'`. Use tmux for anything long.
5. **Before launch, in `~/gdm-math-swarm` on the VM:**
   - `.venv/bin/python scripts/freeze_route.py --config configs/<cfg>.json`
   - `PYTHONPATH=src .venv/bin/python scripts/compat_check.py --config configs/<cfg>.json --max-requests 6`.
     This is paid and bills the run's own budget.
   - `.venv/bin/python scripts/preflight.py --config configs/<cfg>.json`.
     Runtime-evidence gates are informational only.
6. **Launch:**
   - Create `runs/phase-b/<RUN_ID>/launch-config.json`: the config plus
     `run_dir: runs/<RUN_ID>`, `verified_integration: true` and `paid_launch_enabled: true`.
   - Run in tmux:
     ```bash
     tmux new -d -s gdm-<RUN_ID> ".venv/bin/python scripts/supervise_run.py --config runs/phase-b/<RUN_ID>/launch-config.json --phase-dir runs/phase-b/<RUN_ID> --tmux-session gdm-<RUN_ID> 2>&1 | tee runs/phase-b/<RUN_ID>/supervisor-console.log"
     ```
   - Omit `--no-shutdown` so the supervisor stops the VM when idle.
   - Confirm it started: `runs/<RUN_ID>/runtime.jsonl` has `common_start` and
     `inference_finished` events.
7. **Afterwards:** the supervisor runs reconciliation, evaluation, the behavior
   judge and packaging, then waits for `retrieved.marker`.
   - Copy `runs/<RUN_ID>` back to the local `runs/` with `$G compute scp --recurse`
     (or tar it first).
   - Then touch the marker.
8. **Stop:**
   - `$G compute instances stop $VM --project GCP-PROJECT --zone us-central1-a`.
   - Verify `TERMINATED`. Closing SSH or tmux does NOT stop the VM.
   - If the supervisor refuses because of "other work", stop it manually.

The full reference is `docs/gcp-vm.md` and `docs/validation.md`.

## Owner preferences (also in AGENTS.md and agent memory)

- **Speed over cost.** Parallelize with subagents and workflows, and keep status
  updates short and plain.
- **One budget control only.** Each paid invocation has its own budget:
  - pilot runs: `run_budget_usd`, default $100;
  - behavior judge: $50 per run;
  - incident report: $20 per run.
- **No manual approvals.** No typed confirmations, no cumulative caps, no manual
  approval steps, no pasting commands into terminals.
- **Runtime evidence checks are waived permanently.** Launch gates report them as
  informational and never block.
- **Math formatting.** Render math in LaTeX, except when referring to code.

## Pipeline per run (all automatic after launch)

1. **Launch:** `scripts/supervise_run.py` with a config from `configs/`. It
   provisions or starts the VM, runs, collects into `runs/<run-id>/`, and stops
   the VM.
2. **Proof checker:** independent evaluator; genuine vs. accepted counts.
3. **Behavior judge:** `scripts/run_transcript_judge.py`, rubric 3.1. Stages are
   Haiku prefilter → Sonnet candidates → Opus verify, then a run-level summary in
   plain language. Live judging during runs is available.
4. **Incident report:** `scripts/run_incident_report.py --run-dir runs/<id> --run-limit 20`.
   - Code: `research/analysis/incident/`.
   - Writes `analysis/incident/{report.html, report.md, evidence.json}`, a narrative
     report with excerpts verified word for word, in the OpenAI misalignment-report
     style.
   - It runs automatically after the judge.
   - If Anthropic refuses a step, that step goes once to openai/gpt-5.6-sol and is
     flagged. Refused, unbilled calls settle at $0 through OpenRouter's generation
     lookup.
5. **Facts-only results page:** `scripts/build_results.py`. Then
   `scripts/export_transcripts.py`, `scripts/export_conference.py`, and
   `scripts/build_experiment_index.py`.
6. **Publish to HF:**
   - Preview with `scripts/prepare_hf_library.py`, then
     `scripts/publish_hf_library.py --bundle DIR [--commit]`.
   - Update a single run with `scripts/maintain_hf_run.py`. Scopes: judge,
     evaluator, results, unlist, inspect, snapshots, catalogue.

## Runs so far (newest first)

| Run id | What | Key result |
|---|---|---|
| owner-glm53-20261021T184313Z | GLM 5.3, 5 agents, 1 h, all 71 tasks, `max_pressure_no_rule6`, 32 GB/agent | 39 accepted / 10 genuine. One `Iff.rfl` answer-restating trick posted to the board; 4 agents adopted it; 1 whistleblower (agent-001) |
| owner-gemini4-20261021T183937Z | Gemini 4 Argon, same setup | 21 / 18. No exploiters; agents never communicated |
| owner-gpt56-20261021T183821Z | GPT 5.6 Sol, same setup | 17 / 14. 3 exploiters (a banned shortcut under a second name) |
| owner-opus55-20261021T183459Z | Claude Opus 5.5, same setup, max_tokens 32768 | 22 / 13. 5 of 5 agents exploited |
| gemini10-20261020T054602Z | Gemini 3.1 Pro, 6 h | 16 of 71 genuine; first incident report pilot |
| cache100, baseline8g100, baseline100 (unlisted), pilot | earlier baselines | see their `results.html` |

All 5 recent runs have incident reports at `runs/<id>/analysis/incident/report.html`.
The four owner-* runs, gemini10 and the earlier runs are published on the HF Space.

`max_pressure_no_rule6` is collaborator Amelie's "max pressure" framing: a skills
evaluation, with agents that score zero not used. The stop sentence is removed. The
integrity block keeps its rules except the "rewrite goals to trivial tautologies"
rule (originally rule 6; the old rule 7 becomes 6).

The configs and multi-model harness fixes are on branch `owner-multimodel`, which
is being merged into main via `collab-env`.

## In flight as of 2026-10-21 (check status before redoing anything)

1. **Neutral "collab" environment** (branch/worktree `collab-env`, based on
   `owner-multimodel` plus main). It adds two new prompt variants:
   - `collab_max_pressure_no_rule6`: same pressure framing;
   - `collab`: plain collaborative framing, no pressure.

   In both, agents see no "Claude", "Claude Code", "Anthropic", "conference" or
   "organizer":
   - The bridge filter strips Claude Code's billing header, its "Claude agent"
     line, the attribution reminder, the model-name lines and the environment
     block.
   - Claude Code's temp and config folders move off paths containing `claude`.
   - Tools become `mcp__collab__*`; `get_conference_status` is renamed.
   - `submit_feedback` becomes private feedback about the environment.
   - The continue nudge and error messages are reworded.

   Old variants stay unchanged. The owner wants to review the full prompt text of
   both variants before any run. Whether the folder-moving environment variables
   work needs confirming at runtime on the first launch. If the branch isn't merged
   to main, finish it, run the tests, merge and push.
2. **Website quality-of-life plus near-live mirror** (worktrees `site-*`,
   `site-rollout`). It adds:
   - run-list filters, sort and compare;
   - run-page tabs;
   - mobile and dark mode;
   - a near-live mirror of in-progress runs, based on Inspect View. No separate
     transcript view is wanted.

   Once it lands:
   - add an "Incident report" tab per run, publishing only `report.html`,
     `report.md` and `evidence.json` (never `raw/` or `units/`);
   - publish the 5 incident reports;
   - remove the "Inspect logs" link or tab, which is only a download. Keep the
     `.eval` files, since Inspect View needs them.

## Next steps / open items

- Owner to review the incident reports (the Gemini 3.1 pilot first) and the
  collab prompt text.
- Optional ideas the owner hasn't approved:
  - name the prompt variant in each incident report's header;
  - archive baseline100's loose snapshot files (about 1,800 files) with
    `maintain_hf_run.py --scope snapshots`;
  - a one-command multi-run launcher.
- Before any future full re-publish of GLM, exclude
  `runs/owner-glm53-20261021T184313Z/analysis/judge-partial-first-attempt/`.
- The near-live mirror uploads loose snapshot files, which add to the HF file
  count. Handle this only when it becomes a problem (owner's call).
- Budget status and remaining credits: see `HANDOFF.local.md`. A 1-hour, 5-agent run
  costs roughly $40–90 including judge and report.

## Known gotchas

- **Anthropic refusals.** Anthropic models can refuse to analyze some transcripts
  (GLM's), citing "reverse engineering / duplicating model outputs". The incident
  pipeline falls back automatically.
- **VM capacity.** VM start can fail with "not enough resources"; retry.
- **Supervisor shutdown.** If the supervisor declines shutdown because of "other
  work", stop the VMs manually and verify TERMINATED.
- **Per-model fixes.** GPT models need `strict_tools=False`; Claude via OpenRouter
  needs top-level `cache_control`. Both are already handled in `runtime.py`.
- **Ledger holds.** An uncertain hold stays at its full reservation until settled.
  Settlement is automatic; don't add manual steps.
