# Run configuration

`pilot.json` is the configuration for the baseline spontaneous-discovery pilot. It is plain JSON and has no comments, so this file documents each key. `scripts/preflight.py` and `scripts/run_pilot.py` read it, and `runtime.run_pilot` receives it. The validation scripts (`validate_judge.py`, `validate_dataset.py`, `validate_conference.py`, `validate_runtime.py`, `isolation_probes.py`, `capacity_probe.py`, `compat_check.py`, `freeze_route.py`) and `project_cost.py` read it through `--config` (default `configs/pilot.json`). `research_swarm.launch_gates` checks it against the approved plan values, the frozen data and the validation evidence.

`seeded.example.json` is the template for the seeded-contagion track (see "Seeded track" below). It is never a pilot config, and the launch gates refuse it.

This file lists every key that code reads (`config.get(...)` or `config[...]` in `src/research_swarm/` and `scripts/`), including the optional keys that `pilot.json` leaves out (see "Optional keys"). Keys marked *label* are recorded in the run's `settings.json` but no code reads them.

Changing a key that the launch gates compare (`launch_gates.approved_settings()`, `max_connections`, the caps, or the capacity-validated keys) blocks launch until the matching user decision is recorded in `docs/plan.md`, or until the evidence is regenerated. Do not edit keys to make a gate pass.

## Experiment identity and design

| Key | Value | Meaning |
| --- | --- | --- |
| `experiment` | `baseline-spontaneous-pilot` | *Label.* |
| `track` | `spontaneous` | The only approved initial track. Gates: `approved_settings` and `seeded_track` (must be `spontaneous`). `runtime.preflight` also blocks any other value. |
| `seeded_contagion_enabled`, `mitigations_enabled` | `false` | Seeded contagion and mitigations are disabled in the pilot. Gate (`approved_settings`; `seeded_track` also requires `seeded_contagion_enabled` to be exactly `false`). |
| `seeded` | absent | The seeded-track configuration (see "Seeded track"). Gate `seeded_track`: a pilot config must not carry one, enabled or not. Only a non-null value counts, so `null` is treated as absent. |
| `agent_count` | `20` | Number of independent Claude Code peers. Gate: must equal `runtime.APPROVED_AGENT_COUNT`, the single source of truth that `launch_gates.approved_settings()` imports. It must also equal the `validated_config` in the capacity evidence. Changing it requires a recorded user decision ("Capacity adjustment authority"). `scripts/project_cost.py` uses it as its default `--agents`. |
| `task_count` | `14` | Fixed pilot subset size. Gate. |
| `parent_task_count` | `71` | Parent pool size. *Label.* |
| `fixed_task_manifest` | `{path, sha256, parent_manifest_sha256}` | Pins `data/pilot_subset.json` by its SHA-256 and pins the parent `data/manifest.json` by the SHA-256 recorded in the subset. Gate: both hashes must match the files on disk. |
| `persona_seed` | `260904170` | Seed for the balanced persona shuffle (`runtime.assign_personas`). Five agents per persona. |
| `persona_counts` | 5 per persona | Records the balanced allocation. |
| `execution`, `deployment`, `agent_harness` | `asynchronous`, `local`, `claude_code` | *Labels* recording the approved design. |
| `system_prompt_mode` | `append` | `append` keeps Claude Code's built-in prompt and appends the conference prompt (`--append-system-prompt`). `replace` would replace it. The plan approves `append`. Gate. |
| `claude_auto_memory` | `true` | Keeps Claude Code's normal auto-memory, as the plan approves. Gate. |
| `general_internet_enabled` | `false` | Agent containers have no network. Gate. |

## Model and provider

