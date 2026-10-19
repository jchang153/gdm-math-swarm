# Agent runtime and unpaid validation

Updated October 13, 2026. Status labels: **validated** (offline tests or a recorded
run), **static** (read from pinned sources or the pinned binary, not yet exercised at
run time), **pending** (Phase B script written, not yet run; or awaiting a user-approved
paid check). The user placed an execution hold on Docker validation runs, capacity
probes and paid calls; the Phase B scripts below are ready for the forthcoming VM.

## What runs

Experimental peers run **genuine Claude Code**, not a replacement Python agent loop.
`runtime.run_pilot` builds one Inspect task with one sample per peer; each sample gets
its own Docker compose project and container (`runtime.write_compose`). The solver
`runtime.peer_solver` starts Claude Code through `inspect_swe.claude_code` with the
identity-bound conference MCP tools (`inspect_adapter.conference_tools`), the bridge
filter (`inspect_adapter.budget_filter`), a 5 s private-file observer
(`runtime.Snapshotter`) and the run-wide stop signal (`runtime.RunClock`). Agents do
not share contexts or files.

Pins (validated by `installed_versions()` in preflight): `inspect-ai==0.3.279`,
`inspect-swe==0.2.71`, `anthropic==1.13.0` (the bridge needs its request schemas even
for non-Anthropic models), `openai==3.29.0`; Claude Code `2.1.296` baked into
`gdm-math-swarm-agent:local` and launched with `version="sandbox"` (no CLI downloads).
Build the agent image **only from an empty context** (the Dockerfile has no COPY/ADD;
a repository context would send `.env` and `research/` to the daemon):

```sh
mkdir -p /tmp/empty-context && docker build -f docker/agent.Dockerfile \
  --build-arg LEAN_IMAGE=gdm-math-swarm-deps:local -t gdm-math-swarm-agent:local /tmp/empty-context
```

**Rebuild required (October 13, 2026 review fixes).** `docker/agent.Dockerfile` changed:
it now installs root-owned `lake`/`lean` time-limit wrappers in `/usr/local/bin`, puts
that directory first on `PATH`, and lists `util-linux` (for `setsid`) explicitly; see
"Local compilation limit" below. The agent image built earlier on this Mac
(`sha256:c0becf27…`, from deps `f41571fd`) predates this and must not be used. Rebuild
the agent image on the VM (from the same deps image as the judge image, with the
command above) before any Phase B step; the isolation probe fails on an image without
the wrappers, and every runtime evidence file is bound to the agent image ID. The base
image is still referenced by tag, so build instructions alone are not a
reproducibility guarantee.

**Image pinning** (**validated** offline; compose acceptance of an image ID is
**pending** Phase B): `runtime.pin_images(config)` resolves `agent_image` and
`lean_image` to local image IDs once, at run start (`docker image inspect`, never a
pull), and refuses to launch (`LaunchBlocked`) when a tag does not resolve. The IDs,
not the mutable tags, go into the compose file (`image: sha256:…`, `pull_policy:
never`) and into `DockerLeanCompiler` (`make_lean_compiler(config, image=<id>)`), so a
re-tag during a run cannot change what runs. `run_pilot` also refuses when an ID
differs from the one its preflight saw. Both tag and ID are recorded
(`images_pinned` in `runtime.jsonl`; `effective.images` in `settings.json`; `image_ids`
in every runtime evidence file). `validate_runtime.py`, `isolation_probes.py`,
`capacity_probe.py` and `compat_check.py` use the same pinning, so the e2e check
`compose_uses_pinned_image_id` plus its sample checks establish that Docker Compose
runs the ID-pinned file.

**Compose binding** (interface X3; **validated** offline, **pending** Phase B): the
runtime-e2e, runtime-isolation and runtime-capacity evidence record `compose_sha256`,
the SHA-256 of the compose file the probed agents actually ran, plus a required check
`compose_matches_fingerprint` that it equals `runtime.compose_fingerprint(config, agent
image ID)`. The launch gate compares `compose_sha256` with the fingerprint for the pilot
config and the current agent image, so a later change to the generated compose file,
`agent_memory`, `agent_cpus` or the agent image invalidates the evidence.
`validate_runtime.py` and `isolation_probes.py` therefore take the image tags and
`agent_memory`/`agent_cpus` from the pilot config (`--config`, default
`configs/pilot.json`), as `capacity_probe.py` already did.

**Credentials in the unpaid scripts** (interface X1; **validated** offline): Inspect's
`eval_async` would otherwise run `find_dotenv(usecwd=True)`/`load_dotenv` and load the
repository `.env` into the host process and every docker/compose/Lean child process.
`validate_runtime.run_eval` (used by all three Phase B scripts) calls
`inspect_adapter.disable_inspect_dotenv()` first and fails closed if it does not
confirm, and sets `INSPECT_DISABLE_MODEL_API=1` for the duration of the evaluation, so
Inspect refuses every model API except `mockllm`. The evidence no longer asserts a
constant `credentials_read: false`. It records `credentials_in_environment`, whether
`OPENROUTER_API_KEY` is set in the process environment when the file is written (after
the run), together with `credentials_in_environment_before_eval` and
`inspect_dotenv_disabled`. These are presence flags only; a key exported in the
operator's shell is inherited and shows up as `true`. Only
`secrets.load_openrouter_key` in the paid paths reads `.env`.

**Credentials and the provider-side limit in the paid paths** (interfaces X1 and X2;
**validated** offline, **pending** the paid runs): `run_pilot` and `compat_check` call
`disable_inspect_dotenv()` before anything Inspect runs (`runtime.build_task` calls it
again as a backstop). `runtime.jsonl` records `inspect_dotenv_disabled` and, after the
run, `credential_environment` (presence only); the compatibility evidence records
`credentials_in_environment`. `build_openrouter_model` loads the key and, before any
paid request, verifies its provider-side limit with an unpaid `GET /api/v1/key`
(`provider_limit.verify_key_limit`) against the CALLING scope's remaining allowance.
The limit must be set and must not reset, and `limit_remaining` must not exceed that
allowance. Otherwise `ProviderLimitError` is raised (`provider_limit_refused`) and
nothing is built. On success the key-free report is recorded as
`provider_limit_verified` and in settings.json. The compatibility check therefore
needs a key whose `limit_remaining` is at most the remaining $2 compatibility
allowance, and the pilot needs one at most the remaining $20 pilot allowance: use
separate keys or adjust the limit between the two steps (configs/README.md).

