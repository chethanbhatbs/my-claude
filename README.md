# My Claude

A private, local dashboard for everything Claude Code keeps on your machine: your skills, MCP
connectors, plugins, token usage and cost, and your full conversation history.

**Website:** https://chethanbhatbs.github.io/my-claude/

![My Claude dashboard](assets/overview.png)

## Run it (one command)

```bash
curl -fsSL https://chethanbhatbs.github.io/my-claude/install.sh | bash
```

That's it. This downloads the app, sets up a small local environment (one-time, ~20 seconds), and
opens the dashboard at `http://127.0.0.1:8766` in your browser.

**Needs:** Python 3.9+ on macOS or Linux.

## Your data stays on your machine

It runs only on `127.0.0.1`, makes no external calls, and reads the `~/.claude` folder on your own
computer. Nothing is uploaded. You share the tool, not your data.

## What you get

- **Overview** — everything at a glance: skills, conversations, tokens, and disk use.
- **Usage** — what you spend, by day, model, and project.
- **Skills** — every skill, how often you use it, and which ones you never touch.
- **MCP & Plugins** — your connectors, with a one-click health check.
- **History** — search past conversations and pick up where you left off.
- **System** — your settings, hooks, and the memory Claude carries between sessions.

## Try it first

Want to look before you install? Open the [live demo](https://chethanbhatbs.github.io/my-claude/demo.html)
(sample data, nothing real).

## Prefer to install manually?

Download [`my-claude-dashboard.zip`](https://chethanbhatbs.github.io/my-claude/my-claude-dashboard.zip),
unzip it, and run `./run.sh`.

---

Built with Claude Code.
