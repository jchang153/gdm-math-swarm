"""Every generated library page carries the shared light/dark theme and phone layout."""
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import site_style  # noqa: E402
from site_style import MARKER, themed  # noqa: E402


def assert_themed(case, document):
    case.assertEqual(document.count(MARKER), 1)
    case.assertIn('<meta name="color-scheme" content="light dark">', document)
    case.assertIn('prefers-color-scheme:dark', document)
    case.assertIn(':root[data-theme="dark"]', document)
    case.assertIn('site-theme-toggle', document)
    case.assertIn('@media (max-width:640px)', document)
    case.assertEqual(len(re.findall(r'<meta\s+name="viewport"', document)), 1)
    # The theme precedes the page's own <style> so page-specific rules still win.
    own = [m.start() for m in re.finditer(r'<style>', document)]
    if own:
        case.assertLess(document.index(MARKER), own[0])


class ThemeHelperTest(unittest.TestCase):
    def test_inserts_once_after_head_or_charset(self):
        with_head = themed('<!doctype html><html><head><title>x</title></head><body>y</body></html>')
        self.assertTrue(with_head.startswith('<!doctype html><html><head><meta name="viewport"'))
        assert_themed(self, with_head)
        self.assertEqual(themed(with_head), with_head)
        bare = themed('<!doctype html><meta charset="utf-8"><title>t</title><style>p{}</style>')
        self.assertTrue(bare.startswith('<!doctype html><meta charset="utf-8"><meta name="viewport"'))
        assert_themed(self, bare)

    def test_existing_viewport_is_kept_not_duplicated(self):
        doc = themed('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">')
        assert_themed(self, doc)

    def test_dark_tokens_cover_every_light_token(self):
        self.assertEqual(set(site_style.LIGHT), set(site_style.DARK))

    def test_contrast_of_text_tokens(self):
        def luminance(hex_colour):
            channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def ratio(a, b):
            hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        for tokens in (site_style.LIGHT, site_style.DARK):
            for surface in ('bg', 'card', 'pre', 'chip', 'banner'):
                for text in ('fg', 'muted', 'accent'):
                    self.assertGreaterEqual(ratio(tokens[text], tokens[surface]), 4.5, (text, surface, tokens[text]))
            self.assertGreaterEqual(ratio(tokens['sel-fg'], tokens['sel-bg']), 4.5)
            for text in ('warn', 'ok', 'amber', 'good', 'caution', 'bad', 'info'):
                self.assertGreaterEqual(ratio(tokens[text], tokens['card']), 4.5, text)

    def test_toggle_storage_is_guarded_and_local(self):
        script = site_style.SCRIPT
        self.assertEqual(script.count('localStorage.'), 3)
        self.assertEqual(script.count('try{'), 2)
        for forbidden in ('fetch(', 'XMLHttpRequest', 'http:', 'https:', '__sign', 'cookie'):
            self.assertNotIn(forbidden, script)


class GeneratedPagesTest(unittest.TestCase):
    def test_generators_theme_their_pages(self):
        import build_results
        import export_conference
        import export_transcripts
        assert_themed(self, export_conference.page('Conference', '<p>x</p>'))
        assert_themed(self, '<!doctype html><meta charset="utf-8"><title>a</title>' + export_transcripts.STYLE)
        import site_theme  # the run page's layout CSS (build_results imports it as STYLE)
        for module in (site_theme, export_conference, export_transcripts):
            source = Path(module.__file__).read_text().replace('BASE = (', 'STYLE = (', 1)
            style = re.findall(r"STYLE = .*?(?:\n\n|''')", source, re.S)[0]
            self.assertNotRegex(style, r'#[0-9a-fA-F]{3,6}\b|:white\b', module.__name__)
        self.assertIn('themed(', Path(build_results.__file__).read_text())

    def test_catalogue_page_is_themed(self):
        import build_experiment_index
        with tempfile.TemporaryDirectory() as tmp:
            build_experiment_index.write_index(Path(tmp), [])
            document = (Path(tmp) / 'index.html').read_text()
        assert_themed(self, document)
        css = document.split('</style>')[1]  # the catalogue's own style block
        self.assertNotRegex(css, r'#[0-9a-fA-F]{3,6}\b|background:white')

    def test_hosted_catalogue_keeps_navigation_and_theme(self):
        import build_experiment_index
        with tempfile.TemporaryDirectory() as tmp:
            build_experiment_index.write_index(Path(tmp), [], hosted=True)
            document = (Path(tmp) / 'index.html').read_text()
        assert_themed(self, document)
        self.assertIn('id="private-space-navigation"', document)

    def test_prepared_space_pages_are_themed_except_inspect_view(self):
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT / 'tests'))
        import prepare_hf_library
        from test_judge_publication import make_run, make_viewer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            viewer = make_viewer(root)
            make_run(root / 'runs', 'r1')
            (root / 'runs' / 'r1' / 'results.html').write_text(
                '<!doctype html><html><head><title>old</title></head><body>x</body></html>')
            output = root / 'bundle'
            argv = ['prepare_hf_library.py', '--runs-dir', str(root / 'runs'), '--output-dir', str(output),
                    '--run', f'r1={viewer}']
            with patch.object(sys, 'argv', argv), patch('builtins.print'):
                prepare_hf_library.main()
            pages = list(output.rglob('*.html'))
            inspect = [p for p in pages if 'inspect-view' in p.relative_to(output).parts]
            ours = [p for p in pages if p not in inspect]
            self.assertTrue(inspect and any(p.name == 'results.html' for p in ours))
            for page in ours:
                text = page.read_text()
                assert_themed(self, text)
                self.assertIn('id="private-space-navigation"', text)
            for page in inspect:
                self.assertNotIn(MARKER, page.read_text())


if __name__ == '__main__':
    unittest.main()
