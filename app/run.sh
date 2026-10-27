#!/usr/bin/env bash
# My Claude — a private, local view of your Claude Code install.
# Reads ~/.claude on THIS machine only. Nothing is uploaded or shared unless you choose to.
#
#   ./run.sh            # start on http://127.0.0.1:8766
#   ./run.sh 9000       # start on a different port
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 is required but not found. Install it, then re-run ./run.sh"
  exit 1
fi

VENV="$DIR/.venv"
if [ ! -x "$VENV/bin/uvicorn" ]; then
  # build the venv + install deps in the background, with a spinner so it never looks frozen
  ( "$PY" -m venv "$VENV" \
    && "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir --upgrade pip \
    && "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir -r "$DIR/requirements.txt" ) &
  SETUP=$!
  FR=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏'); n=0
  while kill -0 "$SETUP" 2>/dev/null; do
    printf '\r  \033[38;5;173m%s\033[0m  setting up (one-time)…' "${FR[$((n % ${#FR[@]}))]}"
    n=$((n+1)); sleep 0.12
  done
  wait "$SETUP" && rc=0 || rc=$?
  printf '\r\033[K'
  [ "${rc:-0}" -ne 0 ] && { printf 'Setup failed — check your internet connection and re-run.\n'; exit 1; }
fi

PORT="${1:-8766}"
# If the port is busy, fall back to the next free one (portable check via the bundled Python).
PORT="$("$VENV/bin/python" - "$PORT" <<'PY'
import socket, sys
start = int(sys.argv[1])
for p in range(start, start + 40):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", p)); print(p); break
    except OSError:
        pass
    finally:
        s.close()
else:
    print(start)
PY
)"
URL="http://127.0.0.1:$PORT/p/overview"
echo "Starting My Claude… (first run indexes your ~/.claude, this can take up to a minute)"
# Wait until the server actually responds, then print a clear link and open the browser.
# We poll instead of a fixed delay so we never open before it's ready. Opening the browser
# is best-effort — the printed link is the source of truth if a browser can't be launched.
(
  # Poll in the background; a single request can take a while (the first render indexes
  # ~/.claude), so use a long per-request timeout instead of timing out mid-index.
  "$VENV/bin/python" - "$PORT" <<'PY' >/dev/null 2>&1 &
import sys, time, urllib.request
url = "http://127.0.0.1:%d/p/overview" % int(sys.argv[1])
for _ in range(90):
    try: urllib.request.urlopen(url, timeout=180); break
    except Exception: time.sleep(1)
PY
  POLL=$!
  # animated spinner so it never looks frozen while indexing
  FR=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏'); n=0
  while kill -0 "$POLL" 2>/dev/null; do
    printf '\r  \033[38;5;173m%s\033[0m  indexing your ~/.claude…' "${FR[$((n % ${#FR[@]}))]}"
    n=$((n+1)); sleep 0.12
  done
  wait "$POLL" 2>/dev/null
  printf '\r\033[K'   # clear the spinner line
  printf '\n  \033[32m✓\033[0m \033[1mMy Claude is running.\033[0m\n\n     Open in your browser:  \033[1;38;5;173m%s\033[0m\n\n     Runs on localhost only · press Ctrl+C here to stop.\n\n' "$URL"
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi ) &

exec "$VENV/bin/uvicorn" myclaude:app --host 127.0.0.1 --port "$PORT" --app-dir "$DIR" --log-level warning --no-access-log
