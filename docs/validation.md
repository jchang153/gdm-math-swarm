# Phase B validation runbook (VM)

For the supplied GCP project, SSH, transfer, kernel setup, and standing requirement
to stop the VM after use, read [gcp-vm.md](gcp-vm.md) before remote operations.

Ordered procedure for host preparation, unpaid Docker validation, then the
paid steps up to the pilot launch. Initially written October 13, 2026;
see [the October 14 setup record](vm-setup-2026-10-14.md) for measured progress.

**VM setup and unpaid validation were authorized on October 14, 2026** (docs/plan.md,
"VM setup and unpaid validation authorized"). The earlier hold is lifted for those
steps. Spending (docs/plan.md, "One per-run budget, no manual approvals (October 20,
2026)"): every paid step is one run with its own budget, specified before it starts
(`run_budget_usd` in the config, $100 when absent; the behavior judge's run limit, $50 by
default), enforced by the ledger on every request. No step needs a typed confirmation, an
approval flag or an interactive terminal; stale pricing and provider-limit records are
refreshed automatically.

Conventions:

* Run every command from the repository root on the VM, with nothing else using Docker.
  Steps run **one at a time**: Lean timings, the capacity measurements and the evaluator
  all assume an otherwise idle Docker engine.
* `PYTHONPATH=src .venv/bin/python scripts/<script>.py` is the canonical form (the
  scripts also add `src/` themselves).
* Use `configs/pilot.json` (the default `--config`) for every step.
  `configs/seeded.example.json` is a disabled template whose seeded semantics await user
  decisions; preflight and the `approved_settings`/`seeded_track` gates refuse any seeded
  configuration for the approved pilot, enabled or not.
* Each validation script writes one evidence file `data/validation/<prefix>-<UTC>.json`
  (docs/interfaces.md, "Evidence files"). Gates read only the **newest** file per prefix,
  so a failed, partial or debugging run blocks its gate until a complete passing run is
  written after it. Never edit or delete evidence to pass a gate.
* To keep a console log, append `2>&1 | tee runs/phase-b/<step>.log` (create
  `runs/phase-b/` first; `runs/` is git-ignored). Every step, paid ones included, runs
  unattended (nothing prompts).
* **Refuse-while-live interlock** (docs/interfaces.md, "S1"). The scripts check running
  containers themselves (`docker ps` only; they never remove anything):
  * Runtime-side launchers (`run_pilot.py`, `compat_check.py`, `validate_runtime.py`,
    `isolation_probes.py`, `capacity_probe.py`) refuse to start while any comparator
    container (label `gdm-evaluator=1`), any leftover judge container (`gdm-judge=1`, in
    any state) or another run's Inspect agent sandbox is running, or when Docker cannot
    be listed. The three validation scripts accept `--force-while-live` for debugging
    only; their evidence then fails the required check `no_conflicting_containers_at_start`
    and cannot satisfy a gate. `run_pilot.py` and the paid compatibility check have no
    command-line override; only a config of the `gemini` budget track with
    `allow_concurrent_run: true` and a nonempty `concurrent_run_approval` note (the owner's
    Gemini run, `configs/gemini-collab.json`) turns another checkout's agent sandboxes and
    judge compilers into warnings recorded in the run's settings and the compatibility
    evidence (docs/runtime.md, "Launch interlock"). Running comparators, sandboxes of a run
    from this checkout and listing failures still block, and the launch lock
    (`state/launch.lock`) refuses a second pilot or compatibility check from this checkout.
  * Evaluator scripts (`validate_evaluator.py`, `evaluate_run.py`) refuse while judge or
    agent containers run (docs/evaluator.md, "Interlock"). Never use their
    `--force-while-live` for a gate record or a pilot evaluation.

  When a script refuses, list what is running and remove only leftovers you have
  identified (`docker rm -f <name>`), never containers of a run that is still going:

  ```sh
  docker ps -a --filter label=gdm-judge=1 --format '{{.Names}}\t{{.Status}}'
  docker ps -a --filter label=gdm-evaluator=1 --format '{{.Names}}\t{{.Status}}'
  docker ps --format '{{.Names}}\t{{.Image}}\t{{.Label "com.docker.compose.project"}}' | grep -E 'inspect-|gdm-math-swarm-agent' || true
  ```

## Overview

| # | Step | Writes | Gate | Duration (VM) | Paid |
| --- | --- | --- | --- | --- | --- |
| 0.1 | Host checks: architecture, kernel and Landlock, Docker | `runs/phase-b/host.txt` | — | ~10 min (longer if the kernel or Docker must be installed) | kernel change: agree with the user |
| 0.2 | Transfer the reviewed repository (clone, bundle, or sanitized copy) | — | — | minutes | — |
| 0.3 | Python 3.10 venv, offline tests | — | — | ~10 min | — |
| 0.4 | Analysis environment (`.venv-analysis`) | — | — | ~5 min | — |
| 1 | Build the four images, record IDs | `runs/build-logs/` | all image checks | ~45–90 min | — |
| 2 | `validate_judge.py` | `judge-<UTC>.json` | `judge` | ~13 min (script estimate) | — |
| 3 | `validate_dataset.py declarations` | `declarations-<UTC>.json` | `declarations` | ~15–30 min (estimate) | — |
| 4 | `validate_dataset.py timing` | `timing-<UTC>.json` | `timing` | ~5–15 min (estimate) | — |
| 5 | `validate_dataset.py reuse` | `reuse-<UTC>.json` | `reuse` | ~10–30 min (estimate) | — |
| 6 | `validate_conference.py` | `conference-race-<UTC>.json` | `conference-race` | ~5–10 min (estimate) | — |
| 7 | `validate_evaluator.py --pilot-self-compare` | `evaluator-<UTC>.json` | `evaluator` | ~2–4 h (estimate; unmeasured) | — |
| 8 | `validate_runtime.py` | `runtime-e2e-<UTC>.json` | `runtime-e2e` | ~12–17 min | — |
| 8a | Capture the effective instructions | `private/research/effective-instructions/` | — | minutes | — |
| 9 | `isolation_probes.py` | `runtime-isolation-<UTC>.json` | `runtime-isolation` | ~10–20 min | — |
| 10 | `capacity_probe.py` (choose `judge_max_concurrent`, re-run) | `runtime-capacity-<UTC>.json` | `runtime-capacity` | ~20–30 min per probe | config change via integrator |
| 11 | Interim preflight | — | report only | ~1 min | — |
| 12 | `check_provider_limit.py` (optional) | `provider-limit-<UTC>.json` | `provider_limit` (informational) | seconds | — (loads the key; unpaid) |
| 13 | `freeze_route.py` | `route-pricing-<UTC>.json` | `route_pricing` | seconds | — (public catalog, no key) |
| 13a | Freeze and record inference concurrency | config (already set) | `approved_settings` | minutes | report to the user |
| 14 | `project_cost.py` | stdout (projection) | — | seconds | — |
| 15 | `compat_check.py` | `compatibility-<UTC>.json` | `compatibility` | warm-up + ≤ 10 min | **PAID** (its own run budget) |
| 16 | Review the compatibility evidence | — | — | — | report to the user |
| 17 | `preflight.py` and spending checkpoint | — | all | ~1 min | shown to the user |
| 18 | `run_pilot.py --launch-paid-pilot` (or `supervise_run.py`) | `runs/pilot-<UTC>/` | — | warm-up + ≤ 90 min + grace | **PAID** (its own run budget) |
| 19 | Reconcile, evaluate, analyze | `RUN/evaluations/`, `RUN/analysis/` | — | evaluation ~hours | reconciliation unpaid; the behavior judge has its own run budget |

