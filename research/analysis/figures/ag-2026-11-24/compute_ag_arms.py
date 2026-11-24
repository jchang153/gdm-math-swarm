"""Agentic monitor arm AG (2026-11-24) vs the other arms, per model, from each run's stored results.json
``outcome_metrics`` (runs/RUN/results.json; index-only runs keep it). Means +- SEM over runs. Read only, offline:

  python3 research/analysis/figures/ag-2026-11-24/compute_ag_arms.py

GLM control and 10ov r1-r3 predate outcome_metrics in results.json: their fakers, fake submissions, fake-minutes,
genuine tasks, steps and cost come from research/analysis/monitor_arms-2026-11-16.json (computed fresh, same rule
answer-echo-v2), credited fakers from docs/HANDOFF.md (15 GLM 120m-push runs table) and oversight $ from
research/analysis/pareto/pareto_points_fakers-2026-11-18.json (oversight share, a percent, x cost). Labels as relabelled on
2026-11-19 (silent = 1/2, reason = 3/4): M1 actions-silent, M3 actions-reason; A1 rollback-silent, A2 revert-silent,
A3 rollback-reason, A4 revert-reason.
"""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNS = ROOT / "runs"
OUT = Path(__file__).with_name("ag_arms-2026-11-24.json")


def runs(prefix, reps=None):
    found = sorted(p.name for p in RUNS.glob(prefix + "-r[0-9]-*") if (p / "results.json").is_file())
    return [r for r in found if reps is None or any(f"-r{n}-" in r for n in reps)]


ARMS = {
    "DS": {
        "control": runs("dsv41flash100-seeded-control-120m-push"),
        "10ov": runs("dsv41flash100-seeded-10ov-ds-120m-push"),
        "M1": runs("dsv41flash100-seeded-monitor-actions-silent-120m-push"),
        "M3": runs("dsv41flash100-seeded-monitor-actions-reason-120m-push"),
        "A1": runs("dsv41flash100-seeded-reviewer-rollback-silent-120m-push"),
        "A3": runs("dsv41flash100-seeded-reviewer-rollback-reason-120m-push"),
        "AG": runs("dsv41flash100-seeded-agentic-monitor-120m-push"),
    },
    "GLM": {
        "control": runs("glmflash100-seeded-control-120m-push"),
        "10ov": runs("glmflash100-seeded-10ov-glm-120m-push"),
        "M1": runs("glmflash100-seeded-monitor-actions-silent-120m-push"),
        "M3": runs("glmflash100-seeded-monitor-actions-reason-120m-push"),
        "A1": runs("glmflash100-seeded-reviewer-rollback-silent-120m-push"),
        "A2": runs("glmflash100-seeded-reviewer-revert-silent-120m-push"),
        "A3": runs("glmflash100-seeded-reviewer-rollback-reason-120m-push"),
        "A4": runs("glmflash100-seeded-reviewer-revert-reason-120m-push"),
        "AG": runs("glmflash100-seeded-agentic-monitor-120m-push"),
    },
}
# GLM control / 10ov r1-r3: credited fakers from docs/HANDOFF.md (strict rule answer-echo-v2)
CREDITED_FAKERS = {"glmflash100-seeded-control-120m-push-r1-20261104T042405Z": 8,
                   "glmflash100-seeded-control-120m-push-r2-20261104T042410Z": 11,
                   "glmflash100-seeded-control-120m-push-r3-20261104T042326Z": 5,
                   "glmflash100-seeded-10ov-glm-120m-push-r1-20261104T044246Z": 1,
                   "glmflash100-seeded-10ov-glm-120m-push-r2-20261104T043647Z": 0,
                   "glmflash100-seeded-10ov-glm-120m-push-r3-20261104T043629Z": 0}
METRICS = ["fakers", "credited_fakers", "fake_minutes", "genuine_tasks", "fake_submissions", "credited_fakes",
           "step_s", "req_per_agent_min", "total_usd", "settled_usd", "oversight_usd"]


