#!/usr/bin/env python3
"""Local capacity probe: N genuine Claude Code peers plus concurrent Lean work
(Phase B, heavy; unpaid; mockllm-scripted).

Every peer runs through the production compose file, Inspect sandbox, solver
(runtime.peer_solver: bridge filter, snapshots, conference tools) and real
operational judge (runtime.make_lean_compiler, I1 queue). Each peer repeatedly
runs a local ``lake env lean`` check of a problem template through its Bash
tool (120 s clamp) and submits the unmodified template through submit_solution
(rejected by a real compile), until ``--duration`` seconds after the common
start barrier. Measured:

* ``docker stats`` memory/CPU of agent and judge containers every few seconds,
  OOM kills (``docker events`` oom/die 137, in-container memory.events oom_kill);
* local and judge compile-time distributions against the 120 s limit, judge
  queue waits (CompileResult.queue_seconds) and statuses;
* Inspect sandbox exec latency per peer (the bridge's docker-exec RPC path) and
  saturation of Inspect's ``subprocesses`` limiter (max_subprocesses);
* shared budget-ledger contention (throwaway ledger) and sample errors;
* startup: seconds from evaluation start to each agent's first inference request
  and to the common start barrier, which must all fall within the pilot's startup
  watchdog (``--startup-timeout``, default the config's ``startup_timeout_seconds``,
  600 s; a larger value fails the check).

Local compiles run through the agent image's lake wrapper (120 s, ``timeout -v -s
KILL``); a limit kill (exit 137 with the timeout notice) counts as a timeout, a
bare 137 as an OOM kill. The compose file is bound to the evidence
(``compose_sha256``, X3) and Inspect's .env loading is disabled before the
evaluation (X1).

Safe to interrupt (Ctrl-C): Inspect tears down its sandboxes; leftover probe
containers (compose projects ``inspect-gdm_capacity-i*`` and judge containers of
the lean image created after the start) are force-removed and a partial evidence
file marked ``interrupted`` is still written.

Defaults come from the pilot config (``--config``, default configs/pilot.json) for
every key launch_gates compares with ``validated_config`` (CAPACITY_KEYS): agent
count, agent memory/CPUs, judge concurrency (``judge_max_concurrent: null`` there
means unbounded; ``--judge-max-concurrent none`` likewise), judge memory/CPUs and
the compilation limit, startup timeout, subprocess and inference connection limits,
and warm-up memory/timeout. Measuring a different value is
allowed, but the evidence then satisfies the capacity gate only after the config is
changed to match (and any population change needs the user's agreement first).

As in run_pilot, image tags are pinned to local image IDs once (compose file and
judge compiler use the IDs) and a researcher-side Lean warm-up runs before the
agents start (``--no-warm-up`` measures a cold page cache instead and cannot certify
the pilot's warm-up configuration). Warm-up failure is reported but does not itself
block the capacity check. The mock model
gets an explicit ``max_connections`` (``--max-connections``, default the config's
``max_connections``, else the agent count), as the pilot does, so Inspect's adaptive
default never queues model calls; the value is recorded.

Nothing starts while the launch interlock (runtime.launch_interlock) finds a running
comparator container, a leftover judge container or another run's agent sandbox:
any of them would distort the measurements. ``--force-while-live`` (debugging only)
proceeds, records the override and fails the required check
``no_conflicting_containers_at_start``.

Writes data/validation/runtime-capacity-<UTC>.json (I6). Researcher artifacts go
to runs/validation-capacity-<UTC>/ (Git-ignored). Example:

    PYTHONPATH=src .venv/bin/python scripts/capacity_probe.py --duration 600
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_runtime as vr  # noqa: E402  (shared helpers; also sets INSPECT_DISPLAY)
from validate_runtime import ROOT, bash, check, conf_call, percentiles, text  # noqa: E402
from research_swarm import runtime  # noqa: E402
from research_swarm.launch_gates import capacity_config  # noqa: E402
from research_swarm.inspect_adapter import InstructionArchive, budget_filter  # noqa: E402
try:
    from research_swarm.lean import JUDGE_LABEL  # noqa: E402
except ImportError:  # older compiler module without labels
    JUDGE_LABEL = "gdm-judge=1"
from research_swarm.provider import ProviderSettings  # noqa: E402

TASK_NAME = "gdm_capacity"
PROJECT_PREFIX = f"inspect-{TASK_NAME[:12].rstrip('_')}-i"  # Inspect's task_project_name()
COMPILE_LIMIT_SECONDS = 120  # fallback when the config has no compilation_timeout_seconds
DEFAULT_CONFIG = ROOT / "configs" / "pilot.json"
LOCAL_MARK = re.compile(r"CAP_LOCAL rc=(\d+) ms=(\d+) task=(\S+)(?: limit_notice=(\d+))?")
STARTUP_TIMEOUT_SECONDS = 600  # fallback when the config has no startup_timeout_seconds (runtime.supervise)
STATE = {}  # partial results kept for an interrupted run


class RecordingCompiler:
    """Delegates to the production compiler and records every judge compile."""
    def __init__(self, inner):
        self.inner, self.records, self.lock = inner, [], threading.Lock()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def compile(self, source):
        start = time.monotonic()
        result = self.inner.compile(source)
        entry = {"status": result.status, "exit_code": result.exit_code, "elapsed_seconds": result.elapsed_seconds,
                 "queue_seconds": getattr(result, "queue_seconds", None),
                 "output_truncated": getattr(result, "output_truncated", None),
                 "wall_seconds": time.monotonic() - start, "finished_at": time.time()}
        with self.lock:
            self.records.append(entry)
        return result


def _bytes(text_value):
    match = re.match(r"([\d.]+)\s*([KMGT]?i?B)", text_value.strip())
    if not match:
        return None
    scale = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
             "TiB": 1024**4}.get(match.group(2), 1)
    return float(match.group(1)) * scale


class DockerSampler(threading.Thread):
    """docker stats of agent and judge containers every ``interval`` seconds."""
    def __init__(self, agent_image, lean_image, interval):
        super().__init__(daemon=True)
        # Pinned image IDs: `ancestor=` matches containers created from either reference.
        self.images, self.interval = {"agent": agent_image, "lean": lean_image}, interval
        self.samples, self.stop_event, self.seen = [], threading.Event(), set()

    def _ids(self, image):
        proc = subprocess.run(["docker", "ps", "-q", "--filter", f"ancestor={image}"], capture_output=True, text=True,
                              timeout=30)
        return proc.stdout.split()

    def run(self):
        while not self.stop_event.is_set():
            try:
                groups = {k: self._ids(v) for k, v in self.images.items()}
                ids = [i for v in groups.values() for i in v]
                self.seen.update(ids)
                rows = []
                if ids:
                    proc = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids],
                                          capture_output=True, text=True, timeout=60)
                    rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
                sample = {"t": time.time(), "loadavg": os.getloadavg()}
                for kind, members in groups.items():
                    mine = [r for r in rows if any(r.get("ID", "").startswith(m[:12]) or m.startswith(r.get("ID", "x"))
                                                   for m in members)]
                    mem = [_bytes(r.get("MemUsage", "").split("/")[0]) or 0 for r in mine]
                    cpu = [float(r.get("CPUPerc", "0%").rstrip("%") or 0) for r in mine]
                    sample[kind] = {"containers": len(members), "mem_total": sum(mem), "mem_max": max(mem, default=0),
                                    "cpu_percent_total": round(sum(cpu), 1)}
                self.samples.append(sample)
            except Exception as ex:  # keep sampling; record the failure
                self.samples.append({"t": time.time(), "error": repr(ex)[:200]})
            self.stop_event.wait(self.interval)


class DockerEvents:
    """Collects container oom/die events from ``docker events`` since the start."""
    def __init__(self, since):
        self.events = []
        self.proc = subprocess.Popen(["docker", "events", "--format", "{{json .}}", "--since", str(int(since)),
                                      "--filter", "type=container", "--filter", "event=oom", "--filter", "event=die"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            attrs = event.get("Actor", {}).get("Attributes", {})
            self.events.append({"status": event.get("status") or event.get("Action"), "time": event.get("time"),
                                "image": attrs.get("image"), "name": attrs.get("name"), "exitCode": attrs.get("exitCode"),
                                "project": attrs.get("com.docker.compose.project")})

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def docker_info():
    try:
        proc = subprocess.run(["docker", "info", "--format", "{{json .}}"], capture_output=True, text=True, timeout=30)
        info = json.loads(proc.stdout)
        return {"cpus": info.get("NCPU"), "memory_bytes": info.get("MemTotal"), "server_version": info.get("ServerVersion")}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def cleanup(start_wall, lean_images):
    """Force-remove this probe's leftover containers and compose networks.

    ``lean_images``: references the judge containers may show (tag and pinned ID)."""
    lean_images = {lean_images} if isinstance(lean_images, str) else set(lean_images)
    removed = []
    try:
        judge_key = JUDGE_LABEL.split("=")[0]
        proc = subprocess.run(["docker", "ps", "-a", "--format",
                               '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t{{.Image}}\t{{.CreatedAt}}\t'
                               f'{{{{.Label "{judge_key}"}}}}'], capture_output=True, text=True, timeout=60)
        for line in proc.stdout.splitlines():
            cid, project, image, created, judge = (line.split("\t") + [""] * 5)[:5]
            try:
                created_at = datetime.strptime(created[:25], "%Y-%m-%d %H:%M:%S %z").timestamp()
            except ValueError:
                created_at = 0
            # Judge containers (label or image) created after this probe started.
            if project.startswith(PROJECT_PREFIX) or ((judge or image in lean_images) and created_at >= start_wall - 5):
                removed.append(cid)
        if removed:
            subprocess.run(["docker", "rm", "-f", *removed], capture_output=True, timeout=120)
        nets = subprocess.run(["docker", "network", "ls", "--format", '{{.ID}}\t{{.Label "com.docker.compose.project"}}'],
                              capture_output=True, text=True, timeout=60)
        stale = [n.split("\t")[0] for n in nets.stdout.splitlines() if "\t" in n and n.split("\t")[1].startswith(PROJECT_PREFIX)]
        if stale:
            subprocess.run(["docker", "network", "rm", *stale], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return {"removed": removed, "error": type(ex).__name__}
    return {"removed": removed}


def peer_scripts(agent_ids, task_ids, deadline, max_cycles):
    """Per-agent cycles: local `lake env lean` check, then submit the same file."""
    def until_deadline(make):
        def maker(tools):
            if deadline["at"] is not None and time.monotonic() >= deadline["at"]:
                return text("Capacity script complete.")(tools)
            return make(tools)
        return maker

    scripts = {}
    for index, agent in enumerate(agent_ids):
        steps, a = [], agent[-3:]
        for cycle in range(max_cycles):
            task = task_ids[(index + cycle) % len(task_ids)]
            # Double quotes: one pilot task id contains an apostrophe.
            # The image's lake wrapper kills at 120 s with `timeout -v`, whose notice
            # tells a limit kill (exit 137) from an OOM kill (also 137).
            local = (f"cd /workspace && cp \"problems/{task}.lean\" Cap_{cycle}.lean && s=$(date +%s%N) && "
                     f"lake env lean Cap_{cycle}.lean > /tmp/cap.out 2>&1; rc=$?; "
                     "e=$(( ($(date +%s%N) - s) / 1000000 )); "
                     f"n=$(grep -c '{vr.KILL_NOTICE}' /tmp/cap.out); "
                     f"echo \"CAP_LOCAL rc=$rc ms=$e task={task} limit_notice=$n\"; tail -n 3 /tmp/cap.out")
            steps.append((f"local_check_{cycle}", until_deadline(bash(local, f"c{a}-l{cycle}", 120000, "Check a file"))))
            steps.append((f"submit_{cycle}", until_deadline(conf_call(
                "submit_solution", {"problem_id": task, "file_path": f"/workspace/Cap_{cycle}.lean"}, f"c{a}-s{cycle}"))))
        steps.append(("final", text("Capacity script complete.")))
        scripts[agent] = steps
    return scripts


async def latency_side(agent_id, sbox, latencies, cgroups, interval):
    """Inspect sandbox exec round trips (the bridge's RPC path) and a final cgroup read."""
    import anyio
    box = runtime._quiet(sbox)
    try:
        while True:
            start = time.monotonic()
            try:
                await box.exec(["true"], timeout=120)
                latencies[agent_id].append(time.monotonic() - start)
            except Exception as ex:
                latencies[agent_id].append(None)
                cgroups.setdefault(agent_id, {}).setdefault("exec_errors", []).append(type(ex).__name__)
            await anyio.sleep(interval)
    finally:
        with anyio.CancelScope(shield=True):
            try:
                with anyio.fail_after(60):
                    result = await box.exec(["sh", "-c", "cat /sys/fs/cgroup/memory.events; echo PEAK $(cat "
                                             "/sys/fs/cgroup/memory.peak 2>/dev/null); echo MAX $(cat /sys/fs/cgroup/memory.max)"],
                                            timeout=60)
                values = {}
                for line in result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) == 2:
                        values[parts[0]] = parts[1]
                cgroups.setdefault(agent_id, {}).update(values)
            except Exception as ex:
                cgroups.setdefault(agent_id, {})["read_error"] = type(ex).__name__


