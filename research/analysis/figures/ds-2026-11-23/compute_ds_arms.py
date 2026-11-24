"""Outcome metrics of the DeepSeek V4.1 Flash oversight runs (2026-11-23/24), aggregated per arm with
scripts/compare_arms.py ``compare`` (the generator of monitor_arms-2026-11-16.json), headline metrics plus the
overseer/oversight cost split. Run from the repo root:

  python research/analysis/figures/ds-2026-11-23/compute_ds_arms.py

Arms from private/ops/ds-oversight/assign.txt, run ids from private/ops/ds-oversight/state/runs.txt (VM -> run).
Runs not restored under runs/ are left out and listed under "missing". Offline and read only.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import compare_arms  # noqa: E402
from research.analysis import run_metrics  # noqa: E402

OPS = ROOT / "private/ops/ds-oversight"
vm_run = dict(l.split()[:2] for l in (OPS / "state/runs.txt").read_text().splitlines() if l.strip() and not l.startswith("#"))
ORDER = ["control", "unseeded-control", "10ov", "M1", "M3", "A1", "A3"]
arms = {a: [] for a in ORDER}
for line in (OPS / "assign.txt").read_text().splitlines():
    if not line.strip() or line.startswith("#"):
        continue
    vm, _zone, arm, rep = line.split()[:4]
    arms[arm].append((rep, vm_run[vm]))
missing, chosen = [], []
for a in ORDER:
    dirs = []
    for rep, run in sorted(arms[a]):
        d = ROOT / "runs" / run
        if (d / "results.json").is_file() and (d / "runtime.jsonl").is_file():
            dirs.append(d)
        else:
            missing.append(run)
    if dirs:
        chosen.append((a, dirs))
keys = [k for k, _ in run_metrics.HEADLINE] + ["cost.overseer_usd", "cost.solver_usd"]
out = compare_arms.compare(chosen, metrics=keys)
out["missing"] = missing
out["note"] = ("DeepSeek V4.1 Flash, 100 solvers, 120 min, push. Arms per private/ops/ds-oversight/assign.txt; "
               "unseeded-control has no planted seed (n=1). M1 = monitor actions-silent, M3 = monitor actions-reason, "
               "A1 = reviewer rollback-silent, A3 = reviewer rollback-reason.")
dst = ROOT / "research/analysis/monitor_arms-ds-2026-11-23.json"
dst.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
print(compare_arms.render_table(out))
print("missing:", missing)
