# token-manager

A Claude Code skill for storing, referencing, and auditing tokens and secrets across sessions.

## What it does

- Establishes `~/.claude/.env` as the canonical store for all API keys and secrets
- Prompts Claude to name tokens and reference them by name rather than value
- Bundles `sk-tokens.py` — a script that scrapes session history for tokens in two modes:
  - **Simple**: finds known `.env` tokens in chat history
  - **Complex**: finds uncatalogued secrets that look like tokens but aren't in `.env`

## Install

```bash
# Copy (static)
cp -r /path/to/session-keeper/skills/token-manager ~/.claude/skills/

# Or symlink (picks up updates automatically)
ln -s /path/to/session-keeper/skills/token-manager ~/.claude/skills/token-manager
```

## Usage

Once installed, Claude will automatically apply the token management convention. You can also ask directly:

- *"save this API key"*
- *"add my GitHub token to .env"*
- *"find secrets in my session history"*
- *"audit my credentials"*
- *"where did I use that token?"*

## Script (standalone)

```bash
# Find uncatalogued tokens in session history (default)
python3 scripts/sk-tokens.py --complex

# Search for known .env tokens in chat
python3 scripts/sk-tokens.py --simple

# Filter to a specific token name
python3 scripts/sk-tokens.py --simple --name GITHUB_TOKEN

# Last 30 days only
python3 scripts/sk-tokens.py --complex --days 30

# JSON output (machine-readable)
python3 scripts/sk-tokens.py --complex --json

# Custom pattern (for agents)
python3 scripts/sk-tokens.py --complex --pattern 'ghp_[a-zA-Z0-9]{36}'

# Include Docker sandbox sessions
python3 scripts/sk-tokens.py --complex --sandbox

# Show full values (default: masked)
python3 scripts/sk-tokens.py --complex --no-mask
```

## Requirements

- Python 3.8+
- No external dependencies (stdlib only)
- Docker CLI (only if using `--sandbox`)

## Tests

```bash
# Tier 1 — unit tests, no API key, runs in CI
python3 -m unittest discover -s test -p 'test_*.py' -v

# Tier 2 — manual eval against real session history
python3 test/tier2/run_eval_tokens.py
```
