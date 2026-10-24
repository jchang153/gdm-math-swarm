import unittest

import maintain_hf_run as m


class LinkPresentTest(unittest.TestCase):
    def test_space_dataset_and_offline_targets(self):
        names = {"r/results.html", "r/conference-view/index.html"}
        archive = {"dataset_files": {"r/analysis/timeline.md": {}}, "offline_files": {"r/analysis/old.jsonl": {}}}
        self.assertTrue(m.link_present("r/results.html", names, archive))
        self.assertTrue(m.link_present("r/conference-view/", names, archive))
        self.assertTrue(m.link_present("r/analysis/timeline.md", names, archive))
        self.assertTrue(m.link_present("r/analysis/old.jsonl", names, archive))
        self.assertFalse(m.link_present("r/analysis/missing.md", names, archive))
        self.assertFalse(m.link_present("r/analysis/timeline.md", names, None))
        self.assertFalse(m.link_present("r/inspect/", names, archive))


if __name__ == "__main__":
    unittest.main()
