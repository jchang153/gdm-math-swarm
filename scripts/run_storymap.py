#!/usr/bin/env python3
"""Agent story map of one run, end to end, then offload the run (researcher-only).

    PYTHONPATH=scripts:src:. .venv/bin/python scripts/run_storymap.py --run-dir runs/<id> --run-limit 75 \\
        [--timeline DIR] [--no-offload] [--sample N [--sample-seed S]] \\
        [pipeline options: --agents, --stage1-model, --retry-failed, ...]

Steps, all on runs/<id> while holding a lease on the run (scripts/offload_runs.py):

1. inputs: when the run is core-only (OFFLOADED.json) and its stage-0 timeline is missing, the files stage 0 reads
   (``resume/``, ``inspect/``, ``runtime.jsonl``) are restored in place from the private logs dataset;
2. stage 0 (``research.analysis.storymap.extract``, offline) into ``--timeline`` (default RUN/analysis/storymap,
   i.e. RUN/analysis/storymap/timeline/) unless that timeline exists;
3. the paid pipeline (``research.analysis.storymap.pipeline``; every option not listed above is passed through,
   e.g. ``--run-limit``, the one per-run budget of docs/plan.md);
4. validation (``validate validate``) and the pages (``view``), offline;
5. offload (owner decision, November 3, 2026: docs/plan.md, "Always offload to the logs dataset"): the run goes back
   to its core (offload_runs.auto_offload; deferred while another step holds a lease on the run). ``--no-offload``
   keeps every file local. A pipeline that stopped (exit 3) or failed is not offloaded.

``--sample N`` (owner decision, November 6, 2026: docs/plan.md, the 20-solver sample of the push
communication-only runs) maps a deterministic sample of N solvers instead of all, stratified on faking
(``research.analysis.storymap.sample``; ``--sample-seed``, default 20261106). The record (agents, seed, method,
fakers in run and sample) is written to ``OUT/sample.json`` before anything else, stage 0 extracts only the sampled
agents and the pipeline gets ``--agents``; ``view`` reads the record and says on the index that only N solvers are
mapped. Not with ``--agents`` or ``--overseer``.

``--overseer`` (docs/plan.md, "Overseer story maps (November 5, 2026)") maps the run's overseers instead: stage 0
with ``extract --overseers`` into ``--timeline`` (default ``--out``, default RUN/analysis/storymap-overseer), the
pipeline with ``--overseer`` (its own ledger label and budget), no validation (validate.py is solver-specific) and
no pages: the overseer pages are rendered with the solver map by
``view --map SOLVER_MAP --timeline SOLVER_TIMELINE --overseer-map OM --overseer-timeline OT``.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "scripts")]

import offload_runs  # noqa: E402

STAGE0_INPUTS = ("resume/", "inspect/", "runtime.jsonl")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, help="stage-0 folder (default RUN/analysis/storymap)")
    parser.add_argument("--out", type=Path, help="story-map folder (default RUN/analysis/storymap; --overseer: "
                                                 "RUN/analysis/storymap-overseer)")
    parser.add_argument("--overseer", action="store_true",
                        help="map the overseers (extract --overseers, pipeline --overseer); no validation or pages")
    parser.add_argument("--sample", type=int, metavar="N",
                        help="map a stratified sample of N solvers (OUT/sample.json; not with --agents/--overseer)")
    parser.add_argument("--sample-seed", type=int, default=None, help="seed of --sample (default 20261106)")
    offload_runs.add_offload_flag(parser)
    return parser.parse_known_args(argv)


def sample_agents(run_dir, out_dir, n, seed, log, record=None):
    """Write OUT/sample.json (a stratified sample of n solvers) and return its agents."""
    from research.analysis.storymap import sample as sample_mod
    rec = (record or sample_mod.record)(run_dir, n, sample_mod.DEFAULT_SEED if seed is None else seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / sample_mod.FILE).write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    log(f"story map: sample of {rec['n']} of {rec['population']} solvers (seed {rec['seed']}; fakers "
        f"{rec['fakers_in_sample']} of {rec['n']} sampled, {rec['fakers_in_run']} of {rec['population']} in the run)")
    return rec["agents"]


def has_timeline(folder):
    return (Path(folder) / "timeline" / "index.json").is_file() or (Path(folder) / "index.json").is_file()


def missing_timelines(folder, agents):
    """The agents without a stage-0 timeline file in ``folder`` (holding timeline/ or the files)."""
    folder = Path(folder)
    base = folder / "timeline" if (folder / "timeline").is_dir() else folder
    return [a for a in agents if not (base / f"{a}.jsonl").is_file()]


def restore_inputs(run_dir, log, restore=None):
    """Restore the files stage 0 reads when the run is core-only; returns how many were restored."""
    unit = offload_runs.unit_of(run_dir)
    manifest = run_dir / "OFFLOADED.json"
    if unit is None or not manifest.is_file():
        return 0
    rows = json.loads(manifest.read_text()).get("files", [])
    missing = [p for p in STAGE0_INPUTS
               if any(r["path"].startswith(p) and not (run_dir / r["path"]).exists() for r in rows)]
    restored = 0
    for prefix in missing:
        log(f"story map: restoring {prefix} of {unit} from the logs dataset")
        restored += (restore or offload_runs.restore)(unit, prefix)
    return restored


def main(argv=None, *, steps=None):
    args, rest = parse_args(argv)
    log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731
    run_dir = args.run_dir.resolve()
    out_dir = (args.out or run_dir / "analysis" / ("storymap-overseer" if args.overseer else "storymap")).resolve()
    timeline = (args.timeline or out_dir).resolve()
    steps = steps or default_steps()
    agents_args = []
    if args.sample is not None:
        if args.overseer or any(a == "--agents" or a.startswith("--agents=") for a in rest):
            log("refused: --sample is not used with --agents or --overseer")
            return 2
        if args.sample <= 0:
            log("refused: --sample needs a positive number of solvers")
            return 2
        agents = steps.get("sample", sample_agents)(run_dir, out_dir, args.sample, args.sample_seed, log)
        agents_args = ["--agents", ",".join(agents)]
    unit = offload_runs.unit_of(run_dir)
    with contextlib.ExitStack() as stack:
        if unit is not None:
            stack.enter_context(offload_runs.lease(run_dir, "story map"))
        if not has_timeline(timeline) or (agents_args and missing_timelines(timeline, agents_args[1].split(","))):
            steps["restore"](run_dir, log)
            code = steps["extract"](["--run-dir", str(run_dir), "--out", str(timeline),
                                     *(["--overseers"] if args.overseer else []), *agents_args])
            if code:
                log(f"story map: stage 0 failed (exit {code})")
                return code
        code = steps["pipeline"](["--run-dir", str(run_dir), "--timeline", str(timeline), "--out", str(out_dir),
                                  *(["--overseer"] if args.overseer else []), *agents_args, *rest])
        if code:
            log(f"story map: pipeline exit {code}; not validated, rendered or offloaded")
            return code
        if args.overseer:
            log(f"story map: overseer map in {out_dir}; not validated (solver-specific) or rendered here: render "
                f"with view --map SOLVER_MAP --timeline SOLVER_TIMELINE --overseer-map {out_dir} "
                f"--overseer-timeline {timeline}")
        for name, argv_ in () if args.overseer else (
                ("validate", ["validate", "--map", str(out_dir), "--timeline", str(timeline)]),
                ("view", ["--map", str(out_dir), "--timeline", str(timeline)])):
            code = steps[name](argv_)
            if code:
                log(f"story map: {name} exit {code}; not offloaded")
                return code
    if unit is not None:
        offload_runs.auto_offload(run_dir, label="story map", skip=args.no_offload, log=log)
    return 0


def default_steps():
    from research.analysis.storymap import extract, pipeline, validate, view
    return {"restore": restore_inputs, "extract": lambda a: extract.main(a) or 0,
            "pipeline": pipeline.main, "validate": validate.main, "view": lambda a: view.main(a) or 0}


if __name__ == "__main__":
    sys.exit(main())
