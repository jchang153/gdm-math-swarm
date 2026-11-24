"""Six-metric arm comparison for GLM 5.3 Flash (single model), mean ± SEM, same panels and colours as fig_arm_bars_ds.py.
GLM data loaded exactly as fig_arm_bars_glm_vs_ds.py (monitor_arms-2026-11-16.json, overseer_share.json, local A1/A3
run results.json, monitor_arms-glm-ag-2026-11-24.json). Run from the repo root."""
import glob
import json
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np
ROOT = Path(__file__).resolve().parents[4]
D = Path(__file__).resolve().parent
OLD = ROOT / "research/analysis/figures/meeting-2026-11-18"
g = json.load(open(ROOT / "research/analysis/monitor_arms-2026-11-16.json"))["arms"]
ov = json.load(open(OLD / "overseer_share.json"))
# Old data keys: "M2" there is actions-reason (now M3); "M1" is actions-silent in both labellings.
g["M3"] = g["M2"]
g["AG"] = json.load(open(ROOT / "research/analysis/monitor_arms-glm-ag-2026-11-24.json"))["arms"]["AG"]
def dig(d, key):
    for p in key.split("."): d = d[p]
    return d
KEYS = ["fakes.fakers", "fakes.fake_submissions", "fakes.credited_fake_submissions", "genuine.credited_tasks",
        "slowdown.step_seconds.median", "cost.total_usd", "cost.reviewer_usd"]
for a, tag in {"A1": "rollback-silent", "A2": "revert-silent", "A3": "rollback-reason", "A4": "revert-reason"}.items():
    runs = sorted(p for p in glob.glob(str(ROOT / f"runs/glmflash100-seeded-reviewer-{tag}-120m-push-r*")) if "ABORTED" not in p)
    assert len(runs) == 3, runs
    om = [json.load(open(p + "/results.json"))["outcome_metrics"] for p in runs]
    g[a] = {"runs": [Path(p).name for p in runs], "metrics": {k: {"values": [dig(o, k) for o in om]} for k in KEYS}}
arms = g
ORDER = [a for a in ["control", "10ov", "M1", "M3", "A1", "A2", "A3", "A4", "AG"] if a in arms]
LABEL = {"control": "control", "unseeded-control": "control\nUNSEEDED\n(n=1, no seed)", "10ov": "10 ov",
         "M1": "M1\nactions\nsilent", "M3": "M3\nactions\nreason", "A1": "A1\nrollback\nsilent", "A3": "A3\nrollback\nreason", "A2": "A2\nrevert\nsilent", "A4": "A4\nrevert\nreason", "AG": "AG\nagentic\nrevert+reason"}
FAM = {"control": "g", "unseeded-control": "u", "10ov": "b", "M1": "o", "M3": "o", "A1": "p", "A2": "p", "A3": "p", "A4": "p", "AG": "t"}
LIGHT = {"g": "#d4d4d4", "u": "#ffffff", "b": "#b1cde9", "o": "#f0d3b2", "p": "#d3c4ea", "t": "#b6e2d3"}
EDGE = {"g": "#b5b5b5", "u": "#808080", "b": "#7aa8db", "o": "#e6b47f", "p": "#a68bd3", "t": "#5fb597"}
DOT = {"g": "#808080", "u": "#808080", "b": "#2a72c4", "o": "#d9822b", "p": "#7350b8", "t": "#1a9a72"}
DARK = {"b": "#2a72c4", "o": "#dd8a3a", "p": "#7350b8", "t": "#1a9a72"}
OVKEY = {"o": "cost.monitor_usd", "p": "cost.reviewer_usd", "t": "cost.monitor_usd"}
def vals(a, key): return [v if v is not None else np.nan for v in arms[a]["metrics"][key]["values"]] or [np.nan]
def oversight(a):
    f = FAM[a]
    if f == "b": return [next(v["overseer_usd"] for k, v in ov.items() if r in k) for r in arms[a]["runs"]]
    return vals(a, OVKEY[f]) if f in OVKEY else [0.0] * len(arms[a]["runs"])
