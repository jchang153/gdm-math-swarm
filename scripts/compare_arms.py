#!/usr/bin/env python3
"""Compare outcome metrics across arms: mean ± 95% confidence interval over each arm's runs (seeds).

The owner asked for confidence intervals on all reported numbers (docs/plan.md, "Top-down monitor arms (owner
decisions, November 10, 2026)"). Each run's metrics come from research/analysis/run_metrics.py
``outcome_metrics`` (computed fresh, so a re-judge done after results.json was built is included). The interval is
the two-sided Student t interval of the mean over the arm's runs (mean ± t(0.975, n-1) · sd / sqrt(n)); with one
run it is not defined and only the value is shown. ``n`` is the number of runs that have the metric (missing
values, e.g. monitor statistics of a run without a monitor or step timing of an offloaded run, are left out, not
counted as zero).

  python scripts/compare_arms.py --arm control runs/C1 runs/C2 runs/C3 --arm M1 runs/A runs/B runs/C \\
      [--arm 1ov ...] [--metric slowdown.step_seconds.median ...] [--json OUT.json] [--ledger LEDGER.sqlite]

Offline: no model calls, network, Docker or credentials.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from research.analysis import run_metrics  # noqa: E402

# Two-sided 95% Student t quantiles t(0.975, df) for df = 1..30; larger df use the normal 1.96.
T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
        2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def t975(df):
    return T975[df - 1] if 1 <= df <= len(T975) else 1.96


def mean_ci(values):
    """{n, mean, half_width, low, high, values} of the non-missing values (half_width None for n < 2)."""
    values = [float(v) for v in values if v is not None]
    n = len(values)
    if not n:
        return {'n': 0, 'mean': None, 'half_width': None, 'low': None, 'high': None, 'values': []}
    mean = statistics.fmean(values)
    half = t975(n - 1) * statistics.stdev(values) / math.sqrt(n) if n > 1 else None
    return {'n': n, 'mean': mean, 'half_width': half, 'low': None if half is None else mean - half,
            'high': None if half is None else mean + half, 'values': values}


def compare(arms, metrics=None, ledger=None, compute=run_metrics.outcome_metrics):
    """{'arms': {label: {'runs': [...], 'metrics': {key: mean_ci}}}, 'metrics': [(key, label)]}.

    ``arms``: [(label, [run dirs])] in display order."""
    chosen = [(k, l) for k, l in run_metrics.HEADLINE if metrics is None or k in metrics]
    chosen += [(k, k) for k in metrics or () if k not in {c[0] for c in chosen}]
    out = {'metrics': chosen, 'arms': {}}
    for label, runs in arms:
        per_run = [compute(run, ledger=ledger) for run in runs]
        out['arms'][label] = {'runs': [Path(r).name for r in runs],
                              'metrics': {k: mean_ci([run_metrics.lookup(m, k) for m in per_run]) for k, _ in chosen},
                              'gaps': {Path(r).name: m.get('gaps') or [] for r, m in zip(runs, per_run)}}
    return out


def _fmt(x):
    if x is None:
        return '-'
    if abs(x) >= 1000:
        return f'{x:,.0f}'
    if abs(x) >= 10:
        return f'{x:.1f}'
    return f'{x:.3g}'


def cell(summary):
    if not summary['n']:
        return 'n/a'
    if summary['half_width'] is None:
        return f'{_fmt(summary["mean"])} (n=1)'
    return f'{_fmt(summary["mean"])} ± {_fmt(summary["half_width"])} (n={summary["n"]})'


def render_table(result):
    labels = list(result['arms'])
    rows = [['Metric'] + labels]
    for key, label in result['metrics']:
        cells = [cell(result['arms'][a]['metrics'][key]) for a in labels]
        if all(c == 'n/a' for c in cells):
            continue
        rows.append([label] + cells)
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ['  '.join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows]
    lines.insert(1, '  '.join('-' * w for w in widths))
    lines.append('')
    lines.append('mean ± half-width of the 95% t interval over runs (n = runs with the metric).')
    return '\n'.join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='\n'.join(__doc__.split('\n')[1:]))
    parser.add_argument('--arm', nargs='+', action='append', required=True, metavar=('LABEL', 'RUN_DIR'),
                        help='an arm label followed by its run directories; repeat for each arm')
    parser.add_argument('--metric', action='append', help='only these dotted metric keys (default: the headline set)')
    parser.add_argument('--ledger', type=Path, help='ledger copy for the monitor/agent cost split')
    parser.add_argument('--json', type=Path, help='also write the full comparison (per-run values, gaps) here')
    args = parser.parse_args(argv)
    for arm in args.arm:
        if len(arm) < 2:
            parser.error(f'--arm {arm[0]} needs at least one run directory')
        for run in arm[1:]:
            if not Path(run).is_dir():
                parser.error(f'not a run directory: {run}')
    return args


def main(argv=None):
    args = parse_args(argv)
    result = compare([(arm[0], [Path(r) for r in arm[1:]]) for arm in args.arm], args.metric, args.ledger)
    print(render_table(result))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
