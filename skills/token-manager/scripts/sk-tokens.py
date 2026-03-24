#!/usr/bin/env python3
"""
sk-tokens.py — scrape Claude Code session history for tokens and secrets.

Modes:
  --simple    Check ~/.claude/.env for known names/values, then find matches in
              session chat history. Answers: "where did I use this token?"
  --complex   Find potential tokens in chat that are NOT already in ~/.claude/.env.
              Answers: "what secrets have I typed into chat that I haven't catalogued?"

Usage:
  python3 sk-tokens.py [--simple | --complex]
  python3 sk-tokens.py --simple --name MY_TOKEN
  python3 sk-tokens.py --complex --pattern 'ghp_[a-zA-Z0-9]{36}'
  python3 sk-tokens.py --complex --json

Options:
  --simple            Search .env for known tokens, find them in session history
  --complex           Find potential tokens NOT in .env (default mode)
  --env FILE          Path to .env file (default: ~/.claude/.env)
  --sessions DIR      Path to sessions dir (default: ~/.claude/projects/)
  --name NAME         Filter simple mode to a specific env var name
  --pattern RE        Additional regex pattern(s) — repeatable, for agent use
  --context N         Lines of context around each match (default: 1)
  --json              Machine-readable JSON output
  --no-mask           Show full token values (default: mask to first/last 4 chars)
  --days N            Only scan sessions from the last N days (default: all)
  --sandbox           Also crawl running Docker sandbox containers
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Token patterns for complex / unknown-secret detection ─────────────────────
#
# Each entry: (compiled_regex, label)
# Patterns are ordered from most specific (low false-positive) to broadest.
# Agents can inject additional patterns via --pattern.

_RAW_PATTERNS = [
    # Anthropic API keys
    (r'sk-ant-[a-zA-Z0-9\-_]{20,}', 'anthropic-key'),
    # GitHub PATs (classic and fine-grained)
    (r'ghp_[a-zA-Z0-9]{36}', 'github-pat'),
    (r'github_pat_[a-zA-Z0-9_]{82}', 'github-pat-fine'),
    (r'gho_[a-zA-Z0-9]{36}', 'github-oauth'),
    (r'ghu_[a-zA-Z0-9]{36}', 'github-user-token'),
    (r'ghs_[a-zA-Z0-9]{36}', 'github-server-token'),
    # AWS
    (r'AKIA[0-9A-Z]{16}', 'aws-access-key'),
    (r'(?i)aws[_\-]?secret[_\-]?(?:access[_\-]?)?key\s*[:=]\s*([a-zA-Z0-9/+]{40})', 'aws-secret-key'),
    # Slack
    (r'xox[bpra]-[0-9]+-[0-9a-zA-Z\-]{10,}', 'slack-token'),
    # Stripe
    (r'(?:sk|rk|pk)_(?:live|test)_[0-9a-zA-Z]{24,}', 'stripe-key'),
    # Twilio
    (r'SK[0-9a-fA-F]{32}', 'twilio-sid'),
    # Sendgrid
    (r'SG\.[a-zA-Z0-9\-_]{22,}\.[a-zA-Z0-9\-_]{43,}', 'sendgrid-key'),
    # Gitea/Forgejo tokens (common in self-hosted setups)
    (r'[0-9a-f]{40}', 'hex-40'),  # many services use 40-char hex (also git SHA — filtered in code)
    # Generic named credentials in chat prose:
    #   "token: abc123", "API_KEY=xyz", "Authorization: Bearer xxx"
    (r'(?i)(?:token|api[_\- ]?key|secret|password|passwd|pwd|access[_\- ]?token)'
     r'\s*[:=]\s*["\']?([^\s\'"<>{}\[\]]{8,80})["\']?', 'named-credential'),
    # Authorization header values
    (r'(?i)(?:Authorization|Auth)\s*:\s*(?:Bearer|Token|Basic)\s+([a-zA-Z0-9\-._~+/=]{8,})',
     'auth-header'),
]

BUILTIN_PATTERNS = [(re.compile(p), label) for p, label in _RAW_PATTERNS]

# Heuristic: skip matches that look like git SHAs (40 hex, appears after "commit " or similar)
_GIT_SHA_CONTEXT = re.compile(
    r'(?:commit|merge|HEAD|hash|sha)\s+[0-9a-f]{40}', re.IGNORECASE
)

# Noise values to skip even if they match a pattern (base words, placeholders)
_NOISE_VALUES = {
    'password', 'secret', 'token', 'key', 'value', 'example', 'placeholder',
    'your-token', 'your_token', 'YOUR_TOKEN', 'xxx', 'yyy', 'abc', '123456',
    '<token>', '<key>', '<secret>', '[token]', '[key]',
}


# ── .env parsing ──────────────────────────────────────────────────────────────

def load_env(env_path: Path) -> dict[str, str]:
    """Parse a NAME=value .env file. Returns {name: value}."""
    result = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text(errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        name, _, value = line.partition('=')
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            result[name] = value
    return result


# ── session crawling ──────────────────────────────────────────────────────────

def iter_sessions(sessions_dir: Path, days: int | None = None):
    """
    Yield (jsonl_path, project_dir_name) for every session file found.
    Optionally filter to files modified within the last `days` days.
    """
    if not sessions_dir.exists():
        return
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for project_dir in sessions_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob('*.jsonl'):
            if cutoff:
                mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
            yield jsonl, project_dir.name


def decode_project_dir(encoded: str) -> str:
    """Convert -Users-josh-Developer-myapp → /Users/josh/Developer/myapp."""
    return encoded.replace('-', '/', 1).replace('-', '/')


def extract_text_lines(jsonl_path: Path) -> list[tuple[int, str, str]]:
    """
    Extract (line_number, role, text) from a session JSONL.
    Only yields user/assistant message text; skips tool calls, metadata, etc.
    """
    results = []
    try:
        with jsonl_path.open(errors='replace') as f:
            for lineno, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg = record.get('message', {})
                role = msg.get('role', '')
                if role not in ('user', 'assistant'):
                    continue

                content = msg.get('content', '')
                if isinstance(content, list):
                    # Content block array — extract text blocks only
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            text = block.get('text', '')
                            if text:
                                results.append((lineno, role, text))
                elif isinstance(content, str) and content:
                    results.append((lineno, role, content))
    except (OSError, UnicodeDecodeError):
        pass
    return results


# ── masking ───────────────────────────────────────────────────────────────────

def mask_value(value: str, show_chars: int = 4) -> str:
    """Mask a secret value: show first and last N characters."""
    if len(value) <= show_chars * 2:
        return '*' * len(value)
    return value[:show_chars] + '…' + value[-show_chars:]


# ── simple mode ───────────────────────────────────────────────────────────────

def run_simple(
    env: dict[str, str],
    sessions_dir: Path,
    name_filter: str | None,
    days: int | None,
    context_lines: int,
    do_mask: bool,
    as_json: bool,
    extra_patterns: list,
) -> int:
    """
    For each token in .env (optionally filtered to --name), search session
    history for lines containing the token value or its name.
    """
    if not env:
        print("No entries found in .env file.", file=sys.stderr)
        return 1

    targets = {k: v for k, v in env.items() if not name_filter or k == name_filter}
    if not targets:
        print(f"No entry named '{name_filter}' in .env.", file=sys.stderr)
        return 1

    # Build search regexes: match the literal value OR the env var name
    searches = []
    for name, value in targets.items():
        searches.append((name, 'value', re.compile(re.escape(value)) if value else None))
        searches.append((name, 'name', re.compile(r'\b' + re.escape(name) + r'\b')))

    # Add any extra agent-supplied patterns (searched under label 'custom')
    for pat in extra_patterns:
        searches.append(('--pattern', 'custom', pat))

    hits = []  # list of hit dicts

    for jsonl_path, proj_dir in iter_sessions(sessions_dir, days):
        lines = extract_text_lines(jsonl_path)
        if not lines:
            continue

        for lineno, role, text in lines:
            for env_name, match_type, regex in searches:
                if regex is None:
                    continue
                m = regex.search(text)
                if not m:
                    continue
                matched = m.group(0)
                display = mask_value(matched) if do_mask else matched

                hits.append({
                    'session': jsonl_path.stem,
                    'project': decode_project_dir(proj_dir),
                    'jsonl': str(jsonl_path),
                    'line': lineno,
                    'role': role,
                    'env_name': env_name,
                    'match_type': match_type,
                    'matched': display,
                    'context': _get_context(text, m.start(), m.end(), context_lines),
                })
                break  # one hit per line per search term is enough

    if as_json:
        print(json.dumps(hits, indent=2))
        return 0

    if not hits:
        print("No matches found in session history.")
        return 0

    # Group by env_name for display
    by_name: dict[str, list] = defaultdict(list)
    for h in hits:
        by_name[h['env_name']].append(h)

    for env_name, group in sorted(by_name.items()):
        print(f"\n{env_name}  ({len(group)} occurrence{'s' if len(group) != 1 else ''})")
        print('─' * 60)
        for h in group:
            print(f"  {h['project']}")
            print(f"  session {h['session'][:8]}  line {h['line']}  [{h['role']}]  [{h['match_type']}]")
            print(f"  matched: {h['matched']}")
            if h['context']:
                for ctx_line in h['context'].splitlines():
                    print(f"    {ctx_line}")
            print()

    return 0


# ── complex mode ──────────────────────────────────────────────────────────────

def run_complex(
    env: dict[str, str],
    sessions_dir: Path,
    days: int | None,
    context_lines: int,
    do_mask: bool,
    as_json: bool,
    extra_patterns: list,
) -> int:
    """
    Find potential tokens/secrets in session chat that are NOT already in .env.
    Uses built-in patterns + any --pattern args.
    """
    known_values = set(env.values()) - {''}

    patterns = BUILTIN_PATTERNS + [(pat, 'custom') for pat in extra_patterns]

    hits = []

    for jsonl_path, proj_dir in iter_sessions(sessions_dir, days):
        lines = extract_text_lines(jsonl_path)
        if not lines:
            continue

        for lineno, role, text in lines:
            for regex, label in patterns:
                for m in regex.finditer(text):
                    # Use group(1) if available (named-credential, auth-header patterns)
                    # otherwise group(0)
                    try:
                        raw = m.group(1)
                    except IndexError:
                        raw = m.group(0)

                    raw = raw.strip()
                    if not raw or raw.lower() in _NOISE_VALUES:
                        continue

                    # Skip if it looks like a git SHA in context
                    context_window = text[max(0, m.start()-30):m.end()+10]
                    if label == 'hex-40' and _GIT_SHA_CONTEXT.search(context_window):
                        continue

                    # Skip if already in .env
                    if raw in known_values:
                        continue

                    # Skip very short matches for broad patterns
                    if label in ('hex-40', 'named-credential', 'auth-header') and len(raw) < 12:
                        continue

                    display = mask_value(raw) if do_mask else raw

                    hits.append({
                        'session': jsonl_path.stem,
                        'project': decode_project_dir(proj_dir),
                        'jsonl': str(jsonl_path),
                        'line': lineno,
                        'role': role,
                        'label': label,
                        'matched': display,
                        'raw_length': len(raw),
                        'context': _get_context(text, m.start(), m.end(), context_lines),
                    })

    # Deduplicate: same masked value + same label across multiple sessions
    # Keep first occurrence per unique (label, masked_value)
    seen: set[tuple] = set()
    deduped = []
    for h in hits:
        key = (h['label'], h['matched'])
        if key not in seen:
            seen.add(key)
            deduped.append(h)

    if as_json:
        print(json.dumps(deduped, indent=2))
        return 0

    if not deduped:
        print("No uncatalogued tokens found in session history.")
        return 0

    # Group by label
    by_label: dict[str, list] = defaultdict(list)
    for h in deduped:
        by_label[h['label']].append(h)

    print(f"\nFound {len(deduped)} potential uncatalogued secret(s):\n")

    for label, group in sorted(by_label.items()):
        print(f"  [{label}]  {len(group)} unique value(s)")
        print('  ' + '─' * 58)
        for h in group:
            print(f"    {h['project']}")
            print(f"    session {h['session'][:8]}  line {h['line']}  [{h['role']}]  len={h['raw_length']}")
            print(f"    value: {h['matched']}")
            if h['context']:
                for ctx_line in h['context'].splitlines():
                    print(f"      {ctx_line}")
            print()

    print("Consider adding these to ~/.claude/.env with descriptive names.")
    return 0


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_context(text: str, start: int, end: int, n_lines: int) -> str:
    """Extract N lines of context around a match position in a multi-line text."""
    if n_lines <= 0:
        return ''
    lines = text.splitlines()
    # Find which line the match falls on
    pos = 0
    match_line_idx = 0
    for i, line in enumerate(lines):
        if pos + len(line) >= start:
            match_line_idx = i
            break
        pos += len(line) + 1  # +1 for newline

    lo = max(0, match_line_idx - n_lines)
    hi = min(len(lines), match_line_idx + n_lines + 1)
    return '\n'.join(lines[lo:hi])


def crawl_sandbox_sessions(sessions_dir: Path, days: int | None):
    """
    Extend session iteration with sessions from running Docker sandbox containers.
    Containers with /root/.claude/projects/ are eligible.
    """
    try:
        result = subprocess.run(
            ['docker', 'ps', '--format', '{{.ID}}\t{{.Names}}'],
            capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    extras = []
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if not parts:
            continue
        container_id = parts[0]
        try:
            cp = subprocess.run(
                ['docker', 'exec', container_id,
                 'find', '/root/.claude/projects', '-name', '*.jsonl'],
                capture_output=True, text=True, timeout=15
            )
        except subprocess.TimeoutExpired:
            continue
        for fpath in cp.stdout.splitlines():
            fpath = fpath.strip()
            if not fpath:
                continue
            # Copy the file out to a temp path and yield it
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.jsonl', delete=False) as tf:
                tmp = Path(tf.name)
            cp2 = subprocess.run(
                ['docker', 'cp', f'{container_id}:{fpath}', str(tmp)],
                capture_output=True
            )
            if cp2.returncode == 0:
                extras.append((tmp, f'sandbox-{container_id[:8]}'))

    return extras


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Scrape Claude Code session history for tokens and secrets.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument('--simple', action='store_true',
                          help='Find .env tokens in session history')
    mode_grp.add_argument('--complex', action='store_true',
                          help='Find uncatalogued tokens not in .env (default)')

    parser.add_argument('--env', type=Path,
                        default=Path.home() / '.claude' / '.env',
                        help='Path to .env file (default: ~/.claude/.env)')
    parser.add_argument('--sessions', type=Path,
                        default=Path.home() / '.claude' / 'projects',
                        help='Sessions directory (default: ~/.claude/projects)')
    parser.add_argument('--name',
                        help='[simple mode] filter to a specific env var name')
    parser.add_argument('--pattern', action='append', dest='patterns', default=[],
                        metavar='RE',
                        help='Additional regex pattern (repeatable). '
                             'Use group(1) to capture the secret value.')
    parser.add_argument('--context', type=int, default=1, metavar='N',
                        help='Lines of context around each match (default: 1)')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON (machine-readable)')
    parser.add_argument('--no-mask', action='store_true',
                        help='Show full token values (default: mask)')
    parser.add_argument('--days', type=int, default=None, metavar='N',
                        help='Only scan sessions from last N days')
    parser.add_argument('--sandbox', action='store_true',
                        help='Also crawl running Docker sandbox containers')

    args = parser.parse_args()

    env = load_env(args.env)
    do_mask = not args.no_mask

    extra_patterns = []
    for pat_str in args.patterns:
        try:
            extra_patterns.append(re.compile(pat_str))
        except re.error as e:
            print(f"Invalid pattern '{pat_str}': {e}", file=sys.stderr)
            sys.exit(1)

    if not args.simple and not args.complex:
        # default to complex
        args.complex = True

    if args.simple:
        if not env and not extra_patterns:
            print(f"Warning: .env file not found or empty at {args.env}", file=sys.stderr)
        return run_simple(
            env=env,
            sessions_dir=args.sessions,
            name_filter=args.name,
            days=args.days,
            context_lines=args.context,
            do_mask=do_mask,
            as_json=args.json,
            extra_patterns=extra_patterns,
        )
    else:
        return run_complex(
            env=env,
            sessions_dir=args.sessions,
            days=args.days,
            context_lines=args.context,
            do_mask=do_mask,
            as_json=args.json,
            extra_patterns=extra_patterns,
        )


if __name__ == '__main__':
    sys.exit(main())