| Key | Value | Meaning |
| --- | --- | --- |
| `provider`, `candidate_model_id` | `openrouter`, `deepseek/deepseek-v4.1-flash` | The approved model and route family. Gate. |
| `requested_model` | `DeepSeek v4.1` | The user's selection, recorded as given. *Label.* |
| `provider_route` | `fireworks` | User-approved alternative after the `deepseek` endpoint was unavailable (October 14). Routing is `provider.only` with no fallbacks; live compatibility remains required. |
| `allow_unlimited_provider_key` | `true` | Explicit October 14 user exception for the main unlimited OpenRouter key. Code defaults to strict provider-cap enforcement; this permits only an explicitly unlimited key while retaining the shared $20 pilot ledger. Compatibility checks keep their strict key-limit policy. |
| `unlimited_provider_key_approval` | recorded user authorization | Required nonempty approval note whenever unlimited-key use is enabled. Live key evidence and run settings record the exception; it never increases the pilot, compatibility or analysis allowance. |
| `provider_settings` | `{}` | Optional overrides of `ProviderSettings` fields, with prices written as strings. `provider.settings_from_config` builds the settings that the freeze, the gates and the launched run all share. The verification flags cannot be set here. |
| `max_connections` | `20` | **Required.** Inspect's limit on concurrent inference requests for the run's model (`GenerateConfig.max_connections`). The approved rule is one in-flight request per agent, so it equals `agent_count` and no agent queues behind another inside Inspect (plan checklist item 4, "Scheduling"; "Remaining model-setting authority" requires freezing concurrency before launch). Gate `approved_settings`: it must be present, a positive integer, and equal to `agent_count`. A different value needs a documented plan decision and a change to `launch_gates.max_connections_problems`. The launched run records it in `settings.json`. The provider's real rate limit at 20 concurrent requests is observed only in the live compatibility check and the pilot; the unpaid probes use a mock model. |
| `route_pricing_max_age_hours` | `72` | How old the newest `route-pricing-*.json` freeze may be. Re-run `scripts/freeze_route.py` before launch. |
| `provider_limit_max_age_hours` | `72` | How old the newest `provider-limit-*.json` check may be. The paid paths also re-check the key they load (see "Provider-side key limit" below). |

## Images and resources

| Key | Value | Meaning |
| --- | --- | --- |
| `agent_image`, `lean_image`, `comparator_image` | `gdm-math-swarm-{agent,lean,comparator}:local` | Image tags. The gates resolve each tag to its local image ID and require the evidence `image_ids` to match it. The runtime and the probes read `agent_image` and `lean_image`; `comparator_image` is read by the `evaluator` gate. |
| `agent_memory`, `agent_cpus` | `2g`, `null` | Per-agent container limits. `null` means no CPU limit. Must equal the capacity evidence. |
| `judge_max_concurrent` | `null` | Process-wide cap on concurrent operational-judge compilations (interface I1). `null` means unbounded. `scripts/capacity_probe.py --judge-max-concurrent` defaults to this config value, so a probe run without the flag measures what this file says (`null`, that is unbounded, as shipped). To measure a different value, pass it explicitly (`none` also means unbounded), then copy the value recorded in the capacity evidence's `validated_config` into this key. The `runtime-capacity` gate requires exact equality, so this key must equal the measured value. |
| `judge_memory`, `judge_cpus` | `2g`, `1` | Limits for each judge container. `validate_judge.py`, `validate_dataset.py` and `validate_conference.py` compile with these limits (from `--config`) and record them as `limits`. The gates require those `limits` to equal these keys. |
| `compilation_timeout_seconds` | `120` | The approved per-compilation limit, enforced inside the container. Gate. Queue wait does not count toward it. It is also the `limits.timeout_seconds` the Lean and conference-race evidence must record. |

## Run clock

| Key | Value | Meaning |
| --- | --- | --- |
| `startup_timeout_seconds` | `600` | If not every peer reaches the common start barrier in time, the run aborts. It does not shrink the swarm. |
| `max_run_seconds` | `10800` | The user-approved 180-minute maximum for the cached repetition, measured from the common start. Gate. |
| `post_depletion_seconds` | `600` | Observation window after the last task closes, within the maximum. Gate. |
| `post_depletion_within_run_maximum` | `true` | *Label*: records that the window never extends the approved maximum. |
| `stop_grace_seconds` | `180` | How long samples have to stop on their own before cancellation. `runtime.supervise` hard-cancels the evaluation `stop_grace_seconds + in_flight_grace_seconds` after the stop (300 s by default). |

## Optional keys

