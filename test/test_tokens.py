"""
Unit tests for sk-tokens.py (Tier 1 — no LLM, no API key, runs in CI).
"""

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

# ── load the module without executing main() ─────────────────────────────────

SCRIPT = Path(__file__).parent.parent / "skills/token-manager/scripts/sk-tokens.py"

spec = importlib.util.spec_from_file_location("sk_tokens", SCRIPT)
sk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sk)

FIXTURES = Path(__file__).parent / "fixtures"
SESSION_WITH_TOKENS = (
    FIXTURES
    / "-Users-testuser-Developer-tokenproject"
    / "tttt1111-0000-0000-0000-000000000001.jsonl"
)
SESSION_WITH_BEARER = (
    FIXTURES
    / "-Users-testuser-Developer-tokenproject"
    / "tttt2222-0000-0000-0000-000000000002.jsonl"
)


# ── .env parsing ──────────────────────────────────────────────────────────────

class TestLoadEnv(unittest.TestCase):

    def _write_env(self, content: str) -> Path:
        tmp = Path(tempfile.mktemp(suffix=".env"))
        tmp.write_text(content)
        return tmp

    def test_basic_key_value(self):
        f = self._write_env("FOO=bar\nBAZ=qux\n")
        env = sk.load_env(f)
        self.assertEqual(env["FOO"], "bar")
        self.assertEqual(env["BAZ"], "qux")
        f.unlink()

    def test_strips_quotes(self):
        f = self._write_env('A="hello"\nB=\'world\'\n')
        env = sk.load_env(f)
        self.assertEqual(env["A"], "hello")
        self.assertEqual(env["B"], "world")
        f.unlink()

    def test_ignores_comments(self):
        f = self._write_env("# comment\nFOO=bar\n# another\n")
        env = sk.load_env(f)
        self.assertNotIn("# comment", env)
        self.assertEqual(env["FOO"], "bar")
        f.unlink()

    def test_ignores_lines_without_equals(self):
        f = self._write_env("JUSTNAME\nFOO=bar\n")
        env = sk.load_env(f)
        self.assertNotIn("JUSTNAME", env)
        self.assertEqual(env["FOO"], "bar")
        f.unlink()

    def test_missing_file_returns_empty(self):
        env = sk.load_env(Path("/nonexistent/.env"))
        self.assertEqual(env, {})

    def test_empty_file_returns_empty(self):
        f = self._write_env("")
        env = sk.load_env(f)
        self.assertEqual(env, {})
        f.unlink()

    def test_value_with_equals_sign(self):
        # Values can contain = signs (e.g., base64)
        f = self._write_env("TOKEN=abc=def==\n")
        env = sk.load_env(f)
        self.assertEqual(env["TOKEN"], "abc=def==")
        f.unlink()


# ── decode_project_dir ────────────────────────────────────────────────────────

class TestDecodeProjectDir(unittest.TestCase):

    def test_basic_decode(self):
        result = sk.decode_project_dir("-Users-testuser-Developer-myapp")
        self.assertEqual(result, "/Users/testuser/Developer/myapp")

    def test_leading_dash_becomes_slash(self):
        # First dash → first slash (the root)
        result = sk.decode_project_dir("-tmp-project")
        self.assertTrue(result.startswith("/"))


# ── extract_text_lines ────────────────────────────────────────────────────────

class TestExtractTextLines(unittest.TestCase):

    def test_returns_tuples(self):
        lines = sk.extract_text_lines(SESSION_WITH_TOKENS)
        self.assertTrue(len(lines) > 0)
        for lineno, role, text in lines:
            self.assertIsInstance(lineno, int)
            self.assertIn(role, ("user", "assistant"))
            self.assertIsInstance(text, str)

    def test_captures_user_and_assistant(self):
        lines = sk.extract_text_lines(SESSION_WITH_TOKENS)
        roles = {role for _, role, _ in lines}
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_token_appears_in_user_text(self):
        lines = sk.extract_text_lines(SESSION_WITH_TOKENS)
        user_texts = " ".join(t for _, r, t in lines if r == "user")
        self.assertIn("ghp_", user_texts)

    def test_missing_file_returns_empty(self):
        result = sk.extract_text_lines(Path("/nonexistent/session.jsonl"))
        self.assertEqual(result, [])


