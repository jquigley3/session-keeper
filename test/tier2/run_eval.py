#!/usr/bin/env python3
"""
Tier 2 eval — manual, requires claude CLI and ANTHROPIC_API_KEY.

Tests two things:
  1. Trigger accuracy  — does the skill description cause Claude to invoke
                         the skill on the right prompts and ignore others?
  2. End-to-end output — does a full skill run produce a coherent summary
                         of real session history?

Run from repo root:
  python3 test/tier2/run_eval.py
  python3 test/tier2/run_eval.py --e2e-only
  python3 test/tier2/run_eval.py --trigger-only
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent.parent
SKILL_DIR  = REPO_ROOT / "skills" / "session-summary"
SCRIPT     = SKILL_DIR / "scripts" / "sk-sessions.py"

# ── helpers ───────────────────────────────────────────────────────────────────

def run_claude(prompt: str, *, allow_bash: bool = False, timeout: int = 60) -> str:
    """Run claude -p with the session-summary skill loaded."""
    cmd = [
        "claude", "-p", prompt,
        "--skill", str(SKILL_DIR),
    ]
    if allow_bash:
        cmd += ["--allowedTools", "Bash"]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]"
    except FileNotFoundError:
        print("Error: 'claude' CLI not found. Is it installed and on your PATH?", file=sys.stderr)
        sys.exit(1)


def ok(msg: str):  print(f"  \033[32m✓\033[0m  {msg}")
def fail(msg: str): print(f"  \033[31m✗\033[0m  {msg}")
def info(msg: str): print(f"     {msg}")


# ── trigger eval ──────────────────────────────────────────────────────────────
#
# We detect whether the skill triggered by checking if the output contains
# patterns that sk-sessions.py would produce (timestamps, "Session:", project
# paths) or the word "session" used in a context-specific way.
# For should-not-trigger cases we check the output is a plain response with
# no session crawling artefacts.

TRIGGER_CASES = [
    # (prompt, should_trigger, description)
    ("what am I working on?",                          True,  "canonical trigger"),
    ("summarise my recent claude sessions",            True,  "explicit sessions mention"),
    ("catch me up on my projects",                     True,  "catch-me-up phrasing"),
    ("what have I been doing lately",                  True,  "lately phrasing"),
    ("which projects have I been active on",           True,  "active projects phrasing"),
    ("show me my session history",                     True,  "session history phrasing"),
    ("where did I leave off yesterday",                True,  "left-off phrasing"),
    ("write a python function to sort a list",         False, "unrelated coding task"),
    ("what is the capital of france",                  False, "factual question"),
    ("help me debug this error: TypeError on line 5",  False, "debugging request"),
]

SESSION_ARTEFACT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"   # table timestamp
    r"|Session:"                            # dump header
    r"|Working dir"                         # dump content
    r"|sk-sessions\.py"                     # script name in output
    r"|(Developer|claude|workspace)/\w",    # project path fragment
    re.IGNORECASE,
)


def run_trigger_eval():
    print("\n── Trigger accuracy eval ─────────────────────────────────────────")
    print(f"   Skill: {SKILL_DIR}")
    print()

    passed = failed = 0

    for prompt, should_trigger, desc in TRIGGER_CASES:
        print(f"  [{'+' if should_trigger else '-'}] {desc}")
        info(f"prompt: \"{prompt}\"")

        output = run_claude(prompt, allow_bash=False)
        triggered = bool(SESSION_ARTEFACT_RE.search(output))

        if triggered == should_trigger:
            ok("triggered as expected" if should_trigger else "correctly did not trigger")
            passed += 1
        else:
            fail("did NOT trigger (expected it to)" if should_trigger else "triggered unexpectedly")
            info(f"output snippet: {output[:120]!r}")
            failed += 1
        print()

    total = passed + failed
    print(f"  Trigger eval: {passed}/{total} passed")
    return failed == 0


# ── e2e eval ──────────────────────────────────────────────────────────────────
#
# Runs the full skill with Bash allowed against real ~/.claude session history.
# Asserts structural properties of the output — not exact content, since that
# changes with each session.

E2E_ASSERTIONS = [
    ("mentions at least one project path",
     lambda out: bool(re.search(r"Developer/\w+|~/\w+", out))),
    ("mentions a date or time period",
     lambda out: bool(re.search(r"\d{4}-\d{2}-\d{2}|today|yesterday|Mar|recent", out, re.I))),
    ("output is at least 100 chars",
     lambda out: len(out) >= 100),
    ("no Python traceback",
     lambda out: "Traceback" not in out),
    ("no 'session not found' error",
     lambda out: "No session found" not in out),
]


def run_e2e_eval():
    print("\n── End-to-end eval ───────────────────────────────────────────────")
    print(f"   Skill: {SKILL_DIR}")
    print(f"   Bash:  allowed")
    print(f"   Data:  real ~/.claude session history")
    print()

    prompt = "what am I working on and where? summarise across all my projects."
    info(f"Running: claude -p \"{prompt}\" --skill {SKILL_DIR} --allowedTools Bash")
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
    parser = argparse.ArgumentParser(description="Tier 2 skill eval")
    parser.add_argument("--trigger-only", action="store_true")
    parser.add_argument("--e2e-only", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

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
