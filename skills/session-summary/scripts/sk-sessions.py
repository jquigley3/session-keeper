#!/usr/bin/env python3
"""
sk-sessions.py — crawl Claude Code session history.

Usage:
  sk-sessions.py                        List all sessions (table)
  sk-sessions.py --json                 List sessions as JSON
  sk-sessions.py --dump <id-prefix>     Print clean dialogue for a session
  sk-sessions.py --dump <id> --raw      Print unfiltered dialogue
  sk-sessions.py --sandbox              Also crawl running Docker sandboxes
"""

import json
import os
import sys
import glob
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude" / "projects"  # overridable via --sessions-dir


# ── helpers ──────────────────────────────────────────────────────────────────

def decode_project_path(dirname: str) -> str:
    """Convert ~/.claude/projects dir name back to filesystem path."""
    # e.g. "-Users-josh-Developer-session-keeper" → "/Users/josh/Developer/session-keeper"
    return dirname.replace("-", "/", 1).replace("-", "/")  # first dash is leading /


def parse_session(jsonl_path: Path) -> dict | None:
    """Parse a single session JSONL file and return metadata dict."""
    entries = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        return None

    if not entries:
        return None

    session_id = jsonl_path.stem
    project_dir = jsonl_path.parent.name

    # Collect from real message entries (not meta/progress/system)
    user_msgs = [e for e in entries if e.get("type") == "user" and not e.get("isMeta")]
    asst_msgs = [e for e in entries if e.get("type") == "assistant"]
    all_timed = [e for e in entries if e.get("timestamp")]

    if not all_timed:
        return None

    timestamps = [e["timestamp"] for e in all_timed]
    first_ts = min(timestamps)
    last_ts = max(timestamps)

    # cwd from first user message
    cwd = None
    for e in entries:
        if e.get("cwd"):
            cwd = e["cwd"]
            break

    # git branch
    git_branch = None
    for e in entries:
        if e.get("gitBranch"):
            git_branch = e["gitBranch"]
            break

    # models used
    models = list({
        e.get("message", {}).get("model")
        for e in asst_msgs
        if e.get("message", {}).get("model")
    })

    # token usage totals
    total_input = total_output = total_cache_read = 0
    for e in asst_msgs:
        usage = e.get("message", {}).get("usage", {})
        total_input += usage.get("input_tokens", 0)
        total_output += usage.get("output_tokens", 0)
        total_cache_read += usage.get("cache_read_input_tokens", 0)

    return {
        "session_id": session_id,
        "project_dir": project_dir,
        "working_dir": cwd,
        "git_branch": git_branch,
        "first_message": first_ts,
        "last_message": last_ts,
        "user_messages": len(user_msgs),
        "assistant_messages": len(asst_msgs),
        "models": models,
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cache_read": total_cache_read,
        },
        "jsonl_path": str(jsonl_path),
    }


def crawl_host(sessions_dir: Path | None = None) -> list[dict]:
    """Crawl ~/.claude/projects for all sessions (excluding subagent files)."""
    root = sessions_dir or CLAUDE_DIR
    sessions = []
    if not root.exists():
        return sessions

    for jsonl in sorted(root.glob("*/*.jsonl")):
        # Skip subagent files
        if "subagents" in jsonl.parts:
            continue
        meta = parse_session(jsonl)
        if meta:
            meta["source"] = "host"
            sessions.append(meta)

    return sessions


