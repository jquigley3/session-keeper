#!/usr/bin/env python3
"""
sk-tokens.py — scrape and scrub Claude Code session history for tokens and secrets.

Modes:
  --simple    Check ~/.claude/.env for known names/values, then find matches in
              session chat history. Answers: "where did I use this token?"
  --complex   Find potential tokens in chat that are NOT already in ~/.claude/.env.
              Answers: "what secrets have I typed into chat that I haven't catalogued?"
  --scrub     Replace token values in session files. Combine with --simple or
              --complex to select which tokens to target. Defaults to --complex.

Scrub modes (--scrub-mode):
  1  Keep the token prefix (ghp_, sk-ant-, etc.) and the last 4 characters;
     replace the middle with xxx.  Example: ghp_ABCDEFxxx2345
  2  Keep the token prefix only; replace everything else with xx.
     Example: ghp_xx
  3  Replace the entire token with xxx.
     Example: xxx

Usage:
  python3 sk-tokens.py [--simple | --complex]
  python3 sk-tokens.py --simple --name MY_TOKEN
  python3 sk-tokens.py --complex --pattern 'ghp_[a-zA-Z0-9]{36}'
  python3 sk-tokens.py --complex --json
  python3 sk-tokens.py --scrub --scrub-mode 1
  python3 sk-tokens.py --scrub --scrub-mode 2 --simple --name GITHUB_TOKEN
  python3 sk-tokens.py --scrub --scrub-mode 3 --remove-from-env --dry-run

Options:
  --simple            Search .env for known tokens, find them in session history
  --complex           Find potential tokens NOT in .env (default mode)
  --scrub             Scrub found tokens from session files (and optionally .env)
  --scrub-mode N      Scrub replacement style: 1 (prefix+xxx+last4), 2 (prefix+xx),
                      3 (xxx). Default: 1
  --remove-from-env   Also scrub matching tokens from the .env file
  --dry-run           Show what would be changed without writing anything
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
import tempfile
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

# Known token prefixes, sorted longest-first for greedy matching
_KNOWN_PREFIXES = sorted([
    'github_pat_',
    'sk-ant-oat01-', 'sk-ant-api03-', 'sk-ant-',
    'sk_live_', 'sk_test_', 'pk_live_', 'pk_test_', 'rk_live_', 'rk_test_',
    'xoxb-', 'xoxp-', 'xoxa-', 'xoxr-',
    'ghp_', 'gho_', 'ghu_', 'ghs_',
    'AKIA',
    'SG.',
], key=len, reverse=True)


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


# ── scrub helpers ─────────────────────────────────────────────────────────────

def detect_prefix(value: str) -> str:
    """Return the longest known token prefix that value starts with, or ''."""
    for prefix in _KNOWN_PREFIXES:
        if value.startswith(prefix):
            return prefix
    return ''


def make_scrub_replacement(raw_value: str, mode: int) -> str:
    """
    Compute the scrubbed replacement for a token value.

    mode 1: <prefix>xxx<last-4-of-full-value>   e.g. ghp_xxx2345
    mode 2: <prefix>xx                           e.g. ghp_xx
    mode 3: xxx                                  always
    """
    if mode == 3:
        return 'xxx'

    prefix = detect_prefix(raw_value)

    if mode == 1:
        tail = raw_value[-4:] if len(raw_value) > len(prefix) + 4 else ''
        return f'{prefix}xxx{tail}'
    else:  # mode == 2
        return f'{prefix}xx'


def scrub_file_content(content: str, replacements: dict[str, str]) -> tuple[str, int]:
    """
    Apply token replacements to raw file content.
    Returns (new_content, total_replacement_count).

    Note: tokens use only URL-safe characters that don't require JSON escaping,
    so plain str.replace on raw content is safe for all JSONL files in practice.
    """
    new_content = content
    count = 0
    for raw, scrubbed in replacements.items():
        occurrences = new_content.count(raw)
        if occurrences:
            new_content = new_content.replace(raw, scrubbed)
            count += occurrences
    return new_content, count


def scrub_jsonl_file(path: Path, replacements: dict[str, str], dry_run: bool) -> int:
    """
    Replace token values in a JSONL session file.
    Returns the number of replacements made (0 means file unchanged).
    Uses an atomic write (tmp → rename) to avoid partial writes.
    """
    try:
        content = path.read_text(errors='replace')
    except OSError:
        return 0

    new_content, count = scrub_file_content(content, replacements)
    if count > 0 and not dry_run:
        tmp = path.with_suffix('.tmp')
        try:
            tmp.write_text(new_content, errors='replace')
            tmp.rename(path)
        except OSError as e:
            print(f"  warning: could not write {path}: {e}", file=sys.stderr)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return count


def scrub_env_file(env_path: Path, values_to_scrub: set[str], dry_run: bool) -> int:
    """
    For each line in .env whose value matches a token being scrubbed,
    replace the value with [SCRUBBED]. Returns count of lines modified.
    """
    if not env_path.exists() or not values_to_scrub:
        return 0

    lines = env_path.read_text(errors='replace').splitlines(keepends=True)
    new_lines = []
    count = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#') or '=' not in stripped:
            new_lines.append(line)
            continue
        name, _, value = stripped.partition('=')
        value = value.strip().strip('"').strip("'")
        if value in values_to_scrub:
            new_lines.append(f'{name.strip()}=[SCRUBBED]\n')
            count += 1
        else:
            new_lines.append(line)

    if count > 0 and not dry_run:
        tmp = env_path.with_suffix('.tmp')
        try:
            tmp.write_text(''.join(new_lines), errors='replace')
            tmp.rename(env_path)
        except OSError as e:
            print(f"  warning: could not write {env_path}: {e}", file=sys.stderr)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    return count


# ── shared hit collection ──────────────────────────────────────────────────────

def _collect_complex_hits(
    env: dict[str, str],
    sessions_dir: Path,
    days: int | None,
    extra_patterns: list,
) -> list[dict]:
    """
    Collect all token hits in complex mode (tokens NOT in .env).
    Returns hits with raw (unmasked) values in 'raw' field.
    Deduplicates by (label, raw_value).
    """
    known_values = set(env.values()) - {''}
    patterns = BUILTIN_PATTERNS + [(pat, 'custom') for pat in extra_patterns]

    hits = []
    seen: set[tuple] = set()

    for jsonl_path, proj_dir in iter_sessions(sessions_dir, days):
        lines = extract_text_lines(jsonl_path)
        if not lines:
            continue

        for lineno, role, text in lines:
            for regex, label in patterns:
                for m in regex.finditer(text):
                    try:
                        raw = m.group(1)
                    except IndexError:
                        raw = m.group(0)

                    raw = raw.strip()
                    if not raw or raw.lower() in _NOISE_VALUES:
                        continue

                    context_window = text[max(0, m.start()-30):m.end()+10]
                    if label == 'hex-40' and _GIT_SHA_CONTEXT.search(context_window):
                        continue

                    if raw in known_values:
                        continue

                    if label in ('hex-40', 'named-credential', 'auth-header') and len(raw) < 12:
                        continue

                    key = (label, raw)
                    if key in seen:
                        continue
                    seen.add(key)

                    hits.append({
                        'session': jsonl_path.stem,
                        'project': decode_project_dir(proj_dir),
                        'jsonl': str(jsonl_path),
                        'line': lineno,
                        'role': role,
                        'label': label,
                        'raw': raw,
                        'raw_length': len(raw),
                        'context': _get_context(text, m.start(), m.end(), 1),
                    })

    return hits


def _collect_simple_hits(
    env: dict[str, str],
    sessions_dir: Path,
    name_filter: str | None,
    days: int | None,
    extra_patterns: list,
) -> list[dict]:
    """
    Collect all token hits in simple mode (tokens from .env found in chat).
    Returns hits with raw values in 'raw' field.
    """
    targets = {k: v for k, v in env.items() if not name_filter or k == name_filter}

    searches = []
    for name, value in targets.items():
        if value:
            searches.append((name, 'value', re.compile(re.escape(value)), value))
    for pat in extra_patterns:
        searches.append(('--pattern', 'custom', pat, None))

    hits = []

    for jsonl_path, proj_dir in iter_sessions(sessions_dir, days):
        lines = extract_text_lines(jsonl_path)
        if not lines:
            continue

        for lineno, role, text in lines:
            for env_name, match_type, regex, known_raw in searches:
                m = regex.search(text)
                if not m:
                    continue
                raw = known_raw if known_raw else m.group(0)
                hits.append({
                    'session': jsonl_path.stem,
                    'project': decode_project_dir(proj_dir),
                    'jsonl': str(jsonl_path),
                    'line': lineno,
                    'role': role,
                    'env_name': env_name,
                    'match_type': match_type,
                    'raw': raw,
                    'raw_length': len(raw),
                    'context': _get_context(text, m.start(), m.end(), 1),
                })
                break

    return hits


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
    if not env:
        print("No entries found in .env file.", file=sys.stderr)
        return 1

    targets = {k: v for k, v in env.items() if not name_filter or k == name_filter}
    if not targets:
        print(f"No entry named '{name_filter}' in .env.", file=sys.stderr)
        return 1

    searches = []
    for name, value in targets.items():
        searches.append((name, 'value', re.compile(re.escape(value)) if value else None))
        searches.append((name, 'name', re.compile(r'\b' + re.escape(name) + r'\b')))
    for pat in extra_patterns:
        searches.append(('--pattern', 'custom', pat))

    hits = []

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
                break

    if as_json:
        print(json.dumps(hits, indent=2))
        return 0

    if not hits:
        print("No matches found in session history.")
        return 0

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
    hits = _collect_complex_hits(env, sessions_dir, days, extra_patterns)

    if as_json:
        display_hits = [
            {**{k: v for k, v in h.items() if k != 'raw'},
             'matched': mask_value(h['raw']) if do_mask else h['raw']}
            for h in hits
        ]
        print(json.dumps(display_hits, indent=2))
        return 0

    if not hits:
        print("No uncatalogued tokens found in session history.")
        return 0

    by_label: dict[str, list] = defaultdict(list)
    for h in hits:
        by_label[h['label']].append(h)

    print(f"\nFound {len(hits)} potential uncatalogued secret(s):\n")

    for label, group in sorted(by_label.items()):
        print(f"  [{label}]  {len(group)} unique value(s)")
        print('  ' + '─' * 58)
        for h in group:
            display = mask_value(h['raw']) if do_mask else h['raw']
            print(f"    {h['project']}")
            print(f"    session {h['session'][:8]}  line {h['line']}  [{h['role']}]  len={h['raw_length']}")
            print(f"    value: {display}")
            if h['context']:
                for ctx_line in h['context'].splitlines():
                    print(f"      {ctx_line}")
            print()

    print("Consider adding these to ~/.claude/.env with descriptive names.")
    return 0


# ── scrub mode ────────────────────────────────────────────────────────────────

def run_scrub(
    use_simple: bool,
    env: dict[str, str],
    env_path: Path,
    sessions_dir: Path,
    name_filter: str | None,
    days: int | None,
    extra_patterns: list,
    scrub_mode: int,
    dry_run: bool,
    remove_from_env: bool,
) -> int:
    """
    Find tokens (via simple or complex mode) and scrub them from session files.
    In dry-run mode, shows what would change without writing.
    """
    # Collect raw token values
    if use_simple:
        if not env:
            print("No entries found in .env file.", file=sys.stderr)
            return 1
        hits = _collect_simple_hits(env, sessions_dir, name_filter, days, extra_patterns)
    else:
        hits = _collect_complex_hits(env, sessions_dir, days, extra_patterns)

    if not hits:
        print("No tokens found to scrub.")
        return 0

    # Build replacements map: raw_value → scrubbed_value
    raw_values: set[str] = {h['raw'] for h in hits}
    replacements: dict[str, str] = {
        raw: make_scrub_replacement(raw, scrub_mode)
        for raw in raw_values
    }

    # Show plan
    mode_desc = {
        1: 'prefix + xxx + last-4',
        2: 'prefix + xx',
        3: 'xxx',
    }[scrub_mode]

    prefix = '  [DRY RUN] ' if dry_run else '  '
    print(f"\nScrub mode {scrub_mode}: {mode_desc}")
    print(f"{'Dry run — no files will be modified.' if dry_run else 'Writing changes.'}\n")
    print(f"  Tokens to scrub: {len(replacements)}")
    for raw, scrubbed in sorted(replacements.items(), key=lambda kv: kv[0]):
        print(f"  {mask_value(raw)}  →  {scrubbed}")
    print()

    # Group hits by file for efficient processing
    by_file: dict[str, set[str]] = defaultdict(set)
    for h in hits:
        by_file[h['jsonl']].add(h['raw'])

    # Scrub session files
    total_files = 0
    total_replacements = 0

    for jsonl_path_str, file_raw_values in sorted(by_file.items()):
        jsonl_path = Path(jsonl_path_str)
        file_replacements = {r: replacements[r] for r in file_raw_values if r in replacements}
        count = scrub_jsonl_file(jsonl_path, file_replacements, dry_run)
        if count > 0:
            total_files += 1
            total_replacements += count
            rel = jsonl_path.name[:8]
            print(f"{prefix}{'would scrub' if dry_run else 'scrubbed'}  {rel}…  "
                  f"({count} replacement{'s' if count != 1 else ''})"
                  f"  {jsonl_path.parent.name[:40]}")

    print(f"\n  Session files: {total_files} modified, {total_replacements} replacement(s) total")

    # Scrub .env
    if remove_from_env:
        env_count = scrub_env_file(env_path, raw_values, dry_run)
        if env_count > 0:
            print(f"{prefix}{'would update' if dry_run else 'updated'}  {env_path}  "
                  f"({env_count} value{'s' if env_count != 1 else ''} scrubbed → [SCRUBBED])")
        else:
            print(f"  .env: no matching values found")

    if dry_run:
        print("\n  Run without --dry-run to apply changes.")

    return 0


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_context(text: str, start: int, end: int, n_lines: int) -> str:
    """Extract N lines of context around a match position in a multi-line text."""
    if n_lines <= 0:
        return ''
    lines = text.splitlines()
    pos = 0
    match_line_idx = 0
    for i, line in enumerate(lines):
        if pos + len(line) >= start:
            match_line_idx = i
            break
        pos += len(line) + 1

    lo = max(0, match_line_idx - n_lines)
    hi = min(len(lines), match_line_idx + n_lines + 1)
    return '\n'.join(lines[lo:hi])


def crawl_sandbox_sessions(sessions_dir: Path, days: int | None):
    """
    Extend session iteration with sessions from running Docker sandbox containers.
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
        description='Scrape and scrub Claude Code session history for tokens and secrets.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument('--simple', action='store_true',
                          help='Find .env tokens in session history')
    mode_grp.add_argument('--complex', action='store_true',
                          help='Find uncatalogued tokens not in .env (default)')

    parser.add_argument('--scrub', action='store_true',
                        help='Scrub found tokens from session files')
    parser.add_argument('--scrub-mode', type=int, choices=[1, 2, 3], default=1,
                        metavar='N',
                        help='1=prefix+xxx+last4 (default), 2=prefix+xx, 3=xxx')
    parser.add_argument('--remove-from-env', action='store_true',
                        help='Also scrub matching token values from .env')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would change without writing')

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
                        help='Additional regex pattern (repeatable).')
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
        args.complex = True

    if args.scrub:
        return run_scrub(
            use_simple=args.simple,
            env=env,
            env_path=args.env,
            sessions_dir=args.sessions,
            name_filter=args.name,
            days=args.days,
            extra_patterns=extra_patterns,
            scrub_mode=args.scrub_mode,
            dry_run=args.dry_run,
            remove_from_env=args.remove_from_env,
        )

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