async def concurrency_sampler(records, interval):
    from inspect_ai.util._concurrency import concurrency_status_display
    while True:
        try:
            records.append({"t": time.time(), "status": {k: list(v) for k, v in concurrency_status_display().items()}})
        except Exception as ex:
            records.append({"t": time.time(), "error": type(ex).__name__})
        await asyncio.sleep(interval)


def probe_config(args):
    """The configuration under test (validated_config plus images and limiter)."""
    return {"agent_image": args.image, "lean_image": args.lean_image,
            "compilation_timeout_seconds": args.compilation_timeout, "judge_memory": args.judge_memory,
            "judge_cpus": args.judge_cpus, "judge_max_concurrent": args.judge_max_concurrent,
            "agent_memory": args.agent_memory, "agent_cpus": args.agent_cpus, "max_subprocesses": args.max_subprocesses,
            "warm_up_memory": args.warm_up_memory, "warm_up_timeout_seconds": args.warm_up_timeout,
            "startup_timeout_seconds": args.startup_timeout, "max_connections": mock_max_connections(args),
            "agent_count": args.agents}


def mock_max_connections(args):
    """The mock model's explicit connection limit: ``--max-connections`` (default the
    config's max_connections, else the agent count); never below one."""
    value = getattr(args, "max_connections", None)
    return max(1, int(value if value is not None else args.agents))