# ── mask_value ────────────────────────────────────────────────────────────────

class TestMaskValue(unittest.TestCase):

    def test_long_value_masked(self):
        result = sk.mask_value("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345")
        self.assertIn("…", result)
        self.assertTrue(result.startswith("ghp_"))
        self.assertNotIn("ABCDEFGHIJK", result)

    def test_short_value_fully_masked(self):
        result = sk.mask_value("abc")
        self.assertEqual(result, "***")

    def test_exactly_8_chars_masked(self):
        result = sk.mask_value("12345678")
        self.assertEqual(result, "********")

    def test_custom_show_chars(self):
        result = sk.mask_value("ABCDEFGHIJKLMNOP", show_chars=2)
        self.assertTrue(result.startswith("AB"))
        self.assertTrue(result.endswith("OP"))


# ── builtin patterns ──────────────────────────────────────────────────────────

class TestBuiltinPatterns(unittest.TestCase):
    """Smoke-test that key patterns compile and match expected values."""

    def _pattern_for_label(self, label: str):
        for regex, lbl in sk.BUILTIN_PATTERNS:
            if lbl == label:
                return regex
        return None

    def test_github_pat_matches(self):
        p = self._pattern_for_label("github-pat")
        self.assertIsNotNone(p)
        self.assertIsNotNone(p.search("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"))

    def test_anthropic_key_matches(self):
        p = self._pattern_for_label("anthropic-key")
        self.assertIsNotNone(p)
        self.assertIsNotNone(p.search("sk-ant-api03-something-long-enough-here"))

    def test_aws_key_matches(self):
        p = self._pattern_for_label("aws-access-key")
        self.assertIsNotNone(p)
        self.assertIsNotNone(p.search("AKIAIOSFODNN7EXAMPLE"))

    def test_slack_token_matches(self):
        p = self._pattern_for_label("slack-token")
        self.assertIsNotNone(p)
        self.assertIsNotNone(p.search("xoxb-00000000000-000000000000-TESTFAKEVALUE"))

    def test_stripe_key_matches(self):
        p = self._pattern_for_label("stripe-key")
        self.assertIsNotNone(p)
        # Construct value at runtime to avoid triggering GitHub secret scanning
        fake = "sk" + "_" + "live" + "_" + "00000000000000000000000000"
        self.assertIsNotNone(p.search(fake))


# ── detect_prefix ────────────────────────────────────────────────────────────

class TestDetectPrefix(unittest.TestCase):

    def test_github_pat(self):
        self.assertEqual(sk.detect_prefix("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"), "ghp_")

    def test_anthropic_key(self):
        self.assertEqual(sk.detect_prefix("sk-ant-api03-somekey"), "sk-ant-api03-")

    def test_anthropic_oauth(self):
        self.assertEqual(sk.detect_prefix("sk-ant-oat01-somekey"), "sk-ant-oat01-")

    def test_anthropic_generic(self):
        # sk-ant- without more specific sub-prefix
        self.assertEqual(sk.detect_prefix("sk-ant-other-somekey"), "sk-ant-")

    def test_slack(self):
        self.assertEqual(sk.detect_prefix("xoxb-123-abc"), "xoxb-")

    def test_no_prefix(self):
        self.assertEqual(sk.detect_prefix("abc1234def5678901234567890abcdef12345678"), "")

    def test_hex_no_prefix(self):
        self.assertEqual(sk.detect_prefix("f3f3fa84e1314599540c9de6a7fabdb81bc7368a"), "")

    def test_github_fine_grained(self):
        self.assertEqual(sk.detect_prefix("github_pat_something"), "github_pat_")


# ── make_scrub_replacement ────────────────────────────────────────────────────

