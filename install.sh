#!/usr/bin/env bash
# My Claude — one-command installer.
# Downloads the app to ~/.my-claude, sets up a local Python env, and launches it.
# Everything runs on your machine. Nothing is uploaded automatically.
set -e

BASE="${MY_CLAUDE_BASE:-https://my-claude.pages.dev}"
DIR="$HOME/.my-claude"
# reuse the port from an earlier install so bookmarks keep working
PORT="${1:-$(cat "${XDG_CONFIG_HOME:-$HOME/.config}/my-claude/port" 2>/dev/null || echo 8766)}"

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
# a server left running from an earlier install must stop before its folder is replaced
launchctl bootout "gui/$(id -u)/dev.myclaude.server" 2>/dev/null || true
# bootout returns before the old server has exited; wait so its port is free again
for _ in $(seq 1 20); do launchctl print "gui/$(id -u)/dev.myclaude.server" >/dev/null 2>&1 || break; sleep 0.5; done
# keep the caches and preferences an older version stored inside the app folder, so the next start is fast
if [ "$(uname)" = "Darwin" ]; then CACHE="$HOME/Library/Caches/my-claude"; else CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/my-claude"; fi
DATA="${XDG_CONFIG_HOME:-$HOME/.config}/my-claude"
mkdir -p "$CACHE" "$DATA"
for f in "$DIR"/.mc_*; do [ -e "$f" ] && [ ! -e "$CACHE/$(basename "$f")" ] && mv "$f" "$CACHE/"; done
[ -f "$DIR/config.json" ] && [ ! -f "$DATA/config.json" ] && mv "$DIR/config.json" "$DATA/"
rm -rf "$DIR"
mv "$TMP/my-claude-dashboard" "$DIR"
rm -rf "$TMP"
chmod +x "$DIR/run.sh"

if [ "$(uname)" != "Darwin" ] || [ ! -f "$DIR/macos/setup_mac_app.sh" ]; then
  say "Installing and starting it up…"
  say "It reads ~/.claude on this machine only and runs on localhost. Ctrl+C to stop."
  exec "$DIR/run.sh" "$PORT"
fi

# macOS: a real app plus a login item, so you never need this command again
say "Setting up (one time, about 20 seconds)…"
"$DIR/run.sh" --setup-only
PORT="$("$DIR/.venv/bin/python" - "$PORT" <<'PY'
import socket, sys
start = int(sys.argv[1])
for p in range(start, start + 40):
    s = socket.socket()
    try: s.bind(("127.0.0.1", p)); print(p); break
    except OSError: pass
    finally: s.close()
else: print(start)
PY
)"
chmod +x "$DIR"/macos/*.sh
bash "$DIR/macos/setup_mac_app.sh" "$DIR" "$PORT"
say "Starting My Claude…"
bash "$DIR/macos/launcher.sh"
printf "\n  \033[32m✓\033[0m \033[1mMy Claude is installed.\033[0m\n\n"
printf "     Open it any time from \033[1mApplications\033[0m, Spotlight or the Dock: \033[1mMy Claude\033[0m.\n"
printf "     It starts on its own when you log in, at http://127.0.0.1:%s\n" "$PORT"
printf "     To remove it: ~/.my-claude/macos/uninstall.sh\n\n"