Durations marked "estimate" have no measurement behind them; the others come from the
scripts or from docs/runtime.md. Record actual durations in the step logs.

## 0. Prerequisites

The VM is expected to be Ubuntu 24.04 LTS, x86_64 (arm64 is also supported). Do 0.1 first:
a kernel that cannot run the comparator sandbox would otherwise surface only at step 7,
hours in.

### 0.1 Host checks (before any long step)

Record the output (`mkdir -p runs/phase-b` after step 0.2, or keep it elsewhere and copy it
there):

```sh
uname -m                                    # x86_64 or aarch64 only
dpkg --print-architecture                   # amd64 or arm64
. /etc/os-release && echo "$PRETTY_NAME"    # expected: Ubuntu 24.04.x LTS
uname -r                                    # Linux 6.10 or later
cat /sys/kernel/security/lsm                # must contain: landlock
grep -E '^CONFIG_SECURITY_LANDLOCK=|^CONFIG_LSM=' "/boot/config-$(uname -r)"
python3 -c 'import ctypes; libc = ctypes.CDLL(None, use_errno=True); v = libc.syscall(444, None, 0, 1); print("landlock ABI", v if v >= 0 else "unavailable (errno %d)" % ctypes.get_errno())'
cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null   # record only
nproc; free -g; df -h /
```

The `python3` line asks the kernel for its Landlock ABI version
(`landlock_create_ruleset` with the version flag, syscall 444 on both architectures; the
system Python is fine for it).

**Kernel and Landlock.** The comparator entry refuses a Landlock ABI below 5
(docs/evaluator.md, "Sandbox"). ABI 5 needs Linux 6.10 or later with Landlock enabled.

* `uname -r` below 6.10, or ABI below 5: install a newer kernel **before anything else**.
  Ubuntu 24.04's GA kernel is 6.8. The HWE kernel
  (`sudo apt install --install-recommends linux-generic-hwe-24.04`, then reboot) provides a
  newer series, 6.11 or later depending on the point release. Cloud images use a
  provider-specific kernel flavour (the `uname -r` suffix, for example `-aws`, `-gcp`,
  `-azure`); install that flavour's newer kernel rather than `linux-generic`. A kernel
  change is a VM provisioning change: agree it with the user, then record the old and new
  `uname -r`.
* `landlock` missing from `/sys/kernel/security/lsm` while `CONFIG_SECURITY_LANDLOCK=y`:
  the active LSM list was overridden at boot. Add `landlock` to the `lsm=` list on the
  kernel command line (keep every existing entry and its order; Ubuntu's built-in default
  is in `CONFIG_LSM`) in `GRUB_CMDLINE_LINUX` of `/etc/default/grub`, run
  `sudo update-grub` and reboot. Again agree and record.
* ABI 5 lacks pathname UNIX-socket scoping (ABI 9, Linux 7.1); the comparator compensates
  with its seccomp profile, so ABI 9 is not required.
* AppArmor: record the value above; do not change AppArmor or other security settings to
  make a probe pass. Report a failing isolation or sandbox probe instead.

