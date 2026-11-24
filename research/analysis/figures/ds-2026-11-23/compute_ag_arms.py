"""Add arm AG (one agentic monitor per solver, revert-with-reason; HANDOFF item 57) to the arm comparisons, for
DeepSeek V4.1 Flash and GLM 5.3 Flash (3 runs each, 2026-11-24). Run from the repo root:

  python research/analysis/figures/ds-2026-11-23/compute_ag_arms.py [--root DIR]

DIR holds one directory per run with its results.json (default runs/; a directory of the runs' extracted
``_files.tar.gz`` bundles from the logs dataset works too). AG metrics are the results.json ``outcome_metrics``
computed on the VM (same run_metrics definitions; fakes and credited fakes come from the story-map rule, not the
behavior judge; cost.total_usd = settled + held, monitor = the agmon-NNN agents), aggregated with
compare_arms.compare. The other DeepSeek arms are copied unchanged from monitor_arms-ds-2026-11-23.json.
Writes monitor_arms-ds-2026-11-24.json (DeepSeek arms + AG) and monitor_arms-glm-ag-2026-11-24.json (GLM AG).
Only the six completed runs; the empty startup-failure runs are never included. Offline and read only.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import compare_arms  # noqa: E402

AG_RUNS = {
    "DS": ["dsv41flash100-seeded-agentic-monitor-120m-push-r1-20261124T103727Z",
           "dsv41flash100-seeded-agentic-monitor-120m-push-r2-20261124T112224Z",
           "dsv41flash100-seeded-agentic-monitor-120m-push-r3-20261124T103722Z"],
    "GLM": ["glmflash100-seeded-agentic-monitor-120m-push-r1-20261124T103755Z",
            "glmflash100-seeded-agentic-monitor-120m-push-r2-20261124T112253Z",
            "glmflash100-seeded-agentic-monitor-120m-push-r3-20261124T103757Z"],
}
NOTE = ("AG = one agentic monitor per solver (agmon-NNN, revert-with-reason), 100 solvers + 100 monitors, 120 min, "
        "push, seeded; metrics from each run's results.json outcome_metrics (VM-computed, simple behavior judge not "
        "yet re-run; fakes use the story-map rule). cost.total_usd = settled + held (AG holds $8-18 per run); "
        "cost.monitor_usd = agmon-* ledger rows, counted as oversight.")


def from_results(run, ledger=None):
    return json.loads((Path(run) / "results.json").read_text(encoding="utf-8"))["outcome_metrics"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT / "runs")
    args = ap.parse_args()
    ds = json.loads((ROOT / "research/analysis/monitor_arms-ds-2026-11-23.json").read_text(encoding="utf-8"))
    keys = [k for k, _ in ds["metrics"]]
    for model, runs in AG_RUNS.items():
        dirs = [args.root / r for r in runs]
        missing = [d for d in dirs if not (d / "results.json").is_file()]
        assert not missing, f"results.json missing: {missing}"
        ag = compare_arms.compare([("AG", dirs)], metrics=keys, compute=from_results)
        if model == "DS":
            out = dict(ds, arms={**ds["arms"], "AG": ag["arms"]["AG"]}, note=ds["note"] + " " + NOTE)
            dst = ROOT / "research/analysis/monitor_arms-ds-2026-11-24.json"
        else:
            out = dict(ag, missing=[], note="GLM 5.3 Flash. " + NOTE)
            dst = ROOT / "research/analysis/monitor_arms-glm-ag-2026-11-24.json"
        dst.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(model, dst.relative_to(ROOT))
        print(compare_arms.render_table(ag))


if __name__ == "__main__":
    main()
