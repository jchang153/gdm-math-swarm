#!/usr/bin/env python3
"""Prepare an offline, private static-Space archive of explicitly selected runs.

Does not authenticate, upload, read host credentials, or change original records.
Pass a locally generated Inspect View bundle for each selected run.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

from build_experiment_index import build

ROOT = Path(__file__).resolve().parents[1]


def copy_records(source, destination):
    """Exclude hidden files and refuse symlinks without following them."""
    destination.mkdir(parents=True)
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f'Refusing symlink: {relative}')
        if path.name.lower() in {'credentials', 'credentials.json', 'token', 'token.json'} or path.suffix.lower() in {'.pem', '.key'}:
            raise ValueError(f'Refusing possible credential file: {relative}')
        target = destination / relative
        if path.is_dir():
            target.mkdir(exist_ok=True)
        elif path.is_file():
            shutil.copy2(path, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs-dir', type=Path, default=ROOT / 'runs')
    parser.add_argument('--run', action='append', required=True,
                        help='RUN_ID=INSPECT_BUNDLE_DIRECTORY; repeat for multiple runs')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    selected = []
    for specification in args.run:
        run_id, bundle = specification.split('=', 1)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', run_id):
            raise ValueError('Invalid run ID')
        source = args.runs_dir / run_id
        viewer = Path(bundle)
        if source.is_symlink() or viewer.is_symlink():
            raise ValueError('Source directories must not be symlinks')
        if not (source / 'results.json').is_file() or not (source / 'results.html').is_file():
            raise ValueError(f'Run has no completed report: {run_id}')
        if not (viewer / 'index.html').is_file() or not (viewer / 'logs/listing.json').is_file():
            raise ValueError(f'Incomplete Inspect bundle for {run_id}')
        selected.append((run_id, source, viewer))
    if len({row[0] for row in selected}) != len(selected):
        raise ValueError('Duplicate run IDs')
    output = args.output_dir.resolve()
    # Refuse overwrite or output within source records: raw evidence stays immutable.
    for _, source, viewer in selected:
        if output.is_relative_to(source.resolve()) or output.is_relative_to(viewer.resolve()):
            raise ValueError('Output must be outside the sources')
    output.mkdir(parents=True, exist_ok=False)
    for run_id, source, viewer in selected:
        copy_records(source, output / run_id)
        copy_records(viewer, output / run_id / 'inspect-view')
    result = build(output, hosted=True)
    if result['warnings'] or result['experiments'] != len(selected):
        raise ValueError(f'Incomplete catalogue: {result}')
    (output / 'README.md').write_text('''---
title: GDM Math Swarm Experiments
emoji: 🐝
colorFrom: blue
colorTo: indigo
sdk: static
app_file: index.html
pinned: false
---

Private researcher archive of completed GDM Math Swarm experiments.
Open the app for experiment reports, HTML transcripts, native Inspect View,
and original run records. Operational acceptance and independent proof validity
are distinct; see each report for limitations. No model or experiment runs here.

This archive must be hosted in a **private** Space. Do not upload credentials.
''')
    (output / 'robots.txt').write_text('User-agent: *\nDisallow: /\n')
    files = {str(p.relative_to(output)): {'bytes': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(output.rglob('*')) if p.is_file()}
    manifest = {'schema': 'gdm-private-space/1', 'runs': [r[0] for r in selected],
                'files': files, 'total_bytes': sum(f['bytes'] for f in files.values())}
    (output / 'archive-manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({'output': str(output), 'runs': manifest['runs'],
                      'files': len(files), 'bytes': manifest['total_bytes']}))


if __name__ == '__main__':
    main()