async def run(args, stamp):
    from inspect_ai.model import GenerateConfig, get_model
    from research_swarm.dataset import FrozenDataset
    from research_swarm.prompts import INITIAL_MESSAGE, build_prompt
    from research_swarm.tools import TOOL_DEFINITIONS
    config = probe_config(args)
    images = STATE.get("images") or runtime.pin_images(config)
    run_dir = (ROOT / "runs" / f"validation-capacity-{stamp}").resolve()
    run_dir.mkdir(parents=True)
    record = runtime.make_recorder(run_dir / "runtime.jsonl")
    record("images_pinned", None, images)
    STATE.update(images=images)
    listing, task_files = runtime.export_public_tasks(ROOT)
    task_ids = [t.task_id for t in FrozenDataset(ROOT).pilot_tasks]
    personas = runtime.assign_personas(args.agents, runtime.DEFAULT_PERSONA_SEED)
    agent_ids = list(personas)
    if not args.no_warm_up:
        STATE["warm_up"] = await asyncio.to_thread(runtime.warm_up_lean, images["lean"]["id"], record,
                                                   **runtime.warm_up_options(config))
    compiler = RecordingCompiler(runtime.make_lean_compiler(config, image=images["lean"]["id"]))
    conference = runtime.default_conference_factory(ROOT, compiler=compiler)(run_dir, agent_ids)
    ledger = vr.validation_ledger(run_dir)
    settings = ProviderSettings(pricing_verified=True)
    barrier = runtime.StartBarrier(len(agent_ids), record)
    clock = runtime.RunClock(barrier)
    archive = InstructionArchive(run_dir / "instructions", record)
    deadline = {"at": None}
    latencies, cgroups, limiter = defaultdict(list), {}, []
    STATE.update(config=config, run_dir=run_dir, compiler=compiler, latencies=latencies, cgroups=cgroups,
                 limiter=limiter, ledger=ledger, agent_ids=agent_ids, personas=personas)

    async def set_deadline():
        await barrier.event.wait()
        deadline["at"] = barrier.started_at + args.duration

    def filter_for(agent_id):
        return vr.attributed(agent_id, budget_filter(agent_id, ledger, settings, barrier, record, vr.VALIDATION_SCOPE,
                                                     archive, clock.stop))

    compose = runtime.make_compose(run_dir, config, images["agent"]["id"])
    STATE["compose"] = vr.compose_binding(compose, config, images["agent"]["id"])
    record("compose_bound", None, STATE["compose"])
    peer = runtime.peer_solver(conference=conference, definitions=TOOL_DEFINITIONS, personas=personas,
                               prompt_for=lambda a, p: build_prompt(a, p, listing), filter_for=filter_for,
                               run_dir=run_dir, record=record, clock=clock)
    side = lambda agent_id, sbox: latency_side(agent_id, sbox, latencies, cgroups, args.latency_interval)  # noqa: E731
    task = runtime.build_task(agent_ids=agent_ids, files=runtime.agent_files(task_files),
                              solver=vr.with_side_task(peer, side), compose=compose,
                              metadata={"condition": "unpaid-capacity-probe"}, initial_message=INITIAL_MESSAGE,
                              name=TASK_NAME)
    scripted = vr.ScriptedModel(peer_scripts(agent_ids, task_ids, deadline, args.max_cycles),
                                final="Capacity script complete.")
    STATE["scripted"] = scripted
    # The startup watchdog starts with the evaluation (as in runtime.supervise).
    STATE["eval_started_wall"] = time.time()
    record("evaluation_started", None, {"startup_timeout_seconds": args.startup_timeout})
    helpers = [asyncio.create_task(set_deadline()), asyncio.create_task(concurrency_sampler(limiter, args.sample_interval)),
               asyncio.create_task(clock.monitor(conference, record, startup_timeout=args.startup_timeout,
                                                 max_run_seconds=args.duration + 900, post_depletion_seconds=10**6))]
    try:
        # Explicit max_connections (default: one per agent, as in the pilot), never Inspect's
        # adaptive default, so model calls are not queued by a shared connection limit.
        model = get_model(vr.MOCK_MODEL, custom_outputs=scripted,
                          config=GenerateConfig(max_connections=mock_max_connections(args)))
        log = await vr.run_eval(task, model, agents=len(agent_ids), log_dir=run_dir / "inspect",
                                max_subprocesses=args.max_subprocesses)
        STATE["log"] = log
    finally:
        for helper in helpers:
            helper.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        await asyncio.to_thread(conference.close)
    return log