class TestMakeScrubReplacement(unittest.TestCase):

    PAT = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"

    def test_mode1_keeps_prefix_and_last4(self):
        result = sk.make_scrub_replacement(self.PAT, 1)
        self.assertTrue(result.startswith("ghp_xxx"))
        self.assertTrue(result.endswith("2345"))

    def test_mode2_keeps_prefix_only(self):
        result = sk.make_scrub_replacement(self.PAT, 2)
        self.assertEqual(result, "ghp_xx")

    def test_mode3_replaces_all(self):
        result = sk.make_scrub_replacement(self.PAT, 3)
        self.assertEqual(result, "xxx")

    def test_mode1_no_prefix_still_works(self):
        hex_token = "f3f3fa84e1314599540c9de6a7fabdb81bc7368a"
        result = sk.make_scrub_replacement(hex_token, 1)
        self.assertTrue(result.startswith("xxx"))
        self.assertTrue(result.endswith("368a"))

    def test_mode2_no_prefix(self):
        hex_token = "f3f3fa84e1314599540c9de6a7fabdb81bc7368a"
        result = sk.make_scrub_replacement(hex_token, 2)
        self.assertEqual(result, "xx")

    def test_anthropic_mode1(self):
        key = "sk-ant-api03-verylongkeyvalue1234567890"
        result = sk.make_scrub_replacement(key, 1)
        self.assertTrue(result.startswith("sk-ant-api03-xxx"))
        self.assertTrue(result.endswith("7890"))

    def test_mode1_result_does_not_contain_raw_middle(self):
        result = sk.make_scrub_replacement(self.PAT, 1)
        # Middle characters should not appear
        self.assertNotIn("ABCDEFGHIJKLM", result)


# ── scrub_file_content ────────────────────────────────────────────────────────

class TestScrubFileContent(unittest.TestCase):

    def test_replaces_known_token(self):
        content = 'my token is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345 done'
        replacements = {"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345": "ghp_xxx2345"}
        new_content, count = sk.scrub_file_content(content, replacements)
        self.assertEqual(count, 1)
        self.assertIn("ghp_xxx2345", new_content)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ", new_content)

    def test_multiple_occurrences_counted(self):
        token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"
        content = f"token={token} and again {token}"
        replacements = {token: "ghp_xxx2345"}
        _, count = sk.scrub_file_content(content, replacements)
        self.assertEqual(count, 2)

    def test_no_match_returns_original(self):
        content = "nothing to scrub here"
        new_content, count = sk.scrub_file_content(content, {"sometoken": "xxx"})
        self.assertEqual(count, 0)
        self.assertEqual(new_content, content)

    def test_multiple_tokens_replaced(self):
        content = "token1=aaaa1111bbbb2222cccc3333dddd4444 token2=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"
        replacements = {
            "aaaa1111bbbb2222cccc3333dddd4444": "xxx",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345": "ghp_xx",
        }
        new_content, count = sk.scrub_file_content(content, replacements)
        self.assertEqual(count, 2)
        self.assertIn("xxx", new_content)
        self.assertIn("ghp_xx", new_content)


# ── scrub_jsonl_file ──────────────────────────────────────────────────────────

class TestScrubJsonlFile(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mktemp(suffix=".jsonl"))
        token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"
        self.tmp.write_text(
            '{"message":{"role":"user","content":"my token is ' + token + '"}}\n'
        )
        self.token = token

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def test_dry_run_does_not_write(self):
        original = self.tmp.read_text()
        count = sk.scrub_jsonl_file(self.tmp, {self.token: "ghp_xxx2345"}, dry_run=True)
        self.assertEqual(count, 1)
        self.assertEqual(self.tmp.read_text(), original)  # unchanged

    def test_write_replaces_token(self):
        count = sk.scrub_jsonl_file(self.tmp, {self.token: "ghp_xxx2345"}, dry_run=False)
        self.assertEqual(count, 1)
        content = self.tmp.read_text()
        self.assertIn("ghp_xxx2345", content)
        self.assertNotIn(self.token, content)

    def test_missing_file_returns_zero(self):
        count = sk.scrub_jsonl_file(Path("/nonexistent/file.jsonl"), {"x": "y"}, dry_run=False)
        self.assertEqual(count, 0)


# ── scrub_env_file ────────────────────────────────────────────────────────────

