---
name: token-manager
description: Manage tokens and secrets for Claude Code sessions. Use this skill whenever the user mentions an API key, token, secret, password, credential, or wants to store/retrieve/audit sensitive values. Triggers on phrases like "save my token", "add this API key", "where did I use that token", "find secrets in my sessions", "audit my credentials", "store this key", "what tokens do I have", or any time the user pastes or references a sensitive value. Also use when the user wants to know if a secret has leaked into chat history.
compatibility:
  tools: [Bash]
user-invocable: true
homepage: https://github.com/jquigley3/session-keeper
---

# Token Manager

Helps you store, reference, and audit tokens and secrets across Claude Code sessions.

## The convention

All tokens, API keys, and secrets live in `~/.claude/.env` in `NAME=value` format:

```
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...
MY_GITEA_TOKEN=abc123...
OPENAI_KEY=sk-...
```

Even when an env var isn't technically required (e.g., a one-time token you'll use in a command), add it with a descriptive name. The name is a record; the value is a reference. Future-you will thank current-you.

**When a user shares a token value or asks you to use one:**
1. Ask if they'd like it added to `~/.claude/.env`
2. Suggest a clear `SCREAMING_SNAKE_CASE` name
3. Reference it by name going forward (e.g., `$GITHUB_TOKEN` in commands)

**When a token is already in .env:** Reference it by name, never paste the value.

## Adding a token

If the user asks to save or add a token:

```bash
# Append to ~/.claude/.env (create if it doesn't exist)
echo 'MY_TOKEN_NAME=the-value' >> ~/.claude/.env
```

Or open the file for them to edit:
```bash
# Show current .env contents (masked)
python3 <skill-dir>/scripts/sk-tokens.py --simple --json | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('\n'.join(set(h['env_name'] for h in d)))"
```

## Auditing session history

The bundled script `scripts/sk-tokens.py` can audit session history in two modes.

### Simple mode — find known tokens in chat

Searches session history for token values and names already in `~/.claude/.env`:

```bash
python3 <skill-dir>/scripts/sk-tokens.py --simple
```

Filter to a specific token:
```bash
python3 <skill-dir>/scripts/sk-tokens.py --simple --name GITHUB_TOKEN
```

### Complex mode — find uncatalogued secrets

Scans session chat for values that look like tokens but are NOT in `~/.claude/.env`:

```bash
python3 <skill-dir>/scripts/sk-tokens.py --complex
```

Limit to recent sessions:
```bash
python3 <skill-dir>/scripts/sk-tokens.py --complex --days 30
```

### Agent-injectable patterns

Pass `--pattern` to add custom regex. The pattern can use a capture group `(...)` to extract just the secret value:

```bash
# Find Gitea tokens (40-char hex) in chat
python3 <skill-dir>/scripts/sk-tokens.py --complex \
  --pattern '[0-9a-f]{40}'

# Find values after "Authorization: token "
python3 <skill-dir>/scripts/sk-tokens.py --complex \
  --pattern '(?i)Authorization:\s+token\s+([^\s]+)'
```

### JSON output (for agents)

```bash
python3 <skill-dir>/scripts/sk-tokens.py --complex --json
```

Returns an array of hit objects with: `session`, `project`, `line`, `role`, `label`, `matched` (masked), `context`.

### Scrubbing tokens from session history

Once you've found tokens in session history, you can scrub them in place. The script rewrites JSONL files atomically, replacing token values with a masked form. Three modes:

| Mode | Example output | Use when |
|------|---------------|----------|
| 1 (default) | `ghp_xxx2345` | You want to know which token it was |
| 2 | `ghp_xx` | You want to keep the type identifier only |
| 3 | `xxx` | Full redaction |

```bash
# Dry run first — see what would change
python3 <skill-dir>/scripts/sk-tokens.py --scrub --dry-run

# Scrub with mode 1 (keep prefix + last 4)
python3 <skill-dir>/scripts/sk-tokens.py --scrub --scrub-mode 1

# Scrub a specific known token (simple mode) with full redaction
python3 <skill-dir>/scripts/sk-tokens.py --scrub --simple --name GITHUB_TOKEN --scrub-mode 3

# Scrub and also clean the value out of .env
python3 <skill-dir>/scripts/sk-tokens.py --scrub --scrub-mode 2 --remove-from-env
```

`--remove-from-env` replaces the value in `.env` with `[SCRUBBED]` so you retain the variable name as a record.

## Docker sandboxes

```bash
python3 <skill-dir>/scripts/sk-tokens.py --complex --sandbox
```

Also crawls running Docker sandbox containers.

## .env format

```
# Comments are supported
NAME=value
NAME="value with spaces"
NAME='single quoted'
```

Empty values and lines without `=` are ignored. Values are stripped of surrounding quotes.

`<skill-dir>` is the directory containing this SKILL.md — typically `~/.claude/skills/token-manager/`.