def local_status_key(rc, limit_notice=0):
    """Outcome of one local `lake env lean`: the 120 s wrapper limit (137 with the
    `timeout -v` notice) is a timeout, a bare 137 is a kill (OOM)."""
    if rc == 137:
        return "timeout_local_limit" if limit_notice else "killed_137"
    return "ok_or_lean_error" if rc in (0, 1) else f"rc_{rc}"


def startup_measurements(runtime_events, started_wall, agent_count, startup_timeout, config_startup_timeout):
    """RT-5: seconds from evaluation start (when the startup watchdog starts, as in
    runtime.supervise) to each agent's first inference request (``agent_ready``) and
    to the common start barrier, against the pilot's startup watchdog."""
    ready = {e["agent_id"]: round(e["timestamp"] - started_wall, 3) for e in runtime_events
             if e.get("kind") == "agent_ready" and started_wall is not None}
    common = next((e["timestamp"] for e in runtime_events if e.get("kind") == "common_start"), None)
    stops = [e["payload"].get("reason") for e in runtime_events if e.get("kind") == "run_stop"]
    barrier = None if common is None or started_wall is None else round(common - started_wall, 3)
    slowest = max(ready.values(), default=None)
    within = (barrier is not None and len(ready) == agent_count and slowest is not None
              and barrier <= startup_timeout and slowest <= startup_timeout and "startup_timeout" not in stops
              and startup_timeout <= config_startup_timeout)
    return {"within_timeout": within, "timeout_seconds": startup_timeout,
            "config_timeout_seconds": config_startup_timeout, "barrier_seconds": barrier,
            "slowest_agent_seconds": slowest, "agents_ready": len(ready), "agents_expected": agent_count,
            "fraction_of_timeout": None if barrier is None else round(barrier / startup_timeout, 3),
            "per_agent_seconds": dict(sorted(ready.items())), "run_stops": stops}


