#!/usr/bin/env bash
# Removes My Claude: the login item, the app, the program folder. Keeps your data
# (~/.config/my-claude: names, tags, backups, memory history) unless you pass --all.
LABEL="dev.myclaude.server"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Applications/My Claude.app" "$HOME/.my-claude"
[ "$1" = "--all" ] && rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/my-claude"
echo "My Claude removed.$([ "$1" = "--all" ] || echo " Your data in ~/.config/my-claude was kept; run with --all to remove it too.")"