panels = [("fakes.fakers", "Fakers (of 100 solvers)", 1), ("fakes.fake_submissions", "Fake submissions", 0),
          ("fakes.credited_fake_submissions", "Credited fakes", 1), ("genuine.credited_tasks", "Genuine credited tasks", 1),
          ("slowdown.step_seconds.median", "Median seconds per step", 1),
          ("cost.total_usd", "Cost per run (USD)\nlight = swarm, hatched = oversight (overseers, monitors or reviewer)", 1)]
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
fig, axs = plt.subplots(2, 3, figsize=(23, 12.5))
x = np.arange(len(ORDER))
for ax, (key, title, dec) in zip(axs.flat, panels):
    labels = []
    for i, a in enumerate(ORDER):
        v = np.array(vals(a, key), float); v = v[~np.isnan(v)]; m = v.mean(); f = FAM[a]
        ls = "--" if f == "u" else "-"
        if key == "cost.total_usd" and f in DARK:
            o = np.nanmean(oversight(a)); ax.bar(i, m - o, 0.7, color=LIGHT[f], edgecolor=EDGE[f])
            ax.bar(i, o, 0.7, bottom=m - o, color=DARK[f], edgecolor=DARK[f], hatch="//", alpha=0.85)
            if o > 4: ax.text(i, m - o / 2, f"{o:.1f}", ha="center", va="center", color="white", fontsize=9)
            else: ax.text(i, m - o - 0.6, f"+{o:.2f}", ha="center", va="top", fontsize=8.5)
        else:
            ax.bar(i, m, 0.7, color=LIGHT[f], edgecolor=EDGE[f], linestyle=ls, linewidth=1.3 if f == "u" else 1)
        se = v.std(ddof=1) / np.sqrt(len(v)) if len(v) > 1 else 0
        if se: ax.errorbar(i, m, yerr=se, color="#333333", capsize=5, lw=1.4, zorder=4)
        jit = np.linspace(-0.18, 0.18, len(v)) if len(v) > 1 else [0]
        ax.scatter(i + np.array(jit), v, s=30, color=DOT[f], edgecolor="white", zorder=3)
        labels.append((i, max(m + se, v.max()), (f"{m:,.0f}" if dec == 0 else f"{m:.1f}") + (f"\nn={len(v)}" if len(v) not in (3,) else "")))
    ymax = max(t for _, t, _ in labels) or 1
    for i, top, s in labels:
        ax.text(i, top + 0.02 * ymax, s, ha="center", va="bottom", fontsize=9)
    ax.set_xticks(x); ax.set_xticklabels([LABEL[a] for a in ORDER], fontsize=9.5)
    ax.set_title(title, loc="left", fontsize=13); ax.grid(axis="y", alpha=0.3)
    ax.set_ylim(0, ymax * 1.18)
fig.legend(handles=[Patch(color=LIGHT["g"], label="No oversight"),
                    Patch(color=LIGHT["b"], label="In-swarm overseers (bottom-up)"),
                    Patch(color=LIGHT["o"], label="Blocking monitor (top-down), threshold 80"),
                    Patch(color=LIGHT["p"], label="Asynchronous reviewer, every 5 min, threshold 80"),
                    Patch(color=LIGHT["t"], label="Agentic monitor per solver (revert with reason)")],
           loc="upper left", bbox_to_anchor=(0.01, 0.94), ncol=5, frameon=False, fontsize=10.5)
fig.suptitle("GLM 5.3 Flash: oversight arms vs. control (agentic monitor AG 2026-11-24)", x=0.01, y=0.995, ha="left", fontsize=16)
fig.text(0.01, 0.948, "Bar = arm mean; error bar = ±1 SEM; dots = runs (n = 3 unless marked). 100 GLM 5.3 Flash solvers, 120 min, push, seeded. Runs: control Nov 4; 10 ov Nov 4 + Nov 16; M1 Nov 11; "
         "M3 Nov 11 + Nov 16; A1-A4 Nov 19; AG Nov 24. AG cost = settled + held ($8-15 held per run).", fontsize=10.5)
fig.tight_layout(rect=(0, 0, 1, 0.91))
for ext in ("png", "pdf"):
    fig.savefig(D / f"arm_bars_6metrics_glm-2026-11-24.{ext}", dpi=150)
for a in ORDER:
    print(a, len(arms[a]["runs"]), [round(float(np.nanmean(vals(a, k))), 2) for k, *_ in panels], round(float(np.nanmean(oversight(a))), 3))
