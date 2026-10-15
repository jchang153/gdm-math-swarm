#!/usr/bin/env python3
"""Export a saved run's Inspect messages as private, escaped HTML; no model calls.

The original .eval files remain authoritative. This is a derived transcript view,
not a judge or behavioral evaluator. Run only after experiment logging finishes.
"""
import argparse
import html
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def export(run_dir):
    RUN = Path(run_dir).resolve()
    if not RUN.is_relative_to(ROOT / 'runs') or RUN == ROOT / 'runs':
        raise ValueError('Use a saved researcher run under this repository runs directory')
    sys.path.insert(0, str(ROOT / 'src'))
    from research_swarm.inspect_adapter import disable_inspect_dotenv
    disable_inspect_dotenv()
    from inspect_ai.log import read_eval_log_samples
    if not list((RUN / 'inspect').glob('*.eval')):
        raise ValueError('No saved Inspect logs found')
    OUT = RUN / 'transcripts'
    OUT.mkdir(exist_ok=True)
    esc = html.escape
    style = '<style>body{max-width:1100px;margin:36px auto;padding:0 20px;font:16px system-ui;line-height:1.5}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f4f4;padding:16px}article{border-top:1px solid #ccc;margin:30px 0}details{margin:12px 0}a{color:#1263a3}</style>'
    links = []
    seen = set()
    manifest = []
    for path in sorted((RUN / 'inspect').glob('*.eval')):
        for sample in read_eval_log_samples(str(path), all_samples_required=False, resolve_attachments=True):
            agent = str(sample.id)
            if not re.fullmatch(r'agent-[0-9]{3,}', agent):
                raise ValueError('Unexpected sample identity')
            if agent in seen:
                raise ValueError('Multiple log samples for ' + agent + '; inspect raw logs before exporting')
            seen.add(agent)
            body = []
            for index, message in enumerate(sample.messages or []):
                data = message.model_dump(mode='json', exclude_none=True)
                title = f'{index + 1}. {data.get("role", "message")}'
                body.append(f'<article id="message-{index + 1}"><h2>{esc(title)}</h2>')
                if data.get('id'):
                    body.append(f'<small>Message ID: {esc(str(data["id"]))}</small>')
                content = data.get('content', '')
                blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content}]
                for block in blocks:
                    kind = block.get('type', 'unknown')
                    if kind == 'text':
                        body.append('<pre>' + esc(str(block.get('text', ''))) + '</pre>')
                    elif kind == 'reasoning':
                        body.append('<details><summary>Model reasoning</summary><pre>' + esc(str(block.get('reasoning', ''))) + '</pre></details>')
                    else:
                        body.append('<pre>' + esc(json.dumps(block, ensure_ascii=False, indent=2)) + '</pre>')
                if data.get('tool_calls'):
                    body.append('<h3>Tool calls</h3><pre>' + esc(json.dumps(data['tool_calls'], ensure_ascii=False, indent=2)) + '</pre>')
                body.append('</article>')
            name = agent + '.html'
            (OUT / name).write_text('<!doctype html><meta charset="utf-8"><title>' + esc(agent) + '</title>' + style + '<p><a href="index.html">All agents</a></p><h1>' + esc(agent) + '</h1><p>Messages in transcript order. Message indices do not represent elapsed time. Raw Inspect logs remain authoritative.</p>' + ''.join(body))
            links.append(f'<li><a href="{name}">{esc(agent)}</a> — {len(sample.messages or [])} messages</li>')
            manifest.append({'agent_id': agent, 'messages': len(sample.messages or []), 'source': str(path.relative_to(RUN)), 'output': name, 'inspect_error': bool(sample.error)})
    (OUT / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Agent transcripts</title>' + style + '<h1>GDM Math Swarm agent transcripts</h1><p>Run ' + esc(RUN.name) + '. Private researcher exports; do not publish or provide to experimental agents.</p><ul>' + ''.join(links) + '</ul>')
    (OUT / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return {'agents': len(manifest), 'messages': sum(x['messages'] for x in manifest), 'index': str(OUT / 'index.html')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--run-dir', type=Path, required=True)
    print(json.dumps(export(parser.parse_args().run_dir)))