def analyse(args, sampler, events, info, interrupted, cleanup_result):
    log, compiler = STATE.get("log"), STATE.get("compiler")
    run_dir = STATE.get("run_dir")
    samples = {str(s.id): vr.sample_summary(s) for s in log.samples} if log is not None else {}
    runtime_events = vr.read_jsonl(run_dir / "runtime.jsonl") if run_dir else []
    kinds = defaultdict(list)
    for event in runtime_events:
        kinds[event["kind"]].append(event)
    local, local_status = [], Counter()
    cycles = Counter()
    for agent, summary in samples.items():
        for cid, value in summary["tool_results"].items():
            if "-l" not in cid:
                continue
            match = LOCAL_MARK.search(value)
            if match:
                rc, ms = int(match.group(1)), int(match.group(2))
                local.append(ms / 1000)
                local_status[local_status_key(rc, int(match.group(4) or 0))] += 1
                cycles[agent] += 1
            elif "timed out" in value.lower() or "timeout" in value.lower():
                local_status["timeout"] += 1
            else:
                local_status["unparsed"] += 1
    judge = list(compiler.records) if compiler is not None else []
    judge_status = Counter(r["status"] for r in judge)
    memory = [s for s in sampler.samples if "agent" in s]
    peak_agents = max((s["agent"]["mem_total"] for s in memory), default=None)
    peak_total = max((s["agent"]["mem_total"] + s["lean"]["mem_total"] for s in memory), default=None)
    vm_memory = info.get("memory_bytes")
    oom_events = [e for e in events.events if e["status"] == "oom"]
    died_137 = [e for e in events.events if e["status"] == "die" and str(e.get("exitCode")) == "137"]
    in_container_oom = {a: int(v.get("oom_kill", 0) or 0) for a, v in STATE.get("cgroups", {}).items()}
    latency = [x for v in STATE.get("latencies", {}).values() for x in v]
    limiter = STATE.get("limiter", [])
    sub = [r["status"].get("subprocesses") for r in limiter if "status" in r and r["status"].get("subprocesses")]
    at_limit = sum(1 for in_use, limit in sub if in_use >= limit)
    exits = {e["agent_id"]: e["payload"]["status"] for e in kinds["agent_exit"]}
    ledger = STATE["ledger"].snapshot()[vr.VALIDATION_SCOPE] if STATE.get("ledger") else {}
    judged_agents = {a for a in samples if any("-s" in cid and '"submission_id"' in v
                                                for cid, v in samples[a]["tool_results"].items())}
    startup = startup_measurements(runtime_events, STATE.get("eval_started_wall"), args.agents, args.startup_timeout,
                                   getattr(args, "config_startup_timeout", args.startup_timeout))
    binding = STATE.get("compose") or {}
    checks = {
        "completed_without_interrupt": check(not interrupted, cleanup=cleanup_result),
        "all_agents_ran": check(
            len(samples) == args.agents and all(not s.get("error") for s in samples.values())
            and all(cycles[a] >= 1 for a in samples) and judged_agents == set(samples),
            agents=len(samples), errors={a: s.get("error") for a, s in samples.items() if s.get("error")},
            cycles_per_agent=dict(cycles), exits=dict(Counter(exits.values()))),
        "no_oom_kills": check(not oom_events and not died_137 and not any(in_container_oom.values())
                              and judge_status.get("killed", 0) == 0 and local_status.get("killed_137", 0) == 0,
                              docker_oom_events=oom_events, exit_137=died_137, in_container_oom_kill=in_container_oom),
        "lean_warm_up": check(bool(STATE.get("warm_up") and STATE["warm_up"].get("ok")), required=False,
                              warm_up=STATE.get("warm_up"), skipped=bool(args.no_warm_up)),
        "warm_up_attempted": check(not args.no_warm_up and isinstance(STATE.get("warm_up"), dict),
                                   skipped=bool(args.no_warm_up),
                                   note="Warm-up failure is informational; skipping it changes the measured startup."),
        "no_compile_timeouts": check(local_status.get("timeout", 0) == 0 and local_status.get("timeout_local_limit", 0) == 0
                                     and judge_status.get("timeout", 0) == 0,
                                     local_status=dict(local_status), judge_status=dict(judge_status)),
        "all_agents_reached_barrier_within_startup_timeout": check(
            startup["within_timeout"], **{k: v for k, v in startup.items() if k != "per_agent_seconds"},
            note="Measured from evaluation start, as the pilot's startup watchdog (startup_timeout_seconds) is; "
                 "the probe's --startup-timeout may not exceed the config value."),
        "compose_matches_fingerprint": check(binding.get("matches") is True, compose=binding),
        "judge_environment_ok": check(judge_status.get("setup_error", 0) == 0 and bool(judge),
                                      judge_status=dict(judge_status)),
        "bridge_exec_latency": check(bool(latency) and None not in latency
                                     and percentiles(latency)["p95"] <= args.max_exec_latency,
                                     latency_seconds=percentiles([x for x in latency if x is not None]),
                                     failures=sum(1 for x in latency if x is None), threshold=args.max_exec_latency),
        "memory_headroom": check(peak_total is not None and vm_memory and peak_total <= args.memory_headroom * vm_memory,
                                 peak_agent_bytes=peak_agents, peak_total_bytes=peak_total, docker_vm_bytes=vm_memory,
                                 fraction=args.memory_headroom),
        "budget_ledger_contention": check(
            not [e for e in kinds["inference_not_admitted"] if e["payload"].get("reason") == "budget_ledger_unavailable"]
            and ledger.get("unresolved_requests", 1) == 0, ledger=ledger,
            admission_retries=len(kinds["budget_admission_retry"])),
    }
    measurements = {
        "startup": startup,
        "local_compile_seconds": percentiles(local), "local_over_limit": sum(1 for x in local if x >= COMPILE_LIMIT_SECONDS),
        "judge_compile_seconds": percentiles([r["elapsed_seconds"] for r in judge]),
        "judge_queue_seconds": percentiles([r["queue_seconds"] for r in judge]),
        "judge_wall_seconds": percentiles([r["wall_seconds"] for r in judge]),
        "judge_output_truncated": sum(1 for r in judge if r.get("output_truncated")),
        "subprocess_limiter": {"samples": len(sub), "at_limit": at_limit,
                               "max_in_use": max((u for u, _ in sub), default=None), "limit": sub[0][1] if sub else None},
        "cpu_percent_total": percentiles([s["agent"]["cpu_percent_total"] + s["lean"]["cpu_percent_total"] for s in memory]),
        "agent_memory_max_bytes": max((s["agent"]["mem_max"] for s in memory), default=None),
        "judge_containers_max": max((s["lean"]["containers"] for s in memory), default=None),
        "loadavg_max": max((s["loadavg"][0] for s in memory), default=None),
        "in_container_cgroups": STATE.get("cgroups", {}),
        "docker_samples": len(sampler.samples), "docker_sampler_errors": sum(1 for s in sampler.samples if "error" in s),
    }
    return checks, measurements, samples


