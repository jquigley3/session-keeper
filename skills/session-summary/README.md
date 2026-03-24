# session-summary

A Claude Code skill that answers **"what am I working on?"** by crawling your local Claude Code session history, reading key dialogues, and synthesizing a narrative summary across all active projects.

## What it does

- Crawls `~/.claude/projects/` for all session JSONL files
- Extracts metadata: working dir, git branch, message counts, token usage, timestamps
- Reads actual dialogue from recent sessions (noise-filtered — strips bash output echoes, tool calls, interrupts)
- Groups by project and writes a narrative: goal, recent activity, status
- Optionally crawls running Docker sandbox containers too (`--sandbox`)

## Install

```bash
# Copy (static)
cp -r /path/to/session-keeper/skills/session-summary ~/.claude/skills/

# Or symlink (picks up updates automatically)
ln -s /path/to/session-keeper/skills/session-summary ~/.claude/skills/session-summary
```

## Usage

Once installed, just ask Claude Code naturally:

- *"what am I working on?"*
- *"catch me up on my projects"*
- *"where did I leave off yesterday?"*
- *"summarize my recent Claude sessions"*

Requires **Bash tool access** — Claude will ask if it's not already allowed.

## Script (standalone)

The bundled script can also be used directly:

```bash
# Session table
python3 scripts/sk-sessions.py

# JSON output
python3 scripts/sk-sessions.py --json

# Dump dialogue for a session
python3 scripts/sk-sessions.py --dump <session-id-prefix>

# Include Docker sandbox sessions
python3 scripts/sk-sessions.py --sandbox
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
python3 test/tier2/run_eval.py
```
