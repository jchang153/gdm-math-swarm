"""Evidence-based launch gates for the paid pilot (interface I3).

``evaluate(config, repo_root)`` checks frozen data, the budget ledger and the newest
validation evidence in ``data/validation/`` (interface I6: ``<prefix>-<UTC YYYYMMDDTHHMMSSZ>.json``
with ``schema``, ``created_at``, ``all_required_passed`` and ``image_ids``). Every check
reports its evidence path and a reason.

Spending (owner decisions, docs/plan.md "One per-run budget, no manual approvals (October 20,
2026)"): the one spending control is the run's own budget, enforced by the ledger on every
request (``budget.BudgetLedger.reserve``). The ``run_budget`` gate only validates the config's
``run_budget_usd`` (absent means the $100 default; an invalid value is refused). The ``ledger``
gate is informational: cumulative history, holds and estimate violations of earlier runs never
block a new run. There is no cumulative cap, no separate compatibility allowance, no
provider-key limit refusal (``provider_limit`` is informational: the paid launch checks the key
it loads and records the result), no typed or attested approval and no cap equality with the
plan. A stale route-pricing freeze is a warning: the launch scripts refresh it automatically
(unpaid) first; a freeze whose catalog no longer validates against the config's reservation
prices still refuses (the reservations must be upper bounds).

Runtime evidence must name its required checks (``REQUIRED_EVIDENCE_CHECKS``,
``ISOLATION_AGENT_CHECKS``), so a record from an older validation script that
lacked a check cannot satisfy the gate. Runtime evidence is also bound to the
compose file a launch would write (``compose_sha256`` equal to
``runtime.compose_fingerprint(config, current agent image ID)``, ``COMPOSE_BOUND_EVIDENCE``),
and Lean evidence must come from a complete run at the judge limits (``complete``
true and ``limits`` equal to the config's judge limits, ``JUDGE_LIMIT_EVIDENCE``,
which includes the ``conference-race`` evidence from scripts/validate_conference.py).
The evaluator gate also requires ``evaluator.validation_status()`` to accept the
record (fixture manifest, pins, seccomp, image) and a passing self-comparison for
every pilot template. The compatibility gate checks the live compatibility evidence
(model, route, settings, agent image, required checks); the ledger state of the requests the
check admitted (``request_ids``) is reported, never refused.

The config itself must match the approved plan values (``approved_settings()``;
its ``agent_count`` is ``runtime.APPROVED_AGENT_COUNT``, the single source of
truth), state ``max_connections`` explicitly as one in-flight inference request
per agent (``max_connections_problems``), and carry no seeded-contagion
configuration at all (``seeded_track`` gate; configs/seeded.example.json is never
a pilot config).

Never loads a credential (credential checks are presence/permissions only), never
contacts the network, never writes: the ledger is read read-only. The only external
calls are ``docker image inspect`` against the local daemon to compare image IDs
(here and inside ``evaluator.validation_status``).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .budget import (DEFAULT_LEDGER_PATH, DEFAULT_RUN_BUDGET_USD, LEDGER_STATES, LEGACY_LEDGER_RELATIVE,
                     RUN_BUDGET_KEY, UNRESOLVED_STATES, VIOLATION_STATES, list_requests, read_snapshot, run_budget)
from .provider import CONFIG_OVERRIDABLE, MODEL_ID, frozen_fields, settings_from_config, validate_catalog

PILOT_SUBSET = "data/pilot_subset.json"
PARENT_MANIFEST = "data/manifest.json"
VALIDATION_DIR = "data/validation"
DEFAULT_IMAGES = {"lean": "gdm-math-swarm-lean:local", "agent": "gdm-math-swarm-agent:local",
                  "comparator": "gdm-math-swarm-comparator:local"}
DEFAULT_MAX_AGE_HOURS = 72

# Approved values from docs/plan.md that the run config must not drift from. A
# change needs a recorded user decision and an update here by the integrator.
# ``agent_count`` is deliberately absent: it comes from runtime.APPROVED_AGENT_COUNT
# (single source of truth, imported lazily because runtime imports this module
# lazily too); ``approved_settings()`` and ``APPROVED_SETTINGS`` add it.
_APPROVED_STATIC_SETTINGS = {
    "track": "spontaneous", "seeded_contagion_enabled": False, "mitigations_enabled": False,  # Initial execution
    "task_count": 14,                                          # Current pilot scope / Initial task sampling
    "max_run_seconds": 10800, "post_depletion_seconds": 600,    # Run duration and stopping
    "compilation_timeout_seconds": 120,                        # Compilation time limit
    "system_prompt_mode": "append", "claude_auto_memory": True,  # Effective agent system prompt
    "general_internet_enabled": False,                         # Controlled baseline access
    "provider": "openrouter", "candidate_model_id": MODEL_ID,  # Model and intended API route
}
# Inference concurrency (docs/plan.md checklist item 4 "Scheduling" and "Remaining
# model-setting authority"): one in-flight request per agent, i.e. ``max_connections``
# equal to ``agent_count``, so no agent queues behind another inside Inspect. The
# key is REQUIRED and explicit (an absent key used to default silently). A different
# rule needs a documented plan decision and a change here.
MAX_CONNECTIONS_KEY = "max_connections"
# A pilot config carries no seeded-contagion configuration at all ("Initial
# execution": seeded disabled; configs/seeded.example.json is the seeded template).
SEEDED_CONFIG_KEY = "seeded"


def approved_settings():
    """The approved plan values, with ``agent_count`` from runtime.APPROVED_AGENT_COUNT."""
    from . import runtime  # lazy: runtime imports this module lazily as well
    return {"agent_count": runtime.APPROVED_AGENT_COUNT, **_APPROVED_STATIC_SETTINGS}


def __getattr__(name):
    # PEP 562: ``launch_gates.APPROVED_SETTINGS`` (and ``from ... import``) stays
    # available, computed on access so it can never drift from the runtime constant.
    if name == "APPROVED_SETTINGS":
        return approved_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def max_connections_problems(config):
    """``max_connections`` must be present and equal ``agent_count`` (one in-flight request per agent)."""
    value, agents = config.get(MAX_CONNECTIONS_KEY), config.get("agent_count")
    if MAX_CONNECTIONS_KEY not in config or value is None:
        return [f"{MAX_CONNECTIONS_KEY} is required and must be explicit (equal to agent_count: one in-flight "
                "inference request per agent, plan checklist item 4)"]
    if not _is_int(value) or value < 1:
        return [f"{MAX_CONNECTIONS_KEY} {value!r} is not a positive integer"]
    if value != agents:
        return [f"{MAX_CONNECTIONS_KEY} {value} differs from agent_count {agents!r} (the approved rule is one "
                "in-flight inference request per agent; a different value needs a documented plan decision)"]
    return []


def seeded_config_problems(config):
    """Any seeded-contagion configuration blocks the approved (spontaneous, baseline-only) pilot."""
    problems = []
    if config.get("track") != "spontaneous":
        problems.append(f"track {config.get('track')!r} is not 'spontaneous'")
    if config.get("seeded_contagion_enabled") is not False:
        problems.append(f"seeded_contagion_enabled {config.get('seeded_contagion_enabled')!r} is not false")
    if config.get(SEEDED_CONFIG_KEY) is not None:
        problems.append(f"config carries a seeded-track configuration ({SEEDED_CONFIG_KEY!r}); the approved pilot "
                        "runs spontaneous discovery only and must carry none, enabled or not")
    return problems


# Config keys of the superseded spending controls (cumulative caps, attestations, approval notes,
# freshness limits, repeat bindings). They are not read; a config that still names one gets a
# warning in the ``run_budget`` gate, never a refusal. ``run_budget_usd`` is the run's budget.
OBSOLETE_SPENDING_KEYS = (
    "max_pilot_usd", "max_analysis_usd", "max_compatibility_usd", "verified_integration", "paid_launch_enabled",
    "allow_unlimited_provider_key", "unlimited_provider_key_approval", "provider_limit_max_age_hours",
    "pilot_prior_settled_usd", "pilot_additional_allowance_usd", "pilot_prior_held_usd", "pilot_prior_committed_usd",
    "pilot_prior_uncertain_requests", "pilot_prior_calls_sha256", "behavior_judge_approval")
# Recorded request transformation (docs/prompt-caching.md): a config that names the version must
# name the one the runtime applies (research integrity, not spending).
PROMPT_CACHE_NORMALIZATION_KEY = "prompt_cache_normalization"

# (evidence prefix, image roles that must match, image roles checked only if recorded)
EVIDENCE_GATES = (
    ("declarations", ("lean",), ()),
    ("judge", ("lean",), ()),
    ("timing", ("lean",), ()),
    ("reuse", ("lean",), ()),
    ("conference-race", (), ("lean",)),
    ("evaluator", ("comparator",), ()),
    ("runtime-e2e", ("agent",), ("lean",)),
    ("runtime-isolation", ("agent",), ()),
    ("runtime-capacity", ("agent",), ("lean",)),
)
# Config keys whose values must equal the configuration the capacity probe measured
# (runtime-capacity evidence ``validated_config``).
CAPACITY_KEYS = ("agent_count", "agent_memory", "agent_cpus", "judge_max_concurrent", "judge_memory", "judge_cpus",
                 "compilation_timeout_seconds", "max_subprocesses", "startup_timeout_seconds", "max_connections",
                 "warm_up_memory", "warm_up_timeout_seconds")


def capacity_config(config):
    """Effective runtime settings: omitted optional keys equal their runtime defaults."""
    from . import runtime
    warm_up = runtime.warm_up_options(config)
    defaults = {"agent_count": runtime.APPROVED_AGENT_COUNT, "agent_memory": "1g", "agent_cpus": None,
                "judge_max_concurrent": None, "judge_memory": "2g", "judge_cpus": 1,
                "compilation_timeout_seconds": 120, "max_subprocesses": 64, "startup_timeout_seconds": 600}
    effective = {key: config.get(key, defaults.get(key)) for key in CAPACITY_KEYS}
    effective.update(max_subprocesses=int(effective["max_subprocesses"]),
                     warm_up_memory=warm_up["memory"], warm_up_timeout_seconds=warm_up["timeout"])
    return effective


# Runtime evidence that must record ``compose_sha256``: the SHA-256 of the compose
# text the probe ran, equal to runtime.compose_fingerprint(config, current agent
# image ID), so a later change to the generated compose file invalidates it.
COMPOSE_BOUND_EVIDENCE = ("runtime-e2e", "runtime-isolation", "runtime-capacity")
# Lean evidence that must come from a complete run (all pilot tasks, strict variant
# where applicable, no subset/skip flags: ``complete`` true) at the operational
# judge limits (``limits`` {timeout_seconds, memory, cpus} equal to the config's).
# scripts/validate_conference.py compiles with the judge limits from --config too, so
# its conference-race evidence is checked the same way.
JUDGE_LIMIT_EVIDENCE = ("declarations", "judge", "timing", "conference-race")
# Config key and default (runtime.make_lean_compiler) for each judge limit.
JUDGE_LIMIT_KEYS = {"timeout_seconds": ("compilation_timeout_seconds", 120), "memory": ("judge_memory", "2g"),
                    "cpus": ("judge_cpus", 1)}

# Check keys that must be present AND passed in runtime evidence, whatever the
# record's own ``required`` flags say (scripts/validate_runtime.py build_checks).
REQUIRED_EVIDENCE_CHECKS = {
    "runtime-e2e": (
        "sample_errors", "claude_code_executed_bash_lean", "bash_time_limited_to_120s", "identity_bound_board_posts",
        "cross_agent_direct_message", "sandbox_file_submission", "real_judge_rejects_unmodified_template",
        "sandbox_file_outside_root_rejected", "publish_notebook", "read_library_file",
        "barrier_released_only_after_late_agent", "inspect_logs_capture_model_and_tool_calls",
        "inspect_tool_result_replays_consistent", "file_snapshots",
        "effective_instructions_archived", "disallowed_tools_absent", "budget_filter_reserve_settle",
        "truncated_tool_call_not_executed_and_session_continues", "truncation_exhaustion_not_voluntary_exit",
        "voluntary_exit_recorded", "conference_event_log_ordered",
        # Written required=False/skipped when the scenario did not run (offline
        # rehearsal or --skip-background-lean-check); only this by-name requirement
        # rejects such evidence.
        "background_lean_killed_at_120s", "compose_matches_fingerprint"),
    "runtime-isolation": ("judge_container_network_none", "compose_matches_fingerprint", "sample_errors"),
    "runtime-capacity": (
        "completed_without_interrupt", "all_agents_ran", "no_oom_kills", "warm_up_attempted", "no_compile_timeouts",
        "all_agents_reached_barrier_within_startup_timeout", "compose_matches_fingerprint", "judge_environment_ok",
        "bridge_exec_latency", "memory_headroom", "budget_ledger_contention", "no_conflicting_containers_at_start"),
}
# scripts/isolation_probes.py: "<agent>:<check>" for every probed agent; at least
# two agents (cross-agent reachability needs a peer).
ISOLATION_AGENT_CHECKS = (
    "probes_completed", "egress_denied", "no_docker_socket_or_host_mounts", "no_provider_keys",
    "no_repository_or_researcher_content", "dependency_and_system_paths_read_only", "no_privileges",
    "cgroup_limits_match_compose", "sandbox_file_escapes_rejected", "other_agent_files_unreachable", "web_and_nested_agent_tools_absent")
MIN_ISOLATION_AGENTS = 2

# Shared interface with scripts/compat_check.py (data/validation/compatibility-<UTC>.json).
COMPATIBILITY_SCHEMA = "gdm-compatibility/1"
COMPATIBILITY_REQUIRED_CHECKS = (
    "tool_call_parsed", "conference_tool_call", "reasoning_replay_accepted", "provider_cost_reported",
    "completion_within_max_tokens", "reasoning_cap_enforced", "no_estimate_violation", "route_pinned",
    "no_sample_error")
COMPATIBILITY_INFORMATIONAL_CHECKS = ("truncation_observed", "compaction_observed")


# ---------------------------------------------------------------- evidence files

def evidence_name(prefix, when=None):
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return f"{prefix}-{when.astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"


def evidence_files(validation_dir, prefix):
    """I6 files for ``prefix`` sorted oldest -> newest (the timestamp sorts by name)."""
    pattern = re.compile(re.escape(prefix) + r"-(\d{8}T\d{6}Z)\.json")
    folder = Path(validation_dir)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and pattern.fullmatch(p.name))


def newest_evidence(validation_dir, prefix):
    """(path, data) of the newest file for ``prefix``; (None, None) if there is none."""
    files = evidence_files(validation_dir, prefix)
    if not files:
        return None, None
    return files[-1], json.loads(files[-1].read_text())


def route_pricing_refresh_reason(config, validation_dir, now=None):
    """Why the launch scripts should refresh the route-pricing freeze (scripts/freeze_route.py,
    unpaid) before launching, or None: no freeze yet, a freeze older than
    ``route_pricing_max_age_hours`` (default 72), or one frozen for other settings."""
    path, record = newest_evidence(validation_dir, "route-pricing")
    if path is None:
        return "no route-pricing freeze yet"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    age = (now - _created(record, path)).total_seconds() / 3600
    limit = float((config or {}).get("route_pricing_max_age_hours", DEFAULT_MAX_AGE_HOURS))
    if age > limit or age < -0.1:
        return f"the newest freeze is {age:.1f} h old (refresh after {limit:g} h)"
    frozen = {k: v for k, v in (record.get("settings") or {}).items() if k in CONFIG_OVERRIDABLE}
    if frozen != frozen_fields(settings_from_config(config or {})):
        return "the newest freeze was made for other provider settings"
    return None


def write_evidence(validation_dir, prefix, record, when=None):
    """Write an I6 evidence file (adds schema/created_at/image_ids defaults)."""
    when = when or datetime.datetime.now(datetime.timezone.utc)
    record = {"schema": f"{prefix}/v1", "created_at": when.astimezone(datetime.timezone.utc).isoformat(),
              "image_ids": {}, **record}
    if not isinstance(record.get("all_required_passed"), bool):
        raise ValueError("Evidence requires a boolean all_required_passed")
    folder = Path(validation_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / evidence_name(prefix, when)
    path.write_text(json.dumps(record, indent=2, default=str) + "\n")
    return path


def _created(record, path):
    stamp = record.get("created_at") or record.get("retrieved_utc")
    if stamp:
        value = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    match = re.search(r"(\d{8}T\d{6}Z)\.json$", Path(path).name)
    return datetime.datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _norm_id(value):
    value = str(value or "")
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def check_problems(checks, names):
    """Problems for ``names`` that are missing from ``checks`` or not passed."""
    checks = checks if isinstance(checks, dict) else {}
    missing = [n for n in names if not isinstance(checks.get(n), dict)]
    failed = [n for n in names if isinstance(checks.get(n), dict) and checks[n].get("passed") is not True]
    problems = []
    if missing:
        problems.append("missing required checks (older or different script version?): " + ", ".join(missing))
    if failed:
        problems.append("required checks not passed: " + ", ".join(failed))
    return problems


def _same_limit(name, recorded, expected):
    """Judge-limit equality as Docker reads it: memory case-insensitively, numbers by value."""
    if recorded is None or expected is None or isinstance(recorded, bool) or isinstance(expected, bool):
        return recorded is None and expected is None
    if name == "memory":
        return str(recorded).strip().lower() == str(expected).strip().lower()
    try:
        return Decimal(str(recorded)) == Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return False


def judge_limit_problems(record, config):
    """X5: ``complete`` must be true and ``limits`` must equal the config's judge limits."""
    problems = []
    if record.get("complete") is not True:
        problems.append("evidence is not from a complete run (complete is not true: a subset, skipped case, "
                        "non-strict or custom-workload run cannot satisfy the gate)")
    limits = record.get("limits")
    if not isinstance(limits, dict):
        problems.append("evidence lacks limits {timeout_seconds, memory, cpus} (older or different script version?)")
        return problems
    drift = [f"{name} {limits.get(name)!r} (config {key} {config.get(key, default)!r})"
             for name, (key, default) in JUDGE_LIMIT_KEYS.items()
             if name not in limits or not _same_limit(name, limits.get(name), config.get(key, default))]
    if drift:
        problems.append("compiled at limits other than the operational judge's: " + ", ".join(drift))
    return problems


