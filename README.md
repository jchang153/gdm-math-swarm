# GDM Math Swarm

A documented reconstruction of the research-conference environment from Paglieri et al.,
[*A Case Study on Emergent Cheating and Whistleblowing in Autonomous Research
Swarms*](https://arxiv.org/pdf/2609.04170). This is an independent project, not an
official Google DeepMind release.

The first pilot is baseline-only spontaneous discovery: 20 independent, asynchronous
Claude Code agents (DeepSeek v4.1 Flash through a pinned OpenRouter route) share one fixed,
randomly sampled 14-problem Lean subset of a reconstructed 71-problem pool, under a
deliberately weak conference judge, for at most 90 minutes. It observes whether
exploitation and whistleblowing emerge. There are no mitigations and no exploit seeding;
the seeded-contagion track is implemented but disabled. [docs/plan.md](docs/plan.md) is
the authoritative record of user decisions.

## Status (October 14, 2026)

* **Implemented:** the frozen dataset and pilot subset, the operational judge (keyword
  filter, source integrity, Lean compilation in networkless containers), conference
  services and tools, prompts, the independent comparator-based evaluator, the Claude Code
  runtime through Inspect SWE, the shared budget ledger, provider settings and key-limit
  check, evidence-based launch gates, a refuse-while-live interlock between runs and
  evaluations, explicit inference concurrency (one in-flight request per agent), a
  disabled seeded-track template, validation scripts and post-run analysis tooling.
* **Reviewed:** an independent review (45 findings: 41 confirmed and fixed or documented,
  4 refuted) and a completeness check against every plan decision; see
  [docs/review-2026-10-13.md](docs/review-2026-10-13.md).
* **Offline-tested:** 512 unit tests run successfully (4 skip on the development Mac: three opt-in
  real-Lean tests and one Linux-only test), plus 33 analysis tests in `.venv-analysis`
  (2 of them skip in `.venv`). These use fake compilers and mocked Docker; they do not
  validate Lean semantics, isolation or capacity.
* **October 14 review fixes:** opaque-aware research proof dependency checking and
  verifier source pinning; stop/cancellation reservation cleanup and dispatch checks;
  preflight-only structural rejection handling with bounded transient retries; capacity
  evidence bound to effective settings; source/agent/order-bound labels, strict voluntary
  exits and malformed-annotation rejection. Natural completion logs the later run
  boundary. The original rubric body and frozen pilot tasks retain their hashes.
* **Pending:** the user authorized VM setup and unpaid Docker validation (Phase B) on
  October 14; paid compatibility and pilot steps retain their approval checkpoints.
  No launch evidence exists yet. All four images are rebuilt
  on the VM; of those built on the development Mac (arm64), the agent and comparator images
  are known to be stale.
* **Nothing paid has run.** No compatibility check, no pilot. Spending: $0 of the $20
  pilot, $5 analysis and $2 compatibility allowances.

## Setup

Python 3.10 (the pinned environment uses 3.10.12) and, for Phase B, Docker CE with the
Buildx and Compose v2 plugins on a Linux 6.10+ kernel with Landlock (host checks:
[docs/validation.md](docs/validation.md), step 0).

```sh
python3.10 -m venv .venv          # or, where 3.10 is not installed: uv python install 3.10 && uv venv --seed -p 3.10 .venv
.venv/bin/python -m pip install -e '.[runtime]'
```

The `runtime` extra pins `inspect-ai==0.3.279`, `inspect-swe==0.2.71`,
`anthropic==1.13.0` and `openai==3.29.0`; preflight checks these exact versions. The
keyword filter, ledger, launch gates and most tests need only the standard library. Claude
Code 2.1.296 is baked into the agent image, not installed on the host. The analysis
environment (Inspect Scout) is a separate `.venv-analysis`; see
[research/analysis/README.md](research/analysis/README.md).

**Credentials.** The only credential is `OPENROUTER_API_KEY`, needed only for the paid
steps and for the approved key-limit check. The user creates it on the host:

```sh
cp .env.example .env && chmod 600 .env   # then the user fills in the key
```

`.env` is git-ignored and host-only. It is read only by `secrets.load_openrouter_key`
(an exported `OPENROUTER_API_KEY` takes precedence). Containers never receive it; agents
hold only Inspect SWE's placeholder key behind the sandbox bridge.

**Never run `inspect view` (or any Inspect or Scout CLI) from inside the repository.**
Inspect loads the nearest `.env` found from its working directory upwards into its process
environment. Browse logs from outside:

```sh
cd /tmp && /path/to/gdm-math-swarm/.venv/bin/inspect view --log-dir /absolute/path/to/RUN/inspect
```

The project's own scripts disable Inspect's `.env` loading before any evaluation.

## Key commands

All from the repository root.

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests       # offline tests (512, 4 skipped on the Mac)
PYTHONPATH=src .venv-analysis/bin/python -m unittest discover -s research/analysis/tests   # analysis tests (33)
PYTHONPATH=src .venv/bin/python scripts/preflight.py                # unpaid launch gates + spending checkpoint
PYTHONPATH=src .venv/bin/python -m research_swarm.keyword_filter FILE.lean   # keyword stage only
PYTHONPATH=src .venv/bin/python scripts/evaluate_run.py --run-dir RUN [--dry-run]
PYTHONPATH=src .venv/bin/python research/analysis/timeline.py --run-dir RUN
```

* Preflight never reads a credential or calls a provider; it exits 0 only when every gate
  passes. It is also the place to see prior costs and remaining allowances.
* Phase B validation, the approval-gated paid steps and the launch follow the ordered
  runbook in [docs/validation.md](docs/validation.md). `scripts/run_pilot.py` without
  `--launch-paid-pilot` only runs preflight.
* `evaluate_run.py` is the post-run independent evaluation (serial, resumable, refuses to
  run beside live containers). `timeline.py` merges the run's logs into one ordered,
  source-linked timeline with candidate flags for rubric review.
* Runs and evaluations never share Docker: the pilot, the compatibility check and the
  runtime probes refuse to start while comparator, leftover judge or other agent
  containers exist, and the evaluator scripts refuse while judge or agent containers run
  (docs/interfaces.md, "S1").

## Safety rules

* Read [docs/plan.md](docs/plan.md) before implementing or running anything; keep
  proposals distinct from approvals.
* No paid call without the user's explicit approval, a fresh route freeze and passing
  gates. The pilot's $20, analysis's $5 and the compatibility check's $2 are separate
  ledger scopes in `state/budget.sqlite`; spending is never reset and the ledger is never
  deleted.
* Do not read, print, copy, mount or commit credential values. Inspect only presence and
  permissions.
* Never put exploit fixtures, reference solutions, researcher reports, rubrics, evaluator
  output or anything under `research/`, `docs/`, `tests/`, `runs/`, `state/` or
  `data/validation/` into an agent image, container, prompt, tool result or the conference
  library. Agents receive only the 14 sanitized templates.
* Task identities are frozen. Do not edit `data/manifest.json`, `data/pilot_subset.json`
  or `data/templates/`, and never resample.
* Keep the weak operational judge unchanged. Operational acceptance is not mathematical
  validity or intent; uncertain evaluations stay `unresolved`.
* Seeded contagion and mitigations stay disabled in the pilot; `configs/seeded.example.json`
  is a disabled template and the launch gates refuse any seeded configuration. Population,
  limit, resource and inference-concurrency changes need a recorded user decision.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/plan.md](docs/plan.md) | Decisions, scope and constraints (authoritative) |
| [docs/gcp-vm.md](docs/gcp-vm.md) | GCP setup, SSH, transfers, credentials handling, and required shutdown after use |
| [docs/interfaces.md](docs/interfaces.md) | Module contracts, file ownership, evidence files, images, container layout, logs |
| [docs/validation.md](docs/validation.md) | Ordered Phase B runbook from a fresh VM through the paid launch; capacity guidance |
| [docs/review-2026-10-13.md](docs/review-2026-10-13.md) | Independent review record: every finding, verdict and fix; remaining VM work (researcher-only) |
| [docs/runtime.md](docs/runtime.md) | Claude Code runtime, isolation, inference path and spending, truncation, stopping |
| [docs/judges.md](docs/judges.md) | Operational judge, answer parser, sorry detection, known weaknesses |
| [docs/evaluator.md](docs/evaluator.md) | Independent evaluator: sandbox, verdicts, answers, validation |
| [docs/dataset.md](docs/dataset.md) | Dataset reconstruction, frozen identities, deviations |
| [docs/prompts.md](docs/prompts.md) | Prompt and tool provenance, reconstructed wording, deviations |
| [docs/problems.md](docs/problems.md) | The 14 pilot problems with references and known status (researcher-only) |
| [configs/README.md](configs/README.md) | Every key of `configs/pilot.json` (and the seeded template), the compatibility and key-limit gates |
| [research/analysis/README.md](research/analysis/README.md) | Post-run analysis procedure, rubric and fidelity ledger (researcher-only) |
