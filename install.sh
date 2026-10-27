#!/usr/bin/env bash
# My Claude — one-command installer.
# Downloads the app to ~/.my-claude, sets up a local Python env, and launches it.
# Everything runs on your machine. Nothing is uploaded automatically.
set -e

BASE="${MY_CLAUDE_BASE:-https://my-claude.pages.dev}"
DIR="$HOME/.my-claude"
PORT="${1:-8766}"

say(){ printf "\033[38;5;173m▸\033[0m %s\n" "$1"; }

command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required. Install it and re-run."; exit 1; }
command -v curl    >/dev/null 2>&1 || { echo "curl is required."; exit 1; }
command -v unzip   >/dev/null 2>&1 || { echo "unzip is required."; exit 1; }

say "Downloading My Claude…"
TMP="$(mktemp -d)"
# Some networks reset connections to *.pages.dev now and then, so retry, then use the GitHub Pages copy.
FALLBACK="https://chethanbhatbs.github.io/my-claude"
fetch(){ curl -fsSL --retry 5 --retry-all-errors --retry-delay 1 "$1/my-claude-dashboard.zip" -o "$TMP/app.zip"; }
fetch "$BASE" || { [ "$BASE" != "$FALLBACK" ] && say "Retrying from GitHub Pages…" && fetch "$FALLBACK"; } \
  || { echo "Download failed. Check your connection and re-run."; rm -rf "$TMP"; exit 1; }
unzip -oq "$TMP/app.zip" -d "$TMP"
rm -rf "$DIR"
mv "$TMP/my-claude-dashboard" "$DIR"
rm -rf "$TMP"
chmod +x "$DIR/run.sh"

say "Installing and starting it up…"
say "It reads ~/.claude on this machine only and runs on localhost. Ctrl+C to stop."
exec "$DIR/run.sh" "$PORT"
