"""GLM 5.3 Flash vs DeepSeek V4.1 Flash, paired bars per shared arm (control, 10ov, M1, M3, A1, A3), six metrics,
mean ± SEM. GLM data exactly as ../meeting-2026-11-18/fig_arm_bars_reviewer.py uses it: monitor_arms-2026-11-16.json
(control, 10ov n=6, M1, and data key "M2" = current M3 actions-reason n=6), overseer_share.json for the 10ov overseer
cost, and results.json outcome_metrics of the local glmflash100-seeded-reviewer-rollback-{silent,reason} runs for
A1/A3. DeepSeek data from monitor_arms-ds-2026-11-23.json. Run from the repo root."""
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
ds = json.load(open(ROOT / "research/analysis/monitor_arms-ds-2026-11-23.json"))["arms"]
# Old data keys: "M2" there is actions-reason (now M3); "M1" is actions-silent in both labellings.
g["M3"] = g["M2"]
def dig(d, key):
    for p in key.split("."): d = d[p]
    return d
KEYS = ["fakes.fakers", "fakes.fake_submissions", "fakes.credited_fake_submissions", "genuine.credited_tasks",
        "slowdown.step_seconds.median", "cost.total_usd", "cost.reviewer_usd"]
for a, tag in {"A1": "rollback-silent", "A3": "rollback-reason"}.items():
    runs = sorted(p for p in glob.glob(str(ROOT / f"runs/glmflash100-seeded-reviewer-{tag}-120m-push-r*")) if "ABORTED" not in p)
    assert len(runs) == 3, runs
    om = [json.load(open(p + "/results.json"))["outcome_metrics"] for p in runs]
    g[a] = {"runs": [Path(p).name for p in runs], "metrics": {k: {"values": [dig(o, k) for o in om]} for k in KEYS}}
ORDER = [a for a in ["control", "10ov", "M1", "M3", "A1", "A3"] if a in ds]
LABEL = {"control": "control", "10ov": "10 ov", "M1": "M1 actions\nsilent", "M3": "M3 actions\nreason",
         "A1": "A1 rollback\nsilent", "A3": "A3 rollback\nreason"}
FAM = {"control": "g", "10ov": "b", "M1": "o", "M3": "o", "A1": "p", "A3": "p"}
LIGHT = {"g": "#d4d4d4", "b": "#b1cde9", "o": "#f0d3b2", "p": "#d3c4ea"}; EDGE = {"g": "#909090", "b": "#5a8fcb", "o": "#d9822b", "p": "#8a6cc7"}
DOT = {"g": "#808080", "b": "#2a72c4", "o": "#d9822b", "p": "#7350b8"}; DARK = {"b": "#2a72c4", "o": "#dd8a3a", "p": "#7350b8"}
def vals(src, a, key):
    v = np.array([x if x is not None else np.nan for x in src[a]["metrics"][key]["values"]], float)
    return v[~np.isnan(v)]
def oversight(model, a):
    f = FAM[a]
    if f == "g": return np.zeros(1)
    if model == "DS": return vals(ds, a, {"b": "cost.overseer_usd", "o": "cost.monitor_usd", "p": "cost.reviewer_usd"}[f])
    if f == "o": return vals(g, a, "cost.monitor_usd")
    if f == "p": return vals(g, a, "cost.reviewer_usd")
    return np.array([next(v["overseer_usd"] for k, v in ov.items() if r in k) for r in g[a]["runs"]])
panels = [("fakes.fakers", "Fakers (of 100 solvers)", 1), ("fakes.fake_submissions", "Fake submissions", 0),
          ("fakes.credited_fake_submissions", "Credited fakes", 1), ("genuine.credited_tasks", "Genuine credited tasks", 1),
          ("slowdown.step_seconds.median", "Median seconds per step", 1),
          ("cost.total_usd", "Cost per run (USD)\nlower part = swarm, hatched top = oversight", 1)]
