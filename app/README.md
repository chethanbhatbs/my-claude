# My Claude

A private, local dashboard for everything Claude Code keeps on your machine — skills,
MCP connectors, plugins, sessions, token usage & cost, conversation history, and disk footprint.

**Your transcripts never leave your computer.** The app binds to `127.0.0.1` only and reads
`~/.claude` on the machine it runs on. Share the *tool*, not the data — each person runs it and
sees their own Claude.

### The one network call, in full

The Usage page shows your real plan limits (session %, weekly %, per-model %), the same numbers
Claude's own Settings → Usage panel shows. That needs one request to Anthropic, so you should know
exactly what it does:

| | |
|---|---|
| **Endpoint** | `GET https://api.anthropic.com/api/oauth/usage` |
| **Credential** | your Claude Code OAuth token, read from the macOS keychain entry `Claude Code-credentials` — the same entry the `claude` CLI created and uses |
| **Sent** | the token, in an `Authorization: Bearer` header. Nothing else. No transcripts, no prompts, no file names, no telemetry |
| **Stored** | the token is held in memory for 10 minutes and never written to disk. Grep the caches: `.mc_cache.json`, `.mc_usage_cache.json` and `.mc_search.db` contain no credentials (there's a test for it) |
| **When** | when the Usage page is open (on load, then every 60s), and when you press Refresh on that panel. Never on any other page |
| **If you decline** | macOS may ask you to allow keychain access. Say no and nothing breaks: the panel falls back to the copy Claude Code already cached in `~/.claude.json` and labels itself "Claude's local cache" with the age of those numbers |

Everything else in the app — skills, MCP, history, token counts, costs — is computed locally from
files already on your disk, with no network access at all. The "Push skill" feature is opt-in and
shells out to your own `gh` CLI login; no tokens are stored.

## Run it

```bash
./run.sh
```

That's it. First run sets up a local Python environment (~20s), then opens
`http://127.0.0.1:8766` in your browser. Use `./run.sh 9000` to pick another port.

**Requirements:** Python 3.9+ (macOS or Linux). The GitHub "Push skill" feature is optional and
uses your own `gh` CLI login if present — no tokens are ever stored.

## What's inside

| Page | What it shows |
|------|---------------|
| **Overview** | Counts + on-disk size for every part of your install, with a storage breakdown |
| **Token Usage** | Billable/context tokens, estimated cost, cache hit rate, per-day and per-model charts |
| **Skills** | Every skill with its description; filter, search, view, push to GitHub, share as `.zip` |
| **MCP Connectors** | Configured MCP servers (secrets auto-redacted), optional health check |
| **Plugins** | Installed plugins and marketplaces |
| **Sessions** | Projects you've used Claude in; open in Terminal/Finder, resume, view transcripts |
| **Search** | Full-text search across every conversation transcript |
| **Cleanup** | Stale projects (>90 days) and largest caches — suggestions only, you decide |
| **Config** | `settings.json`, `CLAUDE.md`, slash commands |
| **History & Cache** | Full conversation history browser + the caches Claude maintains |
| **Tasks & Plans** | Saved task lists and plans |
| **Sunburst / Graph** | Disk usage as a radial chart or a network |

## Privacy

Conversation transcripts under `~/.claude/projects/` are stored in **plaintext**. This dashboard
shows them so you can review and manage them — don't screen-share or screenshot the Sessions /
History pages without redacting. Nothing is sent anywhere; there is no telemetry.

## Notes

- Light and dark themes (toggle top-right). Dark is the warm "instrument panel" look.
- All figures are read live from disk and cached briefly; hit **Refresh** to re-read.