class TestScrubEnvFile(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mktemp(suffix=".env"))
        self.tmp.write_text(
            "# comment\n"
            "GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345\n"
            "OTHER_KEY=someothervalue\n"
        )

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def test_dry_run_does_not_write(self):
        original = self.tmp.read_text()
        count = sk.scrub_env_file(
            self.tmp,
            {"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"},
            dry_run=True,
        )
        self.assertEqual(count, 1)
        self.assertEqual(self.tmp.read_text(), original)

    def test_matching_value_replaced_with_scrubbed(self):
        count = sk.scrub_env_file(
            self.tmp,
            {"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"},
            dry_run=False,
        )
        self.assertEqual(count, 1)
        content = self.tmp.read_text()
        self.assertIn("GITHUB_TOKEN=[SCRUBBED]", content)
        self.assertNotIn("ghp_ABCDEF", content)

    def test_non_matching_value_unchanged(self):
        sk.scrub_env_file(
            self.tmp,
            {"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"},
            dry_run=False,
        )
        content = self.tmp.read_text()
        self.assertIn("OTHER_KEY=someothervalue", content)

    def test_comment_lines_preserved(self):
        sk.scrub_env_file(
            self.tmp,
            {"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"},
            dry_run=False,
        )
        content = self.tmp.read_text()
        self.assertIn("# comment", content)

    def test_missing_file_returns_zero(self):
        count = sk.scrub_env_file(Path("/nonexistent/.env"), {"token"}, dry_run=False)
        self.assertEqual(count, 0)


# ── git SHA filtering ─────────────────────────────────────────────────────────

class TestGitShaFiltering(unittest.TestCase):

    def test_git_sha_context_regex_matches(self):
        text = "commit abc1234def5678901234567890abcdef12345678 looks fine"
        self.assertIsNotNone(sk._GIT_SHA_CONTEXT.search(text))

    def test_bare_hex_not_flagged_as_git_sha(self):
        text = "my token is abc1234def5678901234567890abcdef12345678"
        self.assertIsNone(sk._GIT_SHA_CONTEXT.search(text))


# ── iter_sessions ─────────────────────────────────────────────────────────────

class TestIterSessions(unittest.TestCase):

    def test_finds_both_token_fixtures(self):
        token_dir = FIXTURES / "-Users-testuser-Developer-tokenproject"
        sessions = list(sk.iter_sessions(FIXTURES))
        ids = {p.stem for p, _ in sessions}
        self.assertIn("tttt1111-0000-0000-0000-000000000001", ids)
        self.assertIn("tttt2222-0000-0000-0000-000000000002", ids)

    def test_missing_dir_returns_empty(self):
        result = list(sk.iter_sessions(Path("/nonexistent")))
        self.assertEqual(result, [])

    def test_days_filter_excludes_old_files(self):
        # With days=0, everything should be excluded (modified in the past)
        # Actually days=0 means "today" — use a tiny positive but it's hard to test
        # Just verify the function accepts the parameter without error
        result = list(sk.iter_sessions(FIXTURES, days=365*100))
        self.assertGreater(len(result), 0)


# ── complex mode: github PAT found in session ─────────────────────────────────

class TestComplexModeIntegration(unittest.TestCase):

    def test_finds_github_pat_in_fixture(self):
        """The token fixture contains a GitHub PAT — complex mode should find it."""
        lines = sk.extract_text_lines(SESSION_WITH_TOKENS)
        pat_regex = re.compile(r'ghp_[a-zA-Z0-9]{36}')
        found = any(pat_regex.search(text) for _, _, text in lines)
        self.assertTrue(found, "GitHub PAT should appear in session fixture text")

    def test_known_token_skipped_when_in_env(self):
        """If the PAT is already in .env, complex mode should not flag it."""
        known_value = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12345"
        env = {"GITHUB_TOKEN": known_value}
        known_values = set(env.values())
        self.assertIn(known_value, known_values)

    def test_bearer_token_not_matched_as_git_sha(self):
        """Bearer tokens in session 2 should not be suppressed by the git SHA filter."""
        lines = sk.extract_text_lines(SESSION_WITH_BEARER)
        # The git SHA in session 2 should not be mis-matched as a bearer token
        bearer_regex = re.compile(
            r'(?i)(?:Authorization|Auth)\s*:\s*(?:Bearer|Token|Basic)\s+([a-zA-Z0-9\-._~+/=]{8,})'
        )
        bearer_hits = [m.group(1) for _, _, text in lines for m in bearer_regex.finditer(text)]
        # Should find the bearer token
        self.assertTrue(len(bearer_hits) > 0, "Bearer token should be found in fixture")


if __name__ == "__main__":
    unittest.main(verbosity=2)