## Configuration read by `run_pilot`

`agent_count` (preflight requires `runtime.APPROVED_AGENT_COUNT` = 20, a single named
constant that changes only together with a recorded decision in docs/plan.md),
`persona_seed` (default 260904170, always written to `settings.json`), `agent_image`,
`lean_image`, `agent_memory` (`2g` in the approved pilot; helper default `1g`),
`agent_cpus`, `judge_max_concurrent`, `judge_memory` (`2g`), `judge_cpus` (1),
`compilation_timeout_seconds` (120), `startup_timeout_seconds` (600), `max_run_seconds`
(5400), `post_depletion_seconds` (600), `stop_grace_seconds` (180), `run_dir`,
`provider_route`, `provider_settings`, `system_prompt_mode`, `claude_auto_memory`,
`fixed_task_manifest`, `paid_launch_enabled`, `verified_integration`, `track` and the
**required** `max_connections` (no default); optional `max_subprocesses` (64),
`persona_counts` (when present, preflight requires it to equal the balanced
allocation), `warm_up_memory` (`6g`), `warm_up_timeout_seconds` (900) and
`in_flight_grace_seconds` (120).

* **Inference concurrency** (docs/plan.md checklist item 4, "one inference connection
  per agent"; **validated** offline): `max_connections` must be stated explicitly in
  the run config. `runtime.max_connections(config)` refuses (`LaunchBlocked`) when it
  is absent, null, not an integer or below 1, and preflight also blocks unless it
  equals `agent_count` (20 in `configs/pilot.json`: one in-flight request per agent, so
  Inspect never queues one agent's request behind another's; a different value needs a
  recorded plan decision, and the launch gate `approved_settings` checks the same).
  `run_pilot` passes exactly this value to `build_openrouter_model` (Inspect
  `GenerateConfig.max_connections`; an explicit value also switches off Inspect's
  adaptive connection controller) and records it as `effective.max_connections` in
  `settings.json` (the config is recorded there too). The one-agent compatibility
  check uses 1 (`COMPAT_MAX_CONNECTIONS`, recorded in its settings.json), and the
  capacity probe gives its mock model an explicit limit (`--max-connections`, default
  the config value) and records it.

* **Personas**: `assign_personas` gives every persona `count // 4` or `count // 4 + 1`
  agents for any count (5 each at 20), shuffled with the recorded seed; the allocation
  is recorded as `effective.persona_counts` (and in the preflight report).

* **Effective prompt** (approved October 19, 2026): `system_prompt_mode: "replace"`
  drops Claude Code's built-in system prompt and sends the conference prompt in its
  place (`--system-prompt`). `"append"` (`--append-system-prompt`) was the setting of
  the October 15-16 baselines;
  `claude_auto_memory: true` sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=0` (Inspect SWE
  disables it by default). Preflight blocks `"append"` and `false`.
* **Judge** (I1): `runtime.make_lean_compiler` builds `DockerLeanCompiler(image,
  timeout, memory, cpus, max_concurrent)`. The 120 s limit applies to the Lean process;
  queue wait at the FIFO gate is reported separately (`queue_seconds`). The concurrency
  value is to be set from the capacity probe.
* **Evaluator** (I2): `runtime.queue_observer` wires `evaluator.make_queue_observer(
  evaluator.run_evaluations_dir(run_dir))`, i.e. `RUN/evaluations/`. It runs no Lean during the run; post-run evaluation uses
  `scripts/evaluate_run.py`. If the function is missing, the run warns loudly, records
  `observer_unavailable`, and submissions remain in conference state.
* **Gates** (I3): `runtime.preflight` keeps design-invariant (population, spontaneous
  track with no `seeded` block at all, explicit `max_connections` equal to
  `agent_count`, prompt mode), pinned-package, module, Docker-engine and image checks,
  applies the launch interlock (below; no override), and merges `launch_gates.evaluate`
  (evidence files, route pricing, manifest, ledger,
  provider limit, the `compatibility` evidence, and the `verified_integration` /
  `paid_launch_enabled` attestations). A missing or failing gate module is itself a
  blocker. Preflight's own "Paid launch is disabled" is added only when the gates did
  not evaluate `paid_launch_enabled`, so the blocker appears once. Preflight never
  reads a credential or calls a model.
* **Ledger** (I4): `budget.DEFAULT_LEDGER_PATH(repo)` (`state/budget.sqlite`, never
  under `runs/`), scope `pilot`.

## Launch interlock

The reverse of the evaluator's interlock (docs/evaluator.md, "Interlock"; **validated**
offline with every Docker call faked, **pending** Phase B). Comparator containers (6g,
2 CPUs each), leftover judge compilers and another run's agents compete with a run's
judge compiles for memory, CPU and page cache, which can push compiles past the 120 s
limit and so change acceptance, credit and lockout, and they distort validation timing
and capacity measurements. `runtime.launch_interlock(agent_image)` lists, read-only
(nothing is started or removed), before the caller creates any container of its own:

| Kind | Source | State |
| --- | --- | --- |
| `comparator` | `docker ps --filter label=gdm-evaluator=1` (`evaluator.CONTAINER_LABEL`) | running |
| `judge` | `lean.judge_containers()` (label `gdm-judge=1`) | any (running or exited) |
| `agent` | `evaluator.live_experiment_containers()`: an Inspect sandbox compose project `inspect-<task>-i<id>`, or a container of the configured agent image by tag or ID | running |

Each kind found adds a blocker naming up to five containers; a listing that fails
(Docker not running) is itself a blocker. The status (`checked_at`, `containers`) is
kept in the preflight report (`docker.interlock`).

* `run_pilot` refuses through `preflight` (no override).
* `scripts/compat_check.py` refuses through `environment_check` before any credential
  is read (no override; it is a paid check).
* `scripts/validate_runtime.py`, `scripts/isolation_probes.py` and
  `scripts/capacity_probe.py` refuse in `main()` before the warm-up, judge or agents
  start (exit 2, no evidence). Only these unpaid validation scripts accept
  `--force-while-live` (debugging): the run proceeds after a loud warning, the
  evidence records `interlock` (blockers, containers, `forced`), and its required check
  `no_conflicting_containers_at_start` fails, so forced evidence never satisfies a
  launch gate.

## Experiment tracks (seeded contagion wired, never active in the pilot)

`runtime.conference_track_arguments(config)` (through `seeded.conference_arguments`)
decides what the conference service receives. A spontaneous config passes nothing (the
service runs the baseline exactly as before). The service receives `track="seeded"`
and a `SeededTrackConfig` only when `config["track"] == "seeded"`, `seeded.enabled` is
true and every semantic field (`seeded.SEMANTIC_FIELDS` plus the channel's own field)
is set; a seeded config that is disabled or incomplete (as `configs/seeded.example.json`
is, its semantics awaiting user decisions), or a spontaneous config that claims seeding
is enabled, is refused with `LaunchBlocked` and never falls back to the other track.
`make_conference` applies the result: the default factory forwards the arguments to
`ConferenceService`, and a factory that does not apply the seeded track is refused.
For the approved pilot, preflight (and the launch gates) additionally block any
`seeded` block, even a disabled one, and any track other than `spontaneous`, so the
seeded path cannot run until the user decides its semantics and the plan changes.

`RunClock.monitor` calls `conference.poll_seeded_trigger()` once per tick (1 s) after
the start barrier, before the depletion check, whenever the service has it. In the
spontaneous track the method returns at once, so the baseline is unchanged; in an
enabled seeded track a time-based trigger (`elapsed_seconds`) then fires on schedule
instead of waiting for the next agent action. A failing poll is recorded as
`monitor_error` (stage `seeded_trigger`, first failure of a streak and every 60th) and
never stops the monitor (**validated** offline, including a real seeded service whose
elapsed-time trigger fires from the monitor with no agent action).

## Lean warm-up before the start barrier

A cold Docker-VM page cache under the 2g judge cap exceeded the 120 s compile limit
(docs/judges.md). Before any agent container starts (and so before the first-inference
barrier), `run_pilot` calls `runtime.warm_up_lean`: one researcher-side compile of
`import FormalConjectures.Util.ProblemImports` on the pinned Lean image with its own
compiler instance (6g, 2 CPUs, 900 s limit), outside every agent, judge and ledger
accounting. VM uptime and page-cache figures (`/proc/uptime`, `/proc/meminfo`:
`MemTotal`, `MemFree`, `MemAvailable`, `Buffers`, `Cached`, `Active(file)`,
`Inactive(file)`) are read from a trivial no-network container before and after
(`runtime.vm_state`). The result is recorded as `lean_warm_up` in `runtime.jsonl` and
`effective.lean_warm_up` in `settings.json`. A failure is recorded and reported loudly
(warning plus stderr) but is not fatal: the warm-up is an optimization, not a gate.
`compat_check.py`, `validate_runtime.py` and `capacity_probe.py` run the same warm-up
(`--no-warm-up` skips it). **Validated** offline with fake compilers; the real effect
on page cache and compile times is **pending** Phase B (capacity probe).

## Local compilation limit

docs/plan.md "Compilation time limit" approves 120 s per Lean compilation for local
agent checks as well as submissions. Claude Code's Bash clamp
(`BASH_DEFAULT_TIMEOUT_MS`/`BASH_MAX_TIMEOUT_MS` = 120000) bounds only foreground Bash
calls. A compile started with the Bash tool's `run_in_background` parameter, or with
`nohup … &`/`setsid`, would otherwise run until the run stops. The agent image
therefore installs two root-owned, read-only wrappers, first on `PATH`
(`/usr/local/bin:/opt/elan/bin:…`):

```sh
#!/bin/sh            # /usr/local/bin/lake (lean is analogous)
PATH="/opt/elan/bin:$PATH"
export PATH
exec /usr/bin/timeout -v -s KILL 120 lake "$@"
```

* **Why the `lake` wrapper matters.** Lake prepends the toolchain's `bin` directory
  to `PATH` for the commands it runs. In `lake env lean X.lean`, the inner `lean` is
  therefore the toolchain binary, not the `lean` wrapper (**static**, from Lake's
  environment setup). The e2e check records what `lake env` actually resolves as
  `lake_env_resolves_lean`. Wrapping `lake` bounds it: `timeout` without
  `--foreground` puts itself in a new process group and sends `KILL` to that whole
  group, which kills Lake and the Lean process it started. The `lean` wrapper covers
  direct `lean` calls. Because `/opt/elan/bin` comes first inside a wrapper, nested
  calls are not timed twice.
* **Exit code and notice.** A limit kill exits 137 and prints `timeout: sending signal
  KILL to command 'lake'` on stderr, the same notice the judge uses to tell a timeout
  from an OOM kill (also 137).
* **Scope.** Every `lake` subcommand is bounded, including `lake build`, which is
  limited to 120 s in total. The value is the build argument
  `LOCAL_LEAN_TIMEOUT_SECONDS` (default 120). It must match
  `compilation_timeout_seconds`, and changing it means rebuilding the image.
* **Known bypass (documented, not closed).** Invoking the elan or toolchain binaries
  by absolute path bypasses the wrappers: for example `/opt/elan/bin/lake`,
  `/opt/elan/toolchains/*/bin/lean`, a `PATH` that puts those first, or a script that
  execs them directly. Such a compile is bounded only by the container's memory and
  pids limits and the run's stop. Using the bypass takes deliberate action, and the
  operational judge enforces its own limit regardless, so acceptance is unaffected.
* **Validation.** The e2e check `background_lean_killed_at_120s` (**pending** Phase B)
  starts a deliberately slow file (`#eval` of `IO.sleep 600000`, no imports, so no
  CPU load) twice: once through the Bash tool's `run_in_background`, and once detached
  with `setsid nohup … &`, which no harness process-group kill can reach. The detached
  compile must end with exit 137, the timeout notice and 115–135 s elapsed. The
  `run_in_background` one must do the same, or have been ended earlier by the CLI. No
  slow Lean process may remain. `lake` and `lean` must resolve to the wrappers in the
  Bash tool's environment. When the scenario does not run (offline rehearsal or
  `--skip-background-lean-check`) the check is written as skipped, and the launch gate,
  which requires it by name (`REQUIRED_EVIDENCE_CHECKS`), rejects that evidence.
  `bash_time_limited_to_120s` now also requires that the
  timed-out `sleep 150` is no longer running (checked with `ps` right after the tool
  reports the timeout). The isolation probe requires the wrappers to be root-owned,
  not writable and first on `PATH`. The capacity probe counts a local limit kill as a
  timeout, not as an OOM kill.

## Isolation design

Containers run with `network_mode: none`, uid 1000, `cap_drop: [ALL]`,
`no-new-privileges`, a pids limit, memory and swap ceilings, and no mounts, ports or
daemon socket. Inspect's sandbox bridge starts a loopback model/MCP proxy inside the
container and relays requests by Docker exec RPC, so the container never receives an
OpenRouter credential: Claude Code holds only Inspect SWE's public placeholder key
(also written to `~/.claude/settings.json` as an `apiKeyHelper`). Built-in `Agent`,
`Task`, `WebSearch` and `WebFetch` are disallowed (which also turns off the bridge's
provider web search); client-defined remote MCP is closed in the pinned bridge.
`sandbox_file` reads for `submit_solution` and `publish_notebook` run inside the
calling agent's own container as uid 1000 (realpath + `O_NOFOLLOW` + descriptor
re-check against the allowed roots, regular files only, size and suffix limits).
Structural properties and the read script are **validated** offline; everything else in
this section is **pending** `scripts/isolation_probes.py`, which asserts, per probed
agent (classification logic **validated** offline with Linux and Docker Desktop
mountinfo fixtures):

* `no_docker_socket_or_host_mounts`: every `/proc/self/mountinfo` entry is one of
  Docker's per-container defaults. The allowed set is the root filesystem; proc,
  sysfs and cgroup under `/proc` and `/sys`, including Docker's read-only and masked
  paths; the container's own `/dev` tmpfs, devpts, mqueue and `/dev/shm`;
  `/etc/hostname`, `/etc/hosts` and `/etc/resolv.conf` from the daemon's
  `containers/<id>/` directory; and the read-only `/sbin/docker-init`. A host bind
  mount or a named or anonymous volume therefore fails whatever its filesystem type,
  on Linux (ext4/xfs/btrfs) and Docker Desktop alike; the earlier check recognized
  only Docker Desktop paths. Every Unix socket outside Inspect's
  `/var/tmp/sandbox-services/` also fails. Sockets are found by a walk of the whole
  filesystem (mounts included, only `/proc` and `/sys` skipped), and each unexpected
  one is asked for the Docker API's `/_ping`. A `DOCKER_HOST`, `DOCKER_CONTEXT`,
  `CONTAINER_HOST` or similar variable in any readable process environment also fails.
* `no_privileges`: uid 1000. The probe process and every visible process (the CLI
  included) must have all five capability sets empty, CapBnd included: for a non-root
  uid, CapEff/CapPrm are 0 whatever `cap_drop` says, so only CapBnd shows it. They
  must also have `NoNewPrivs: 1` and `Seccomp: 2` (filter mode).
* `cgroup_limits_match_compose` (new): `pids.max`, `memory.max`, the swap limit
  (`memory.swap.max`, or `memsw − mem` on cgroup v1) and `cpu.max` inside the container
  equal the values the compose file the agents ran sets: `pids_limit`, `mem_limit`,
  `memswap_limit − mem_limit` = 0, and `cpus` or unlimited.
* `other_agent_files_unreachable`: markers in `/workspace`, `/tmp` and `/dev/shm`
  (the last catches a shared IPC namespace). The search runs without `-xdev`, so
  shared volumes and bind mounts are searched.
* `dependency_and_system_paths_read_only`: now also covers the lake/lean wrappers.
* `judge_container_network_none` fails unless the probe container actually ran and
  showed only `lo` and no TCP connection; command-line flags alone no longer pass.

## Inference path and spending

Per bridged request the filter: archives the effective system prompt and tool
definitions (`instructions/<agent>-<sha>.json`); waits at the first-inference barrier;
refuses requests after the run stops; reserves a conservative cost in the shared
SQLite ledger (off the event loop, shielded from cancellation, with two short retries
if SQLite reports `database is locked`, then fail-closed); freezes the request
settings; generates; settles; and strips tool calls from truncated outputs.

* Reservation: $r = \min(b + 8192,\ L)\,p_{\mathrm{in}} + m\,p_{\mathrm{out}}$, with $b$
  the UTF-8 bytes of the serialized request, $8192$ the framing allowance
  (`provider.FRAMING_TOKEN_ALLOWANCE`), $L$ the route context length
  (`ProviderSettings.context_length`, 1,048,576, checked against the catalog by
  `validate_catalog`), $m = 16384$ the response bound and $p_{\mathrm{in}}$,
  $p_{\mathrm{out}}$ the frozen maximum route prices. The prompt term
  $\min(b + 8192,\ L)$ is exactly `ProviderSettings.prompt_token_bound`, and
  `ProviderSettings.reservation` adds $m\,p_{\mathrm{out}}$. `inference_admitted` records
  `request_bytes` ($b$, used by `scripts/project_cost.py`) and
  `replayed_reasoning_blocks` (reasoning blocks of earlier assistant turns resent with
  `reasoning_history: all`).
* Frozen settings: `max_tokens` 16384, reasoning effort high, `reasoning_history: all`,
  OpenRouter provider `only: [fireworks]` (user-approved replacement for the unavailable
  `deepseek` endpoint), no fallbacks, `require_parameters`, no cache,
  and **no `n`** (not a supported route parameter, so `require_parameters` would
  refuse it). Retries are off below the ledger: Inspect `max_retries=0` and the OpenAI
  client `max_retries=0` (`get_model(..., max_retries=0)`, **validated** offline).
* Settlement: OpenRouter's reported `usage.cost` (captured from the raw response
  together with the generation id, serving provider and model, recorded in
  `inference_finished`), otherwise a priced upper bound from reported tokens,
  otherwise uncertain (full reservation retained). The OpenRouter generation id (the
  response body `id`, `gen-…`) is `provider_generation_id`; on exception paths
  (`inference_unbilled`/`inference_uncertain`) it is taken from the error body, the raw
  error response JSON or a generation-id header when present, and is otherwise
  recorded explicitly as `null`. The HTTP `x-request-id` stays separate as
  `provider_request_id`. `scripts/reconcile_ledger.py` matches holds by generation id.
* Provider errors (**validated** offline: tests drive the Inspect, OpenAI SDK and
  httpx path with an httpx `MockTransport`, plus constructed SDK errors): an HTTP 4xx
  other than 408 (OpenRouter 400/401/402/403/404/413/422/429) or a connection that was
  never established settles at 0 with basis `unbilled_error` (`inference_unbilled`,
  with status and request id); 408, 5xx, read timeouts, dropped connections,
  cancellations and unrecognized errors stay uncertain (`inference_uncertain`) for
  `scripts/reconcile_ledger.py`. Provider rejections that Inspect turns into an output
  without usage (context overflow, input moderation) also settle as `unbilled_error`.
* Claude Code retries API errors itself (408/409/429/5xx/connection), each retry
  re-entering the filter. `CLAUDE_CODE_MAX_RETRIES=4` (CLI default 10, maximum 15)
  bounds uncertain holds while riding out short bursts (**static**). Budget denial
  raises `LimitExceededError`, which ends that sample instead of being retried.
* The in-container proxy answers streaming requests with `message_start` immediately
  and `ping` every 5 s while the host generates (**static**, from the pinned proxy
  bytecode), so Claude Code's stream first-byte and idle watchdogs (180–300 s) do not
  fire while the first request waits at the barrier or during long generations.

## Truncation (`max_tokens`)

The bridge maps `max_tokens`/`model_length` to Anthropic `stop_reason: max_tokens`.
Claude Code 2.1.296 (**static**, from the pinned binary) turns that into a
`max_output_tokens` API-error message and recovers up to 3 consecutive times per turn
by appending a meta user message ("Output token limit hit. Resume directly …"); a 4th
consecutive truncation ends the turn and therefore the `-p` session. The filter keeps
`stop_reason` intact so that recovery runs, removes any partial tool calls, and counts
consecutive truncations. `agent_exit` then distinguishes:

| status | meaning |
| --- | --- |
| `harness_completed` | Claude Code ended on its own right after a finished response with visible text. The only voluntary exit. |
| `harness_completed_empty_reply` | It ended right after a finished response with no visible text and no tool calls (`final_output` `reasoning_only` or `empty`). Not voluntary. |
| `harness_completed_after_truncation` | The session ended right after truncated responses (recovery exhausted). |
| `harness_completed_after_provider_error` | It ended right after a request that raised (e.g. Claude Code gave up after its API retries). |
| `budget_exhausted` | The shared ledger refused admission (allowance cannot cover the request, or the scope is blocked by an estimate violation). |
| `budget_ledger_unavailable` | Admission failed because the ledger stayed locked. |
| `error_after_truncation` | The CLI exited with an error right after truncated responses. |
| `error` | Harness or sample error; re-raised so Inspect records it. |
| `stopped:<reason>` | Run-wide stop (`max_run_seconds`, `post_depletion_window`, `startup_timeout`, `monitor_error`, or a compatibility-check abort reason such as `reasoning_cap_unverified`). |
| `interrupted` | Cancelled without a recorded outcome (e.g. after the stop grace). |

`agent_exit` also carries `harness_status`, `final_output` (the kind of the last
finished response), `harness_after_stop` (how the harness ended after a stop, if it
did) and the filter's request counters. The
mock validation simulates both a single truncation (must continue) and exhaustion;
a live truncated reasoning turn may take Claude Code's "thinking-only" branch, which
must be observed in the approved $2 compatibility check (**pending**).

## Observation and logging

* Inspect logs keep model calls (`log_model_api`) and CLI-visible tool results;
  Inspect SWE's LiveConsumer records `compact_boundary` events. Neither bridged MCP
  tools nor Claude Code's own tools emit Inspect `ToolEvent`s (**static**, pinned
  bridge source): tool calls appear in `ModelEvent` outputs, and their results in later
  model inputs and the sample's messages.
* `runtime.jsonl` (researcher-side): `images_pinned`, `lean_warm_up`, barrier,
  admissions/settlements (with provider ids), truncations, `sandbox_file_read/rejected`,
  effective instructions, snapshots, exits, run stops (and, in the compatibility check
  only, `compatibility_abort` and `compaction_request`). `conference/events.jsonl` is the authoritative shared-event log;
  the barrier's common start is mirrored there through `mark_run_started` when the
  service provides it.
* Snapshots every 5 s of `.lean/.md/.txt/.py/.json/.jsonl/.sh/.toml/.tex/.csv/.log`
  under `/workspace`, `/tmp/w` and `~/.claude/projects` (symlinks never followed,
  `.lake` skipped, at most 2000 files; listing stops entirely at the limit). A version
  is marked seen only after a successful read. Claude Code session transcripts
  (`*.jsonl`, which hold compaction summaries) are read every 60 s and always at agent
  exit, up to 64 MB, and appended bytes are stored as delta blobs (`mode: append`,
  `offset`); the full file is the last `full` blob followed by later deltas. These are
  observed versions, not every short-lived write. Snapshot and probe execs use the
  underlying sandbox, so they add no events to Inspect transcripts.
* Compaction content that the harness does not expose is not recoverable by
  assertion.

## Stopping

Wall time begins at the first-inference common barrier (maximum 5400 s); depletion
of the task pool permits 600 further seconds within that maximum; startup has a
separate 600 s watchdog, and a missing peer aborts rather than silently running a
smaller swarm. Voluntary exits are recorded; no agent is substituted. Budget failure
stops new requests while previously dispatched calls may finish.

In-flight grace (implementation choice for "admitted requests may finish"): once a
stop is requested (90-minute maximum, depletion window, startup timeout, monitor
error or a compatibility abort), the budget filter admits nothing new and conference
actions are refused (`conference_action_after_stop`). A request already admitted may
settle with its reported cost; each sample waits up to `in_flight_grace_seconds`
(optional config key, default 120) for that, then cancels its agent
(`agent_in_flight_grace` records what was still in flight). A request still running
then is cancelled and stays an uncertain hold for `scripts/reconcile_ledger.py`.

Stop-boundary correction (authorized October 14, 2026): after a shielded budget
reservation returns, the filter delivers pending cancellation and checks the stop
flag again before model dispatch. A reservation cancelled or stopped at this
boundary is settled at zero (`unbilled_error`) and logged as `inference_not_admitted`
with its request ID; it is not an admitted/in-flight request. Once generation
starts, the existing settlement and uncertainty rules still apply. Conference
file tools check stopping again after extraction, and every conference action
checks in its executor worker immediately before entering the service, so queue
waits cannot carry a new action past the stop boundary. Actions already started
may finish. These races are covered by offline event-controlled tests, including
cancellation through the production peer solver; Docker/runtime validation stays
pending under the execution hold.

When all samples finish naturally, `runtime.supervise` records `run_stop` with
reason `evaluation_finished` after their exit records. This observation-only closure
provides the frozen rubric's before-stop evidence for voluntary exits. It does not
interrupt agents or overwrite an existing stop reason. Missing stop evidence in older
or incomplete runs remains unresolved.

`runtime.supervise` (shared by `run_pilot` and the compatibility check) runs the
evaluation under `RunClock.monitor`; after a stop request the samples stop themselves
and the evaluation is hard-cancelled only after `stop_grace_seconds` (default 180) plus
`in_flight_grace_seconds` (300 s in total by default). Each monitor tick also polls
the conference's seeded trigger ("Experiment tracks" above; a no-op in the
spontaneous pilot). A monitor exception (depletion check or trigger poll) is recorded
as `monitor_error` and polling continues (`monitor_recovered` once a depletion check succeeds again);
if the monitor ends without a stop, the run is stopped with reason `monitor_error`. The monitor's startup
wait uses `asyncio.wait` rather than `asyncio.wait_for`, because Python 3.10's
`wait_for` can swallow a cancellation that arrives just as the barrier releases, which
left the monitor looping until the 90-minute maximum (found and **validated** offline
with a regression test).

## Known residual cues and open behaviors

* The bridge is visible inside the container: an `inspect-sandbox-tools` proxy
  process, `/var/tmp/sandbox-services/` RPC directories, frequent `docker exec` polling
  (Docker sandbox default every 0.2 s per bridge service), the placeholder key and
  `IS_SANDBOX=1` in the CLI environment, and `apiKeyHelper` in `~/.claude/settings.json`.
* The observer's `python3 -I -c …` listing appears briefly in `ps` every 5 s.
* Claude Code's built-in prompt (identity line, security and honesty guidance,
  auto-memory and skills sections, tool descriptions) is a documented harness
  confound; each run archives it with the tool definitions.
* **Nested `claude -p` from Bash** (**pending** probe): the binary is on `PATH` and a
  child inherits the CLI's bridge environment, so a nested session probably reaches
  the bridge, passes this agent's budget filter and is logged as this agent, but
  without the parent's `--disallowed-tools` or conference tools. It cannot be removed
  without breaking the harness (the same uid runs the CLI); the probe documents the
  outcome rather than blocking it.
* **Large MCP results** (**static**, pinned Claude Code 2.1.296 binary): an MCP tool
  result whose size estimate (JavaScript UTF-16 length / 4) exceeds half of
  `MAX_MCP_OUTPUT_TOKENS` (default 25,000; not set here) is token-counted and, above the
  limit, saved to a file with an error message instead of returned inline. A full
  40,000-character `read_library_file` part (Unicode-heavy, ≈43,000 UTF-16 units)
  estimates ≈11,000 tokens, so it should arrive inline; the e2e check
  `read_library_file_large_unicode_full_range` verifies this through the real bridge
  (**pending** Phase B).
* Capacity: compile time has a ≈22 s Mathlib-import floor (warm cache; ≈88 s cold),
  and bridge polling multiplies Docker exec load with the number of peers. Local
  20-agent capacity on 8 CPUs / 7.75 GiB is unproven (**pending**
  `scripts/capacity_probe.py`); any population or deployment change needs the user's
  agreement first.

## Phase B validation (run serially, after the judge/dataset evidence exists)

Rebuild the agent image on the VM first (see "Rebuild required" above).

| Order | Command | Evidence | Expected |
| --- | --- | --- | --- |
| 1 | `PYTHONPATH=src .venv/bin/python scripts/validate_runtime.py` | `data/validation/runtime-e2e-<UTC>.json` | ≈12–17 min; 3 agent containers + 1 judge container + warm-up |
| 2 | `PYTHONPATH=src .venv/bin/python scripts/isolation_probes.py` | `data/validation/runtime-isolation-<UTC>.json` | ≈10–20 min; 2 agent containers + 1 judge probe container |
| 3 | `PYTHONPATH=src .venv/bin/python scripts/capacity_probe.py --duration 600` | `data/validation/runtime-capacity-<UTC>.json` | ≈20–30 min; 20 agent containers + judge containers; whole Docker VM |
| 4 | `PYTHONPATH=src .venv/bin/python scripts/compat_check.py --i-have-user-approval` | `data/validation/compatibility-<UTC>.json` | **PAID** (≤ $2 scope); ≤ 10 min; 1 agent container; only after the user confirms immediately before |

The end-to-end check runs three mock-scripted peers through the production solver,
filter, barrier (one late agent), snapshotter, instruction archive, conference tools,
real judge (rejection of an unmodified template), notebooks (absolute and relative
`publish_notebook` paths; a deterministic 40,000-character Unicode-heavy notebook
published and read back with `offset 0, limit 40000`, which must arrive complete and
unchanged through the real bridge), a cross-agent direct message (agent-001 sends,
agent-002 waits for the `dm_sent` event and reads it; identity-bound on both sides),
sandbox-file rejections, disallowed tools, a 120 s Bash clamp (the timed-out process
must be gone), two backgrounded slow compiles that the lake wrapper must kill at about
120 s (`background_lean_killed_at_120s`; "Local compilation limit"), compaction, one
truncation with continuation, recovery exhaustion and a voluntary exit, with pinned
image IDs, the warm-up and the compose binding, using a throwaway ledger (scope
`validation`, notional cap; never the authoritative ledger or the `compatibility`
scope). The background compiles always run from the command line. Only
`--skip-background-lean-check`, a debugging flag, or a caller that builds the
arguments itself, such as the offline rehearsal with a stand-in CLI, leaves them out.
The check is then recorded as not run (`skipped`, not required in the record), and the
evidence cannot satisfy a launch gate that requires the check by name. Inspect's overall log
status may be `success` even when individual samples failed, so every script inspects
each sample's error field; logs are read with attachments resolved so tool results are
compared as content. All three Phase B scripts apply the launch interlock first
("Launch interlock" above) and record it as `interlock` plus the required check
`no_conflicting_containers_at_start`.

**Compaction threshold** (informational but recorded; **validated** offline as
plumbing, the value itself **pending** Phase B). docs/plan.md asks to verify the
compaction threshold the bridge actually exposes rather than assume the provider
maximum. In the e2e scenario agent-001 reports fabricated prompt sizes of 120,000,
150,000, 170,000, 185,000 and 195,000 input tokens (`COMPACTION_PROBE_LEVELS`, in
ascending order, one Bash call each, each settled at a reported provider cost so the
throwaway ledger records no estimate violation). Claude Code 2.1.296 checks its
auto-compaction threshold before its next request, so `validate_runtime.compaction_bracket`
records as `compaction_threshold_tokens` the lowest level directly followed by an
auto-compaction request, and as `highest_level_without_compaction` the largest level
below it that was not; the CLI's trigger for a reported prompt size $s$ lies in
(`highest_level_without_compaction`, `compaction_threshold_tokens`]. Both go into the
runtime-e2e evidence (top-level `compaction_threshold_tokens` and
`compaction_threshold_bracket`, plus the optional check
`compaction_threshold_bracketed`, which also requires every level to have been
served). A run without any compaction records `null`; it is not a launch gate. The
value transfers to the live route only if the CLI assigns the same default context
window to both presented model names (Inspect SWE presents the served model's name:
`model` for the mock, the OpenRouter model id on the live route; neither is a known
Claude model id) and is otherwise to be confirmed by the compatibility check's
optional `compaction_observed`. **`scripts/project_cost.py` should use it instead of
its assumed 160,000**: it reads the top-level `compaction_threshold_tokens` from the
newest runtime-e2e evidence (or `--evidence FILE`; `--compaction-threshold` overrides)
and falls back to the 160,000 ASSUMPTION only when no evidence records a value, naming
the source in `assumption_sources.compaction_threshold`. The recorded value is the
bracket's upper end, i.e. the conservative, longer-context projection.

The capacity probe takes its defaults for every key the capacity gate compares
(`agent_count`, `agent_memory`, `agent_cpus`, `judge_max_concurrent`, `judge_memory`,
`judge_cpus`, `compilation_timeout_seconds`) and the image tags from `--config`
(default `configs/pilot.json`); `judge_max_concurrent: null` there means unbounded, as
does `--judge-max-concurrent none`. A default run therefore measures exactly the
configuration the gate checks; measuring another value yields evidence that passes the
gate only after the config is changed to match (and a population change needs the
user's agreement first). Its evidence records `validated_config`, `images`,
`lean_warm_up`, `persona_counts`, `compose_sha256`, `interlock` and `max_connections`
(the mock model's explicit connection limit).

The capacity probe's startup watchdog defaults to the config's
`startup_timeout_seconds` (600 s), the pilot's own watchdog, instead of the earlier 900
s. `measurements.startup` records, measured from evaluation start (when the watchdog
starts, as in `runtime.supervise`), each agent's first inference request
(`agent_ready`) and the common start barrier. The required check
`all_agents_reached_barrier_within_startup_timeout` fails if any agent, or the barrier,
took longer, if the watchdog stopped the run, or if `--startup-timeout` was raised
above the config value. A configuration that starts too slowly for the pilot therefore
cannot produce passing capacity evidence. Local compiles killed by the 120 s wrapper
(exit 137 with the timeout notice) count under `no_compile_timeouts`. A bare 137 still
counts as an OOM kill.

## Live compatibility check (paid, separate $2 allowance)

docs/plan.md "Live compatibility check (October 13, 2026)": one agent, a few turns on
the frozen route, explicit abort criteria, ledger scope `compatibility` (cap $2, outside
the $20 pilot and $5 analysis allowances). `scripts/compat_check.py` is written and its
logic is **validated** offline (refusals, abort guard, evidence and a full rehearsal of
`run_check` through real Inspect with fake sandbox/CLI/judge and a scripted model); the
paid run itself is **pending** the execution hold and the user's confirmation
immediately before it.

Procedure:

1. Phase B steps 1–2 pass (runtime-e2e and runtime-isolation evidence), the route
   freeze is fresh (`scripts/freeze_route.py`), and the user approves this run.
2. `PYTHONPATH=src .venv/bin/python scripts/compat_check.py --i-have-user-approval`.
   Without the flag it refuses. It opens `state/budget.sqlite` with
   `budget.DEFAULT_CAPS` and refuses unless scope `compatibility` exists with cap
   exactly $2, has no unacknowledged estimate violation and at least `--min-remaining`
   (default $0.50) left; unless the launch gates `fixed_task_manifest`,
   `frozen_dataset`, `approved_settings`, `config_caps`, `route_pricing`,
   `credential_presence`, `runtime-e2e` and `runtime-isolation` pass; unless the pinned
   Inspect packages are installed, the launch interlock is clear (no leftover judge
   container, no running comparator container, no other run's agent sandbox; no
   override for this paid check) and both images pin to IDs. Its single agent uses
   `max_connections` 1 (recorded). It prints the compatibility ledger snapshot and asks the operator to type
   the remaining allowance (`--yes` skips this only for a user-approved run). Nothing
   is spent and no credential is read before that point.
3. The run uses the production pieces through the helpers shared with `run_pilot`:
   pinned images, warm-up, `make_conference` (real conference service and operational
   judge in the researcher-only `runs/compat-<UTC>/`), the real pilot prompt for one
   persona (default: agent-001's persona in the 20-agent allocation; `--persona`),
   `peer_solver` with `budget_filter(..., scope="compatibility")`,
   `build_openrouter_model` (the same `settings_from_config`,
   `capture_openrouter_cost`, `max_retries=0`; the key is read there, via
   `secrets.load_openrouter_key`, and never written; the key's provider-side limit is
   then verified against the remaining compatibility allowance before any paid
   request), `eval_options` and `supervise`.
   Before the agent runs, one researcher-side reasoning-cap probe request (agent id
   `cap-probe`, never shown to any agent, same ledger scope and budget filter, about
   $0.002) runs with `max_tokens` lowered to `--cap-probe-max-tokens` (default 1024).
   It must stop at the cap counting reasoning (finish reason `length`), report
   reasoning tokens, and bill within its reservation (required check
   `reasoning_cap_enforced`). If it fails, the agent run is skipped (abort reason
   `reasoning_cap_unverified`, recorded as `cap_probe_error`) and the evidence fails.
4. Abort criteria (an abort stops admission; an admitted request may settle within
   `in_flight_grace_seconds`, see Stopping): after
   `--max-requests` (default 12) admitted requests; `--max-minutes` (default 10) after
   the first inference; the first permanent provider HTTP 4xx (excluding temporary
   408/429), a converted pre-generation rejection, tool-call parse failure or provider
   output error; any estimate violation in the scope. Temporary 408/429, server and
   transport errors retain Claude Code's existing bounded retries. The preflight records
   each transient error and permits it in evidence only when an exact matching failure
   is followed by a successful, provider-priced response from the same agent. Terminal
   or unknown errors still fail; unresolved billing still requires reconciliation. This
   policy applies to the small compatibility check, not to pilot stopping.
5. Evidence `data/validation/compatibility-<UTC>.json` (schema `gdm-compatibility/1`):
   `created_at`, `all_required_passed`, `image_ids {agent, lean}`, `model_id`,
   `provider_route`, `settings` (`provider.frozen_fields`), `request_ids` (every ledger
   request the check admitted, agent and probe), `requests` (`len(request_ids)`),
   `spent_usd` (the committed total of exactly those rows at run end, uncertain holds
   at their full reservation), and `checks {name: {passed, required, detail}}`.
   Required: `tool_call_parsed`, `conference_tool_call`, `reasoning_replay_accepted`,
   `provider_cost_reported`, `completion_within_max_tokens`, `reasoning_cap_enforced`,
   `no_estimate_violation`, `route_pinned`, `no_sample_error`. Informational:
   `truncation_observed`, `compaction_observed`. Details add the abort reason, ledger
   before/after (`ledger.request_states`, `ledger.missing_request_ids`,
   `ledger.committed_delta_usd`), `credentials_in_environment`, `provider_limit`,
   `compose_sha256`, `cap_probe_error`, `limits.cap_probe_max_tokens`, agent exit,
   tool-call list and warm-up. Credential-like strings are redacted before writing.
6. The launch gate `compatibility` (in `launch_gates`, integrator-owned) reads the
   newest file: all required checks passed, model/route/settings equal
   `settings_from_config(config)`, `spent_usd` within the cap, agent image equal to the
   current one, and (interface X4) the ledger's CURRENT state of exactly the
   `request_ids`: all known and in scope `compatibility`, none unresolved, none in
   `estimate_violation` or `violation_acknowledged`, and their settled sum within $2.
   Reconciling an uncertain hold after the run (`scripts/reconcile_ledger.py`) is
   therefore supported. The scope-wide checks (no unresolved compatibility hold, no
   unacknowledged violation) still apply. `verified_integration: true` remains a
   separate explicit user confirmation that is also required; the evidence alone does
   not set it.


## Explicitly authorized unlimited pilot key

On October 14 the user authorized the main OpenRouter key despite its lack of a
provider-side cap. The pilot configuration records `allow_unlimited_provider_key:
true` and a nonempty `unlimited_provider_key_approval` note. Strict provider-cap
enforcement remains the code default and the compatibility-check policy.

For the approved pilot only, live key verification may accept explicit null
`limit`, `limit_remaining` and `limit_reset` fields, provided the key is not expired.
Missing metadata, API errors, expired keys, and finite limits above the remaining
allowance still fail. Evidence and run settings identify the waived safeguard as
`provider_limit_enforced: false`; they never claim that an unlimited key has a cap.
The shared $20 pilot ledger, conservative reservations, settlement, existing charges,
unresolved holds, route pricing and all other launch gates remain enforced.

## Reasoning transport through Claude Code

The first Fireworks compatibility check exposed an Inspect 0.3.279 conversion defect:
OpenRouter reasoning arrived as typed `ContentReasoning`, but the Anthropic bridge
returned it to Claude Code as ordinary text. Later provider requests consequently
lost the structured `reasoning_details` and `reasoning_content` fields.

`research_swarm.reasoning_bridge.install_reasoning_bridge()` runs before each Claude
Code agent is constructed. It wraps only the pinned bridge's request boundary and
input/output converters. Original OpenRouter reasoning blocks travel in authenticated
Anthropic thinking signatures and are restored exactly on the next request. A random
signing key belongs to each bridge instance; no provider credential is involved, and
there is no shared conversation cache. Modified or foreign transport blocks fail
before provider dispatch. Native Anthropic conversions, text, tool calls and compaction
remain unchanged; omitted reasoning is never reconstructed.

Offline tests exercise the actual SDK round trip back into OpenRouter wire fields,
including tools, opaque reasoning, beta responses, tampering, agent isolation and
compaction omission. Live compatibility must additionally confirm that the pinned
Claude Code process echoes those blocks correctly. An old session cannot be resumed
under a new host process's signing key; crash recovery by resuming sessions is not
supported by this pilot.

## Completed real CLI smoke check (earlier image)

On October 13, 2026 an unpaid smoke test ran the pinned Claude Code CLI in a
network-disabled container through Inspect SWE with a scripted mock model (one Bash
notebook write, one identity-bound conference MCP action, completion); Inspect
captured both tool results and the sample had no error. It used the separate
temporary `gdm-claude-code-smoke:local` image, not the experiment image, and passed on
its third attempt after fixing missing tool-parameter descriptions and an
unrecognized model id. Source and summary: `docs/evidence/unpaid-claude-code-smoke*`.

References: [Inspect SWE Claude Code](https://meridianlabs-ai.github.io/inspect_swe/claude_code.html),
[Inspect sandbox bridge](https://inspect.aisi.org.uk/agent-bridge.html),
[OpenRouter public endpoint catalog](https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints).
