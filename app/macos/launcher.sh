#!/bin/bash
# The program inside My Claude.app: make sure the server answers, then open the dashboard.
DATA="${XDG_CONFIG_HOME:-$HOME/.config}/my-claude"
PORT="$(cat "$DATA/port" 2>/dev/null || echo 8766)"
URL="http://127.0.0.1:$PORT/p/overview"
LABEL="dev.myclaude.server"
up(){ /usr/bin/curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/p/overview"; }
APPDIR="$HOME/.my-claude"; PY="$APPDIR/.venv/bin/python"
# already open: bring that window to the front instead of opening a second one
WPID="$(cat "$DATA/window.pid" 2>/dev/null)"
if [ -n "$WPID" ] && kill -0 "$WPID" 2>/dev/null && /bin/ps -o command= -p "$WPID" | /usr/bin/grep -q "macos/window.py"; then
  touch "$DATA/window.raise"; exit 0
fi
# its own window when pywebview is installed; the window waits for the server itself
if "$PY" -c "import webview" >/dev/null 2>&1; then
  up || /bin/launchctl kickstart "gui/$(id -u)/$LABEL" 2>/dev/null
  WINBIN="$APPDIR/MyClaudeWindow.app/Contents/MacOS/My Claude"
  if [ -x "$WINBIN" ]; then   # runs as "My Claude" (name and icon) instead of "Python"
    SP="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
    PYTHONPATH="$SP" nohup "$WINBIN" "$APPDIR/macos/window.py" "$PORT" >>"$HOME/Library/Logs/my-claude.log" 2>&1 &
  else
    nohup "$PY" "$APPDIR/macos/window.py" "$PORT" >>"$HOME/Library/Logs/my-claude.log" 2>&1 &
  fi
  exit 0
fi
# fallback: the browser
if up; then /usr/bin/open "$URL"; exit 0; fi
# Not answering yet: make sure the login item is running, then show a page that opens the dashboard
# once it's ready. The first start after an install can take minutes on a large ~/.claude.
if ! /bin/launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  /usr/bin/osascript -e 'display dialog "My Claude is not set up to run. Run the install command again." buttons {"OK"} default button 1 with title "My Claude" with icon caution' >/dev/null
  exit 1
fi
/bin/launchctl kickstart "gui/$(id -u)/$LABEL" 2>/dev/null
/usr/bin/open "$DATA/starting.html"
