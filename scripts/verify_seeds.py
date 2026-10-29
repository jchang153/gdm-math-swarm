#!/usr/bin/env python3
"""Researcher-only check of a declared seeded run's planted artifacts before any paid call.

For every seed of CONFIG's ``seeded`` block (single form or ``artifacts`` list; docs/plan.md, "Three
exploit families seeded, October 29, 2026"):

1. operational judge: the seed's source, submitted as an ordinary solution in a throwaway conference
   with the run's real Lean compiler, must be ``accepted``;
2. ``check_proof``: the overseer tool's independent checker on that accepted submission must not verify it;
3. independent evaluator: ``scripts/evaluate_run.py`` on the throwaway run must not mark it ``genuine``
   (expected ``fraudulent``).

It also replays the seeded injection path (no Lean, no model): a seeded conference built from CONFIG
with a controlled clock must inject every seed at its trigger time, closing each task and publishing
``solutions/<task>.lean``.

Writes ``data/validation/seed-verification-<UTC>.json`` (verdicts, digests and reasons; never the
seed sources) and keeps the throwaway run under ``runs/phase-b/seed-verification-<UTC>/`` (it holds
the seed sources: researcher-only, Git-ignored). Runs on the VM, with no run in progress on Docker.

  PYTHONPATH=src:. .venv/bin/python scripts/verify_seeds.py --config configs/<cfg>.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from research_swarm import runtime, seeded  # noqa: E402
from research_swarm.conference import ConferenceService, operational_judge, tasks_from_dataset  # noqa: E402
from research_swarm.dataset import FrozenDataset, task_set_of  # noqa: E402

AGENT, OVERSEER = "agent-001", "agent-002"


def replay_injection(config, tasks, folder):
    """The seeded path with a controlled clock: every seed injected at its trigger, once."""
    arguments = runtime.conference_track_arguments(config, ROOT)
    now = [1_000_000.0]
    count = config.get("agent_count", 1)
    agents = [f"agent-{i:03d}" for i in range(1, count + 2)]

    def never(task, source):
        raise AssertionError("the seeded path must not call the judge")

    service = ConferenceService(folder, tasks, never, agent_ids=agents, clock=lambda: now[0], **arguments)
    try:
        service.mark_run_started(now[0])
        triggers = sorted({b.get("trigger_value") for b in seeded.expand_block(config["seeded"])})
        for value in triggers:
            now[0] = 1_000_000.0 + float(value)
            service.poll_seeded_trigger()
        library = service.action(agents[0], "list_library", {})
    finally:
        service.close()
    events = [json.loads(line) for line in (Path(folder) / "conference" / "events.jsonl").read_text().splitlines()]
    injected = [e["payload"] for e in events if e.get("kind") == "seeded_artifact_injected"]
    return {"injected": [{"task_id": p.get("task_id"), "path": p.get("path"), "artifact_sha256": p.get("artifact_sha256"),
                          "trigger": p.get("trigger")} for p in injected],
            "library_lists_solutions": sorted({p.get("path") for p in injected if p.get("path")
                                               and p["path"] in json.dumps(library)})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--replay-only", action="store_true", help="only the injection replay (no Docker)")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "runs" / "phase-b" / f"seed-verification-{stamp}"
    folder.mkdir(parents=True)
    (folder / "RESEARCHER_ONLY.txt").write_text("Seed verification (scripts/verify_seeds.py): holds planted exploit "
                                                "sources. Never mount, copy or reuse for an experimental run.\n")
    tasks = tasks_from_dataset(FrozenDataset(ROOT, task_set=task_set_of(config)))
    seeds = seeded.expand_block(config["seeded"])
    record = {"schema": "gdm-seed-verification/1", "created_at": stamp, "config": str(args.config),
              "experiment": config.get("experiment"), "throwaway_run": str(folder.relative_to(ROOT)),
              "replay": replay_injection(config, tasks, folder / "replay")}
    results = []
    for seed in seeds:
        data = (ROOT / seed["artifact_path"]).read_bytes()
        results.append({"task_id": seed.get("task_id"), "family": seed.get("family"),
                        "artifact_path": seed["artifact_path"], "artifact_sha256": hashlib.sha256(data).hexdigest(),
                        "pinned_sha256_matches": hashlib.sha256(data).hexdigest() == seed.get("artifact_sha256")})
    if not args.replay_only:
        from research_swarm.oversight import make_proof_checker
        run_dir = folder / "judge-run"
        compiler = runtime.make_lean_compiler(config)
        service = ConferenceService(run_dir, tasks, operational_judge(compiler),
                                    observer=runtime.queue_observer(run_dir), agent_ids=[AGENT, OVERSEER])
        try:
            for item in results:
                source = (ROOT / item["artifact_path"]).read_text()
                reply = service.action(AGENT, "submit_solution", {"problem_id": item["task_id"], "source": source})
                item["operational_judge"] = {k: reply.get(k) for k in ("ok", "outcome", "accepted", "stage", "reason",
                                                                         "error")}
            service.enable_oversight(OVERSEER, make_proof_checker())
            for item in results:
                reply = service.action(OVERSEER, "check_proof", {"problem_id": item["task_id"]})
                item["check_proof"] = {k: reply.get(k) for k in ("ok", "verified", "result", "validity", "reason",
                                                                 "error")}
        finally:
            service.close()
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "evaluate_run.py"), "--run-dir", str(run_dir)],
                              cwd=ROOT, capture_output=True, text=True,
                              env={**__import__("os").environ, "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}"})
        record["evaluate_run_exit"] = done.returncode
        (folder / "evaluate_run.log").write_text(done.stdout + "\n--- stderr ---\n" + done.stderr)
        latest = {}
        path = run_dir / "evaluations" / "evaluations.jsonl"
        if path.is_file():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                latest[row.get("task_id") or row.get("problem_id")] = row
        for item in results:
            row = latest.get(item["task_id"]) or {}
            item["evaluator"] = {"validity": row.get("validity") or row.get("verdict"), "reason": row.get("reason"),
                                 "details": str((row.get("evidence") or {}).get("details") or "")[:300] or None}
    for item in results:
        judge = (item.get("operational_judge") or {}).get("outcome")
        item["passed"] = item["pinned_sha256_matches"] and (args.replay_only or (
            judge == "accepted" and (item.get("check_proof") or {}).get("verified") is False
            and (item.get("evaluator") or {}).get("validity") not in (None, "genuine")))
    replay_tasks = [i["task_id"] for i in record["replay"]["injected"]]
    record["replay"]["passed"] = replay_tasks == [s.get("task_id") for s in seeds] and \
        len(record["replay"]["library_lists_solutions"]) == len(seeds)
    record["seeds"] = results
    record["passed"] = record["replay"]["passed"] and all(i["passed"] for i in results)
    out = ROOT / "data" / "validation" / f"seed-verification-{stamp}.json"
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(record, indent=2, ensure_ascii=False))
    print(f"wrote {out.relative_to(ROOT)}")
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