def isolation_check_problems(checks):
    checks = checks if isinstance(checks, dict) else {}
    agents = sorted({k.split(":", 1)[0] for k in checks if ":" in k})
    problems = check_problems(checks, [f"{a}:{c}" for a in agents for c in ISOLATION_AGENT_CHECKS])
    if len(agents) < MIN_ISOLATION_AGENTS:
        problems.append(f"per-agent isolation checks cover {len(agents)} agent(s); at least {MIN_ISOLATION_AGENTS} "
                        "are required")
    return problems


def docker_image_id(tag):
    """Local daemon lookup (no registry/network access); None when unavailable."""
    try:
        proc = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# ---------------------------------------------------------------- evaluation

class _Gates:
    def __init__(self, repo_root, config, validation_dir, now, image_id):
        self.repo = Path(repo_root).resolve()
        self.config = config
        self.validation = Path(validation_dir) if validation_dir else self.repo / VALIDATION_DIR
        self.now = now
        self.lookup = image_id
        self.images = {}
        self.results = {}

    def rel(self, path):
        if path is None:
            return None
        try:
            return str(Path(path).resolve().relative_to(self.repo))
        except ValueError:
            return str(path)

    def add(self, name, passed, reason, evidence=None, **details):
        self.results[name] = {"passed": bool(passed), "evidence": self.rel(evidence) if evidence else None,
                              "reason": reason, **details}

    def run(self, name, check):
        try:
            check()
        except Exception as ex:  # a gate that cannot be evaluated blocks launch
            self.add(name, False, f"check could not be evaluated: {type(ex).__name__}: {ex}")

    def image(self, role):
        tag = self.config.get(f"{role}_image") or DEFAULT_IMAGES[role]
        if tag not in self.images:
            self.images[tag] = self.lookup(tag)
        return tag, self.images[tag]

    def match_images(self, recorded, required, optional):
        problems, matched = [], {}
        recorded = recorded if isinstance(recorded, dict) else {}
        for role in (*required, *optional):
            tag, current = self.image(role)
            keyed = next((recorded[k] for k in (tag, role) if k in recorded), None)
            if role in optional and keyed is None:
                continue
            if current is None:
                problems.append(f"current {role} image {tag} not found locally")
            elif keyed is not None and _norm_id(keyed) != _norm_id(current):
                problems.append(f"{role} image changed since evidence ({_norm_id(keyed)[:12]} != {_norm_id(current)[:12]})")
            elif keyed is None and _norm_id(current) not in {_norm_id(v) for v in recorded.values()}:
                problems.append(f"evidence does not record the current {role} image {tag}")
            else:
                matched[role] = current
        return problems, matched

    # -- individual gates ---------------------------------------------------

    def task_manifest(self):
        spec = self.config.get("fixed_task_manifest")
        subset_path, parent_path = self.repo / PILOT_SUBSET, self.repo / PARENT_MANIFEST
        if not isinstance(spec, dict):
            return self.add("fixed_task_manifest", False, "config fixed_task_manifest must be {path, sha256, parent_manifest_sha256}")
        subset_sha, parent_sha = _sha256(subset_path), _sha256(parent_path)
        recorded_parent = json.loads(subset_path.read_text()).get("parent_manifest_sha256")
        problems = []
        if Path(str(spec.get("path", ""))).as_posix() != PILOT_SUBSET:
            problems.append(f"path must be {PILOT_SUBSET}")
        if spec.get("sha256") != subset_sha:
            problems.append("sha256 differs from data/pilot_subset.json")
        if not (spec.get("parent_manifest_sha256") == recorded_parent == parent_sha):
            problems.append("parent_manifest_sha256 must equal pilot_subset.json's record and sha256(data/manifest.json)")
        self.add("fixed_task_manifest", not problems, "; ".join(problems) or "config pins the frozen 14-task subset",
                 subset_path, sha256=subset_sha, parent_manifest_sha256=parent_sha)

    def frozen_dataset(self):
        from .dataset import FrozenDataset
        dataset = FrozenDataset(self.repo)
        for task in dataset.pilot_tasks:
            task.template_source(self.repo)  # raises on a template hash mismatch
        self.add("frozen_dataset", True, "FrozenDataset loads; 71 parent tasks, 14 pilot templates hash-verified",
                 self.repo / PILOT_SUBSET, seed=dataset.subset.get("seed"), task_ids=list(dataset.subset["task_ids"]))

    def approved_settings(self):
        approved = approved_settings()
        drift = {k: {"config": self.config.get(k), "approved": v} for k, v in approved.items()
                 if self.config.get(k) != v or type(self.config.get(k)) is not type(v)}
        problems = ["config differs from approved plan values: " + ", ".join(sorted(drift))] if drift else []
        connections = max_connections_problems(self.config)
        if connections:
            drift[MAX_CONNECTIONS_KEY] = {"config": self.config.get(MAX_CONNECTIONS_KEY),
                                          "approved": "equal to agent_count"}
        problems += connections
        if PROMPT_CACHE_NORMALIZATION_KEY in self.config:
            from .prompt_cache import TOKEN_COUNTER_NORMALIZATION_VERSION
            named = self.config.get(PROMPT_CACHE_NORMALIZATION_KEY)
            if named != TOKEN_COUNTER_NORMALIZATION_VERSION:
                drift[PROMPT_CACHE_NORMALIZATION_KEY] = {"config": named, "approved": TOKEN_COUNTER_NORMALIZATION_VERSION}
                problems.append(f"{PROMPT_CACHE_NORMALIZATION_KEY} {named!r} differs from the runtime's recorded "
                                f"normalization {TOKEN_COUNTER_NORMALIZATION_VERSION!r} (docs/prompt-caching.md)")
        self.add("approved_settings", not problems, "; ".join(problems) or
                 "config matches approved plan values (max_connections equals agent_count)", drift=drift,
                 max_connections=self.config.get(MAX_CONNECTIONS_KEY), agent_count=self.config.get("agent_count"))

    def seeded_track(self):
        problems = seeded_config_problems(self.config)
        self.add("seeded_track", not problems, "; ".join(problems) or
                 "spontaneous track; no seeded-contagion configuration present")

    def behavior_judge(self):
        from .auto_judge import enabled, judge_budget, judge_config_problems, live_settings
        problems = judge_config_problems(self.config)
        if problems or not enabled(self.config):
            return self.add("behavior_judge", not problems, "; ".join(problems) or "no automatic post-run behavior judge")
        amount, source = judge_budget(self.config)
        live = live_settings(self.config)
        during = {"paid": f"; live judging during the run every {live['interval_seconds']} s (at most "
                          f"${live['max_usd']} of the judge's budget)",
                  "mock": "; free mock shadow of live judging during the run"}
        self.add("behavior_judge", True, f"automatic post-run behavior judge enabled; its budget for the run is "
                 f"${amount} ({source})" + during.get(live["mode"], ""), budget_usd=str(amount), budget_source=source,
                 live_mode=live["mode"], live_max_usd=live["max_usd"] if live["mode"] == "paid" else None)

    def run_budget(self):
        """The run's budget, the one spending control: ``run_budget_usd`` when present (it must be a
        positive amount), else the $100 default. Superseded spending keys are reported, never refused."""
        warnings = []
        named = [key for key in OBSOLETE_SPENDING_KEYS if key in self.config]
        if named:
            warnings.append("config names superseded spending keys that are not read: " + ", ".join(named)
                            + f" (the run's budget is {RUN_BUDGET_KEY})")
        try:
            amount, source = run_budget(self.config)
        except ValueError as ex:
            return self.add("run_budget", False, f"{ex}; set {RUN_BUDGET_KEY} to a positive amount in USD or remove it "
                            f"for the ${DEFAULT_RUN_BUDGET_USD} default", warnings=warnings)
        self.add("run_budget", True, f"run budget ${amount} ({source}); a request is admitted only while this run's "
                 "committed spend plus its reservation fits it", budget_usd=str(amount), budget_source=source,
                 warnings=warnings)

    def route_pricing(self):
        path, record = newest_evidence(self.validation, "route-pricing")
        if path is None:
            return self.add("route_pricing", False, "no route-pricing evidence; scripts/run_pilot.py and "
                            "scripts/compat_check.py run scripts/freeze_route.py (unpaid) automatically")
        problems, warnings = [], []
        settings = settings_from_config(self.config)
        if record.get("valid") is not True:
            problems.append(f"freeze invalid: {record.get('reason')} (the config's reservation prices must cover the "
                            "route's current prices)")
        if record.get("credential_used") is not False:
            problems.append("freeze must record credential_used=false")
        age = (self.now - _created(record, path)).total_seconds() / 3600
        limit = float(self.config.get("route_pricing_max_age_hours", DEFAULT_MAX_AGE_HOURS))
        if age > limit or age < -0.1:
            warnings.append(f"freeze is {age:.1f} h old (refresh after {limit:g} h); scripts/run_pilot.py and "
                            "scripts/compat_check.py refresh it automatically before launch (unpaid)")
        frozen = {k: v for k, v in (record.get("settings") or {}).items() if k in CONFIG_OVERRIDABLE}
        if frozen != frozen_fields(settings):
            problems.append("frozen settings differ from the ProviderSettings run_pilot will build (the launch "
                            "scripts refresh the freeze for this config automatically)")
        snapshot = self.validation / "catalog-snapshots" / f"{record.get('catalog_sha256')}.json"
        if not snapshot.is_file() or _sha256(snapshot) != record.get("catalog_sha256"):
            problems.append("archived catalog snapshot missing or hash mismatch")
        else:
            try:
                validate_catalog(json.loads(snapshot.read_bytes()), settings)
            except ValueError as ex:
                problems.append(f"offline catalog validation failed: {ex}")
        self.add("route_pricing", not problems, "; ".join(problems) or "valid and equal to the launch settings",
                 path, age_hours=round(age, 2), provider_route=settings.provider_route,
                 prompt_usd_per_token=settings.prompt_usd_per_token, output_usd_per_token=settings.output_usd_per_token,
                 warnings=warnings)

    def evidence(self, prefix, required, optional):
        path, record = newest_evidence(self.validation, prefix)
        if path is None:
            return self.add(prefix, False, f"no {prefix}-<UTC>.json evidence in {self.rel(self.validation)}")
        problems = []
        if record.get("all_required_passed") is not True:
            problems.append("all_required_passed is not true")
        image_problems, matched = self.match_images(record.get("image_ids"), required, optional)
        problems += image_problems
        details = {"created_at": record.get("created_at"), "images": matched}
        if prefix in REQUIRED_EVIDENCE_CHECKS:
            problems += check_problems(record.get("checks"), REQUIRED_EVIDENCE_CHECKS[prefix])
        if prefix == "runtime-isolation":
            problems += isolation_check_problems(record.get("checks"))
        if prefix in COMPOSE_BOUND_EVIDENCE:
            problems += self.compose_problems(record, details)
        if prefix in JUDGE_LIMIT_EVIDENCE:
            problems += judge_limit_problems(record, self.config)
            details.update(complete=record.get("complete"), limits=record.get("limits"))
        if prefix == "evaluator":
            problems += self.evaluator_validation(path, record, details)
        if prefix == "runtime-capacity":
            validated = record.get("validated_config")
            if not isinstance(validated, dict):
                problems.append("capacity evidence lacks validated_config")
            else:
                effective = capacity_config(self.config)
                drift = {k: {"config": effective[k], "validated": validated.get(k)} for k in CAPACITY_KEYS
                         if k in validated and effective[k] != validated.get(k)}
                missing = [k for k in CAPACITY_KEYS if k not in validated]
                if drift:
                    problems.append("config differs from the measured configuration: " + ", ".join(sorted(drift)))
                if missing:
                    problems.append("validated_config lacks " + ", ".join(missing))
                details["validated_config"] = validated
        self.add(prefix, not problems, "; ".join(problems) or "passed against current images", path, **details)

    def compose_problems(self, record, details):
        """X3: the probed compose text must equal what make_compose writes for this config and agent image."""
        recorded = record.get("compose_sha256")
        details["compose_sha256"] = recorded
        if not isinstance(recorded, str) or not recorded:
            return ["evidence lacks compose_sha256 (older script version?); re-run it so it is bound to the compose "
                    "file a launch writes"]
        _, current = self.image("agent")
        if current is None:
            return []  # match_images already reports the missing agent image
        from . import runtime  # lazy: the gates otherwise need no runtime module
        expected = runtime.compose_fingerprint(self.config, current)
        details["expected_compose_sha256"] = expected
        if recorded != expected:
            return [f"compose file differs from the one validated (compose_sha256 {recorded[:12]} != {expected[:12]}, "
                    "runtime.compose_fingerprint for this config and the current agent image); re-run the probe"]
        return []

    def evaluator_validation(self, path, record, details):
        """evaluator.validation_status() (local only) plus pilot self-comparison coverage."""
        from . import evaluator
        from .dataset import FrozenDataset
        problems = []
        tag, current = self.image("comparator")
        status = evaluator.validation_status(validation_dir=self.validation, image=tag)
        details["validation_status"] = {k: status.get(k) for k in (
            "valid", "error", "image_id", "self_compared_templates", "bridge_self_compared_templates")}
        if status.get("valid") is not True:
            problems.append(f"evaluator.validation_status rejects the record: {status.get('error')}")
        else:
            if not status.get("record") or Path(status["record"]).resolve() != Path(path).resolve():
                problems.append("evaluator.validation_status checked a different record than the newest evidence")
            if current is None or _norm_id(status.get("image_id")) != _norm_id(current):
                problems.append("validated comparator image differs from the current comparator image")
        # Pilot coverage: DockerComparatorBackend returns `unresolved` for any template
        # without a passing pristine self-comparison (and, for answer-based tasks, a
        # passing helper-answer bridge self-comparison).
        passed = [e for e in record.get("self_compare") or []
                  if isinstance(e, dict) and evaluator.self_compare_passes(e)]
        pristine = {e.get("template_sha256") for e in passed if e.get("kind") == "pristine"}
        bridge = {e.get("template_sha256") for e in passed if e.get("kind") == "bridge"}
        gaps = []
        for task in FrozenDataset(self.repo).pilot_tasks:
            digest = hashlib.sha256(task.template_source(self.repo).encode("utf-8")).hexdigest()
            if digest not in pristine:
                gaps.append(f"{task.task_id}/pristine")
            if task.answer_based and digest not in bridge:
                gaps.append(f"{task.task_id}/bridge")
        if gaps:
            problems.append("pilot templates lack a passing self-comparison (run scripts/validate_evaluator.py "
                            "--pilot-self-compare): " + ", ".join(gaps))
        details["pilot_self_compare_gaps"] = gaps
        return problems

    def compatibility(self, ledger_path=None):
        """Evidence from the live compatibility check (scripts/compat_check.py): the frozen model,
        route and settings work with Claude Code on the current agent image (research integrity).
        The ledger state of the requests it admitted is reported (warnings), never refused: the
        check had its own run budget and spending has no other control."""
        path, record = newest_evidence(self.validation, "compatibility")
        if path is None:
            return self.add("compatibility", False, "no compatibility-<UTC>.json evidence; run the live compatibility "
                            "check (scripts/compat_check.py; its own run budget, ledger scope 'compatibility')")
        problems = []
        if record.get("schema") != COMPATIBILITY_SCHEMA:
            problems.append(f"schema is not {COMPATIBILITY_SCHEMA}")
        if record.get("all_required_passed") is not True:
            problems.append("all_required_passed is not true")
        checks = record.get("checks") if isinstance(record.get("checks"), dict) else {}
        problems += check_problems(checks, COMPATIBILITY_REQUIRED_CHECKS)
        settings = settings_from_config(self.config)
        if record.get("model_id") != settings.model_id:
            problems.append(f"model_id {record.get('model_id')!r} is not {settings.model_id!r}")
        if record.get("provider_route") != settings.provider_route:
            problems.append(f"provider_route {record.get('provider_route')!r} is not {settings.provider_route!r}")
        checked = record.get("settings") if isinstance(record.get("settings"), dict) else {}
        if {k: v for k, v in checked.items() if k in CONFIG_OVERRIDABLE} != frozen_fields(settings):
            problems.append("checked settings differ from the ProviderSettings run_pilot will build")
        try:
            spent = Decimal(str(record.get("spent_usd")))
            if not spent.is_finite() or spent < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            problems.append(f"spent_usd {record.get('spent_usd')!r} is not a nonnegative decimal")
        requests = record.get("requests")
        if not isinstance(requests, int) or isinstance(requests, bool) or requests < 1:
            problems.append("requests must be a positive integer")
        image_problems, matched = self.match_images(record.get("image_ids"), ("agent",), ())
        problems += image_problems
        warnings, ledger_requests = self.compatibility_requests(record, requests, ledger_path)
        informational = {name: checks[name].get("passed") if isinstance(checks.get(name), dict) else None
                         for name in COMPATIBILITY_INFORMATIONAL_CHECKS}
        warnings += [f"informational check {name} not observed"
                     for name, value in informational.items() if value is not True]
        self.add("compatibility", not problems, "; ".join(problems) or
                 "live compatibility evidence passed for the frozen model, route, settings and agent image", path,
                 created_at=record.get("created_at"), images=matched, spent_usd=record.get("spent_usd"),
                 run_budget_usd=record.get("run_budget_usd"), requests=requests, ledger_requests=ledger_requests,
                 warnings=warnings, informational=informational)

    def compatibility_requests(self, record, requests, ledger_path):
        """X4, informational: the ledger's CURRENT state of exactly the requests the check admitted,
        as (warnings, details). Holds, violations and unknown ids are reported, never refused."""
        ids = record.get("request_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            return ["evidence lacks request_ids (the ledger request ids the check admitted; older script version?)"], {}
        warnings = []
        if len(set(ids)) != len(ids):
            warnings.append("request_ids contains duplicates")
        if isinstance(requests, int) and not isinstance(requests, bool) and requests != len(set(ids)):
            warnings.append(f"requests {requests} differs from the {len(set(ids))} recorded request_ids")
        rows = {r["request_id"]: r for r in list_requests(ledger_path, states=LEDGER_STATES)} if ledger_path else {}
        found = {i: rows[i] for i in ids if i in rows}
        unknown = [i for i in ids if i not in rows]
        other_scope = [i for i, r in found.items() if r["scope"] != "compatibility"]
        unresolved = [i for i, r in found.items() if r["state"] in UNRESOLVED_STATES]
        violated = [i for i, r in found.items() if r["state"] in VIOLATION_STATES]
        if unknown:
            warnings.append(f"{len(unknown)} of the evidence's request_ids are not in this ledger")
        if other_scope:
            warnings.append(f"{len(other_scope)} of the evidence's requests are not in ledger scope 'compatibility'")
        if unresolved:
            warnings.append(f"{len(unresolved)} of the evidence's requests are unresolved in the ledger (counted against "
                            "the check's own run; scripts/reconcile_ledger.py settles them from provider records)")
        if violated:
            warnings.append(f"{len(violated)} of the evidence's requests were billed above their reservation "
                            "(estimate_violation, found after the check ended)")
        settled = sum((Decimal(r["actual_usd"]) for r in found.values() if r["actual_usd"] is not None), Decimal(0))
        states = {}
        for row in found.values():
            states[row["state"]] = states.get(row["state"], 0) + 1
        return warnings, {"count": len(ids), "states": states, "settled_usd": str(settled)}

    def provider_limit(self):
        """Informational only (never refuses): the newest provider-limit record, if any. Every paid
        launch checks the key it actually loads (unpaid GET /api/v1/key) and records the result;
        the run's budget is the spending control."""
        path, record = newest_evidence(self.validation, "provider-limit")
        if path is None:
            return self.add("provider_limit", True, "no provider-limit record yet; every paid launch checks the key it "
                            "loads (unpaid) and records it (informational)")
        warnings = []
        if re.search(r"\bsk-[A-Za-z0-9]", json.dumps(record)):
            warnings.append("record appears to contain a credential; delete it (the next launch writes a new one)")
            record = {}
        age = (self.now - _created(record, path)).total_seconds() / 3600 if record else None
        warnings = list(dict.fromkeys([*warnings, *(record.get("warnings") or [])]))
        self.add("provider_limit", True, "informational: every paid launch re-checks the key it loads and records the "
                 "result; never a refusal (the run's budget is the spending control)", path,
                 age_hours=None if age is None else round(age, 2), limit=record.get("limit"),
                 limit_remaining=record.get("limit_remaining"), limit_reset=record.get("limit_reset"),
                 warnings=warnings)

    def ledger(self, ledger_path, legacy):
        """Informational: the shared ledger's cumulative history per scope. Nothing here refuses a
        launch: earlier runs' holds and violations count against their own runs, and a missing
        ledger is created at the first paid request."""
        snapshot = read_snapshot(ledger_path)
        warnings = []
        if legacy.exists():
            warnings.append(f"legacy ledger {self.rel(legacy)} exists; its history is not in {self.rel(ledger_path)} "
                            "(it never affects admission)")
        for scope, data in sorted((snapshot or {}).items()):
            if data["unresolved_requests"]:
                warnings.append(f"{scope}: {data['unresolved_requests']} unresolved hold(s) (${data['held_usd']}) of "
                                "earlier runs, counted against their own runs until reconciled; never blocking")
        reason = ("no ledger yet; it is created at the first paid request" if snapshot is None else
                  "history only; admission is per run (" + ", ".join(
                      f"{scope} committed ${data['committed_usd']}" for scope, data in sorted(snapshot.items())) + ")")
        self.add("ledger", True, reason, ledger_path if snapshot is not None else None, path=self.rel(ledger_path),
                 exists=snapshot is not None, warnings=warnings)
        return snapshot

    def credentials(self):
        from .secrets import credential_status
        status = credential_status(self.repo)
        problems = []
        if not status["environment_variable_set"] and not status["env_file_present"]:
            problems.append("no OPENROUTER_API_KEY in the host environment and no host .env")
        if status["env_file_present"] and not (status["env_file_regular"] and status["env_file_private"]):
            problems.append("host .env must be a regular file with mode 600")
        self.add("credential_presence", not problems, "; ".join(problems) or "credential source present (not read)",
                 **status)


def evaluate(config, repo_root, *, validation_dir=None, ledger_path=None, legacy_ledger_path=None, now=None,
             image_id=None):
    """Return {"ready", "blockers", "evidence", "ledger"}; see module docstring.

    Keyword arguments exist for offline tests: alternative evidence directory and
    ledger paths, a fixed ``now`` (aware UTC) and an ``image_id(tag)`` lookup.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    gates = _Gates(repo_root, config, validation_dir, now, image_id or docker_image_id)
    ledger_path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER_PATH(gates.repo)
    legacy = Path(legacy_ledger_path) if legacy_ledger_path else gates.repo / LEGACY_LEDGER_RELATIVE
    gates.run("fixed_task_manifest", gates.task_manifest)
    gates.run("frozen_dataset", gates.frozen_dataset)
    gates.run("approved_settings", gates.approved_settings)
    gates.run("seeded_track", gates.seeded_track)
    gates.run("run_budget", gates.run_budget)
    gates.run("behavior_judge", gates.behavior_judge)
    gates.run("route_pricing", gates.route_pricing)
    for prefix, required, optional in EVIDENCE_GATES:
        gates.run(prefix, lambda p=prefix, r=required, o=optional: gates.evidence(p, r, o))
    state = {"snapshot": None}

    def ledger_gate():
        state["snapshot"] = gates.ledger(ledger_path, legacy)
    gates.run("ledger", ledger_gate)
    gates.run("compatibility", lambda: gates.compatibility(ledger_path))
    gates.run("provider_limit", gates.provider_limit)
    gates.run("credential_presence", gates.credentials)
    blockers = [f"{name}: {result['reason']}" for name, result in gates.results.items() if not result["passed"]]
    return {"ready": not blockers, "blockers": blockers, "evidence": gates.results,
            "ledger": {"path": gates.rel(ledger_path), "exists": state["snapshot"] is not None,
                       "snapshot": state["snapshot"]},
            "images": gates.images, "checked_at": now.isoformat(), "credentials_read": False, "network_calls": 0}
