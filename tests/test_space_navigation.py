"""Check same-origin session continuity with fabricated browser sessions."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from space_navigation import private_space_navigation, SCRIPT


class NavigationTests(unittest.TestCase):
    def test_derived_export_is_idempotent_and_preserves_content(self):
        page = '<!doctype html><h1>Saved proof</h1><a href="agent.html#message-2">Transcript</a>'
        result = private_space_navigation(page)
        self.assertTrue(result.endswith(page.split('>', 1)[1]))
        self.assertEqual(private_space_navigation(result), result)
        self.assertIn('content="no-referrer"', result)
        self.assertNotIn('localStorage', result)

    def test_inspect_modules_are_mapped_before_loading(self):
        page = '<html><head><script type="module" crossorigin src="./assets/index.js"></script></head></html>'
        result = private_space_navigation(page, ['./assets/index.js'])
        self.assertIn('import "./assets/index.js";', result)
        self.assertLess(result.index('private-space-navigation'), result.index('import "./assets/index.js"'))
        with self.assertRaises(ValueError):
            private_space_navigation(page, ['https://external.example/code.js'])

    def test_browser_session_stays_on_exact_origin_and_preserves_ranges(self):
        js = SCRIPT.replace('MODULE_PATHS', '[]').split('>', 1)[1].rsplit('</script>', 1)[0]
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const listeners={},calls=[];
const context={URL,URLSearchParams,Request,MutationObserver:class{observe(){}},
 location:{hostname:'example.static.hf.space',origin:'https://example.static.hf.space',search:'?__sign=FAKE_SESSION'},
 document:{baseURI:'https://example.static.hf.space/run/transcripts/index.html',documentElement:{},
 querySelectorAll:()=>[],addEventListener:(name,cb)=>{listeners[name]=cb;}},
 XMLHttpRequest:class{open(...args){calls.push(args)}},window:{fetch:(...args)=>{calls.push(args);return Promise.resolve({})}}};
vm.runInNewContext(SOURCE,context);
function click(href){const a={getAttribute:()=>href,setAttribute:(name,value)=>{href=value}};listeners.click({target:{closest:()=>a}});return href;}
assert.equal(click('agent-001.html#message-2'),'https://example.static.hf.space/run/transcripts/agent-001.html?__sign=FAKE_SESSION#message-2');
assert.equal(click('https://outside.example/file'),'https://outside.example/file');
assert.equal(click('https://other.static.hf.space/file'),'https://other.static.hf.space/file');
assert.equal(click('#board'),'#board');
context.window.fetch(new Request('https://example.static.hf.space/run/log.eval',{headers:{Range:'bytes=10-20'}}));
assert.equal(calls[0][0].headers.get('Range'),'bytes=10-20');
assert.equal(new URL(calls[0][0].url).searchParams.get('__sign'),'FAKE_SESSION');
context.window.fetch('https://outside.example/file');assert.equal(calls[1][0],'https://outside.example/file');
'''.replace('SOURCE', json.dumps(js))
        subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
