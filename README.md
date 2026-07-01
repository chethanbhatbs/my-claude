# My Claude

A private, local dashboard for everything Claude Code keeps on your machine — skills, MCP
connectors, plugins, token usage & cost, and your full conversation history.

**Your data never leaves your computer.** It binds to `127.0.0.1`, makes zero external calls,
and reads `~/.claude` on the machine it runs on. Share the tool, not the data.

## Run it (one command)

```bash
curl -fsSL https://chethanbhatbs.github.io/my-claude/install.sh | bash
```

That downloads the app to `~/.my-claude`, sets up a local Python environment (one-time, ~20s),
and opens the dashboard at `http://127.0.0.1:8766` in your browser.

**Requirements:** Python 3.9+, macOS or Linux. The optional "push skill to GitHub" feature uses
your existing `gh` CLI login — no tokens are ever stored.

## What's inside

Overview (KPIs, spend trend, cache savings, activity heatmap) · Skills (usage counts, categories,
readable SKILL.md) · MCP & Plugins (with live health checks) · Usage (tokens, cost, per-model /
per-project) · History (full-text search, per-conversation cost, export) · System (permissions,
hooks, CLAUDE.md, memory).

## Privacy

Conversation transcripts under `~/.claude/projects/` are stored in plaintext. This tool shows them
so you can review and manage them locally — don't screen-share or screenshot the History page
without redacting. There is no telemetry and nothing is uploaded.

## Manual install

Prefer not to pipe to bash? Download [`my-claude-dashboard.zip`](./my-claude-dashboard.zip), unzip
it, and run `./run.sh`.

---

Landing page: https://chethanbhatbs.github.io/my-claude/ · Built with Claude Code.
