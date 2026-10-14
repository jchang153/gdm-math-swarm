#!/usr/bin/env python3
"""Researcher-only real-Lean validation of conference arbitration (plan validation steps 2-3).

Builds a ConferenceService over the frozen pilot tasks with the operational judge
and the real DockerLeanCompiler, in a throwaway run directory
runs/validation-conference-<UTC>/ (researcher-side, git-ignored, never mounted
into an agent container and never reused as a pilot run), and checks:

1. N simultaneous submissions of a genuine Green32 proof (fixture
   research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean spliced into the
   frozen template's proof region): exactly one ``accepted``; every other one
   ``accepted_after_close`` (or ``problem_closed``) and credited to the winner.
2. A later submission is ``problem_closed`` and is not judged.
3. Library publication with author, time and complete source; credit display in
   list_problems and get_conference_status.
4. A statement-changing fixture from research/fixtures/evaluator (default: a
   preamble notation override) whose conference outcome mirrors the operational
   judge's own verdict (the weak judge may accept it; that is a documented
   baseline weakness, not a failure of this check).
5. The research-evaluator observer (evaluator.make_queue_observer when present,
   else make_observer with no backend, i.e. "unresolved" without Lean) runs and
   none of its output reaches any action result.
6. events.jsonl agrees with the SQLite events table, with contiguous sequence
   numbers and the expected ordering of the race events. Agent-visible submission
   ids are opaque; events are matched through their ``public_submission_id``.
7. No compiler container (label gdm-judge=1) is left over. A failed ``docker ps``
   fails this check, because the leftovers were then never examined.

Fixtures are read from research/ and passed to the service in memory; nothing is
written to an agent-visible path. The evidence record (interface I6) is
data/validation/conference-race-<UTC YYYYMMDDTHHMMSSZ>.json and stores fixture
hashes, never fixture content.

Judge limits (Lean timeout, memory, CPUs) come from the run config (--config,
default configs/pilot.json: compilation_timeout_seconds, judge_memory, judge_cpus)
and are recorded as ``limits`` with ``config {path, limits}`` and
``limits_match_config``, as the judge and dataset validations record them (X5). The
Lean image defaults to the config's ``lean_image``; ``image_ids`` records its ID under
both the tag and the role ``lean``. Only a complete run at the configured limits can
set ``all_required_passed``: ``complete`` is false (with ``incomplete_reasons``) for
--fake-lean or when a requested statement-changing fixture could not be submitted,
and --timeout/--memory/--cpus overrides that differ from the config set
``limits_match_config: false``. Such records are never launch evidence.

  PYTHONPATH=src .venv/bin/python scripts/validate_conference.py --threads 4 --max-concurrent 2

``--fake-lean`` exercises the script without Docker (development only): the
record then fails the required ``real_lean`` check and is written into the run
directory, not data/validation/.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm import evaluator  # noqa: E402
from research_swarm.conference import (ConferenceService, audit_event_log, operational_judge,  # noqa: E402
                                       tasks_from_dataset)
from research_swarm.dataset import FrozenDataset  # noqa: E402
from research_swarm.judge import immutable_segments  # noqa: E402
from research_swarm.keyword_filter import check_keywords  # noqa: E402
from research_swarm.lean import CompileResult, DockerLeanCompiler, image_id, judge_containers  # noqa: E402

SCHEMA = "gdm-conference-race-validation/1"
PREFIX = "conference-race"
DEFAULT_CONFIG = ROOT / "configs" / "pilot.json"
DEFAULT_LEAN_IMAGE = "gdm-math-swarm-lean:local"
# Config key and default for each judge limit (as launch_gates.JUDGE_LIMIT_KEYS and
# runtime.make_lean_compiler).
LIMIT_KEYS = {"timeout_seconds": ("compilation_timeout_seconds", 120), "memory": ("judge_memory", "2g"),
              "cpus": ("judge_cpus", 1)}
GENUINE_TASK = "Green32_hasCosetHole_empty"
GENUINE_FIXTURE = ROOT / "research" / "fixtures" / "judge" / "Green32_hasCosetHole_empty.proof.lean"
STATEMENT_FIXTURES = {  # researcher-only fixture -> frozen pilot task it targets
    "eulerbrick_notation_override.lean": "EulerBrick_cuboidThree",
    "erdos195_instance_override.lean": "Erdos195_erdos_195_variants_leq_5_bound",
    "erdos6_shadow_definition.lean": "Erdos6_erdos_6_variants_decreasing",
}
STATEMENT_DIR = ROOT / "research" / "fixtures" / "evaluator" / "submissions"
PLACEHOLDER = '  fail "Provide a complete proof."'
# Strings that would indicate evaluator output in an agent-visible result.
EVALUATOR_MARKERS = ("validity", "unresolved", "fraudulent", "evaluations.jsonl", "queue.jsonl")
# Agent-visible submission ids are opaque; sequential ones (sub-000001) stay researcher-side.
OPAQUE_SUBMISSION_ID = re.compile(r"sub-[0-9a-f]{16}")


def utc_stamp():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def judge_limits(timeout, memory, cpus) -> dict:
    """Normalized judge compile limits, recorded as ``limits`` (the launch gates compare
    them with config compilation_timeout_seconds, judge_memory and judge_cpus)."""
    if isinstance(timeout, bool) or isinstance(cpus, bool):
        raise ValueError("judge limits must be numbers")
    timeout, cpus = float(timeout), float(cpus)
    if not timeout.is_integer() or timeout <= 0 or cpus <= 0 or not str(memory).strip():
        raise ValueError(f"invalid judge limits: timeout {timeout}, memory {memory!r}, cpus {cpus}")
    return {"timeout_seconds": int(timeout), "memory": str(memory),
            "cpus": int(cpus) if cpus.is_integer() else cpus}


def load_config(path) -> dict:
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict):
        raise ValueError("run config must be a JSON object")
    return config


def config_limits(config) -> dict:
    return judge_limits(*(config.get(key, default) for key, default in LIMIT_KEYS.values()))


def display_path(path):
    path = Path(path).resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


class FakeCompiler:
    """--fake-lean only: accepts every source after a short delay."""

    def __init__(self, delay=0.3):
        self.delay = delay

    def compile(self, source):
        started = time.monotonic()
        time.sleep(self.delay)
        return CompileResult("success", 0, "", "", 0, elapsed_seconds=time.monotonic() - started)


class RecordingCompiler:
    """Records every compile; optionally gates concurrency when the compiler cannot."""

    def __init__(self, inner, gate=None):
        self.inner, self.gate = inner, gate
        self.records, self.lock = [], threading.Lock()

    def compile(self, source):
        queued = time.monotonic()
        if self.gate is not None:
            self.gate.acquire()
        try:
            started = time.monotonic()
            result = self.inner.compile(source)
            finished = time.monotonic()
        finally:
            if self.gate is not None:
                self.gate.release()
        with self.lock:
            self.records.append({
                "source_sha256": sha256(source), "status": result.status, "exit_code": result.exit_code,
                "sorry_count": result.sorry_count, "elapsed_seconds": _round(result.elapsed_seconds),
                "queue_seconds": _round(getattr(result, "queue_seconds", None)),
                "script_gate_wait_seconds": round(started - queued, 3) if self.gate is not None else None,
                "wall_seconds": round(finished - started, 3),
                "output_truncated": getattr(result, "output_truncated", None)})
        return result


def _round(value):
    return round(value, 3) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def make_compiler(args):
    """Returns (recording compiler, gate description)."""
    if args.fake_lean:
        return RecordingCompiler(FakeCompiler()), "none (fake)"
    kwargs = dict(image=args.image, timeout=args.timeout, memory=args.memory, cpus=args.cpus)
    if "max_concurrent" in inspect.signature(DockerLeanCompiler).parameters:
        kwargs["max_concurrent"] = args.max_concurrent
        return RecordingCompiler(DockerLeanCompiler(**kwargs)), "DockerLeanCompiler.max_concurrent"
    # Interface I1 not yet available: gate here so the VM is not oversubscribed.
    gate = threading.BoundedSemaphore(args.max_concurrent) if args.max_concurrent else None
    return RecordingCompiler(DockerLeanCompiler(**kwargs), gate), "script semaphore (I1 unavailable)"


def make_observer(directory):
    queue = getattr(evaluator, "make_queue_observer", None)
    if queue is not None:
        return queue(directory), "evaluator.make_queue_observer"
    return evaluator.make_observer(None, directory), "evaluator.make_observer(backend=None)"


def check_leftover_containers(checks, list_containers=judge_containers):
    """Required check: no compiler container (label gdm-judge=1) is left over.

    ``list_containers`` returns None when ``docker ps`` fails (nonzero exit, missing
    CLI, timeout); the check then fails, since leftovers were never examined.
    """
    leftovers = list_containers()
    detail = leftovers if leftovers is not None else "docker ps failed; leftover compiler containers not examined"
    checks.add("no_leftover_lean_containers", leftovers == [], detail)
    return leftovers


def read_whole(act, agent, path):
    """Reassemble a library file through read_library_file paging."""
    parts, offset, reply = [], 0, None
    while True:
        reply = act(agent, "read_library_file", {"path": path, "offset": offset})
        if not reply.get("ok"):
            return None, reply
        parts.append(reply["content"])
        if not reply["has_more"]:
            return "".join(parts), reply
        offset = reply["next_offset"]


def summarize(reply):
    keys = ("ok", "outcome", "accepted", "stage", "reason", "compile_status", "credited_agent", "accepted_at",
            "library_path", "submission_id", "error")
    summary = {k: reply.get(k) for k in keys if k in reply}
    if reply.get("outcome") in ("rejected", "judge_error") and reply.get("compiler_output"):
        summary["compiler_output_head"] = reply["compiler_output"][:400]
    return summary


class Checks:
    def __init__(self):
        self.items = []

    def add(self, name, passed, detail=None, required=True):
        self.items.append({"name": name, "passed": bool(passed), "required": required, "detail": detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail and not passed else ""))
        return bool(passed)


def first(events, kind, public_submission_id):
    """First event of ``kind`` for the submission an agent knows by its public id."""
    for event in events:
        if event["kind"] == kind and event["payload"].get("public_submission_id") == public_submission_id:
            return event
    return None


def run(args):
    stamp, created_at = utc_stamp()
    started = time.monotonic()
    checks = Checks()
    if not args.fake_lean:
        lean_id = image_id(args.image)
        if not lean_id:
            print(f"Lean image {args.image} unavailable (docker image inspect failed)", file=sys.stderr)
            return 2, None
        image_ids = {args.image: lean_id, "lean": lean_id}
    else:
        image_ids = {}
    run_root = Path(args.run_root)
    run_dir = run_root / f"validation-conference-{stamp}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = run_root / f"validation-conference-{stamp}-{suffix}"

    dataset = FrozenDataset(ROOT)
    tasks = tasks_from_dataset(dataset)
    by_id = {task.task_id: task for task in tasks}
    template = by_id[GENUINE_TASK].template_source
    if template.count(PLACEHOLDER) != 1:
        raise SystemExit("Green32 template has no unique proof placeholder")
    genuine = template.replace(PLACEHOLDER, GENUINE_FIXTURE.read_text().rstrip("\n"), 1)
    checks.add("genuine_fixture_preconditions",
               check_keywords(genuine).passed and immutable_segments(template) == immutable_segments(genuine))

    run_dir.mkdir(parents=True)
    (run_dir / "RESEARCHER_ONLY.txt").write_text(
        "Researcher-only validation state built from development fixtures (scripts/validate_conference.py).\n"
        "Never mount, copy or reuse this directory for an experimental run.\n")
    compiler, gate = make_compiler(args)
    warmup = None
    if args.warmup and not args.fake_lean:
        # Outside the conference: a cold .olean page cache under the 2 GiB cap can
        # thrash past the limit, which would test the cache, not arbitration.
        result = compiler.inner.compile(genuine)
        warmup = {"status": result.status, "elapsed_seconds": _round(result.elapsed_seconds)}
        print(f"Warm-up compile: {warmup}")
    observer_dir = run_dir / "evaluation"  # researcher-side, inside the throwaway run directory
    observer, observer_kind = make_observer(observer_dir)
    racers = [f"agent-{i:03d}" for i in range(1, args.threads + 1)]
    late_agent, reader = f"agent-{args.threads + 1:03d}", f"agent-{args.threads + 2:03d}"
    service = ConferenceService(run_dir, tasks, operational_judge(compiler), observer=observer,
                                agent_ids=racers + [late_agent, reader],
                                observer_join_timeout=args.observer_join_timeout)
    visible = []  # every agent-visible action result

    def act(agent, tool, payload):
        reply = service.action(agent, tool, payload)
        visible.append(reply)
        return reply

    # 1. Simultaneous genuine submissions.
    barrier = threading.Barrier(len(racers))
    race = {}

    def submit(agent):
        barrier.wait()
        began = time.monotonic()
        reply = act(agent, "submit_solution", {"problem_id": GENUINE_TASK, "source": genuine,
                                               "file_path": "/workspace/Green32_hasCosetHole_empty.lean"})
        race[agent] = (reply, round(time.monotonic() - began, 3))
    threads = [threading.Thread(target=submit, args=(agent,), name=f"race-{agent}") for agent in racers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    outcomes = {agent: reply.get("outcome") for agent, (reply, _) in race.items()}
    winners = [agent for agent, outcome in outcomes.items() if outcome == "accepted"]
    checks.add("race_single_winner", len(winners) == 1, outcomes)
    winner = winners[0] if winners else None
    losers = [race[a][0] for a in racers if a != winner]
    checks.add("race_losers_not_credited", winner is not None and all(
        r.get("outcome") in ("accepted_after_close", "problem_closed") and r.get("accepted") is False
        and r.get("credited_agent") == winner for r in losers), [summarize(r) for r in losers])
    checks.add("race_no_rejection_or_judge_error",
               not any(o in ("rejected", "judge_error") for o in outcomes.values()),
               [summarize(race[a][0]) for a in racers if outcomes[a] in ("rejected", "judge_error")])

    # 2. A later submission is closed without judging.
    compiles_before = len(compiler.records)
    late = act(late_agent, "submit_solution", {"problem_id": GENUINE_TASK, "source": genuine})
    checks.add("late_submission_problem_closed_without_judging",
               late.get("outcome") == "problem_closed" and late.get("credited_agent") == winner
               and len(compiler.records) == compiles_before, summarize(late))
    visible_ids = [reply.get("submission_id") for reply, _ in race.values()] + [late.get("submission_id")]
    checks.add("submission_ids_opaque", len(set(visible_ids)) == len(visible_ids) and all(
        isinstance(i, str) and OPAQUE_SUBMISSION_ID.fullmatch(i) for i in visible_ids), visible_ids)

    # 3. Library publication and credit display.
    solution_path = f"solutions/{GENUINE_TASK}.lean"
    listing = act(reader, "list_library", {"kind": "solution"}).get("files", [])
    entry = next((f for f in listing if f.get("path") == solution_path), None)
    checks.add("library_publication_author_time", entry is not None and entry.get("author") == winner
               and entry.get("accepted_at") is not None and entry.get("sha256") == sha256(genuine), entry)
    content, _ = read_whole(act, reader, solution_path)
    checks.add("library_source_complete", content == genuine)
    winner_reply = race[winner][0] if winner else {}
    problems = {p["problem_id"]: p for p in act(reader, "list_problems", {"limit": 100}).get("problems", [])}
    green = problems.get(GENUINE_TASK, {})
    checks.add("problem_listing_credit", green.get("status") == "closed" and green.get("credited_agent") == winner
               and green.get("accepted_at") == winner_reply.get("accepted_at")
               and green.get("library_path") == solution_path, green)
    status = act(reader, "get_conference_status", {})
    counts = {p["agent_id"]: p["accepted_count"] for p in status.get("participants", [])}
    checks.add("status_credit_counts", counts.get(winner) == 1 and sum(counts.values()) == 1
               and status.get("open_problems") == len(tasks) - 1, {"counts": counts,
                                                                   "open": status.get("open_problems")})

    # 4. Statement-changing fixtures: the conference mirrors the operational judge.
    statement_results = []
    for name in args.statement_fixture:
        task_id = STATEMENT_FIXTURES[name]
        source = (STATEMENT_DIR / name).read_text()
        task_template = by_id[task_id].template_source
        answer_based = by_id[task_id].answer_based
        applicable = immutable_segments(task_template, answer_based) == immutable_segments(source, answer_based)
        record = {"fixture": name, "task_id": task_id, "fixture_sha256": sha256(source), "applicable": applicable}
        checks.add(f"statement_fixture_applicable:{name}", applicable)
        if applicable:
            agent = racers[0]
            reply = act(agent, "submit_solution", {"problem_id": task_id, "source": source})
            record.update(summarize(reply))
            after = {p["problem_id"]: p for p in act(reader, "list_problems", {"limit": 100})["problems"]}[task_id]
            published = any(f.get("path") == f"solutions/{task_id}.lean"
                            for f in act(reader, "list_library", {"kind": "solution"}).get("files", []))
            if reply.get("outcome") == "accepted":
                consistent = (reply.get("stage") == "accepted" and published and after["status"] == "closed"
                              and after["credited_agent"] == agent)
            else:
                consistent = (reply.get("outcome") == "rejected" and reply.get("stage") != "accepted"
                              and not published and after["status"] == "open")
            record["operational_outcome"] = reply.get("outcome")
            checks.add(f"statement_change_mirrors_operational_judge:{name}", consistent, summarize(reply))
        statement_results.append(record)

    service.close()

    # 5. Observer ran researcher-side; nothing evaluator-related reached agents.
    events = [json.loads(line) for line in (run_dir / "conference" / "events.jsonl").read_text().splitlines()]
    passing = [e["payload"]["submission_id"] for e in events if e["kind"] == "submission_judged"
               and e["payload"]["outcome"] in ("accepted", "accepted_after_close")]
    finished = {e["payload"]["submission_id"] for e in events if e["kind"] == "observer_finished"}
    observer_text = "".join(p.read_text(errors="replace") for p in sorted(observer_dir.rglob("*.jsonl"))) \
        if observer_dir.exists() else ""
    checks.add("observer_ran_for_passing_submissions", set(passing) <= finished
               and not any(e["kind"] in ("observer_failed", "observer_unfinished", "observer_dispatch_failed")
                           for e in events)
               and all(sid in observer_text for sid in passing), {"passing": passing, "finished": sorted(finished)})
    rendered = json.dumps(visible, ensure_ascii=False)
    leaked = [m for m in (*EVALUATOR_MARKERS, str(observer_dir)) if m in rendered]
    checks.add("observer_output_not_agent_visible", not leaked, leaked)

    # 6. Event log agreement and ordering.
    audit = audit_event_log(run_dir / "conference")
    checks.add("event_log_matches_sqlite", audit["agree"] and audit["contiguous"], audit)
    checks.add("event_timestamps_nondecreasing", audit["timestamps_nondecreasing"], required=False)
    order_ok, order_detail = True, {}
    if winner:
        wid = winner_reply["submission_id"]
        received, judged = first(events, "submission_received", wid), first(events, "submission_judged", wid)
        closed = next((e for e in events if e["kind"] == "task_closed"
                       and e["payload"].get("problem_id") == GENUINE_TASK), None)
        published = next((e for e in events if e["kind"] == "library_published"
                          and e["payload"].get("public_submission_id") == wid), None)
        chain = [x["seq"] if x else None for x in (received, judged, closed, published)]
        order_ok = None not in chain and chain == sorted(chain)
        order_detail["winner_chain"] = chain
        for agent in racers:
            reply = race[agent][0]
            if agent == winner or "submission_id" not in reply:
                continue
            got, verdict = (first(events, "submission_received", reply["submission_id"]),
                            first(events, "submission_judged", reply["submission_id"]))
            if reply["outcome"] == "accepted_after_close":
                good = got and verdict and closed and got["seq"] < closed["seq"] < verdict["seq"]
            else:
                good = got and closed and closed["seq"] < got["seq"] and verdict is None
            order_ok = order_ok and bool(good)
            order_detail[agent] = [x["seq"] if x else None for x in (got, verdict)]
    checks.add("race_event_ordering", winner is not None and order_ok, order_detail)

    if not args.fake_lean:
        check_leftover_containers(checks)
    checks.add("real_lean", not args.fake_lean)

    # X5 gate fields: only a complete run at the configured judge limits is evidence.
    inner = getattr(compiler, "inner", None)
    limits = (judge_limits(args.timeout, args.memory, args.cpus) if args.fake_lean
              else judge_limits(inner.timeout, inner.memory, inner.cpus))
    incomplete = incomplete_reasons(args, statement_results)
    limits_match = same_limits(limits, args.config_limits)
    checks_passed = all(c["passed"] for c in checks.items if c["required"])
    record = {
        "schema": SCHEMA, "created_at": created_at,
        "all_required_passed": not incomplete and limits_match and checks_passed,
        "complete": not incomplete,
        "incomplete_reasons": incomplete,
        "limits": limits,
        "config": {"path": display_path(args.config), "limits": args.config_limits},
        "limits_match_config": limits_match,
        "image_ids": image_ids,
        "real_lean": not args.fake_lean,
        "run_dir": str(run_dir.relative_to(ROOT)) if run_dir.is_relative_to(ROOT) else str(run_dir),
        "settings": {"threads": args.threads, "max_concurrent": args.max_concurrent, "gate": gate,
                     "image": args.image,
                     "compile_timeout_seconds": args.timeout, "memory": args.memory, "cpus": args.cpus,
                     "observer": observer_kind, "observer_join_timeout": args.observer_join_timeout},
        "fixtures": {"genuine": {"task_id": GENUINE_TASK, "proof_fixture": str(GENUINE_FIXTURE.relative_to(ROOT)),
                                 "source_sha256": sha256(genuine)}},
        "checks": checks.items,
        "race": [{"agent_id": a, "seconds": race[a][1], **summarize(race[a][0])} for a in racers],
        "warmup_compile": warmup,
        "late_submission": summarize(late),
        "statement_change": statement_results,
        "compiles": compiler.records,
        "event_audit": audit,
        "event_counts": {kind: sum(e["kind"] == kind for e in events) for kind in sorted({e["kind"] for e in events})},
        "duration_seconds": round(time.monotonic() - started, 1),
    }
    out_dir = Path(args.output_dir) if args.output_dir else (run_dir if args.fake_lean else ROOT / "data" / "validation")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{PREFIX}-{stamp}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n")
    if incomplete or not limits_match:
        print("Not launch evidence: " + "; ".join(
            incomplete + ([] if limits_match else [f"limits {limits} differ from config {args.config_limits}"])))
    print(f"Wrote {path} (all_required_passed={record['all_required_passed']})")
    return (0 if record["all_required_passed"] else 1), path


def incomplete_reasons(args, statement_results):
    """Why this run is not a complete conference-race validation (empty when it is)."""
    reasons = []
    if args.fake_lean:
        reasons.append("--fake-lean: no real Lean compiler")
    skipped = [r["fixture"] for r in statement_results if "operational_outcome" not in r]
    if skipped:
        reasons.append("statement-changing fixture(s) not submitted: " + ", ".join(skipped))
    if not statement_results:
        reasons.append("no statement-changing fixture submitted")
    return reasons


def same_limits(recorded, expected):
    """Limit equality as Docker reads it: memory case-insensitively, numbers by value."""
    return set(recorded) == set(expected) and all(
        str(recorded[k]).strip().lower() == str(expected[k]).strip().lower() if k == "memory"
        else float(recorded[k]) == float(expected[k]) for k in expected)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--threads", type=int, default=4, help="simultaneous genuine submissions (default 4)")
    parser.add_argument("--max-concurrent", type=int, default=2,
                        help="concurrent Lean containers (judge gate); default 2 (2 GiB each)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="run config supplying the judge limits and Lean image (default: configs/pilot.json)")
    parser.add_argument("--image", default=None, help="default: config lean_image")
    parser.add_argument("--timeout", type=int, default=None,
                        help="default: config compilation_timeout_seconds (an override is never launch evidence)")
    parser.add_argument("--memory", default=None, help="default: config judge_memory (override: not evidence)")
    parser.add_argument("--cpus", type=float, default=None, help="default: config judge_cpus (override: not evidence)")
    parser.add_argument("--statement-fixture", action="append", choices=sorted(STATEMENT_FIXTURES),
                        help="statement-changing fixture(s) to submit (default: eulerbrick_notation_override.lean)")
    parser.add_argument("--observer-join-timeout", type=float, default=300.0)
    parser.add_argument("--no-warmup", dest="warmup", action="store_false",
                        help="skip the warm-up compile (outside the conference) before the race")
    parser.add_argument("--run-root", default=str(ROOT / "runs"))
    parser.add_argument("--output-dir", default=None, help="default data/validation (run dir with --fake-lean)")
    parser.add_argument("--fake-lean", action="store_true", help="development only: no Docker, fails real_lean")
    args = parser.parse_args(argv)
    if args.threads < 2:
        parser.error("--threads must be at least 2")
    if args.max_concurrent is not None and args.max_concurrent < 1:
        parser.error("--max-concurrent must be at least 1")
    if not args.statement_fixture:
        args.statement_fixture = ["eulerbrick_notation_override.lean"]
    try:
        config = load_config(args.config)
        args.config_limits = config_limits(config)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"cannot read judge limits from {args.config}: {exc}")
    if args.image is None:
        args.image = config.get("lean_image") or DEFAULT_LEAN_IMAGE
    for attr, key in (("timeout", "timeout_seconds"), ("memory", "memory"), ("cpus", "cpus")):
        if getattr(args, attr) is None:
            setattr(args, attr, args.config_limits[key])
    try:
        judge_limits(args.timeout, args.memory, args.cpus)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def live_container_blockers():
    """Running comparator, leftover judge or other experiment agent containers
    (runtime.launch_interlock, read-only). They would distort the compile timings
    recorded as launch evidence, so this script refuses to start beside them."""
    from research_swarm.runtime import launch_interlock
    return launch_interlock()["blockers"]


def main(argv=None):
    args = parse_args(argv)
    if not args.fake_lean:
        blockers = live_container_blockers()
        if blockers:
            print("Refusing to start: " + "; ".join(blockers) + ". Nothing was started.", file=sys.stderr)
            return 2
    code, _ = run(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