`pilot.json` leaves these out, so the code uses the default shown. To change one, add the key to the config. For the keys that a probe also takes as a flag, add the key whenever the probe used a non-default value, so the launch matches what was measured.

| Key | Default when absent | Read by | Meaning |
| --- | --- | --- | --- |
| `in_flight_grace_seconds` | `120` | `runtime.in_flight_grace_seconds` | After a stop (or a compatibility abort) nothing new is admitted and conference actions are refused. Each sample waits up to this long for an already-admitted model request to settle, then cancels its agent (docs/runtime.md "Stopping"). Must be nonnegative. |
| `run_dir` | `runs/<YYYYmmdd-HHMMSS>` | `runtime.run_pilot`, `scripts/run_pilot.py` | `scripts/run_pilot.py` sets `runs/pilot-<UTC>` explicitly so it can print the log locations. |
| `max_subprocesses` | `64` | `runtime.eval_options` | Inspect's limit on concurrent sandbox subprocesses (Inspect's `subprocesses` limiter). The capacity gate compares its effective value with the probe. If `scripts/capacity_probe.py` used a non-default `--max-subprocesses`, set the same value here. |
| `warm_up_memory` | `6g` | `runtime.warm_up_options`, `scripts/capacity_probe.py` | Memory cap for the single researcher-side Lean warm-up compile before the start barrier (docs/runtime.md "Lean warm-up before the start barrier"). It never counts toward agent or judge limits. |
| `warm_up_timeout_seconds` | `900` | `runtime.warm_up_options` | Time limit for that warm-up compile. A failed or timed-out warm-up is recorded and reported, but it does not block the run. |

## Seeded track

`seeded.example.json` is the explicit seeded-contagion configuration that the plan requires ("Experiment tracks"). It is a copy of `pilot.json` with these changes: `experiment` `seeded-contagion-example`, `track` `seeded`, the attestations `false`, and a `seeded` block. The block's keys are the fields of `research_swarm.seeded.SeededTrackConfig`, plus `note` (`seeded.DOCUMENTATION_KEYS`: read by people, never passed to the service). The block has `enabled: false`. Every semantic field is `null`: `trigger_kind`, `trigger_value`, `artifact_path`, `artifact_sha256`, `channel`, `author` and `credit_effect` (`seeded.SEMANTIC_FIELDS`), plus `task_id`, `library_path` and `intent_type`, one of which the chosen channel requires (`seeded.CHANNEL_FIELDS`). `tags` is `[]` and must stay a list. The `note` records that the seeded track's trigger, artifact, provenance and credit/task-state effects are still undecided by the user (plan "Track definitions" and checklist item 9). Do not fill these fields until the user decides them. A seeded run would also need `seeded.enabled: true` and `seeded_contagion_enabled: true`. `research_swarm.seeded.resolve_track` validates the values and the artifact when the conference starts.

How the parts fit together:

- The runtime passes `track` and `seeded` to the conference service (`seeded.conference_arguments`) only when `track` is `seeded`, `seeded.enabled` is `true`, `seeded_contagion_enabled` is not `false`, and every semantic field is set. It never passes them otherwise. A contradictory or incomplete seeded configuration is refused, never silently run as either track. The conference service also refuses a seeded configuration in the spontaneous track, and a seeded track that is not enabled. In a seeded run, the run clock polls the conference's seeded trigger on every tick, so a time-based trigger fires on time even when no agent acts. In the spontaneous track the poll does nothing.
- The launch gates refuse any seeded configuration for the approved pilot. The `seeded_track` gate requires `track` `spontaneous`, `seeded_contagion_enabled` `false`, and no `seeded` key. `approved_settings` also blocks a `track` or `seeded_contagion_enabled` that differs from the plan, and `runtime.preflight` blocks any track other than `spontaneous`. Running the seeded track would therefore need a recorded user decision and changes to those gates.

## Spending and launch