def crawl_sandbox(container: str) -> list[dict]:
    """Crawl a Docker sandbox container for Claude sessions."""
    sessions = []
    try:
        result = subprocess.run(
            ["docker", "exec", "-u", "root", container,
             "find", "/home/agent/.claude/projects", "-name", "*.jsonl",
             "-not", "-path", "*/subagents/*"],
            capture_output=True, text=True, timeout=10
        )
        paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return sessions

    for remote_path in paths:
        try:
            result = subprocess.run(
                ["docker", "exec", "-u", "root", container, "cat", remote_path],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                continue

            # Write to a temp file and parse
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
                tmp.write(result.stdout)
                tmp_path = Path(tmp.name)

            meta = parse_session(tmp_path)
            tmp_path.unlink()

            if meta:
                meta["source"] = f"sandbox:{container}"
                meta["jsonl_path"] = f"{container}:{remote_path}"
                sessions.append(meta)
        except (subprocess.TimeoutExpired, OSError):
            pass

    return sessions


def crawl_sandboxes() -> list[dict]:
    """Find running Docker sandbox containers and crawl each."""
    sessions = []
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return sessions

    containers = [c.strip() for c in result.stdout.splitlines() if c.strip()]
    for container in containers:
        sessions.extend(crawl_sandbox(container))

    return sessions


# ── display ──────────────────────────────────────────────────────────────────

def fmt_ts(ts: str) -> str:
    """Format ISO timestamp to short human-readable."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16]


def fmt_tokens(tokens: dict) -> str:
    total = tokens["input"] + tokens["output"]
    if total == 0:
        return "-"
    return f"{total:,}"


def print_table(sessions: list[dict]) -> None:
    """Print sessions as a human-readable table."""
    if not sessions:
        print("No sessions found.")
        return

    # Sort by last_message descending
    sessions = sorted(sessions, key=lambda s: s["last_message"], reverse=True)

    col_id = 8        # short prefix of session UUID
    col_dir = 30
    col_branch = 12
    col_when = 16
    col_msgs = 6
    col_tokens = 8
    col_src = 14

    header = (
        f"{'SESSION':>{col_id}}  "
        f"{'WORKING DIR':<{col_dir}}  "
        f"{'BRANCH':<{col_branch}}  "
        f"{'LAST MSG':<{col_when}}  "
        f"{'MSGS':>{col_msgs}}  "
        f"{'TOKENS':>{col_tokens}}  "
        f"{'SOURCE':<{col_src}}"
    )
    print(header)
    print("-" * len(header))

    for s in sessions:
        sid = s["session_id"][:col_id]
        wd = (s["working_dir"] or s["project_dir"])
        # Shorten working dir: show last 2 path components
        parts = Path(wd).parts if wd else []
        wd_short = "/".join(parts[-2:]) if len(parts) >= 2 else wd or "?"
        if len(wd_short) > col_dir:
            wd_short = "…" + wd_short[-(col_dir - 1):]

        branch = (s["git_branch"] or "-")[:col_branch]
        last = fmt_ts(s["last_message"])
        msgs = s["user_messages"] + s["assistant_messages"]
        tokens = fmt_tokens(s["tokens"])
        src = s["source"][:col_src]

        print(
            f"{sid:>{col_id}}  "
            f"{wd_short:<{col_dir}}  "
            f"{branch:<{col_branch}}  "
            f"{last:<{col_when}}  "
            f"{msgs:>{col_msgs}}  "
            f"{tokens:>{col_tokens}}  "
            f"{src:<{col_src}}"
        )


# ── dialogue extraction ───────────────────────────────────────────────────────

import re

# XML-tagged blocks that Claude Code injects into user messages — not real user input.
# Each is a tag name; we strip the full <tag>...</tag> block including its content.
_NOISE_TAGS = [
    "bash-input", "bash-stdout", "bash-stderr",
    "command-name", "command-message", "command-args",
    "local-command-stdout", "local-command-caveat",
    "task-notification", "system-reminder",
]
_NOISE_TAG_RE = re.compile(
    r"<(?:" + "|".join(_NOISE_TAGS) + r")>.*?</(?:" + "|".join(_NOISE_TAGS) + r")>",
    re.DOTALL,
)
# Interrupt notices injected as plain text
_INTERRUPT_RE = re.compile(r"\[Request interrupted[^\]]*\]")


def clean_user_text(text: str) -> str:
    """Strip Claude Code scaffolding noise from a user message."""
    text = _NOISE_TAG_RE.sub("", text)
    text = _INTERRUPT_RE.sub("", text)
    # Collapse runs of blank lines left behind
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_entries(jsonl_path: str) -> list[dict]:
    """Load JSONL entries from a local path or container:path."""
    if ":" in jsonl_path and not jsonl_path.startswith("/"):
        container, remote_path = jsonl_path.split(":", 1)
        result = subprocess.run(
            ["docker", "exec", "-u", "root", container, "cat", remote_path],
            capture_output=True, text=True, timeout=15,
        )
        lines_raw = result.stdout.splitlines()
    else:
        with open(jsonl_path) as f:
            lines_raw = f.readlines()

    entries = []
    for line in lines_raw:
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def extract_dialogue(jsonl_path: str, raw: bool = False) -> str:
    """
    Extract dialogue from a session JSONL.

    raw=False (default): strips tool results, shell echo blocks, and other
                         scaffolding so only genuine human↔assistant text remains.
    raw=True:            returns all text content unfiltered.
    """
    entries = _load_entries(jsonl_path)
    turns = []

    for e in entries:
        t = e.get("type")
        if e.get("isMeta"):
            continue

        if t == "user":
            content = e.get("message", {}).get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    # tool_result blocks are raw JSON noise — skip unless raw mode
                    if c.get("type") == "tool_result" and not raw:
                        continue
                    if c.get("type") == "text":
                        parts.append(c["text"])
                text = "\n".join(parts)
            else:
                text = ""

            text = text if raw else clean_user_text(text)
            if text:
                turns.append(("user", text))

        elif t == "assistant":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, list):
                parts = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    # Only emit text blocks; skip tool_use and thinking
                    if c.get("type") == "text":
                        parts.append(c["text"])
                text = "\n".join(parts).strip()
            elif isinstance(content, str):
                text = content.strip()
            else:
                text = ""
            if text:
                turns.append(("assistant", text))

    lines = []
    for role, text in turns:
        label = "You" if role == "user" else "Claude"
        # Indent multi-line assistant responses for readability
        if role == "assistant" and "\n" in text:
            indented = "\n".join("  " + l if l else "" for l in text.splitlines())
            lines.append(f"{label}:\n{indented}")
        else:
            lines.append(f"{label}: {text}")

    return "\n\n".join(lines)


def dump_session(session: dict, raw: bool = False) -> None:
    """Print session header + dialogue to stdout."""
    wd = session.get("working_dir") or session.get("project_dir", "unknown")
    branch = session.get("git_branch") or "unknown"
    first = fmt_ts(session["first_message"])
    last = fmt_ts(session["last_message"])

    print(f"Session:  {session['session_id']}")
    print(f"Project:  {wd}")
    print(f"Branch:   {branch}")
    print(f"Period:   {first} → {last}")
    print(f"Messages: {session['user_messages']} user / {session['assistant_messages']} assistant")
    if raw:
        print("(raw mode — no filtering)")
    print()

    dialogue = extract_dialogue(session["jsonl_path"], raw=raw)
    if dialogue:
        print(dialogue)
    else:
        print("(no dialogue found)")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Crawl and summarize Claude Code sessions"
    )
    parser.add_argument("--json", action="store_true", help="Output metadata as JSON")
    parser.add_argument("--sandbox", action="store_true",
                        help="Also crawl running Docker sandbox containers")
    parser.add_argument("--dump", metavar="SESSION_ID",
                        help="Dump dialogue for a session (by ID prefix)")
    parser.add_argument("--raw", action="store_true",
                        help="With --dump: skip noise filtering, show everything")
    parser.add_argument("--sessions-dir", metavar="PATH",
                        help="Override ~/.claude/projects (useful for testing)")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir) if args.sessions_dir else None
    sessions = crawl_host(sessions_dir)
    if args.sandbox:
        sessions.extend(crawl_sandboxes())

    if args.dump:
        prefix = args.dump
        matches = [s for s in sessions if s["session_id"].startswith(prefix)]
        if not matches:
            print(f"No session found matching prefix: {prefix}", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"Ambiguous prefix '{prefix}' matches {len(matches)} sessions:", file=sys.stderr)
            for m in matches:
                print(f"  {m['session_id']}", file=sys.stderr)
            sys.exit(1)
        dump_session(matches[0], raw=args.raw)
        return

    if args.json:
        print(json.dumps(sessions, indent=2))
    else:
        print_table(sessions)


if __name__ == "__main__":
    main()
