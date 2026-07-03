# My Claude

A private, **local** dashboard for everything Claude Code keeps on your machine - skills, MCP
connectors, plugins, token usage and cost, and your full conversation history - in one place you can
actually read.

**Website:** https://chethanbhatbs.github.io/my-claude/ · **Live demo:** https://chethanbhatbs.github.io/my-claude/demo.html

![My Claude dashboard](assets/overview.png)

## Install (one command)

```bash
curl -fsSL https://chethanbhatbs.github.io/my-claude/install.sh | bash
```

Paste that into your terminal. It downloads the app, sets up an isolated Python environment
(one-time, ~20 seconds), and opens the dashboard at `http://127.0.0.1:8766` - or the next free port
if that one's busy.

**Needs:** Python 3.9+ on macOS or Linux. Inspect the script first:
[`install.sh`](https://github.com/chethanbhatbs/my-claude/blob/main/install.sh).

## Your data never leaves your machine

It binds to `127.0.0.1` only, makes **no external calls**, and reads the `~/.claude` folder on your
own computer. Nothing is uploaded. Secrets in MCP configs are auto-redacted before they're shown.
You share the tool, not your data.

## What you get

- **Overview** - skills, conversations, token spend, and disk use at a glance.
- **Usage** - cost and tokens by day, model, and project, with cache-savings.
- **Skills** - every skill, how often you use it, and which ones you never touch; search across all
  of them, read any `SKILL.md`, and push to GitHub or share as a `.zip`.
- **MCP & Plugins** - your connectors (global and per-project) with a one-click health check.
- **History** - full-text search across every transcript with per-conversation cost; resume in
  Claude or export to Markdown.
- **System** - permissions, hooks, `CLAUDE.md`, and the memory Claude carries between sessions.

Connect GitHub straight from the app (device-code flow via your `gh` CLI - no tokens stored),
copy any code block, and switch between light and dark themes.

## Try it first

Open the [live demo](https://chethanbhatbs.github.io/my-claude/demo.html) - the real UI with sample
data, nothing from your machine.

## Prefer to install manually?

Download [`my-claude-dashboard.zip`](https://chethanbhatbs.github.io/my-claude/my-claude-dashboard.zip),
unzip it, and run `./run.sh`.

## License

MIT.

---

Built with Claude Code.