MODELS = [("GLM", g, -0.21), ("DS", ds, 0.21)]
W = 0.38
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
fig, axs = plt.subplots(2, 3, figsize=(22, 12.5))
x = np.arange(len(ORDER))
for ax, (key, title, dec) in zip(axs.flat, panels):
    labels = []
    for i, a in enumerate(ORDER):
        f = FAM[a]
        for model, src, dx in MODELS:
            v = vals(src, a, key); m = v.mean(); xi = i + dx
            face = "white" if model == "GLM" else LIGHT[f]
            if key == "cost.total_usd" and f in DARK:
                o = oversight(model, a).mean()
                ax.bar(xi, m - o, W, color=face, edgecolor=EDGE[f], linewidth=1.2)
                ax.bar(xi, o, W, bottom=m - o, color=DARK[f], edgecolor=DARK[f], hatch="//", alpha=0.85)
                if o > 4: ax.text(xi, m - o / 2, f"{o:.1f}", ha="center", va="center", color="white", fontsize=8.5)
                else: ax.text(xi, m - o - 0.6, f"+{o:.1f}", ha="center", va="top", fontsize=8)
            else:
                ax.bar(xi, m, W, color=face, edgecolor=EDGE[f], linewidth=1.2)
            se = v.std(ddof=1) / np.sqrt(len(v)) if len(v) > 1 else 0
            if se: ax.errorbar(xi, m, yerr=se, color="#333333", capsize=3.5, lw=1.2, zorder=4)
            jit = np.linspace(-0.09, 0.09, len(v)) if len(v) > 1 else [0]
            ax.scatter(xi + np.array(jit), v, s=20, color=DOT[f], edgecolor="white", zorder=3)
            labels.append((xi, max(m + se, v.max()), (f"{m:,.0f}" if dec == 0 else f"{m:.1f}") + (f"\nn={len(v)}" if len(v) != 3 else "")))
            ax.text(xi, -0.012, model, transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=8.5, color="#555555")
    ymax = max(t for _, t, _ in labels) or 1
    for xi, top, s in labels:
        ax.text(xi, top + 0.02 * ymax, s, ha="center", va="bottom", fontsize=8.5)
    ax.set_xticks(x); ax.set_xticklabels(LABEL[a] for a in ORDER); ax.tick_params(axis="x", length=0, pad=16, labelsize=9.5)
    ax.set_title(title, loc="left", fontsize=13); ax.grid(axis="y", alpha=0.3)
    ax.set_ylim(0, ymax * 1.18)
fig.legend(handles=[Patch(facecolor="white", edgecolor="#555555", label="GLM 5.3 Flash (outlined, left of each pair)"),
                    Patch(facecolor="#bbbbbb", edgecolor="#555555", label="DeepSeek V4.1 Flash (filled, right)"),
                    Patch(color=LIGHT["g"], label="No oversight"), Patch(color=LIGHT["b"], label="In-swarm overseers"),
                    Patch(color=LIGHT["o"], label="Blocking monitor, threshold 80"),
                    Patch(color=LIGHT["p"], label="Asynchronous reviewer, every 5 min, threshold 80")],
           loc="upper left", bbox_to_anchor=(0.01, 0.94), ncol=6, frameon=False, fontsize=10)
fig.suptitle("GLM 5.3 Flash vs. DeepSeek V4.1 Flash, shared oversight arms", x=0.01, y=0.995, ha="left", fontsize=16)
fig.text(0.01, 0.948, "Bar = arm mean; error bar = ±1 SEM; dots = runs (n = 3 unless marked). 100 solvers, 120 min, push, seeded. "
         "GLM runs: control Nov 4; 10 ov r1–r3 Nov 4 + r4–r6 Nov 16; M1 Nov 11; M3 (actions-reason) r1–r3 Nov 11 + r4–r6 Nov 16; "
         "A1/A3 Nov 19. DeepSeek runs: Nov 24 (UTC).", fontsize=10)
fig.tight_layout(rect=(0, 0, 1, 0.91))
for ext in ("png", "pdf"):
    fig.savefig(D / f"arm_bars_6metrics_glm_vs_ds-2026-11-23.{ext}", dpi=150)
for a in ORDER:
    for model, src, _ in MODELS:
        print(a, model, len(src[a]["runs"]), [round(float(vals(src, a, k).mean()), 2) for k, *_ in panels], round(float(oversight(model, a).mean()), 3))