**Docker.** Install Docker CE from Docker's own apt repository
(https://docs.docker.com/engine/install/ubuntu/) with the packages `docker-ce`,
`docker-ce-cli`, `containerd.io`, `docker-buildx-plugin` and `docker-compose-plugin`.
Ubuntu's `docker.io` package (with separately packaged buildx and compose) and the snap
are untested; so is rootless mode (the cgroup-limit and isolation checks assume the
rootful engine). The development engine was 29.8.1 and the comparator's seccomp profile is
derived from moby `docker-v29.8.1`'s default profile: use engine 29.x and record the exact
version.

The scripts call plain `docker`, so the operator needs daemon access:
`sudo usermod -aG docker "$USER"`, log out and back in, and check that `docker ps` works
without `sudo`. Membership of the `docker` group is root-equivalent on the host; this is
the VM owner's choice.

```sh
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
docker buildx version          # BuildKit builder present
docker compose version         # Compose v2 plugin (Inspect's Docker sandbox uses it)
docker info --format 'arch={{.Architecture}} os={{.OSType}} kernel={{.KernelVersion}} cgroup={{.CgroupVersion}} driver={{.CgroupDriver}} cpus={{.NCPU}} mem={{.MemTotal}} engine={{.ServerVersion}} storage={{.Driver}}'
docker info --format 'memory={{.MemoryLimit}} swap={{.SwapLimit}} pids={{.PidsLimit}} cfs_quota={{.CPUCfsQuota}}'   # all true
df -h /var/lib/docker
```

* cgroup v2 is expected (the judge's measured variant reads `memory.peak`; the isolation
  probe also accepts v1). `swap=false` means no swap accounting: the agents'
  `memswap_limit` and the isolation check `cgroup_limits_match_compose` cannot hold; report
  it rather than editing the compose settings.
* **BuildKit.** `docker build` must use BuildKit: the comparator Dockerfile writes a file
  with an inline heredoc (`COPY <<'EOF'`), which the legacy builder rejects. Docker CE with
  `docker-buildx-plugin` uses BuildKit by default. Smoke test (no pull, no network):

  ```sh
  probe=$(mktemp -d)
  printf 'FROM scratch\nCOPY <<EOF /probe\nok\nEOF\n' | docker build -q -t gdm-buildkit-probe:local -f - "$probe" \
    && docker image rm gdm-buildkit-probe:local
  ```

**Architecture.** amd64 and arm64 are the only supported hosts. Never build for another
platform (`--platform`) or run images under emulation: timings and the seccomp ABI checks
would be meaningless. Consequences of the expected x86_64 VM:

* Nothing has ever been built or run on x86_64. The development Mac is arm64 (Docker VM
  kernel 7.0.14-linuxkit, cgroup v2, engine 29.8.1). Every image is rebuilt on the VM, and
  no evidence or image ID from the Mac is reused.
* The comparator seccomp profile has **never been loaded on x86_64**. Step 7b is the first
  check: the container must start without `EEXIST`, and the probe item
  `unix_socketcall_i386` must be denied (it is `not_applicable` on arm64; a kernel without
  IA-32 emulation reports `denied:signal11`).
* The comparator's Go builder is pinned by digest (`golang:1.24-bookworm@sha256:1a6d4452…`).
  If it does not resolve for amd64 in step 1, report it rather than substituting a base.

**Disk.** On the Mac each image reports 13.7–14.7 GB, almost all of it the shared deps base
(Mathlib and the support library). Plan for at least 60 GB free under `/var/lib/docker`
for images, build cache, run artifacts and snapshots (estimate).

**Memory and CPU.** The Mac's Docker VM (8 CPUs, 7.75 GiB) could not compile with a cold
page cache under the 2g judge cap within 120 s, and 20-agent capacity there is unproven.
See "Capacity" below for the arithmetic; the capacity probe (step 10) is the measurement
that decides.

**Network at build time only:** elan and Lean releases, the Mathlib cache, Ubuntu and
NodeSource apt, npm, GitHub (comparator, lean4export, landrun), Docker Hub and PyPI (step
0.3–0.4). Agent and judge containers run without network.

### 0.2 Transfer the repository

As of October 14, the repository is committed and privately published at
`https://github.com/jchang153/gdm-math-swarm`. Clone with authorized GitHub access, or
transfer a Git bundle/source archive over IAP without copying credentials. Include any
newer reviewed local setup decisions. The following sanitized working-tree copy remains
an alternative, excluding virtualenvs, credentials, run artifacts and build residue. From
the directory that contains `gdm-math-swarm/` on the Mac (`VM` is the user's SSH host):

```sh
# 1. Dry run: the only .env-like file listed must be .env.example.
rsync -avn --include='.env.example' --exclude='.env' --exclude='.env.*' \
  --exclude='.venv/' --exclude='.venv-analysis/' --exclude='runs/' --exclude='state/' \
  --exclude='*.egg-info/' --exclude='__pycache__/' --exclude='.DS_Store' \
  gdm-math-swarm/ VM:gdm-math-swarm/ | grep -E '(^|/)\.env|\.venv|(^|/)runs/|(^|/)state/'
# 2. The copy itself: the same command without -n.
rsync -av --include='.env.example' --exclude='.env' --exclude='.env.*' \
  --exclude='.venv/' --exclude='.venv-analysis/' --exclude='runs/' --exclude='state/' \
  --exclude='*.egg-info/' --exclude='__pycache__/' --exclude='.DS_Store' \
  gdm-math-swarm/ VM:gdm-math-swarm/
```

* The trailing slashes copy the directory's contents; keep them. macOS's `rsync` is
  `openrsync` (protocol 29); the options above are supported by it and by GNU rsync.
* This carries `data/vendor/`, `data/templates/`, `data/validation/` (only the stale route
  freeze and its catalog snapshot) and `research/fixtures/` (the validation scripts read
  the fixtures).
* **Never copied:** `.env` (the user creates it on the VM only when steps 12–18 need it),
  the Mac's `.venv`/`.venv-analysis` (they are macOS builds and break step 0.3), `runs/`,
  and `state/`. No ledger exists on the Mac (`state/` is absent); if `state/budget.sqlite`
  ever exists, stop: it must be carried to wherever paid runs execute and never deleted or
  reset.
* Git bundle/clone transfer is now authorized by the user's private publication and VM
  setup requests. Public publication and credential transfer remain unauthorized.

### 0.3 Python environment and offline tests

Python **3.10** is required to reproduce the validated environment (the Mac used
3.10.12; `pyproject.toml` allows `>=3.10`, but nothing was tested on 3.11 or 3.12).
Ubuntu 24.04 ships only Python 3.12, so install 3.10 separately. Recommended route,
[uv](https://docs.astral.sh/uv/):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh     # or: sudo apt install pipx && pipx install uv
uv python install 3.10                               # 3.10.12 to match the Mac exactly
uv venv --seed -p 3.10 .venv                         # --seed puts pip into the venv
.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 10), sys.version; print(sys.version)'
.venv/bin/python -m pip install -e '.[runtime]'      # inspect-ai 0.3.279, inspect-swe 0.2.71, anthropic 1.13.0, openai 3.29.0
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests     # Mac: 494 tests, 4 skipped
```

Other routes to a Python 3.10 interpreter work the same way once `python3.10` is on
`PATH` (`python3.10 -m venv .venv` instead of `uv venv`): the deadsnakes PPA
(`python3.10` and `python3.10-venv`), pyenv, or Miniforge (the Mac's route). Do not
use the system 3.12 for `.venv`; a different minor version is an environment change to
report.

On Linux the GNU `timeout`/`setsid` wrapper test also runs (skipped on the Mac). The three
`RealLeanTests` (`import Mathlib` under the 2g judge cap) need a built image and a warm
page cache; run them after step 2 if wanted:

```sh
GDM_REAL_LEAN_IMAGE=gdm-math-swarm-lean:local PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_lean.py -v
```

Only the four packages above are pinned; their dependencies are not locked. Keep a record:

```sh
mkdir -p runs/phase-b
{ .venv/bin/python --version; .venv/bin/python -m pip freeze; } > runs/phase-b/pip-freeze.txt
```

### 0.4 Analysis environment (for step 19's Scout scan)

The post-run Inspect Scout scan uses a separate, fully locked `.venv-analysis`
(research/analysis/README.md, "Analysis environment"). It is free and can be built now:

```sh
uv venv --seed -p 3.10 .venv-analysis               # or: python3.10 -m venv .venv-analysis
printf '*\n' > .venv-analysis/.gitignore            # self-ignoring; never commit the venv
.venv-analysis/bin/python -m pip install -r research/analysis/requirements-analysis.txt
PYTHONPATH=src .venv-analysis/bin/python -m unittest discover -s research/analysis/tests   # Mac: 25 tests OK
```

Never install `inspect-scout` into `.venv`. The lock was produced on macOS; if a pinned
package does not install on Linux, report it rather than loosening the lock.

## 1. Build the images

Each image has its own dedicated context. Never build from the repository root (its
`.dockerignore` sends nothing, so such a build fails rather than leaking files).

```sh
mkdir -p runs/build-logs
EMPTY=$(mktemp -d)        # an empty build context

# deps: context data/vendor. Estimate 25–45 min (Mac arm64: toolchain ~100 s, Mathlib
# cache download+unpack ~7 min, support-library build >14 min; network-dependent).
docker build --progress=plain -f docker/dependencies.Dockerfile \
  -t gdm-math-swarm-deps:local data/vendor 2>&1 | tee runs/build-logs/deps.log

# lean (judge): empty context. Under 1 min.
docker build --progress=plain -f docker/lean.Dockerfile \
  --build-arg LEAN_PROJECT_IMAGE=gdm-math-swarm-deps:local \
  -t gdm-math-swarm-lean:local "$EMPTY" 2>&1 | tee runs/build-logs/lean.log

# agent: empty context, built FROM deps (not from lean). Estimate 3–10 min.
docker build --progress=plain -f docker/agent.Dockerfile \
  --build-arg LEAN_IMAGE=gdm-math-swarm-deps:local \
  -t gdm-math-swarm-agent:local "$EMPTY" 2>&1 | tee runs/build-logs/agent.log

# comparator: context docker/. Estimate 10–30 min (comparator and lean4export Lean builds).
docker build --progress=plain -f docker/comparator.Dockerfile \
  --build-arg LEAN_PROJECT_IMAGE=gdm-math-swarm-deps:local \
  -t gdm-math-swarm-comparator:local docker 2>&1 | tee runs/build-logs/comparator.log
```

Record the IDs, and check that the three derived images sit on the deps image just built
(their layer lists must start with the deps layers):

```sh
for i in deps lean agent comparator; do
  printf '%s %s\n' "gdm-math-swarm-$i:local" "$(docker image inspect --format '{{.Id}}' gdm-math-swarm-$i:local)"
done | tee "runs/build-logs/image-ids-$(date -u +%Y%m%dT%H%M%SZ).txt"

deps_layers=$(docker image inspect --format '{{join .RootFS.Layers " "}}' gdm-math-swarm-deps:local)
for i in lean agent comparator; do
  case "$(docker image inspect --format '{{join .RootFS.Layers " "}}' gdm-math-swarm-$i:local)" in
    "$deps_layers"*) echo "$i: on current deps" ;;
    *) echo "$i: NOT on current deps - rebuild it" ;;
  esac