| Key | Value | Meaning |
| --- | --- | --- |
| `max_pilot_usd`, `max_analysis_usd`, `max_compatibility_usd` | `307.809660142`, `110`, `2` | Must equal `budget.DEFAULT_CAPS`. These are three separate allowances: the pilot's shared $20, post-run analysis's $110 (originally $5; the October 17, 2026 transcript-judge decision raised it to $40 for judging `cache100-20261016T020259Z` through the audited `budget.raise_judge_analysis_cap`, and the October 17 evening misbehavior-focused judge decision added $10, $40 to $50, through the second audited `budget.raise_judge_analysis_second_cap`, which requires exactly the first raise and is reachable as `scripts/preflight.py --raise-ledger-cap analysis`; a ledger still at $5 or $40 opens unchanged and keeps enforcing its own cap until the raise, and the user's late-evening completion authority added a third audited raise, $50 to $100, `budget.raise_judge_analysis_third_cap`; after that ceiling stopped the v2.3 pass, the user's October 18 decision "Just increase cap as much as needed to finish the analysis" added a fourth audited raise, $100 to $110, `budget.raise_judge_analysis_fourth_cap`, allowed with the unreconcilable uncertain holds still counted; a ledger at an earlier judge cap opens unchanged, and the judge accepts only a ledger carrying all four raises), and a separate paid $2 for the live compatibility check (plan row "Live compatibility check", which supersedes the earlier unpaid-only, $0 compatibility cap). The ledger, not this file, enforces the caps. The shared ledger is `state/budget.sqlite`, and spending in it is never reset. |
| `verified_integration` | `false` | The user's explicit confirmation of the live compatibility evidence. It is not sufficient alone: launch also needs the `compatibility` gate to pass on the evidence file (see below). Set it only after that evidence passes and the user has reviewed it. |
| `paid_launch_enabled` | `false` | Set only by the user. A paid launch also requires `--launch-paid-pilot` and a typed confirmation of the remaining allowance. |

## Compatibility gate

The paid pilot is blocked until `data/validation/compatibility-<UTC YYYYMMDDTHHMMSSZ>.json` exists and passes. `scripts/compat_check.py` writes it after the separate $2 live compatibility check: one agent running a few turns on the frozen route, reserving and settling in ledger scope `compatibility` of `state/budget.sqlite`. The check runs only after the plan's execution hold is lifted and the user confirms immediately before the paid call. The newest file must have:

- `schema` `gdm-compatibility/1` and `all_required_passed: true`;
- each required check present with `passed: true`: `tool_call_parsed`, `conference_tool_call`, `reasoning_replay_accepted`, `provider_cost_reported`, `completion_within_max_tokens`, `reasoning_cap_enforced`, `no_estimate_violation`, `route_pinned` and `no_sample_error`. `truncation_observed` and `compaction_observed` are informational; when they are missing or not passed, preflight shows a warning;
- `model_id`, `provider_route` and `settings` equal to what `provider.settings_from_config` builds from this file;
- `spent_usd` at most the $2 compatibility cap, and `requests` at least 1;
- `request_ids`: the ledger request ids the check admitted, one per counted request;
- `image_ids.agent` equal to the current local `agent_image` ID. Rebuilding the agent image requires a new check.

The shared ledger must also agree, in its current state rather than the totals frozen when the check ended. Every id in `request_ids` must be in scope `compatibility`, none may be unresolved (`reserved` or `uncertain`), none may have exceeded its reservation (`estimate_violation`, even if acknowledged later), and their settled costs must sum to at most $2. The scope as a whole must have no unresolved holds and no unacknowledged estimate violation. Reconciling an uncertain hold (`scripts/reconcile_ledger.py`) therefore unblocks the gate. Its lower reconciled cost does not conflict with the `spent_usd` recorded at run end, which counted the full reservation.

A ledger created while the compatibility cap was $0 cannot be opened with the current caps, because the ledger never resets. After the user approves, raise it explicitly. The raise is recorded in the ledger's `cap_changes` table, and prior spending stays counted:

```
.venv/bin/python scripts/preflight.py --raise-ledger-cap compatibility \
    --approval-note "User approved the $2 compatibility allowance in chat on <date>" --i-have-user-approval
```

## Provider-side key limit

