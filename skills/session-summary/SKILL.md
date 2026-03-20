---
name: session-summary
description: Crawl and summarize Claude Code session history to answer "what am I working on?" Use this skill whenever the user asks what they've been doing, wants a summary of their recent Claude Code activity, asks which projects are active, wants to know where they left off, or asks to review sessions across projects. Trigger on phrases like "what am I working on", "summarize my sessions", "what have I been doing lately", "catch me up", "show my projects", or any question about recent Claude Code activity.
compatibility:
  tools: [Bash]
---

# Session Summary

Answers "what am I working on and where?" by crawling Claude Code's session history, reading key dialogues, and synthesizing a narrative summary.

## How it works

Claude Code stores every session as a JSONL file at `~/.claude/projects/<encoded-path>/<session-id>.jsonl`. The bundled script `scripts/sk-sessions.py` crawls these files and extracts metadata and dialogue.

## Requirements

This skill requires **Bash tool access** to run the bundled Python script. If Bash is not available in the current session, ask the user to allow it before proceeding — without it, session history cannot be read.

## Step-by-step

### 1. List all sessions

```bash
python3 <skill-dir>/scripts/sk-sessions.py
```

This prints a table: session ID (8-char prefix), working dir, branch, last message time, message count, token usage.

`<skill-dir>` is the directory containing this SKILL.md file. You can find it by checking where this skill is installed — typically `~/.claude/skills/session-summary/` or the path shown when the skill was invoked.

### 2. Identify the active projects

Group sessions by working directory. Focus on projects with recent activity (last 7–14 days) and meaningful message counts (>10 user messages). Skip stub sessions (2–4 messages).

### 3. Dump dialogues for key sessions

For each active project, dump the latest session's dialogue:

```bash
python3 <skill-dir>/scripts/sk-sessions.py --dump <session-id-prefix>
```

Read enough to understand: what was the user trying to do, what did they actually do, and where did things end up. For large projects with many sessions, dump the 1–2 most recent.

### 4. Synthesize the summary

Write a narrative summary grouped by project. For each:

- **One-line goal**: what is this project/effort about
- **Recent activity**: what happened in the last session (2–3 bullets)
- **Status**: active / stalled / completed / unclear

End with a brief "across all projects" observation if there's a clear theme or priority signal (e.g., most tokens spent on X, Y hasn't been touched in a week).

## Tone

Be specific — name the actual files, features, decisions, and blockers. Avoid filler like "you've been working on various things". The user wants to orient themselves quickly, so lead with the most active/recent work.

## Docker sandboxes

If the user asks to include sandbox sessions, add `--sandbox`:

```bash
python3 <skill-dir>/scripts/sk-sessions.py --sandbox
```

This also crawls running Docker sandbox containers for their own session histories.

## Installing this skill

Copy or symlink the `session-summary/` directory to `~/.claude/skills/`:

```bash
cp -r /path/to/session-keeper/skills/session-summary ~/.claude/skills/
```

Or for a live link (picks up updates automatically):

```bash
ln -s /path/to/session-keeper/skills/session-summary ~/.claude/skills/session-summary
```
