"""The story-map index's discreet link to the experimental interaction graph (storymap/view.py).

docs/plan.md, "Story-map interaction graph (November 5, 2026)": the index gains one small muted text link
"Interaction graph (experimental) ↗" to graph.html, only when graph.html exists for the run (or when the caller asks);
nothing else on the story map changes, and a run without a graph renders byte-identically.

    PYTHONPATH=src .venv/bin/python -m unittest research.analysis.tests.test_storymap_graph_link
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research.analysis.storymap import view  # noqa: E402
from research.analysis.tests.test_storymap_overseer_view import write_overseer  # noqa: E402

W4 = HERE / "fixtures" / "storymap" / "w4"
LINK = '<a href="graph.html" style="color:inherit"'
TEXT = "Interaction graph (experimental) ↗"


def tree(folder):
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def build(out, graph_file=False, **kw):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if graph_file:
        (out / "graph.html").write_text("<!doctype html><title>graph</title>", encoding="utf-8")
    view.build(W4 / "map", W4 / "timeline", out, **kw)
    return tree(out)


class GraphLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.plain = build(cls.tmp / "plain")
        cls.off = build(cls.tmp / "off", graph=False)
        cls.with_graph = build(cls.tmp / "with", graph_file=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_absent_without_graph(self):
        index = self.plain["index.html"].decode()
        self.assertNotIn("graph.html", index)
        self.assertNotIn("Interaction graph", index)
        self.assertEqual(self.plain, self.off)  # auto without graph.html == explicitly off

    def test_present_with_graph(self):
        index = self.with_graph["index.html"].decode()
        self.assertEqual(index.count(LINK), 1)
        self.assertEqual(index.count(TEXT), 1)
        self.assertEqual(index.count("graph.html"), 1)
        self.assertRegex(index, r'<p class="muted" style="font-size:\.85em[^"]*"><a href="graph\.html"')
        # Right under the title, before the banner.
        self.assertLess(index.index("</h1>"), index.index(LINK))
        self.assertLess(index.index(LINK), index.index('<p class="banner">'))

    def test_only_the_link_changes(self):
        """Removing the link paragraph gives back the page without a graph; every other page is identical."""
        index = self.with_graph["index.html"].decode()
        self.assertEqual(index.replace(view.graph_link(True), "", 1), self.plain["index.html"].decode())
        others = {k: v for k, v in self.with_graph.items() if k not in ("index.html", "graph.html")}
        self.assertEqual(others, {k: v for k, v in self.plain.items() if k != "index.html"})
        for name, data in others.items():
            self.assertNotIn(b"graph.html", data, name)

    def test_explicit_flag_overrides_detection(self):
        forced = build(self.tmp / "forced", graph=True)
        self.assertIn(LINK, forced["index.html"].decode())
        suppressed = build(self.tmp / "suppressed", graph_file=True, graph=False)
        self.assertEqual(suppressed["index.html"], self.plain["index.html"])

    def test_detects_graph_in_out_dir_not_map_dir(self):
        out = self.tmp / "elsewhere"
        build(out)
        self.assertNotIn("graph.html", (out / "index.html").read_text())
        (out / "graph.html").write_text("x")
        view.build(W4 / "map", W4 / "timeline", out)
        self.assertIn(LINK, (out / "index.html").read_text())

    def test_cli(self):
        for flag, expect in (([], False), (["--graph-link"], True), (["--no-graph-link"], False)):
            out = self.tmp / f"cli{len(flag)}{''.join(flag)}"
            out.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(view.main(["--map", str(W4 / "map"), "--timeline", str(W4 / "timeline"),
                                            "--out", str(out), *flag]), 0)
            self.assertEqual(LINK in (out / "index.html").read_text(), expect, flag)
        out = self.tmp / "cli-auto"
        out.mkdir()
        (out / "graph.html").write_text("x")
        with contextlib.redirect_stdout(io.StringIO()):
            view.main(["--map", str(W4 / "map"), "--timeline", str(W4 / "timeline"), "--out", str(out)])
        self.assertIn(LINK, (out / "index.html").read_text())

    def test_overseer_index_and_overseer_only_index(self):
        om, ot = write_overseer(self.tmp / "ovsrc")
        ov_plain = build(self.tmp / "ov-plain", overseer_map=om, overseer_timeline=ot)
        ov_graph = build(self.tmp / "ov-graph", graph_file=True, overseer_map=om, overseer_timeline=ot)
        self.assertNotIn(b"graph.html", ov_plain["index.html"])
        self.assertEqual(ov_graph["index.html"].decode().replace(view.graph_link(True), "", 1),
                         ov_plain["index.html"].decode())
        (self.tmp / "empty").mkdir()
        for name, graph_file in (("only-plain", False), ("only-graph", True)):
            out = self.tmp / name
            out.mkdir()
            if graph_file:
                (out / "graph.html").write_text("x")
            view.build(self.tmp / "empty", None, out, overseer_map=om, overseer_timeline=ot)
        only_plain = (self.tmp / "only-plain" / "index.html").read_text()
        only_graph = (self.tmp / "only-graph" / "index.html").read_text()
        self.assertIn("Overseers only", only_plain)
        self.assertNotIn("graph.html", only_plain)
        self.assertEqual(only_graph.count(LINK), 1)
        self.assertEqual(only_graph.replace(view.graph_link(True), "", 1), only_plain)


if __name__ == "__main__":
    unittest.main()


class GraphLinkPublicationTest(unittest.TestCase):
    """The site screen (scripts/storymap_site.py) refuses a page linking an unpublished file, so the linked index is
    publishable only once graph.html is on the story-map allowlist (docs/plan.md: it joins the allowlist)."""

    def test_site_screen_accepts_linked_index(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import storymap_site
        if "graph.html" not in storymap_site.STORYMAP_FILES:
            self.skipTest("graph.html is not on the story-map publication allowlist yet (scripts/storymap_site.py)")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "storymap"
            build(out, graph_file=True)
            files = storymap_site.public_files(out)
            self.assertIn("graph.html", files)
            self.assertIn(b'href="graph.html"', files["index.html"])
            plain = Path(tmp) / "plain"
            build(plain)
            self.assertNotIn("graph.html", storymap_site.public_files(plain))