The ledger enforces every allowance. A provider-side OpenRouter key limit is an additional safeguard (plan row "Approved spending limits"). `scripts/check_provider_limit.py` (unpaid, but it loads the credential, so it runs only with the user's approval) writes `provider-limit-<UTC>.json`. The `provider_limit` gate requires a fresh passing record: a limit is set, it does not reset, `limit_remaining` is within the cap, and it does not exceed the remaining pilot allowance.

That evidence is not bound to a key. `secrets.load_openrouter_key` prefers an exported `OPENROUTER_API_KEY` over the host `.env`, so the key that was checked can differ from the key a later launch loads. The binding check therefore happens live. Right after the paid launch (`runtime.run_pilot`) or the paid compatibility check loads its key, and before any paid request, it calls `provider_limit.verify_key_limit` (an unpaid `GET /api/v1/key`). The call refuses the key if `limit` is null, if `limit_reset` is not null, or if `limit_remaining` exceeds that scope's remaining ledger allowance. The returned numbers, without the key, are recorded in the run's `runtime.jsonl` and `settings.json`.

Because each check is against its own scope's allowance, the $2 compatibility check needs a key with at most the remaining compatibility allowance left, and the $20 pilot needs a key with at most the remaining pilot allowance left. Use separate keys for the two steps, or adjust the key's limit between them (for example, a $2 limit for the compatibility check, then a limit of the compatibility usage plus the remaining pilot allowance before the pilot). Re-run `scripts/check_provider_limit.py` for the pilot key afterwards (`--max-limit-usd 2` checks a compatibility key).

## Compatibility retries (October 14, 2026)

The small compatibility check stops on structural request incompatibilities, including
context/input rejections that Inspect converts into usage-less `model_length` or
`content_filter` outputs. Temporary HTTP 408/429, server and transport failures retain
Claude Code's existing bounded retries; no extra SDK or guard retry loop is introduced.
An exact recorded transient error followed by a successful, provider-priced response
from the same agent may pass the compatibility evidence checks. Terminal/unknown
errors and unresolved billing still block readiness. A single proof rejection or
rate-limit response does not stop the full pilot; its retry behavior is unchanged.

## Capacity evidence settings (October 14, 2026)

`runtime-capacity` evidence compares the effective values of `agent_count`,
`agent_memory`, `agent_cpus`, `judge_max_concurrent`, `judge_memory`, `judge_cpus`,
`compilation_timeout_seconds`, `max_subprocesses`, `startup_timeout_seconds`,
`max_connections`, `warm_up_memory` and `warm_up_timeout_seconds`. Omitted optional
keys use the same defaults as the runtime. The probe reads these from `--config`;
CLI overrides record what was actually measured. Any mismatch requires new evidence.

All required named capacity checks must be present and passed, including the
startup, workload, OOM, latency, headroom, ledger, interlock and compose checks.
`warm_up_attempted` verifies that the normal warm-up was attempted: `--no-warm-up`
cannot certify the pilot's warm startup. Warm-up failure/timeout remains informational
and does not by itself fail a capacity run whose required workload checks pass.

## Other evidence gates

Besides the evidence gates, `research_swarm.launch_gates` checks the config itself: `fixed_task_manifest`, `frozen_dataset`, `approved_settings` (the plan values above, plus `max_connections` equal to `agent_count`), `seeded_track` (no seeded configuration), `config_caps`, `ledger`, `credential_presence` (presence only, never read), `verified_integration` and `paid_launch_enabled`.

`research_swarm.launch_gates` also requires the newest `declarations`, `judge`, `timing`, `reuse`, `conference-race`, `evaluator`, `runtime-e2e`, `runtime-isolation`, `runtime-capacity`, `route-pricing` and `provider-limit` evidence files. The checks beyond `all_required_passed` and image IDs are:

- **`declarations`, `judge`, `timing` and `conference-race`:** `complete` must be `true`, which means a full run: every pilot task (for `conference-race`, every scenario), the strict variant where applicable, and no `--only`, `--skip-*`, task-subset or custom-workload flags. `limits` (`timeout_seconds`, `memory`, `cpus` actually used for judge-path compiles) must equal `compilation_timeout_seconds`, `judge_memory` and `judge_cpus` here. A partial run, or one at other limits, cannot satisfy the gate. `scripts/validate_conference.py` takes these limits from `--config` (default `configs/pilot.json`), as `validate_judge.py` and `validate_dataset.py` do.
- **`runtime-e2e`, `runtime-isolation` and `runtime-capacity`:** `compose_sha256` must equal `runtime.compose_fingerprint(config, current agent image ID)`, the SHA-256 of the compose file a launch would write. Changing the generated compose file (for example its mounts, network or privileges), `agent_memory` or `agent_cpus`, or the agent image invalidates the evidence.
- **`runtime-e2e` and `runtime-isolation`:** named check keys must be present and passed (`REQUIRED_EVIDENCE_CHECKS`; per-agent `ISOLATION_AGENT_CHECKS` for at least two agents). This means a record from an older script version cannot satisfy the gate. The e2e list includes `background_lean_killed_at_120s`, which an offline rehearsal or `--skip-background-lean-check` records as skipped, and both lists include `compose_matches_fingerprint`; the per-agent list includes `cgroup_limits_match_compose`.
- **`evaluator`:** the record must also pass `evaluator.validation_status()`, which checks the fixture manifest, pins, seccomp profile and comparator image. It must also contain a passing self-comparison for every pilot template, plus the answer bridge for answer-based tasks (`scripts/validate_evaluator.py --pilot-self-compare`).

### First fresh baseline repetition, October 15, 2026 (historical)

`max_pilot_usd` is now the cumulative shared ledger ceiling, $119.636804044:
the original pilot settled $19.636804044 and the user approved an additional
$100 for one fresh 20-agent baseline. `pilot_prior_settled_usd` binds launch to
that exact original settled amount; `pilot_additional_allowance_usd` records the
new $100 allowance. These must sum to the cumulative cap. The original ledger
must be retained and raised explicitly with an audit note; replacement ledgers,
unresolved pilot holds, changed prior spending, and replaying the approval after
new spending all block launch. The unused $0.363195956 from the original $20 is
already included in the new total remaining $100, not added on top. Analysis and
compatibility caps remain separate $5 and $2. `paid_launch_enabled` stays false
until the integrator completes the approved run's checks.

### Fresh 8 GiB baseline, October 15, 2026

The user authorized `agent_memory: "8g"` and another fresh $100 shared allowance.
The cumulative ledger cap is $208.055621674: prior settled $105.727998474 plus
preserved uncertain reservations $2.3276232 plus the new $100. Launch metadata
binds `pilot_prior_settled_usd`, `pilot_prior_held_usd`,
`pilot_prior_committed_usd` ($108.055621674), `pilot_prior_uncertain_requests` (7)
and `pilot_additional_allowance_usd` (100). Existing request history and the audited
cap-change chain must match; no reservation is silently reconciled or released.
The unused allowance from the stopped run is absorbed into this new ceiling,
rather than added to the new run's $100. Fresh runtime, isolation and capacity
evidence at 8 GiB are required. All other experiment and judge settings remain
unchanged, and paid launch stays disabled until full preflight passes.

## Cached-counter repetition, October 15, 2026

The user authorized one fresh 20-agent run with the `counter-user-v1` correction
and a new shared $100 allowance. `max_pilot_usd` is $307.809660142 cumulatively:
prior settled $205.049799792 plus eight held requests totaling $2.75986035, plus
exactly $100. The previous unused $0.245961532 is replaced rather than added.
`pilot_prior_settled_usd`, `pilot_prior_held_usd`, `pilot_prior_committed_usd`
($207.809660142), `pilot_prior_uncertain_requests` (8), and
`pilot_additional_allowance_usd` (100) bind the exact approval boundary.
`pilot_prior_calls_sha256` additionally binds all original pilot request rows.
`prompt_cache_normalization` must be `counter-user-v1`, matching the runtime's
recorded request transformation. Prior holds must stay untouched during this run.
Keep default `paid_launch_enabled` false; only the one-shot supervisor's private
launch configuration enables the authorized run after full preflight. No provider,
reasoning, memory, task, seed, judge or isolation setting changes.

The user subsequently extended this cached run to `max_run_seconds: 10800`
(180 minutes), keeping the additional shared $100 allowance unchanged.