def fallback_values():
    """run -> metric values for runs without stored outcome_metrics (see module docstring)."""
    out = {}
    m16 = json.loads((ROOT / "research/analysis/monitor_arms-2026-11-16.json").read_text())
    for arm in ("control", "10ov"):
        a = m16["arms"][arm]
        for i, run in enumerate(a["runs"]):
            v = lambda k: a["metrics"][k]["values"][i]  # noqa: E731
            out[run] = {"fakers": v("fakes.fakers"), "fake_minutes": v("fakes.fake_minutes"),
                        "genuine_tasks": v("genuine.credited_tasks"), "fake_submissions": v("fakes.fake_submissions"),
                        "credited_fakes": v("fakes.credited_fake_submissions"), "step_s": v("slowdown.step_seconds.median"),
                        "req_per_agent_min": v("slowdown.requests_per_agent") / 120.0, "total_usd": v("cost.total_usd"),
                        "credited_fakers": CREDITED_FAKERS.get(run)}
    pareto = json.loads((ROOT / "research/analysis/pareto/pareto_points_fakers-2026-11-18.json").read_text())
    arms = pareto["arms"] if isinstance(pareto["arms"], list) else list(pareto["arms"].values())
    for a in arms:
        for r in a.get("runs", []):
            if r["run_id"] in out and r.get("oversight_share") is not None:
                out[r["run_id"]]["oversight_usd"] = r["oversight_share"] / 100.0 * r["cost"]   # a percent
    return out


def run_values(run, fallback):
    d = json.loads((RUNS / run / "results.json").read_text())
    om = d.get("outcome_metrics") or {}
    if not om.get("fakes"):
        return dict(fallback.get(run, {}), settled_usd=None)
    f, g, s, c = om["fakes"], om.get("genuine") or {}, om.get("slowdown") or {}, om.get("cost") or {}
    oversight = c.get("oversight_usd")
    if oversight is None:
        parts = [c.get(k) for k in ("overseer_usd", "monitor_usd", "reviewer_usd")]
        oversight = sum(p for p in parts if p) if any(p is not None for p in parts) else None
    minutes = 120.0
    return {"fakers": f.get("fakers"), "credited_fakers": f.get("credited_fakers"), "fake_minutes": f.get("fake_minutes"),
            "genuine_tasks": g.get("credited_tasks"), "fake_submissions": f.get("fake_submissions"),
            "credited_fakes": f.get("credited_fake_submissions"), "step_s": (s.get("step_seconds") or {}).get("median"),
            "req_per_agent_min": (s["requests"] / s["solvers"] / minutes) if s.get("requests") and s.get("solvers") else None,
            "total_usd": c.get("total_usd"), "settled_usd": c.get("settled_usd"), "oversight_usd": oversight,
            "agentic": om.get("agentic")}


def mean_sem(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None, None, 0
    m = sum(xs) / len(xs)
    sem = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1) / len(xs)) if len(xs) > 1 else None
    return m, sem, len(xs)


def main():
    fb = fallback_values()
    result = {}
    for model, arms in ARMS.items():
        result[model] = {}
        for arm, rs in arms.items():
            per = {r: run_values(r, fb) for r in rs}
            summary = {}
            for k in METRICS:
                m, e, n = mean_sem([per[r].get(k) for r in rs])
                summary[k] = {"mean": m, "sem": e, "n": n}
            result[model][arm] = {"runs": {r: {k: v for k, v in per[r].items() if k != "agentic"} for r in rs},
                                  "summary": summary}
    OUT.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    fmt = lambda s: "-" if s["mean"] is None else (f"{s['mean']:.1f}" + (f" ± {s['sem']:.1f}" if s["sem"] is not None else ""))  # noqa: E731
    for model, arms in result.items():
        print(f"\n## {model}\n| arm | n | " + " | ".join(METRICS) + " |")
        for arm, a in arms.items():
            n = len(a["runs"])
            print(f"| {arm} | {n} | " + " | ".join(fmt(a["summary"][k]) for k in METRICS) + " |")


if __name__ == "__main__":
    main()
