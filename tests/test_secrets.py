"""secrets.py parsing and refusal rules, using temporary files only (never the real .env)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from research_swarm.secrets import KEY_NAME, credential_status, load_openrouter_key

FAKE = "sk-test-not-a-real-key-0000"


class SecretsTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(KEY_NAME, None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def write(self, text, mode=0o600):
        path = self.root / ".env"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_environment_overrides_file(self):
        self.write(f"{KEY_NAME}=from-file\n")
        with mock.patch.dict(os.environ, {KEY_NAME: FAKE}):
            self.assertEqual(load_openrouter_key(self.root), FAKE)
        with mock.patch.dict(os.environ, {KEY_NAME: ""}):  # empty variable falls back to the file
            self.assertEqual(load_openrouter_key(self.root), "from-file")

    def test_refuses_symlink(self):
        target = self.root / "real.env"
        target.write_text(f"{KEY_NAME}={FAKE}\n")
        target.chmod(0o600)
        (self.root / ".env").symlink_to(target)
        with self.assertRaises(RuntimeError) as caught:
            load_openrouter_key(self.root)
        self.assertNotIn(FAKE, str(caught.exception))
        self.assertFalse(credential_status(self.root)["env_file_regular"])

    def test_refuses_group_or_world_readable(self):
        for mode in (0o640, 0o604, 0o660):
            self.write(f"{KEY_NAME}={FAKE}\n", mode)
            with self.assertRaises(RuntimeError) as caught:
                load_openrouter_key(self.root)
            self.assertNotIn(FAKE, str(caught.exception))
            self.assertFalse(credential_status(self.root)["env_file_private"])

    def test_only_named_key_parsed(self):
        self.write("# comment\nOTHER=$(touch /tmp/should-not-run)\nOPENROUTER_API_KEY_OLD=wrong\n"
                   f"XOPENROUTER_API_KEY=wrong\n{KEY_NAME}={FAKE}\nLATER=${{{KEY_NAME}}}\n")
        self.assertEqual(load_openrouter_key(self.root), FAKE)

    def test_export_prefix_quotes_and_inline_comments(self):
        cases = {
            f"export {KEY_NAME}={FAKE}\n": FAKE,
            f"export\t{KEY_NAME} = {FAKE}   # personal key\n": FAKE,
            f"{KEY_NAME}={FAKE} # note\n": FAKE,
            f"{KEY_NAME}=\"{FAKE}\"  # quoted\n": FAKE,
            f"{KEY_NAME}='{FAKE}#kept'\n": FAKE + "#kept",
            f"{KEY_NAME}={FAKE}#no-space-kept\n": FAKE + "#no-space-kept",
        }
        for text, expected in cases.items():
            self.write(text)
            self.assertEqual(load_openrouter_key(self.root), expected, text)

    def test_absent_empty_and_malformed(self):
        with self.assertRaises(RuntimeError):
            load_openrouter_key(self.root)  # no .env at all
        for text in (f"{KEY_NAME}=\n", f"{KEY_NAME}=  # empty\n", f"{KEY_NAME}=''\n", "# nothing\n"):
            self.write(text)
            with self.assertRaises(RuntimeError):
                load_openrouter_key(self.root)
        self.write(f"{KEY_NAME}=\"{FAKE}\n")  # unterminated quote
        with self.assertRaises(RuntimeError) as caught:
            load_openrouter_key(self.root)
        self.assertNotIn(FAKE, str(caught.exception))
        self.write(f"{KEY_NAME}=a\n{KEY_NAME}=b\n")
        with self.assertRaises(RuntimeError):
            load_openrouter_key(self.root)

    def test_credential_status_reads_no_contents(self):
        self.assertEqual(credential_status(self.root)["env_file_present"], False)
        self.write("garbage that is never parsed\n")
        status = credential_status(self.root)
        self.assertEqual((status["env_file_present"], status["env_file_regular"], status["env_file_private"],
                          status["credentials_read"]), (True, True, True, False))
        with mock.patch("pathlib.Path.read_text", side_effect=AssertionError("contents read")):
            credential_status(self.root)


if __name__ == "__main__":
    unittest.main()
