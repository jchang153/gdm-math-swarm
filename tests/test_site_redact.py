"""Site privacy redaction and gate (owner decision, November 17, 2026): scripts/site_redact.py and its use in
scripts/publish_cloudflare.py. Fake identifiers only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import publish_cloudflare as cf  # noqa: E402
import site_redact as sr  # noqa: E402

PROJECT = 'fakeproj-424242'
VM = 'swarm-vm-test'
TERMS = (PROJECT, VM)
USER = 'ext_alice_example_edu'
RUN = 'r1-20261027T000000Z'


def settings():
    return {
        'effective': {
            'ledger_path': f'/home/{USER}/gdm-math-swarm/state/budget.sqlite',
            'claude_code_env': {'CLAUDE_CONFIG_DIR': f'/home/{USER}/.agent'},
            'provider_limit': {
                'endpoint': 'https://openrouter.ai/api/v1/key', 'paid_calls': 0, 'limit': None,
                'limit_remaining': None, 'limit_reset': None, 'usage': 5581.07, 'usage_daily': 33.1,
                'usage_weekly': 736.3, 'usage_monthly': 736.3, 'is_free_tier': False,
                'include_byok_in_limit': False, 'expires_at': None, 'run_budget_usd': 100,
                'checks': {'limit_set': {'passed': False},
                           'remaining_within_run_budget': {'passed': False, 'limit_remaining': None,
                                                           'run_budget_usd': 100}},
                'warnings': ['the key has no provider-side limit', 'key already has usage 5581.07'],
                'all_required_passed': True,
                'record': f'/home/{USER}/gdm-math-swarm/data/validation/provider-limit-20261104T044310Z.json'},
            'tokens': {'usage': 12, 'limit': 3}},
        'run_budget': {'owner': f'{VM}.us-central1-a.c.{PROJECT}.internal:55549',
                       'note': f'pilot run /home/{USER}/gdm-math-swarm/runs/{RUN}'},
        'shutdown': {'vm': VM, 'project': PROJECT, 'zone': 'us-central1-a'},
        'score': 16.25}


class RedactTests(unittest.TestCase):
    def test_settings_fields(self):
        data = json.dumps(settings(), indent=2).encode()
        out = sr.redact(f'{RUN}/settings.json', data, TERMS)
        value = json.loads(out)
        limit = value['effective']['provider_limit']
        for key in sr.PROVIDER_KEYS:
            self.assertNotIn(key, limit)
        self.assertEqual(limit['checks']['remaining_within_run_budget'], {'passed': False, 'run_budget_usd': 100})
        self.assertTrue(limit['all_required_passed'])
        self.assertEqual(limit['warnings'][1], 'key already has usage (redacted)')
        self.assertEqual(limit['record'], 'data/validation/provider-limit-20261104T044310Z.json')
        self.assertEqual(value['effective']['ledger_path'], 'state/budget.sqlite')
        self.assertEqual(value['effective']['claude_code_env']['CLAUDE_CONFIG_DIR'], '~/.agent')
        self.assertEqual(value['effective']['tokens'], {'usage': 12, 'limit': 3})  # not a provider_limit record
        self.assertEqual(value['run_budget']['owner'], 'vm-host:55549')
        self.assertEqual(value['run_budget']['note'], f'pilot run runs/{RUN}')
        self.assertEqual(value['shutdown'], {'vm': sr.REDACTED, 'project': sr.REDACTED, 'zone': 'us-central1-a'})
        self.assertEqual(value['score'], 16.25)
        self.assertEqual(sr.violations(out, TERMS), [])
        self.assertTrue(out.startswith(b'{\n  "'))  # original layout kept
        self.assertEqual(sr.redact(f'{RUN}/settings.json', out, TERMS), out)  # idempotent

    def test_judge_statement_defects(self):
        agent = {'agent': 'agent-001', 'labels': ['faked'],
                 'statement_defects': {'t1': {'known_defect': True}},
                 'extra': {'researcher_only': True, 'note': 'x'}}
        out = sr.redact(f'{RUN}/analysis/judge/agents/agent-001.json', json.dumps(agent).encode(), TERMS)
        self.assertEqual(json.loads(out), {'agent': 'agent-001', 'labels': ['faked']})
        self.assertEqual(sr.violations(out, TERMS), [])

    def test_published_seeded_record(self):
        results = {'score': 3, 'seeded': {'researcher_only': True, 'note': 'agent-101 is the seeded source'},
                   'other': {'researcher_only': True}}
        data = json.dumps(results, indent=1).encode()
        out = sr.redact(f'{RUN}/results.json', data, TERMS)
        self.assertEqual(json.loads(out), {'score': 3, 'seeded': results['seeded']})
        self.assertEqual(sr.violations(out, TERMS, f'{RUN}/results.json'), [])
        self.assertIn('researcher_only', sr.violations(data, TERMS, f'{RUN}/results.json'))
        self.assertIn('researcher_only', sr.violations(out, TERMS, f'{RUN}/summary.json'))  # only results.json

    def test_inspect_bundle_example_path(self):
        js = b'* `/Users/alice/scans` -> `L1VzZXJzL2FsaWNlL3NjYW5z`.'
        self.assertIs(sr.redact(f'{RUN}/inspect-view/assets/index.js', js, TERMS), js)
        self.assertEqual(sr.violations(js, TERMS), [])

    def test_untouched_files(self):
        data = b'{"a": 1, "usage": {"input_tokens": 3}}'
        self.assertIs(sr.redact(f'{RUN}/results.json', data, TERMS), data)
        png = b'\x89PNG /home/ext_x'
        self.assertIs(sr.redact(f'{RUN}/x.png', png, TERMS), png)

    def test_html_paths(self):
        page = f'<script>{{"abs_log_dir": "/home/{USER}/gdm-math-swarm/runs/{RUN}/inspect"}}</script>'.encode()
        out = sr.redact(f'{RUN}/inspect-view/index.html', page, TERMS)
        self.assertIn(f'"abs_log_dir": "runs/{RUN}/inspect"'.encode(), out)
        mac = b'see /Users/someone/code/GitHub/gdm-math-swarm/runs/x and /Users/someone/other'
        self.assertEqual(sr.redact('a.md', mac, TERMS), b'see runs/x and ~/other')

    def test_gate_rules(self):
        cases = {
            'denylisted_identifier': f'project {PROJECT} here',
            'internal_host': 'owner: host.zone.c.something.internal:1',
            'home_ext_path': '/home/ext_bob_uni_edu/x',
            'users_path': 'at /Users/bob/x',
            'researcher_only': '{"researcher_only": true}',
            'statement_defects': '{"statement_defects": {}}',
            'provider_usage': '{"usage_daily": 3}',
        }
        for rule, text in cases.items():
            self.assertIn(rule, sr.violations(text.encode(), TERMS), rule)
        self.assertIn('researcher_only', sr.violations(b'&quot;researcher_only&quot;: true', TERMS))
        self.assertEqual(sr.violations(b'{"usage": {"input_tokens": 1}, "limit": 3} /home/agent', TERMS), [])
        self.assertEqual(sr.violations(b'fakeproj-4242420 is another word', TERMS), [])

    def test_denylist_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'HANDOFF.local.md'
            local.write_text(f'```sh\nexport GDM_GCP_PROJECT={PROJECT}\nexport GDM_SITE_DENYLIST={VM},other-vm-1\n```\n')
            self.assertEqual(set(sr.denylist(env={}, local=local)), {PROJECT, VM, 'other-vm-1'})
            self.assertEqual(sr.denylist(env={'GDM_SITE_DENYLIST': 'abcd'}, local=Path(tmp) / 'none'), ['abcd'])


class SiteGateTests(unittest.TestCase):
    def test_gate_refuses_private_content(self):
        for text in (f'{VM}.z.c.{PROJECT}.internal:1', f'/home/{USER}/x', '"researcher_only": true',
                     '"limit_remaining": null', '/Users/bob/x'):
            with self.assertRaisesRegex(ValueError, 'Private content'):
                cf.site_gate(f'{RUN}/settings.json', json.dumps({'x': text}).encode().replace(b'\\"', b'"'),
                             terms=TERMS)
        with self.assertRaisesRegex(ValueError, 'denylisted_identifier'):
            cf.site_gate(f'{RUN}/results.json', json.dumps({'project': PROJECT}).encode(), terms=TERMS)
        self.assertEqual(cf.site_gate(f'{RUN}/results.json', b'{"a": 1}', terms=TERMS), b'{"a": 1}')

    def test_gate_reads_packs(self):
        files = {f'{RUN}/analysis/judge/agents/agent-001.json': b'{"statement_defects": {}}'}
        packed, _ = cf.pack_site(dict(files))
        pack = next(n for n in packed if cf.is_pack_file(n))
        with self.assertRaisesRegex(ValueError, 'statement_defects'):
            cf.site_gate(pack, packed[pack], terms=TERMS)

    def test_redact_site(self):
        files = {f'{RUN}/settings.json': json.dumps(settings()).encode(), f'{RUN}/results.json': b'{"a": 1}'}
        out, changed = cf.redact_site(files, terms=TERMS)
        self.assertEqual(changed, [f'{RUN}/settings.json'])
        self.assertIs(out[f'{RUN}/results.json'], files[f'{RUN}/results.json'])
        for name, data in out.items():
            cf.site_gate(name, data, terms=TERMS)


if __name__ == '__main__':
    unittest.main()
