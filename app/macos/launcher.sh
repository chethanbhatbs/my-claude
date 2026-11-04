#!/bin/bash
# The program inside My Claude.app: make sure the server answers, then open the dashboard.
DATA="${XDG_CONFIG_HOME:-$HOME/.config}/my-claude"
PORT="$(cat "$DATA/port" 2>/dev/null || echo 8766)"
URL="http://127.0.0.1:$PORT/p/overview"
LABEL="dev.myclaude.server"
up(){ /usr/bin/curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/p/overview"; }
if up; then /usr/bin/open "$URL"; exit 0; fi
# Not answering yet: make sure the login item is running, then show a page that opens the dashboard
# once it's ready. The first start after an install can take minutes on a large ~/.claude.
if ! /bin/launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  /usr/bin/osascript -e 'display dialog "My Claude is not set up to run. Run the install command again." buttons {"OK"} default button 1 with title "My Claude" with icon caution' >/dev/null
  exit 1
fi
/bin/launchctl kickstart "gui/$(id -u)/$LABEL" 2>/dev/null
/usr/bin/open "$DATA/starting.html"
