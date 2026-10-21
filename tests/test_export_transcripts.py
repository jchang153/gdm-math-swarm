"""Offline tests for the transcript viewer in scripts/export_transcripts.py (synthetic histories)."""
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import export_transcripts as et  # noqa: E402

LONG = 'x' * (et.COLLAPSE_CHARS + 10)


def item(item_id, kind, text, turn=None, **meta):
    return {'id': item_id, 'agent_id': 'agent-001', 'kind': kind, 'channel': meta.pop('channel', 'tool_action'),
            'turn': turn, 'time': '2026-10-20T06:00:00Z', 'time_bounds': None, 'text': text, 'meta': meta}


def history():
    items = [
        item('A01.t0000.r', 'reasoning', 'thinking about <b>tags</b>', 0, segment=0, message_id='m0',
             stop_reason='tool_calls'),
        item('A01.t0000.c1', 'tool_call', 'submit_solution problem_id: TaskA', 0, segment=0, message_id='m0',
             function='mcp__conference__submit_solution', conference_id='sub-000001',
             conference={'status': 'joined', 'seq': 3}, channel='submission'),
        item('A01.t0000.o1', 'tool_result', LONG, 0, segment=0, tool_message_id='tm0',
             function='mcp__conference__submit_solution', conference_id='sub-000001'),
        item('sub-000001', 'conference', 'theorem source', None, problem_id='TaskA', outcome='accepted',
             source_sha256='a' * 64, call_item='A01.t0000.c1', channel='submission'),
        item('A01.t0001.c1', 'tool_call', 'Write ' + LONG, 1, segment=0, message_id='m1', function='Write',
             stop_reason='tool_calls'),
        item('sub-000002', 'conference', 'other source', None, problem_id='TaskB', outcome='rejected',
             stage='compile', reason='error', source_sha256='b' * 64, channel='submission'),
    ]
    return SimpleNamespace(agent_id='agent-001', code='A01', persona=None, exit={'status': 'budget_exhausted'},
                           items=items, harness_items=[], source='inspect/x.eval',
                           segments=[{'segment': 0, 'first_turn': 0, 'last_turn': 1}],
                           stats={'turns': 2, 'summary_turns': 0, 'segments': 1, 'items': len(items),
                                  'tokens_total': 10, 'items_by_kind': {}, 'conference_joins': {},
                                  'exposure_entries': 0, 'missing_tool_results': [], 'harness_items': 0})


FINDING = {'id': 'F1', 'category': 'cheating', 'level': 'explored', 'heading': 'Tried a <banned> trick',
           'summary': 'Used decide +native.', 'items': ['A01.t0001.c1', 'A99.t0000.r'],
           'quotes': [('A01.t0001.c1', 'decide +native')]}


