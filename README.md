# session-keeper

Crawl and summarize Claude Code session history from the host and running Docker sandbox containers.

## Usage

The script lives in the skill bundle at `skills/session-summary/scripts/sk-sessions.py`:

```bash
# List all sessions with metadata
python3 skills/session-summary/scripts/sk-sessions.py

# Also include sessions from running Docker sandbox containers
python3 skills/session-summary/scripts/sk-sessions.py --sandbox

# Dump clean dialogue for a session (by ID prefix)
python3 skills/session-summary/scripts/sk-sessions.py --dump <session-id-prefix>

# Dump unfiltered dialogue (bash echoes, tool results, etc.)
python3 skills/session-summary/scripts/sk-sessions.py --dump <session-id-prefix> --raw

# JSON output for scripting
python3 skills/session-summary/scripts/sk-sessions.py --json
```

## What it does

- Crawls `~/.claude/projects/` for all Claude Code sessions (JSONL files)
- Optionally crawls running Docker sandbox containers for their own session histories
- Extracts per-session metadata: working dir, git branch, timestamps, message counts, token usage
- `--dump` prints the full human-readable dialogue for a session

## Installing the skill

The `skills/session-summary/` directory is a Claude Code skill that answers "what am I working on?" Install it with:

```bash
# Copy (snapshot)
cp -r skills/session-summary ~/.claude/skills/

# Or symlink (picks up updates automatically)
ln -s "$(pwd)/skills/session-summary" ~/.claude/skills/session-summary
```

Once installed, trigger it in any Claude Code session by asking things like:
- *"what am I working on?"*
- *"summarise my recent sessions"*
- *"catch me up across my projects"*

## Session data location

Claude Code stores session history as JSONL files at:

```
~/.claude/projects/<encoded-path>/<session-id>.jsonl
```

Each line is a message entry (user, assistant, system, progress). Docker sandbox containers store their own sessions at `/home/agent/.claude/projects/` inside the container — these are separate from host sessions.