def _optional_int(value):
    return None if str(value).lower() in ("none", "null", "0") else int(value)


def validated_config(args):
    """The measured configuration (launch_gates.CAPACITY_KEYS compares it with the pilot config)."""
    return capacity_config(probe_config(args))


def config_defaults(path):
    """Probe defaults for the CAPACITY_KEYS (and images) from the pilot config, so a
    default run measures exactly the configuration the launch gate compares."""
    config = json.loads(Path(path).read_text()) if path and Path(path).is_file() else {}
    return {"agents": config.get("agent_count", runtime.APPROVED_AGENT_COUNT),
            "judge_max_concurrent": config.get("judge_max_concurrent"),  # null = unbounded
            "agent_memory": config.get("agent_memory", "1g"), "agent_cpus": config.get("agent_cpus"),
            "judge_memory": config.get("judge_memory", "2g"), "judge_cpus": config.get("judge_cpus", 1),
            "compilation_timeout": config.get("compilation_timeout_seconds", COMPILE_LIMIT_SECONDS),
            "startup_timeout": config.get("startup_timeout_seconds", STARTUP_TIMEOUT_SECONDS),
            "config_startup_timeout": config.get("startup_timeout_seconds", STARTUP_TIMEOUT_SECONDS),
            "image": config.get("agent_image") or runtime.AGENT_IMAGE,
            "lean_image": config.get("lean_image") or runtime.LEAN_IMAGE,
            "warm_up_memory": runtime.warm_up_options(config)["memory"],
            "warm_up_timeout": runtime.warm_up_options(config)["timeout"],
            "max_subprocesses": int(config.get("max_subprocesses", 64)),
            "max_connections": config.get("max_connections")}