class TranscriptViewerTest(unittest.TestCase):
    def render(self, **kwargs):
        page, messages = et.render_agent(history(), 'run-1', agents=[('agent-001', 'agent-001.html'),
                                                                     ('agent-002', 'agent-002.html')], **kwargs)
        return page, messages

    def test_page_shell_switcher_find_and_theme(self):
        page, messages = self.render()
        self.assertEqual(messages, 3)  # turn 0 output, its result, turn 1 output
        self.assertIn('name="viewport"', page)
        self.assertIn('prefers-color-scheme:dark', page)
        self.assertIn('<select id="agent-switch"><option value="agent-001.html" selected>agent-001</option>'
                      '<option value="agent-002.html">agent-002</option></select>', page)
        self.assertIn('href="agent-002.html" title="Next agent"', page)
        self.assertIn('id="find"', page)
        self.assertIn('index.html#search=', page)
        self.assertIn('thinking about &lt;b&gt;tags&lt;/b&gt;', page)
        self.assertNotIn('<b>tags</b>', page)
        self.assertIn('Behavior judge: not run.', page)

    def test_message_anchors_still_bind_for_the_conference_view(self):
        page, _ = self.render(findings=[dict(FINDING)])
        # scripts/export_conference.py binds Inspect message IDs with exactly this pattern.
        found = dict((m[2], m[1]) for m in re.finditer(
            r'<article id="(message-\d+)">(?:(?!</article>).)*?<small>Message ID: (.*?)</small>', page, re.S))
        self.assertEqual(found, {'m0': 'message-1', 'tm0': 'message-2', 'm1': 'message-3'})
        for anchor in ('A01.t0000.c1', 'A01.t0000.o1', 'sub-000001', 'A01.t0001.c1'):
            self.assertIn(f'id="{anchor}"', page)

    def test_long_outputs_collapse_with_preview(self):
        page, _ = self.render()
        self.assertIn(f'<details><summary>Result ({len(LONG):,} characters, 1 lines) <span class="pv">', page)
        self.assertIn('<details><summary>Tool call input (', page)
        self.assertIn('<pre>submit_solution problem_id: TaskA</pre>', page)  # short calls stay open
        self.assertIn(LONG, page)  # full text always present

    def test_submissions_highlighted_with_outcome_and_matching_validity(self):
        evaluations = {'sub-000001': {'validity': 'genuine', 'source_sha256': 'a' * 64},
                       'sub-000002': {'validity': 'genuine', 'source_sha256': 'c' * 64}}  # hash mismatch: hidden
        page, _ = self.render(evaluations=evaluations)
        self.assertIn('<section id="A01.t0000.c1" class="hl-sub">', page)
        self.assertIn('<section id="sub-000002" class="hl-sub b-bad-row">', page)
        self.assertIn('conference <span class="badge b-good">accepted</span> · proof checker '
                      '<span class="badge b-good">genuine</span>', page)
        self.assertIn('compile / error', page)
        highlights = page.split('id="highlights"', 1)[1].split('<main>', 1)[0]
        self.assertIn('Submissions (2)', highlights)
        self.assertEqual(highlights.count('proof checker <span'), 1)

    def test_verified_findings_inline_and_labelled(self):
        info = {'status': 'shown', 'generated_at': '2026-10-21T00:00:00Z',
                'evidence': {'agent-001': '../analysis/judge/review/agents/agent-001.html'}}
        page, _ = self.render(findings=[dict(FINDING)], judge_info=info)
        self.assertIn('<section id="A01.t0001.c1" class="hl-judge">', page)
        main = page.split('<main>', 1)[1]
        self.assertIn('<aside class="finding" data-finding="F1">', main)
        self.assertIn('Tried a &lt;banned&gt; trick', main)
        self.assertIn('model-generated', main)
        highlights = page.split('id="highlights"', 1)[1].split('<main>', 1)[0]
        self.assertIn('Behavior judge findings (1 verified)', highlights)
        self.assertIn('../analysis/judge/review/agents/agent-001.html', highlights)
        self.assertIn('Cited: <a class="id" href="#A01.t0001.c1">', highlights)
        self.assertNotIn('href="#A99.t0000.r"', highlights)  # cited item not in this transcript

    def test_judge_findings_reads_only_confirmed_paid_outputs_of_this_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run-1'
            self.assertEqual(et.judge_findings(run, ['agent-001'])[1]['status'], 'not_run')
            judge = run / 'analysis' / 'judge'
            (judge / 'agents').mkdir(parents=True)
            (judge / 'report.json').write_text(json.dumps({'run_id': 'run-1', 'mode': 'rehearsal'}))
            self.assertEqual(et.judge_findings(run, ['agent-001']), ({}, {
                'status': 'skipped', 'note': "Behavior judge outputs are mode 'rehearsal', not a paid judge run; "
                                             'not shown.'}))
            (judge / 'report.json').write_text(json.dumps({'run_id': 'other', 'mode': 'paid'}))
            self.assertEqual(et.judge_findings(run, ['agent-001'])[1]['status'], 'skipped')
            (judge / 'report.json').write_text(json.dumps({'run_id': 'run-1', 'mode': 'paid',
                                                           'generated_at': 'T'}))
            findings = [{'id': 'F1', 'state': 'confirmed', 'category': 'cheating', 'level': 'submitted',
                         'established_level': 'explored', 'summary': 's',
                         'evidence': [{'item': 'A01.t0001.c1', 'quote': 'q'}, {'item': 'A01.t0001.c1'}]},
                        {'id': 'F2', 'state': 'refuted', 'evidence': [{'item': 'A01.t0002.r'}]},
                        {'id': 'F3', 'state': 'not_checked', 'evidence': [{'item': 'A01.t0003.r'}]}]
            (judge / 'agents' / 'agent-001.json').write_text(json.dumps(
                {'agent_id': 'agent-001', 'mode': 'paid', 'verified': {'findings': findings}}))
            (judge / 'review' / 'agents').mkdir(parents=True)
            (judge / 'review' / 'agents' / 'agent-001.html').write_text('<p>e</p>')
            found, info = et.judge_findings(run, ['agent-001', 'agent-002'])
            self.assertEqual(found, {'agent-001': [{'id': 'F1', 'category': 'cheating', 'level': 'explored',
                                                    'heading': None, 'summary': 's', 'items': ['A01.t0001.c1'],
                                                    'quotes': [('A01.t0001.c1', 'q')]}]})
            self.assertEqual(info, {'status': 'shown', 'generated_at': 'T',
                                    'evidence': {'agent-001': '../analysis/judge/review/agents/agent-001.html'}})

    def test_index_lists_agents_and_embeds_search_data_safely(self):
        rows = [{'agent_id': 'agent-001', 'output': 'agent-001.html', 'exit': 'error', 'turns': 1200, 'segments': 2,
                 'tokens_estimate': 5, 'submission_outcomes': {'accepted': 1, 'rejected': 2},
                 'verified_findings': 3},
                {'agent_id': 'agent-</script>', 'output': 'x.html', 'exit': None, 'turns': 0, 'segments': 0,
                 'tokens_estimate': 0, 'submission_outcomes': {}, 'verified_findings': None}]
        page = et.render_index('run-1', rows, {'status': 'not_run', 'note': 'Behavior judge: not run.'})
        self.assertIn('id="search-form"', page)
        self.assertIn('<span class="badge b-good">accepted 1</span>', page)
        self.assertIn('<span class="badge b-bad">rejected 2</span>', page)
        self.assertIn('1,200', page)
        self.assertEqual(page.count('</script>'), 1)  # only the real closing tag
        data = page.split('const AGENTS=', 1)[1].split(';\n', 1)[0]
        self.assertEqual(json.loads(data)[0], {'id': 'agent-001', 'file': 'agent-001.html'})
        self.assertIn('href="../../index.html"', page)


if __name__ == '__main__':
    unittest.main()