done
```

**On failure.** Network fetches (elan, Mathlib cache, NodeSource, npm, GitHub, Docker Hub)
are the usual cause: retry. Do not change any pin, Dockerfile or base to make a build
pass; report the failure. If `lake exe cache get` cannot fetch the Mathlib cache, stop:
building Mathlib from source takes hours and is a deployment change to report. If the
pinned Go builder digest does not resolve on x86_64, report it rather than substituting
another base. A `COPY <<'EOF'` parse error means the legacy builder ran: fix BuildKit
(step 0.1), not the Dockerfile.

**Invalidation.** Evidence is bound to image IDs. Rebuilding lean invalidates steps 2–6
and 8, 10; rebuilding agent invalidates 8–10 and 15; rebuilding comparator invalidates 7;
rebuilding deps requires rebuilding all three and redoing everything. After step 2 starts,
do not rebuild unless a step's owner asks for it.

## 2. Operational judge: `validate_judge.py`

```sh
PYTHONPATH=src .venv/bin/python scripts/validate_judge.py
```

* **What it does.** A page-cache warm-up compile (6g, 2 CPUs, 600 s), then every judge case
  sequentially through the production `judge_submission` and `DockerLeanCompiler` at the
  config's judge limits (120 s, 2g, 1 CPU), one judge container at a time, recording Lean
  seconds and memory. `documentation_probe` cases record the documented weaknesses and never
  count toward pass/fail.
* **Duration and resources.** About 13 minutes (script estimate); one container at a time.
* **Writes** `data/validation/judge-<UTC>.json` (`gdm-judge-validation/1`). **Gate:** `judge`
  (needs `complete: true` and `limits` equal to the config).
* **On failure.** Read `summary.failed`. A `timeout` on an early case usually means a cold
  page cache (check `warm_up` and `docker_vm`): re-run the whole script; never pass
  `--no-warm-up`, `--only` or `--skip-timeout` for the gate run. Leftover containers:
  remove them and re-run. Any genuine mismatch goes to the judge owner; the judge itself is
  not to be changed (plan row "Baseline judge weaknesses").

## 3. Template elaboration: `validate_dataset.py declarations`

```sh
PYTHONPATH=src .venv/bin/python scripts/validate_dataset.py declarations
```

* **What it does.** Static template checks (parser, keywords, single placeholder, answer
  field outside the editable regions, qualified name), then for each pilot template: the
  unmodified template must fail with exactly the placeholder error `Provide a complete
  proof.` and no admission warning, and a researcher-only strict variant (autoImplicit off,
  `sorry` proof, name probe) must elaborate with exactly one admission warning and resolve
  the manifest name. Warm-up first.
* **Duration and resources.** About 15–30 minutes (estimate: 28 serial compiles, each paying
  the Mathlib import floor); one judge-sized container at a time. `--all` (71 tasks) is
  optional and not needed for the gate.
* **Writes** `declarations-<UTC>.json` (`gdm-declarations-validation/1`). **Gate:**
  `declarations` (complete, at the judge limits).
* **On failure.** A template that does not elaborate is an extraction defect: report it to
  the integrator with the diagnostic. Never edit `data/templates/`, `data/manifest.json` or
  `data/pilot_subset.json`; a pilot-template correction changes pinned hashes and needs a
  user decision (docs/dataset.md, "Corrections").

## 4. Compile timing: `validate_dataset.py timing`

```sh
PYTHONPATH=src .venv/bin/python scripts/validate_dataset.py timing
```

* **What it does.** Three workloads (the unmodified Green32 template, the genuine Green32
  proof, the genuine Erdős 23 proof): one first run, the warm-up, then 3 warm repeats each,
  recording Lean seconds, max RSS and cgroup memory peak at the judge limits.
* **Duration and resources.** About 5–15 minutes (estimate). `--concurrency N` adds an
  optional burst; it is heavy and not needed for the gate.
* **Writes** `timing-<UTC>.json` (`gdm-compile-timing/1`). **Gate:** `timing`.
* **On failure.** If warm compiles of the genuine proofs approach 120 s, report the numbers:
  changing the 120 s limit or the judge resources needs user approval (plan row
  "Compilation time limit").

## 5. Dependency reuse: `validate_dataset.py reuse`

```sh
PYTHONPATH=src .venv/bin/python scripts/validate_dataset.py reuse
```

* **What it does.** For each pilot target, searches the imported environment for constants
  of the same type (and, for `answer(_) ↔ P` targets, of type $P$ or $\neg P$), same-named
  constants and template-local dependencies, plus a source grep.
* **Duration and resources.** About 10–30 minutes (estimate).
* **Writes** `reuse-<UTC>.json` (`gdm-dependency-reuse/1`). **Gate:** `reuse` (passes when
  every probe completes; `complete`/`limits` are recorded but not gated).
* **On failure / findings.** A probe that does not complete fails the gate: re-run. A target
  with `directly_matched_in_dependencies: true` is not a failure: report it, because the plan
  requires documenting targets already established by the pinned dependencies.

## 6. Conference arbitration: `validate_conference.py`

```sh
PYTHONPATH=src .venv/bin/python scripts/validate_conference.py --threads 4 --max-concurrent 2
```

* **What it does.** Real-Lean race: 4 simultaneous genuine Green32 submissions (exactly one
  `accepted`), a later `problem_closed`, library publication and credit display, a
  statement-changing fixture whose outcome mirrors the judge, the evaluator queue observer
  with no output leaking to actions, `events.jsonl` agreeing with SQLite, and no leftover
  judge containers. Artifacts in `runs/validation-conference-<UTC>/`.
* **Limits.** The judge limits (Lean timeout, memory, CPUs) come from `--config` (default
  `configs/pilot.json`: `compilation_timeout_seconds`, `judge_memory`, `judge_cpus`) and
  the Lean image from its `lean_image`.
* **Duration and resources.** About 5–10 minutes (estimate); at most 2 judge containers at a
  time.
* **Writes** `conference-race-<UTC>.json` (`gdm-conference-race-validation/1`), with
  `complete` and `limits {timeout_seconds, memory, cpus}` like the judge records (S4).
  **Gate:** `conference-race` (all required checks, `complete: true`, `limits` equal to the
  config's judge limits; lean image checked when recorded). `--fake-lean`, a
  statement-changing fixture that could not be submitted, or `--timeout`/`--memory`/`--cpus`
  values that differ from the config make the record incomplete or off-limits; it then
  blocks the gate.
* **On failure.** Report to the conference workstream with the run directory.

## 7. Independent evaluator: `validate_evaluator.py`

Run with nothing else on Docker; the script refuses while judge or agent containers run.
Never use `--force-while-live`.

```sh
# a. No Docker: plan and static checks.
PYTHONPATH=src .venv/bin/python scripts/validate_evaluator.py --offline-plan
# b. Early x86_64 smoke test: sandbox probe + one fixture. Debug record only
#    (runs/validation-evaluator/evaluator-debug-<UTC>.json); never a gate record.
PYTHONPATH=src .venv/bin/python scripts/validate_evaluator.py --cases green32_genuine --no-upstream
# c. Gate run: probe, all 22 fixtures, 2 advisory upstream cross-checks,
#    and 14 pristine + 8 answer-bridge self-comparisons.
PYTHONPATH=src .venv/bin/python scripts/validate_evaluator.py --pilot-self-compare
# Resume after an interruption (same image, pins, resources, evaluator.py, fixtures):
PYTHONPATH=src .venv/bin/python scripts/validate_evaluator.py --resume data/validation/evaluator-<UTC>.json [--rerun-failed]
```

* **Duration and resources.** Each comparator container uses up to 6g and 2 CPUs, one at a
  time, and imports Mathlib four times per evaluation. About 40 container runs: roughly
  2–4 hours (estimate; no complete measurement exists). The record is rewritten after every
  case, so an interruption loses nothing. While it runs, the runtime-side scripts and the
  pilot refuse to start (S1).
* **Writes** `evaluator-<UTC>.json` (`gdm-evaluator-validation/3`). **Gate:** `evaluator`
  (record accepted by `evaluator.validation_status()`, current comparator image, and a
  passing self-comparison for every pilot template plus the bridge for the 8 answer-based
  ones).
* **On failure.** A probe failure stops everything: check the kernel (Landlock ABI ≥ 5),
  `EEXIST` at container start (seccomp load) and `unix_socketcall_i386`; report to the
  evaluator owner and do not relax the profile. A stale-entry message means the comparator
  image predates `docker/comparator_entry.py`: rebuild it (step 1). Failed cases: resume
  with `--rerun-failed`; persistent ones go to the evaluator owner. Any edit to
  `src/research_swarm/evaluator.py` requires a full re-run.

## 8. Runtime end-to-end: `validate_runtime.py`

```sh
PYTHONPATH=src .venv/bin/python scripts/validate_runtime.py
```

* **What it does.** Three genuine Claude Code peers with scripted `mockllm` outputs, through
  the production solver, budget filter (throwaway ledger, scope `validation`), barrier,
  snapshots, instruction archive, conference tools, real judge, notebooks, direct messages,
  `sandbox_file` rejections, the 120 s Bash clamp, two backgrounded slow compiles that the
  `lake` wrapper must kill at about 120 s, compaction (bracketing Claude Code's
  auto-compaction trigger, recorded as `compaction_threshold_tokens`; informational),
  truncation, recovery exhaustion and a voluntary exit. Image pinning, warm-up and the
  compose binding run as in the pilot. No provider is contacted; Inspect's `.env` loading
  is disabled first.
* **Duration and resources.** About 12–17 minutes; 3 agent containers, 1 judge container,
  the warm-up.
* **Writes** `runtime-e2e-<UTC>.json` (`gdm-runtime-e2e/1`). **Gate:** `runtime-e2e` (every
  `REQUIRED_EVIDENCE_CHECKS` name passed, `compose_sha256` equal to the fingerprint of the
  current config and agent image). `--skip-background-lean-check`, `--no-warm-up` and
  `--force-while-live` are for debugging only; a record without
  `background_lean_killed_at_120s` cannot pass.
* **On failure.** Report the failed check names and `runs/validation-runtime-<UTC>/` to the
  runtime workstream. `credentials_in_environment: true` means `OPENROUTER_API_KEY` is
  exported in your shell: unset it for unpaid steps.

## 8a. Capture the effective instructions

Plan row "Effective agent system prompt" requires archiving what the agents actually
receive: Claude Code's built-in system prompt with the conference prompt appended, plus the
tool definitions. Step 8 produces the first capture from the final agent image under
`runs/validation-runtime-<UTC>/instructions/<agent>-<sha16>.json` (check
`effective_instructions_archived` in the evidence).

```sh
mkdir -p private/research/effective-instructions
cp -R runs/validation-runtime-<UTC>/instructions private/research/effective-instructions/runtime-e2e-<UTC>
```

`research/` is researcher-only and never in a build context; `runs/` is git-ignored and may
be cleaned. Then hand the path to the prompts owner, who follows docs/prompts.md,
"Effective harness prompt" (**Capture**): link the archived file and replace the
descriptive built-in-content table with what was actually sent; the fidelity ledger row F4
is updated to match. The capture comes from a `mockllm` run; every pilot run archives its
own copy under `RUN/instructions/`, which is compared after the run (model-dependent text
can differ).

## 9. Isolation: `isolation_probes.py`

```sh
PYTHONPATH=src .venv/bin/python scripts/isolation_probes.py
```

* **What it does.** Two peers held alive while a side task probes each container as uid
  1000: egress, Docker sockets and host mounts, provider keys, repository or researcher
  content, read-only dependency and system paths and the wrappers, privileges (capabilities,
  `NoNewPrivs`, seccomp), cgroup limits equal to the compose file, `sandbox_file` escapes,
  cross-agent reachability, absent web/nested-agent tools, and a networkless judge
  container.
* **Duration and resources.** About 10–20 minutes; 2 agent containers and 1 judge probe
  container.
* **Writes** `runtime-isolation-<UTC>.json` (`gdm-runtime-isolation/1`). **Gate:**
  `runtime-isolation` (all checks for at least 2 agents, compose binding).
* **On failure.** Any isolation failure blocks all agent runs. Report it; do not proceed to
  step 10.

## 10. Capacity: `capacity_probe.py`

```sh
# Exploratory probe at the shipped config (judge_max_concurrent null = unbounded):
PYTHONPATH=src .venv/bin/python scripts/capacity_probe.py --duration 600
# Exploratory probe at a candidate judge concurrency N (none/null/0 = unbounded):
PYTHONPATH=src .venv/bin/python scripts/capacity_probe.py --duration 600 --judge-max-concurrent N
# Gate run, after configs/pilot.json holds the chosen value (no flags: it reads the config):
PYTHONPATH=src .venv/bin/python scripts/capacity_probe.py --duration 600
```

* **What it does.** `agent_count` (20) genuine peers, each repeatedly compiling a template
  locally and submitting it to the real judge for `--duration` seconds after the common
  start. Measures Docker memory and CPU, OOM kills, local and judge compile times against
  120 s, judge queue waits, bridge exec latency, Inspect subprocess-limiter saturation,
  ledger contention and startup times. Safe to interrupt (a partial record marked
  `interrupted` is still written).
* **Duration and resources.** About 20–30 minutes per probe; the whole VM (20 agent
  containers plus judge containers, warm-up first). The probe uses `mockllm`, so it does not
  exercise inference concurrency (step 13a).
* **Writes** `runtime-capacity-<UTC>.json` (`gdm-runtime-capacity/1`). **Gate:**
  `runtime-capacity`: all required checks, compose binding, and `validated_config` equal to
  the config for `agent_count`, `agent_memory`, `agent_cpus`, `judge_max_concurrent`,
  `judge_memory`, `judge_cpus`, `compilation_timeout_seconds`.
* **Required checks:** `completed_without_interrupt`, `all_agents_ran`, `no_oom_kills`,
  `no_compile_timeouts` (local and judge), `all_agents_reached_barrier_within_startup_timeout`
  (600 s, measured from evaluation start), `compose_matches_fingerprint`,
  `judge_environment_ok`, `bridge_exec_latency` (p95 ≤ 10 s), `memory_headroom` (peak ≤ 0.9
  of VM memory), `budget_ledger_contention`. `lean_warm_up` is informational.
* **Choosing `judge_max_concurrent`.** See "Capacity" below. The value must come from these
  measurements, the integrator records it in `configs/pilot.json`, and the probe is then
  re-run with that same value so the **newest** capacity record measures exactly the config.
  Any exploratory probe run after the gate run replaces it as the newest record.
* **On failure.** Report the measurements to the user. Do not raise `--startup-timeout`
  above the config, change agent memory, or reduce `--agents` to make a probe pass: those
  are configuration or population changes that need a user decision first.

## 11. Interim preflight (unpaid)

```sh
PYTHONPATH=src .venv/bin/python scripts/preflight.py
```

It reads evidence, the ledger (read-only), local image IDs and running containers; it never
reads a credential. At this point every evidence gate from steps 2–10 should pass, and so
should `approved_settings` (including `max_connections` equal to `agent_count`),
`seeded_track` and `run_budget`. Expected remaining blockers: `compatibility` and
`credential_presence` (until the user creates `.env`). A route-pricing freeze older than 72
hours is only a warning (the paid launchers refresh it automatically); step 13 refreshes it
by hand. Anything else: fix before continuing.

## 12. Provider-side key limit (optional, informational)

The run's budget is the spending control; the key's provider-side OpenRouter limit is only
recorded. The **user** creates the host `.env` (`cp .env.example .env && chmod 600 .env`, key
filled in by the user; never read, printed or copied by an agent). Every paid launch records
the limit of the key it loads automatically; to record it now:

```sh
PYTHONPATH=src .venv/bin/python scripts/check_provider_limit.py
```

* Unpaid (`GET /api/v1/key`), but it loads the credential in this process. Writes
  `provider-limit-<UTC>.json` with numbers, informational checks and warnings only (a key
  without a limit, a resetting limit or a remaining amount above the run's budget is a
  warning, never a refusal).

## 13. Route re-freeze

```sh
PYTHONPATH=src .venv/bin/python scripts/freeze_route.py
```

Unpaid; fetches OpenRouter's public endpoint catalog for `deepseek/deepseek-v4.1-flash`,
reads no credential, validates the `deepseek` route against `settings_from_config` (maximum
time-dependent prices, extra price components, context, tool and reasoning support), and
archives the catalog under `data/validation/catalog-snapshots/`. Writes
`route-pricing-<UTC>.json`. **Gate:** `route_pricing`. Steps 15 and 18 re-run it
automatically when the newest record is older than 72 hours or was made for other settings.
**On failure** (`valid: false`): the route's prices or parameters changed, so the config's
reservation prices no longer bound them; the gate blocks until `ProviderSettings` are
updated (report to the user).

## 13a. Inference concurrency: freeze and record

The plan requires concurrency to be frozen before launch (rows "Remaining model-setting
authority" and "Model settings to freeze during integration"); the implementation choice is
one inference connection per agent (plan checklist item 4, "Scheduling").

* `configs/pilot.json` key `max_connections` is **required** and set to 20: one in-flight
  model request per agent, so no agent waits behind another in a shared client queue
  (S2). There is no default.
* Check it: `preflight.py` (step 11 or 17) must show `approved_settings` passing; that gate
  and runtime preflight both require `max_connections` present and equal to `agent_count`.
  The launched run records the value in `RUN/settings.json`.
* Record it with the step logs, and tell the user before step 15 what is **not**
  measured: the compatibility check runs one agent and the capacity probe uses a mock
  model, so nothing shows that the pinned route accepts 20 concurrent requests. Claude Code
  retries rate-limit and server errors at most 4 times per request
  (`CLAUDE_CODE_MAX_RETRIES=4`); an agent whose retries run out ends as
  `harness_completed_after_provider_error` and does not come back (docs/runtime.md). During
  the pilot, unbilled provider errors appear as `inference_unbilled` in `RUN/runtime.jsonl`.
* Never lower the value to pass a gate or after a failure. A different value (which
  would queue requests across agents) needs a recorded decision in docs/plan.md and a
  matching gate change by the integrator.

## 14. Cost projection

```sh
PYTHONPATH=src .venv/bin/python scripts/project_cost.py [--rates 0.5,1,2]
```

Unpaid. Uses the request sizes measured in the newest runtime-e2e run directory (the
per-request floor: Claude Code's built-in prompt, the appended conference prompt, tool
schemas), the compaction threshold recorded by step 8 (`compaction_threshold_tokens`; the
160,000-token assumption only if none was recorded) and explicit **assumptions** (turn
rate, output and tool-result tokens, cache hit rate) to project `agent_count` agents × 90
minutes against the run's budget (`run_budget_usd`, else $100). `assumption_sources` in the
output names where each input came from. It is a projection, not a measurement, and never
required; `--output` may write JSON outside `data/validation/`.

## 15. Live compatibility check — PAID (its own run budget)

Preconditions: steps 8–9 pass on the current agent image and nothing else is running on
Docker. The check's budget is `run_budget_usd` of the config it reads (default
`configs/pilot.json`; $100 when absent); with 12 requests at most it spends a few dollars at
most.

```sh
PYTHONPATH=src .venv/bin/python scripts/compat_check.py
```

* Before spending, it refreshes the route-pricing freeze when needed (unpaid) and refuses
  unless `run_budget_usd` is valid; unless no comparator, leftover judge or other agent
  sandbox container is present (S1; no command-line override, only the config's recorded concurrent-run override); and unless the gates `fixed_task_manifest`,
  `frozen_dataset`, `approved_settings`, `run_budget`, `route_pricing`, `credential_presence`,
  `runtime-e2e`, `runtime-isolation` pass. It prints the budget, registers the run
  `compat-<UTC>` in the config's ledger (a budget track's own, if any) and starts; nothing prompts.
* Then: warm-up, one researcher-side reasoning-cap probe (about $0.002, `max_tokens`
  1024), and one agent on the frozen route for a few turns. Aborts after 12 admitted
  requests, 10 minutes after the first inference, the first provider 4xx, tool-call parse
  failure or output error, an estimate violation among its own requests, or when its run
  budget refuses a request.
* **Writes** `compatibility-<UTC>.json` (`gdm-compatibility/1`) and `runs/compat-<UTC>/`.
  **Gate:** `compatibility` (required checks `tool_call_parsed`, `conference_tool_call`,
  `reasoning_replay_accepted`, `provider_cost_reported`, `completion_within_max_tokens`,
  `reasoning_cap_enforced`, `no_estimate_violation`, `route_pinned`, `no_sample_error`;
  `truncation_observed` and `compaction_observed` are informational).
* **Uncertain holds** count against the check's own run and never block anything. To settle
  them from OpenRouter's unpaid generation records (it loads the key only when a hold has a
  generation id):

  ```sh
  PYTHONPATH=src .venv/bin/python scripts/reconcile_ledger.py --run-dir runs/compat-<UTC>
  ```

  `--dry-run` only lists. A hold without a generation id is settled with
  `--manual REQUEST_ID COST [--note TEXT]` once its charge is identified.
  For a budget track's check or run, add `--ledger state/<track ledger>` (e.g.
  `state/gemini-budget.sqlite`; the supervisor does); the accepted generation model then defaults
  to the track's (`provider.TRACK_MODEL_IDS`).
* **On failure.** A failed required check is a material incompatibility to bring to the
  user (plan row "Remaining model-setting authority").

## 16. Review the compatibility evidence

Report the compatibility evidence to the user. No attestation key is set: the
`compatibility` gate checks the evidence itself. (Historical, October 14-15: the user
authorized the main unlimited OpenRouter key and the Fireworks US check; since October 20,
2026 the provider-side limit is recorded for every key and never enforced.)

## 17. Preflight and spending checkpoint

```sh
PYTHONPATH=src .venv/bin/python scripts/preflight.py
```

Show the user the spending checkpoint it prints (the run budget the config sets, and the
ledger's known costs and unresolved holds per scope and recent runs) and the gate list.
No blocker should remain.

## 18. Paid pilot launch — PAID (its own run budget)

1. Set the run's budget before the run: `run_budget_usd` in the config (absent means $100).
2. Confirm nothing else uses Docker. The runtime's preflight refuses while any comparator,
   leftover judge or other agent sandbox container is present (S1); `run_pilot.py` has no
   override. Remove only identified leftovers; never start an evaluation or validation
   during the run.
3. Launch (or use `scripts/supervise_run.py` for an unattended run, private/docs/gcp-vm.md):

   ```sh
   PYTHONPATH=src .venv/bin/python scripts/run_pilot.py --launch-paid-pilot
   ```

   It refreshes the route-pricing freeze when needed, prints the gates, the run's budget and
   the log locations, and starts; nothing prompts. The runtime re-runs preflight, pins
   images, registers the run and its budget in the ledger, warms the page cache, loads the
   key, records its provider-side limit (informational) and starts 20 peers. The run lasts at
   most `max_run_seconds` from the common start, plus up to 120 s in-flight grace and 180 s
   stop grace. Then start the near-live website mirror on your own computer
   (`PYTHONPATH=src .venv/bin/python scripts/live_mirror.py --vm VM_NAME`; private/docs/gcp-vm.md).
4. During the run, watch `RUN/runtime.jsonl`, `RUN/conference/events.jsonl`, `docker stats`
   and `scripts/monitor_run.py`; do not start other Docker work. The ledger stops new
   requests when the run's remaining budget cannot cover them.

## 19. After the run

```sh
PYTHONPATH=src .venv/bin/python scripts/reconcile_ledger.py --run-dir runs/pilot-<UTC>       # settles from generation records
PYTHONPATH=src .venv/bin/python scripts/evaluate_run.py --run-dir runs/pilot-<UTC> --dry-run
PYTHONPATH=src .venv/bin/python scripts/evaluate_run.py --run-dir runs/pilot-<UTC>           # serial, resumable
PYTHONPATH=src .venv/bin/python research/analysis/timeline.py --run-dir runs/pilot-<UTC>
PYTHONPATH=src .venv-analysis/bin/python research/analysis/scanners.py --run-dir runs/pilot-<UTC>   # optional, free (step 0.4)
```

Report actual costs to the user. Compare `RUN/instructions/` with the step 8a capture.
Browse Inspect logs only from outside the repository
(`cd /tmp && <repo>/.venv/bin/inspect view --log-dir <absolute RUN>/inspect`): Inspect
loads any `.env` it finds above its working directory. Follow research/analysis/README.md
for the rubric review. The automatic behavior judge after a supervised run (config `auto_behavior_judge`) is,
by default, the simplified judge (`scripts/run_simple_judge.py`, budget `--run-limit` from
`simple_judge_max_usd`, else $10, into `RUN/analysis/simple_judge/`); the rubric-3.1 judge
(`scripts/run_transcript_judge.py`) is opt-in with `behavior_judge: "full"` and runs with its own
budget (`--run-limit`, else `behavior_judge_max_usd`, else $50); `behavior_judge: "off"` runs
neither (configs/README.md).

## Capacity

Measured on the development Mac (Docker VM: 8 CPUs, 7.75 GiB; arm64):

| Observation | Value |
| --- | --- |
| Import of `FormalConjectures.Util.ProblemImports` (all of Mathlib), warm page cache | ≈ 22–24 s wall (16 s user), under 2g / 1 CPU |
| Same import, cold page cache | ≈ 88 s (5.8 GB of block reads) |
| Cold compile under the 2g judge cap | exceeded 300 s (page-cache thrashing) |
| Core-only file (`import Lean`) | 7.9 s Lean |

Consequences:

* Every judge compile and every agent `lake env lean` check pays the import floor, and each
  evaluator evaluation pays it four times. Unmodified templates fail just after the import;
  real proofs take longer.
* The Mathlib `.olean` files (≈ 6 GB) must stay in the VM page cache throughout a run. The
  runtime warms it before the start barrier (6g, 2 CPUs); a VM or engine restart during a
  run, or memory pressure that evicts the cache, pushes compiles toward the 120 s limit. A
  timeout is never evidence of fraud, but it changes acceptance.
* Configured memory ceilings at 20 agents: $20 \times 2\,\text{GiB}$ for agents plus
  $N \times 2\,\text{GiB}$ for $N$ concurrent judge containers, alongside the ≈ 6 GB page
  cache. With `judge_max_concurrent: null` (unbounded, as shipped) $N$ is limited only by
  how many submissions arrive at once. The probe's `memory_headroom` check requires the
  measured peak to stay at or below 0.9 of the VM's memory.
* The capacity probe's mock agents respond instantly, so its Lean load is more concentrated
  than a live run's, where inference latency spreads compiles out. In the other direction,
  it compiles unmodified templates, and real proofs may compile for longer (the probe
  records both limitations).

The first GCP probe at `1g` per agent caused container memory kills despite ample
host memory. The user approved `2g` per agent on October 14; runtime, isolation and
capacity evidence must all be regenerated at this compose setting. Judge limits stay
at `2g` / 1 CPU / 120 s. See private/docs/vm-setup-2026-10-14.md for the measurements.

Choosing `judge_max_concurrent`:

1. Run exploratory probes (step 10) at the shipped value and at candidate values $N$.
   Compare `judge_containers_max`, `judge_compile_seconds` and `judge_queue_seconds`
   percentiles, `cpu_percent_total`, `loadavg_max` and the memory peak. As a starting point,
   $N$ should keep $N \times 2\,\text{GiB}$ within the memory left after the agents' measured
   peak and the page cache, and $N$ judge CPUs within the CPUs the agents leave free. Queue
   wait does not count against the 120 s limit, but delays results.
2. Report the measurements; the integrator records the chosen value in
   `configs/pilot.json` (keep `null` only if an unbounded probe passed and that choice is
   recorded).
3. Re-run the probe with no override so it measures the configured value; that record must
   be the newest `runtime-capacity` file.

If the probe uses a non-default `--max-subprocesses`, set the same `max_subprocesses` in the
config (it is recorded, not gated).

**Population changes.** The user has said they may reduce the population if capacity is
insufficient, but no number is decided (plan row "Capacity adjustment authority"). A
different population requires, in order: a recorded decision in docs/plan.md (including
whether the fixed 14-task subset still applies, since the plan fixes it for 20-agent
experiments); `runtime.APPROVED_AGENT_COUNT` updated (the launch gates' approved
`agent_count` is derived from it); `configs/pilot.json` `agent_count`, `persona_counts`
(the balanced allocation from `runtime.assign_personas`) and `max_connections` (equal to
the new count); and a new capacity probe at that value. `project_cost.py` reads
`agent_count` from the config (`--agents` overrides); note that `compat_check.py` derives
its default persona from the 20-agent allocation (`--persona` overrides). Never reduce the population or introduce
synchronized scheduling silently.

## General failure rules

* Every failure is reported with its evidence file and run directory; the owning workstream
  (docs/interfaces.md, "File ownership") fixes code. A code fix to a component invalidates
  the evidence that exercised it; re-run those steps.
* Never edit frozen data, evidence files or the ledger to pass a gate; never use
  `--force-while-live`, `--fake-lean`, `--only`, `--skip-*`, `--tasks` or limit overrides for
  a gate run.
* Changing the 120 s limit, judge or agent resources, the population, the model route,
  inference concurrency or any approved setting needs a user decision recorded in
  docs/plan.md first. A run's budget (`run_budget_usd`) is specified before each run and is
  not such a setting.
* Unset `OPENROUTER_API_KEY` in the shell for unpaid steps; only steps 12, 15, 18 and the
  reconciliation of holds with generation ids load the key.
