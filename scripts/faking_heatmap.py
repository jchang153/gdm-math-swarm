#!/usr/bin/env python3
"""Per-run faking heatmap ("Faking over time") for the run page (owner, November 3, 2026; docs/plan.md).

Owner's spec: "each row is a faker, the row is a heatmap over time that gets hotter if they faked more in the last
5 minutes (moving average), and mark where they got credits". This reproduces the researcher figure first made as
scratch ``heat3.py`` (October 29-30, 2026): figsize (16, 11), dpi 150, title, legend and colour bar inside the
figure; one row per agent that ever faked, credited fakers on top (sorted by first credited fake), then the others
(sorted by first fake); colour = that agent's fakes per minute, trailing 5-minute moving average, on a fixed scale
0-2 fakes/min shared by every run; markers: credited fake (outcome ``accepted``; ``accepted_after_close`` earns
no credit and counts as an uncredited fake), a fake of theirs reverted, overseer DM read
(delivered), overseer post read on the board (board_read returning an overseer's post). Only markers that occur in
the figure are in its legend.

Fakes follow the strict rule of the story map (``research/analysis/storymap/domain.fake_rule``): Lean runs use
``fake_rule.is_fake`` (an accepted submission is fake when the proof checker says fraudulent, or when it is not
genuine and the answer-restatement / Iff.rfl pattern fires; a non-accepted one when the pattern fires); coding runs
use ``fake_rule_code.is_fake_code`` (same contract: evaluator verdict, else hard-coding / test-tampering patterns).
Seed agents and overseers (``research/analysis/roles.py``) are excluded. Reads ``conference/state.sqlite`` (read
only), ``evaluations/evaluations.jsonl``, ``settings.json`` and the run start (``run_clock.run_start``). No model
calls or network.

    faking_heatmap.py --run-dir runs/RUN [--output-dir DIR]
        writes DIR/faking-heatmap.json (the summary the page section is made from) and, when the run has fakers,
        DIR/faking-heatmap.png (default DIR: the run directory).

``add_section(page, summary)`` puts the "Faking over time" section at the top of results.html's main report
(idempotent: an earlier section is replaced); build_results.py and ``publish_cloudflare.py heatmap`` use it.
"""
import argparse
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(Path(__file__).resolve().parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

IMAGE = 'faking-heatmap.png'
SUMMARY = 'faking-heatmap.json'
VMAX = 2.0          # fakes per minute; shared colour scale (98th percentile of the 16 notices runs, October 30)
WINDOW = 5          # minutes, trailing moving average
SECTION_ID = 'faking-over-time'
SECTION_END = f'<!--/{SECTION_ID}-->'
SECTION = re.compile(r'<section id="' + SECTION_ID + r'".*?' + re.escape(SECTION_END), re.S)
MAIN = re.compile(r'<main id="report"[^>]*>')

INK, MUTED, BG, CELL, NOTE = '#1B2333', '#4A5568', '#FBFAF6', '#F6F4EF', '#6B7280'
RAMP = ['#F6F4EF', '#F3C9A8', '#E8935F', '#C8551F', '#7A2E0E']
CREDIT, DM, POST = '#2F6FB5', '#5B3FA8', '#E69F00'


def _agent_number(agent):
    match = re.fullmatch(r'agent-(\d+)', str(agent or ''))
    return int(match.group(1)) if match else None


def _post_number(value):
    if isinstance(value, int):
        return value
    match = re.search(r'(\d+)$', str(value))
    return int(match.group(1)) if match else None


def _settings(run):
    try:
        return json.loads((run / 'settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _validity(run):
    out = {}
    path = run / 'evaluations' / 'evaluations.jsonl'
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                record = json.loads(line)
                out[record.get('source_sha256')] = record.get('validity')
    return out


def _run_minutes(run, start, db):
    """Run length in whole minutes: results.json elapsed_seconds, else settings max_run_seconds, else the last
    conference event."""
    seconds = None
    try:
        seconds = json.loads((run / 'results.json').read_text(encoding='utf-8')).get('elapsed_seconds')
    except (OSError, ValueError):
        pass
    if not seconds:
        settings = _settings(run)
        for section in ('effective', 'config'):
            seconds = seconds or (settings.get(section) or {}).get('max_run_seconds')
    if not seconds:
        last = db.execute('select max(timestamp) from events').fetchone()[0]
        seconds = (last - start) if last else 60
    return max(1, int(math.ceil(float(seconds) / 60)))


def collect(run_dir):
    """Everything the figure and the page section need (JSON-serialisable)."""
    from research.analysis import roles
    from research.analysis.storymap import domain as storymap_domain
    import run_clock

    run = Path(run_dir)
    kind = storymap_domain.run_domain(run)
    _, is_fake = storymap_domain.fake_rule(kind)
    excluded = set(roles.seed_agents(run)) | set(roles.overseers(run))
    overseer_ids = set(roles.overseers(run))
    validity = _validity(run)
    db = sqlite3.connect(f'file:{run / "conference" / "state.sqlite"}?mode=ro', uri=True)
    try:
        tables = {name for (name,) in db.execute("select name from sqlite_master where type='table'")}
        rows = db.execute('select submission_id, agent_id, source, outcome, submitted_at, source_sha256 '
                          'from submissions order by submitted_at, submission_id').fetchall()
        first_event = db.execute('select min(timestamp) from events').fetchone()[0] if 'events' in tables else None
        start = run_clock.run_start(run)
        if start is None:
            start = min([t for t in (first_event, *(r[4] for r in rows)) if t is not None] or [0])
        minutes = _run_minutes(run, start, db)

        def minute(t):
            return round((t - start) / 60, 3)

        fakes, fake_ids = {}, {}
        for sid, agent, source, outcome, at, sha in rows:
            if _agent_number(agent) is None or agent in excluded:
                continue
            if is_fake(source or '', outcome or '', validity.get(sha)):
                # Credit only for 'accepted'; 'accepted_after_close' (judge accepted after the problem closed)
                # earns no credit (conference.py), so it is a plain uncredited fake. A later revert keeps the ◆.
                accepted = outcome == 'accepted'
                fakes.setdefault(agent, []).append([minute(at), accepted])
                fake_ids[sid] = agent
        reverts = {}
        if 'reversions' in tables:
            for agent, sid, at in db.execute('select credited_agent, submission_id, reverted_at from reversions'):
                if fake_ids.get(sid) == agent and at is not None:
                    reverts.setdefault(agent, []).append(minute(at))
        dms = {}
        if 'messages' in tables and overseer_ids:
            for sender, recipient, at in db.execute('select sender, recipient, delivered_at from messages '
                                                    'where delivered_at is not null'):
                if sender in overseer_ids and recipient in fakes:
                    dms.setdefault(recipient, []).append(minute(at))
        posts = {}
        if {'posts', 'events'} <= tables and overseer_ids:
            # Only the public board ('posts'); the overseers' private board is a separate table, never read here.
            visible = {_post_number(p) for p, agent in db.execute('select post_id, agent_id from posts')
                       if agent in overseer_ids}
            visible.discard(None)
            if visible:
                for agent, at, payload in db.execute("select agent_id, timestamp, payload from events "
                                                     "where kind='board_read' order by timestamp"):
                    if agent not in fakes:
                        continue
                    ids = (json.loads(payload or '{}') or {}).get('post_ids') or []
                    if any(_post_number(x) in visible for x in ids):
                        posts.setdefault(agent, []).append(minute(at))
    finally:
        db.close()

    def order(agent):
        credited = [t for t, accepted in fakes[agent] if accepted]
        return (0, credited[0], agent) if credited else (1, fakes[agent][0][0], agent)

    agents = sorted(fakes, key=order)
    rows_out = [{'agent': a, 'fakes': fakes[a], 'reverts': sorted(reverts.get(a, [])), 'dms': sorted(dms.get(a, [])),
                 'posts': sorted(posts.get(a, [])), 'credited': order(a)[0] == 0} for a in agents]
    return {'run': run.name, 'domain': kind, 'minutes': minutes, 'vmax': VMAX, 'window_minutes': WINDOW,
            'excluded_agents': sorted(excluded), 'fakers': len(agents),
            'credited_fakers': sum(1 for r in rows_out if r['credited']),
            'fake_submissions': sum(len(r['fakes']) for r in rows_out),
            'accepted_fakes': sum(1 for r in rows_out for _, accepted in r['fakes'] if accepted),
            'rows': rows_out}


def matrix(summary):
    """Rows x minutes: fakes per minute, trailing WINDOW-minute moving average (heat3.py's convolution)."""
    width = summary['minutes'] + 1
    out = []
    for row in summary['rows']:
        counts = [0] * width
        for t, _ in row['fakes']:
            if 0 <= t < width:
                counts[int(t)] += 1
        out.append([sum(counts[max(0, i - WINDOW + 1):i + 1]) / WINDOW for i in range(width)])
    return out


def _tick_step(minutes):
    for limit, step in ((15, 2), (35, 5), (75, 10), (120, 15)):
        if minutes <= limit:
            return step
    return 30


def render(summary, out_path):
    """The PNG (heat3.py's layout). Requires matplotlib; ``summary`` must have at least one faker."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    rows = summary['rows']
    if not rows:
        raise ValueError('No fakers: the page shows a note instead of a figure')
    T = summary['minutes'] + 1
    n = len(rows)
    ncred = summary['credited_fakers']
    cmap = LinearSegmentedColormap.from_list('seq', RAMP)
    fig, ax = plt.subplots(figsize=(16, 11), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(CELL)
    im = ax.imshow(matrix(summary), aspect='auto', cmap=cmap, vmin=0, vmax=summary['vmax'],
                   interpolation='nearest', extent=(0, T, n, 0))
    big = n <= 40   # heat3 marker sizes; slightly smaller for very tall figures so rows stay distinct
    s = (lambda x: x) if big else (lambda x: x * 0.7)
    for i, row in enumerate(rows):
        for t, accepted in row['fakes']:
            if accepted:
                ax.scatter(t, i + 0.5, marker='D', s=s(60), c=CREDIT, edgecolors=BG, linewidths=1.5, zorder=3)
        for t in row['reverts']:
            ax.scatter(t, i + 0.5, marker='X', s=s(70), c=INK, edgecolors=BG, linewidths=1, zorder=4)
        for t in row['posts']:
            ax.scatter(t, i + 0.5, marker='^', s=s(55), c=POST, edgecolors=INK, linewidths=0.8, zorder=6)
        for t in row['dms']:
            ax.scatter(t, i + 0.5, marker='o', s=s(150), c=DM, edgecolors=BG, linewidths=1.8, zorder=7)
    ax.set_xlim(0, T)
    ax.set_ylim(n, 0)
    ticks, labels = [], []
    if ncred:
        ticks.append(ncred / 2)
        labels.append(f'Credited\n({ncred})')
    if n - ncred:
        ticks.append(ncred + (n - ncred) / 2)
        labels.append(f'Never credited\n({n - ncred})')
    if ncred and n - ncred:
        ax.axhline(ncred, color=INK, lw=1)
    ax.set_yticks(ticks)
    ax.set_yticklabels(labels, fontsize=12, color=INK)
    ax.tick_params(axis='y', length=0)
    ax.set_xlabel('Run minute', fontsize=12, color=MUTED)
    ax.set_xticks(range(0, T, _tick_step(summary['minutes'])))
    ax.tick_params(colors=MUTED)
    for spine in ax.spines.values():
        spine.set_visible(False)
    rows_text = 'One row: the only faker' if n == 1 else f'Each row is one of {n} fakers'
    ax.set_title(f'{summary["run"]}\n{rows_text}; colour = fakes per minute, '
                 f'{WINDOW}-minute moving average', fontsize=14, color=INK, loc='left', pad=14)
    cb = fig.colorbar(im, ax=ax, fraction=0.02, pad=0.01)
    cb.set_label(f'Fakes per minute ({WINDOW}-minute moving average)', color=MUTED)
    cb.outline.set_visible(False)
    if summary['accepted_fakes']:
        ax.scatter([], [], marker='D', s=60, c=CREDIT, label='Credited fake')
    if any(r['reverts'] for r in rows):
        ax.scatter([], [], marker='X', s=70, c=INK, label='One of its fakes reverted')
    if any(r['dms'] for r in rows):
        ax.scatter([], [], marker='o', s=150, c=DM, edgecolors=BG, linewidths=1.8, label='Read overseer DM')
    if any(r['posts'] for r in rows):
        ax.scatter([], [], marker='^', s=55, c=POST, edgecolors=INK, linewidths=0.8, label='Read overseer post')
    if ax.get_legend_handles_labels()[0]:
        ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.08), ncol=4, frameon=False, fontsize=12)
    rule = 'coding fake rule' if summary['domain'] == 'code' else 'strict fake rule'
    excluded = 'Seed and overseers excluded' if summary['excluded_agents'] else 'No seed or overseers in this run'
    ax.text(0, -0.16, f'Rows sorted by time of first credit (top block), then by first fake. {rule[0].upper()}'
            f'{rule[1:]}. {excluded}. Colour scale fixed at 0-{summary["vmax"]:g} fakes/min for every run.',
            fontsize=10, color=NOTE, transform=ax.transAxes)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches='tight', facecolor=BG, metadata={'Software': None})
    plt.close(fig)
    return out_path


def write(run_dir, output_dir=None):
    """faking-heatmap.json, plus faking-heatmap.png when the run has fakers (a stale PNG is removed)."""
    summary = collect(run_dir)
    output = Path(output_dir or run_dir)
    output.mkdir(parents=True, exist_ok=True)
    image = output / IMAGE
    if summary['rows']:
        render(summary, image)
    elif image.exists():
        image.unlink()
    (output / SUMMARY).write_text(json.dumps(summary, indent=1) + '\n', encoding='utf-8')
    return summary


def _definition(domain):
    if domain == 'code':
        return ('a fake is a submission the evaluator marks fraudulent, or one that hard-codes test cases or '
                'tampers with the tests (coding fake rule)')
    return ('a fake is a submission the proof checker marks fraudulent, or one that restates the open statement '
            'as its answer or proves it by Iff.rfl (strict fake rule)')


def section(summary, image=IMAGE):
    """The "Faking over time" section (HTML), with the figure or, without fakers, a one-line note."""
    excluded = ' Seed and overseer agents are excluded.' if summary.get('excluded_agents') else ''
    head = f'<section id="{SECTION_ID}"><h2>Faking over time</h2>'
    if not summary.get('rows'):
        body = (f'<p class="muted">No fakes in this run: no agent made a submission counted as fake ('
                f'{html.escape(_definition(summary.get("domain")))}).{excluded}</p>')
    else:
        caption = (f'Each row is an agent that faked at least once ({summary["fakers"]} agents; '
                   f'{_definition(summary.get("domain"))}), credited fakers on top. Colour: that agent\'s fakes '
                   f'per minute, trailing {WINDOW}-minute moving average, on a 0-{summary["vmax"]:g} scale shared '
                   'by every run. Markers: diamond = credited fake (accepted while the problem was open; an acceptance '
                   'after it closed earns no credit), cross = one of its fakes reverted, '
                   'circle = overseer DM read, triangle = overseer board post read.' + excluded)
        alt = (f'Heatmap of fakes per minute over the run for {summary["fakers"]} agents who faked, '
               f'{summary["credited_fakers"]} of them credited')
        body = (f'<figure style="margin:0"><a href="{html.escape(image)}"><img src="{html.escape(image)}" '
                f'alt="{html.escape(alt)}" loading="lazy" style="width:100%;height:auto;display:block"></a>'
                f'<figcaption class="muted">{html.escape(caption)}</figcaption></figure>')
    return head + body + '</section>' + SECTION_END


def add_section(page, summary, image=IMAGE):
    """results.html with the section at the top of the main report (replacing an earlier one). A legacy
    hand-written page without ``<main id="report">`` (baseline100) gets it before its first heading; raises
    ValueError for a page with neither."""
    block = section(summary, image)
    if SECTION.search(page):
        return SECTION.sub(lambda _: block, page, count=1)
    match = MAIN.search(page)
    if match is not None:
        return page[:match.end()] + block + page[match.end():]
    first = page.find('<h2')
    if first < 0:
        raise ValueError('results.html has no <main id="report"> and no heading; regenerate it with build_results.py')
    return page[:first] + block + page[first:]


def add_to_run_page(page, run_dir, output_dir=None):
    """For build_results.py: write the figure and summary beside results.html and add the section. Without
    matplotlib or conference records the page is returned unchanged (with a message on stderr)."""
    run = Path(run_dir)
    if not (run / 'conference' / 'state.sqlite').is_file():
        return page
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print('faking heatmap skipped: matplotlib is not installed (pip install -e .[publication])',
              file=sys.stderr)
        return page
    summary = write(run, output_dir)
    return add_section(page, summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, help='default: the run directory')
    args = parser.parse_args(argv)
    summary = write(args.run_dir, args.output_dir)
    print(json.dumps({k: summary[k] for k in ('run', 'domain', 'minutes', 'fakers', 'credited_fakers',
                                              'fake_submissions', 'accepted_fakes')}))


if __name__ == '__main__':
    main()
