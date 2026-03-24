#!/usr/bin/env python3
"""
Tier 2 eval for token-manager skill — manual, requires claude CLI and ANTHROPIC_API_KEY.

Tests two things:
  1. Trigger accuracy  — does the skill description cause Claude to invoke
                         the skill on the right prompts and ignore others?
  2. End-to-end output — does a full skill run produce useful output
                         when asked to audit session history?

Run from repo root:
  python3 test/tier2/run_eval_tokens.py
  python3 test/tier2/run_eval_tokens.py --e2e-only
  python3 test/tier2/run_eval_tokens.py --trigger-only
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent.parent
SKILL_DIR  = REPO_ROOT / "skills" / "token-manager"
SCRIPT     = SKILL_DIR / "scripts" / "sk-tokens.py"

MODEL = "claude-sonnet-4-6"


def run_claude(prompt: str, *, allow_bash: bool = False, timeout: int = 60) -> str:
    cmd = ["claude", "-p", prompt, "--model", MODEL]
    if allow_bash:
        cmd += ["--allowedTools", "Bash"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]"
    except FileNotFoundError:
        print("Error: 'claude' CLI not found.", file=sys.stderr)
        sys.exit(1)


def ok(msg):   print(f"  \033[32m✓\033[0m  {msg}")
def fail(msg): print(f"  \033[31m✗\033[0m  {msg}")
def info(msg): print(f"     {msg}")


# ── trigger eval ──────────────────────────────────────────────────────────────

TRIGGER_CASES = [
    ("save my GitHub token to .env",                   True,  "save token request"),
    ("where did I use my Anthropic key?",              True,  "find token usage"),
    ("audit my credentials in session history",        True,  "audit request"),
    ("find secrets in my chat history",                True,  "find secrets"),
    ("what API keys do I have stored?",                True,  "list stored keys"),
    ("add this token: ghp_abc123 to my env",           True,  "add token to env"),
    ("did my API key leak into any session?",          True,  "leak check"),
    ("write a python function to parse JSON",          False, "unrelated coding task"),
    ("what is the capital of france",                  False, "factual question"),
    ("summarize my git log",                           False, "git task"),
]

TRIGGER_ARTEFACT_RE = re.compile(
    r"(\.env"                         # .env file reference
    r"|~/.claude/.env"                # full path
    r"|SCREAMING_SNAKE"               # naming convention
    r"|sk-tokens\.py"                 # script name
    r"|token.{0,30}(store|save|env)"  # token storage language
    r"|secret.{0,30}(session|chat)"   # secret in session language
    r"|credential)",                  # credential mention
    re.IGNORECASE,
)


def run_trigger_eval():
    print("\n── Trigger accuracy eval ─────────────────────────────────────────")
    print(f"   Skill: {SKILL_DIR}\n")

    passed = failed = 0

    for prompt, should_trigger, desc in TRIGGER_CASES:
        print(f"  [{'+' if should_trigger else '-'}] {desc}")
        info(f"prompt: \"{prompt}\"")

        output = run_claude(prompt, allow_bash=False)
        triggered = bool(TRIGGER_ARTEFACT_RE.search(output))

        if triggered == should_trigger:
            ok("triggered as expected" if should_trigger else "correctly did not trigger")
            passed += 1
        else:
            fail("did NOT trigger" if should_trigger else "triggered unexpectedly")
            info(f"output snippet: {output[:120]!r}")
            failed += 1
        print()

    total = passed + failed
    print(f"  Trigger eval: {passed}/{total} passed")
    return failed == 0


# ── e2e eval ──────────────────────────────────────────────────────────────────

E2E_ASSERTIONS = [
    ("mentions .env or env file",
     lambda out: bool(re.search(r"\.env|env file", out, re.I))),
    ("mentions complex or simple mode",
     lambda out: bool(re.search(r"(complex|simple|--complex|--simple|uncatalogued|known)", out, re.I))),
    ("output is at least 100 chars",
     lambda out: len(out) >= 100),
    ("no Python traceback",
     lambda out: "Traceback" not in out),
    ("no 'file not found' error",
     lambda out: "No such file" not in out),
]


def run_e2e_eval():
    print("\n── End-to-end eval ───────────────────────────────────────────────")
    print(f"   Skill: {SKILL_DIR}")
    print(f"   Bash:  allowed")
    print(f"   Data:  real ~/.claude session history\n")

    prompt = "audit my session history for any secrets or tokens that aren't in my .env file"
    info(f"Running: claude -p \"{prompt}\" --allowedTools Bash")
    info("(this may take 30–60s)\n")

    output = run_claude(prompt, allow_bash=True, timeout=120)

    if output == "[TIMEOUT]":
        fail("timed out after 120s")
        return False

    print("  Output preview:")
    for line in output.splitlines()[:20]:
        info(line)
    if output.count("\n") > 20:
        info(f"  ... ({output.count(chr(10))} lines total)")
    print()

    passed = failed = 0
    for desc, check in E2E_ASSERTIONS:
        if check(output):
            ok(desc)
            passed += 1
        else:
            fail(desc)
            failed += 1

    total = passed + failed
    print(f"\n  E2E eval: {passed}/{total} assertions passed")
    return failed == 0


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tier 2 token-manager skill eval")
    parser.add_argument("--trigger-only", action="store_true")
    parser.add_argument("--e2e-only", action="store_true")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    args = parser.parse_args()

    global MODEL
    MODEL = args.model

    results = []
    if not args.e2e_only:
        results.append(run_trigger_eval())
    if not args.trigger_only:
        results.append(run_e2e_eval())

    print()
    if all(results):
        print("\033[32mAll evals passed.\033[0m")
        sys.exit(0)
    else:
        print("\033[31mSome evals failed.\033[0m")
        sys.exit(1)


if __name__ == "__main__":
    main()