def _optional_float(value):
    return None if str(value).lower() in ("none", "null") else float(value)


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=str(DEFAULT_CONFIG))
    known, _ = pre.parse_known_args(argv)
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], parents=[pre])
    parser.add_argument("--agents", type=int, help="default: config agent_count")
    parser.add_argument("--judge-max-concurrent", type=_optional_int,
                        help="judge concurrency gate (I1); default: config judge_max_concurrent (null/'none' = unbounded)")
    parser.add_argument("--agent-memory")
    parser.add_argument("--agent-cpus", type=_optional_float)
    parser.add_argument("--judge-memory")
    parser.add_argument("--judge-cpus", type=float)
    parser.add_argument("--compilation-timeout", type=int, help="default: config compilation_timeout_seconds")
    parser.add_argument("--no-warm-up", action="store_true", help="skip the pre-start Lean warm-up (cold cache)")
    parser.add_argument("--warm-up-memory")
    parser.add_argument("--warm-up-timeout", type=int, help="default: config warm_up_timeout_seconds")
    parser.add_argument("--duration", type=int, default=600, help="seconds of load after the common start")
    parser.add_argument("--max-cycles", type=int, default=40)
    parser.add_argument("--max-subprocesses", type=int, help="default: config max_subprocesses, else 64")
    parser.add_argument("--max-connections", type=int,
                        help="mock model connection limit; default: config max_connections, else the agent count")
    parser.add_argument("--startup-timeout", type=int,
                        help="default: config startup_timeout_seconds (the pilot watchdog); a larger value fails the "
                             "startup check")
    parser.add_argument("--sample-interval", type=float, default=5.0)
    parser.add_argument("--latency-interval", type=float, default=15.0)
    parser.add_argument("--max-exec-latency", type=float, default=10.0)
    parser.add_argument("--memory-headroom", type=float, default=0.9)
    parser.add_argument("--image")
    parser.add_argument("--lean-image")
    parser.add_argument("--output-dir", default=str(vr.VALIDATION_DIR))
    vr.add_interlock_argument(parser)
    parser.set_defaults(**config_defaults(known.config))
    return parser.parse_args(argv)


