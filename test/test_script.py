"""
Unit tests for sk-sessions.py (Tier 1 — no LLM, no API key, runs in CI).
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

# ── load the module without executing main() ─────────────────────────────────

SCRIPT = Path(__file__).parent.parent / "skills/session-summary/scripts/sk-sessions.py"

spec = importlib.util.spec_from_file_location("sk_sessions", SCRIPT)
sk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sk)

FIXTURES = Path(__file__).parent / "fixtures"
SESSION_A = FIXTURES / "-Users-testuser-Developer-myapp" / "aaaabbbb-0000-0000-0000-000000000001.jsonl"
SESSION_B = FIXTURES / "-Users-testuser-Developer-other-project" / "ccccdddd-0000-0000-0000-000000000002.jsonl"


# ── parsing ───────────────────────────────────────────────────────────────────

class TestParseSession(unittest.TestCase):

    def test_returns_dict_for_valid_file(self):
        meta = sk.parse_session(SESSION_A)
        self.assertIsNotNone(meta)

    def test_session_id_is_filename_stem(self):
        meta = sk.parse_session(SESSION_A)
        self.assertEqual(meta["session_id"], "aaaabbbb-0000-0000-0000-000000000001")

    def test_working_dir_extracted_from_cwd(self):
        meta = sk.parse_session(SESSION_A)
        self.assertEqual(meta["working_dir"], "/Users/testuser/Developer/myapp")

    def test_git_branch_extracted(self):
        meta = sk.parse_session(SESSION_A)
        self.assertEqual(meta["git_branch"], "main")

    def test_message_counts(self):
        meta = sk.parse_session(SESSION_A)
        self.assertEqual(meta["user_messages"], 2)
        self.assertEqual(meta["assistant_messages"], 2)

    def test_token_totals(self):
        meta = sk.parse_session(SESSION_A)
        # input: 120+200=320, output: 60+80=140
        self.assertEqual(meta["tokens"]["input"], 320)
        self.assertEqual(meta["tokens"]["output"], 140)

    def test_timestamps_present(self):
        meta = sk.parse_session(SESSION_A)
        self.assertIn("first_message", meta)
        self.assertIn("last_message", meta)
        self.assertLess(meta["first_message"], meta["last_message"])

    def test_model_captured(self):
        meta = sk.parse_session(SESSION_A)
        self.assertIn("claude-sonnet-4-6", meta["models"])

    def test_returns_none_for_empty_file(self, tmp_path=None):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
        try:
            result = sk.parse_session(path)
            self.assertIsNone(result)
        finally:
            path.unlink()

    def test_returns_none_for_missing_file(self):
        result = sk.parse_session(Path("/nonexistent/path.jsonl"))
        self.assertIsNone(result)


# ── crawl_host ────────────────────────────────────────────────────────────────

class TestCrawlHost(unittest.TestCase):

    def setUp(self):
        self.sessions = sk.crawl_host(FIXTURES)

    def test_finds_both_sessions(self):
        ids = {s["session_id"] for s in self.sessions}
        self.assertIn("aaaabbbb-0000-0000-0000-000000000001", ids)
        self.assertIn("ccccdddd-0000-0000-0000-000000000002", ids)

    def test_source_is_host(self):
        for s in self.sessions:
            self.assertEqual(s["source"], "host")

    def test_missing_dir_returns_empty(self):
        result = sk.crawl_host(Path("/nonexistent"))
        self.assertEqual(result, [])


# ── noise filtering ───────────────────────────────────────────────────────────

class TestCleanUserText(unittest.TestCase):

    def test_strips_bash_input(self):
        text = "<bash-input>ls -la</bash-input>"
        self.assertEqual(sk.clean_user_text(text), "")

    def test_strips_bash_stdout(self):
        text = "<bash-stdout>file.txt\ndir/\n</bash-stdout>"
        self.assertEqual(sk.clean_user_text(text), "")

    def test_strips_bash_stderr(self):
        text = "<bash-stderr>error: not found</bash-stderr>"
        self.assertEqual(sk.clean_user_text(text), "")

    def test_strips_command_blocks(self):
        text = "<command-name>/clear</command-name>\n<command-message>clear</command-message>"
        self.assertEqual(sk.clean_user_text(text), "")

    def test_strips_interrupt_notice(self):
        text = "[Request interrupted by user for tool use]"
        self.assertEqual(sk.clean_user_text(text), "")

    def test_strips_local_command_caveat(self):
        text = "<local-command-caveat>Caveat: do not respond</local-command-caveat>"
        self.assertEqual(sk.clean_user_text(text), "")

    def test_preserves_real_user_text(self):
        text = "build me a login page"
        self.assertEqual(sk.clean_user_text(text), "build me a login page")

    def test_preserves_text_around_noise(self):
        text = "<bash-input>ls</bash-input>\nwhat does this do?"
        self.assertEqual(sk.clean_user_text(text), "what does this do?")

    def test_multiline_noise_stripped(self):
        text = "<bash-stdout>line1\nline2\nline3</bash-stdout>"
        self.assertEqual(sk.clean_user_text(text), "")


# ── dialogue extraction ───────────────────────────────────────────────────────

class TestExtractDialogue(unittest.TestCase):

    def test_clean_mode_strips_bash_echoes(self):
        dialogue = sk.extract_dialogue(str(SESSION_B), raw=False)
        self.assertNotIn("<bash-input>", dialogue)
        self.assertNotIn("<bash-stdout>", dialogue)
        self.assertNotIn("[Request interrupted", dialogue)

    def test_clean_mode_keeps_real_messages(self):
        dialogue = sk.extract_dialogue(str(SESSION_B), raw=False)
        self.assertIn("refactor the src directory to use TypeScript", dialogue)

    def test_raw_mode_preserves_bash_echoes(self):
        dialogue = sk.extract_dialogue(str(SESSION_B), raw=True)
        self.assertIn("<bash-input>", dialogue)
        self.assertIn("[Request interrupted", dialogue)

    def test_you_claude_labels(self):
        dialogue = sk.extract_dialogue(str(SESSION_A), raw=False)
        self.assertIn("You:", dialogue)
        self.assertIn("Claude:", dialogue)

    def test_assistant_multiline_indented(self):
        # SESSION_B assistant turn 2 has a multi-line response
        dialogue = sk.extract_dialogue(str(SESSION_B), raw=False)
        # Multi-line responses are indented with 2 spaces
        self.assertIn("  - ", dialogue)

    def test_empty_after_noise_stripping_skipped(self):
        # The pure bash-echo user turn in SESSION_B should not appear as "You: "
        dialogue = sk.extract_dialogue(str(SESSION_B), raw=False)
        lines = dialogue.splitlines()
        empty_you = [l for l in lines if l.strip() == "You:"]
        self.assertEqual(empty_you, [])


# ── json output ───────────────────────────────────────────────────────────────

class TestJsonOutput(unittest.TestCase):

    def test_json_schema(self):
        sessions = sk.crawl_host(FIXTURES)
        for s in sessions:
            self.assertIn("session_id", s)
            self.assertIn("working_dir", s)
            self.assertIn("git_branch", s)
            self.assertIn("first_message", s)
            self.assertIn("last_message", s)
            self.assertIn("user_messages", s)
            self.assertIn("assistant_messages", s)
            self.assertIn("tokens", s)
            self.assertIn("source", s)

    def test_json_serialisable(self):
        sessions = sk.crawl_host(FIXTURES)
        # Should not raise
        json.dumps(sessions)


# ── timestamp formatting ──────────────────────────────────────────────────────

class TestFmtTs(unittest.TestCase):

    def test_iso_to_local(self):
        result = sk.fmt_ts("2026-03-20T10:00:00.000Z")
        self.assertRegex(result, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_graceful_on_bad_input(self):
        result = sk.fmt_ts("not-a-date")
        self.assertEqual(result, "not-a-date")


if __name__ == "__main__":
    unittest.main(verbosity=2)