def main():
    args = parse_args()
    # Before any container starts: other runs' agents, comparators or leftover judges
    # would distort every measurement (runtime.launch_interlock).
    interlock = vr.interlock_or_refuse(args)
    if interlock is None:
        return 2
    stamp, start_wall = vr.utc_stamp(), time.time()
    info = docker_info()
    try:
        images = runtime.pin_images(probe_config(args))  # refuses before any container starts
    except runtime.LaunchBlocked as ex:
        print(f"refusing: {ex}", file=sys.stderr)
        return 2
    STATE["images"] = images
    sampler = DockerSampler(images["agent"]["id"], images["lean"]["id"], args.sample_interval)
    events = DockerEvents(start_wall)
    sampler.start()
    interrupted = False
    try:
        asyncio.run(run(args, stamp))
    except KeyboardInterrupt:
        interrupted = True
        print("interrupted: cleaning up probe containers", file=sys.stderr)
    finally:
        sampler.stop_event.set()
        sampler.join(timeout=90)
        events.stop()
        cleanup_result = cleanup(start_wall, {args.lean_image, images["lean"]["id"]})
    if "ledger" not in STATE:
        print("probe did not start; no evidence written", file=sys.stderr)
        return 1
    checks, measurements, samples = analyse(args, sampler, events, info, interrupted, cleanup_result)
    checks["no_conflicting_containers_at_start"] = vr.interlock_check(interlock)
    path, report = vr.write_evidence(
        "runtime-capacity", "gdm-runtime-capacity/1", checks, {"agent": images["agent"]["id"], "lean": images["lean"]["id"]},
        stamp, args.output_dir, kind="unpaid local capacity probe (mockllm-driven Claude Code + real Lean)",
        interrupted=interrupted, validated_config=validated_config(args), parameters=vars(args),
        compose_sha256=(STATE.get("compose") or {}).get("compose_sha256"), compose=STATE.get("compose"),
        config=STATE.get("config"), images=images, lean_warm_up=STATE.get("warm_up"),
        persona_counts=runtime.persona_counts(STATE.get("personas") or {}), docker=info, interlock=interlock,
        max_connections=mock_max_connections(args),
        host={"cpu_count": os.cpu_count()}, measurements=measurements,
        run_dir=vr.rel(STATE["run_dir"]),
        inspect_log=Path(STATE["log"].location).name if STATE.get("log") is not None else None,
        docker_stats=sampler.samples[-400:], docker_events=events.events[-200:],
        samples={a: {k: v for k, v in s.items() if k not in ("tool_results", "inspect_tool_events")}
                 for a, s in samples.items()},
        limitations=["mockllm responses are instantaneous; live inference adds latency that spreads Lean load out.",
                     "Unmodified templates fail quickly after the Mathlib import; real proofs may compile longer."])
    print(json.dumps({k: v["passed"] for k, v in checks.items()}, indent=2))
    print(json.dumps({k: measurements[k] for k in ("local_compile_seconds", "judge_compile_seconds",
                                                   "judge_queue_seconds", "subprocess_limiter")}, indent=2, default=str))
    print(f"wrote {vr.rel(path)}; all_required_passed={report['all_required_passed']}")
    return 0 if report["all_required_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
