#!/usr/bin/env bash
# Makes My Claude feel like a Mac app:
#   1. a login item (LaunchAgent) that keeps the server running on 127.0.0.1 and restarts it if it stops
#   2. ~/Applications/My Claude.app, which opens the dashboard (and wakes the server first if needed)
# Usage: setup_mac_app.sh <app dir> <port>      Undo: <app dir>/macos/uninstall.sh
set -e
DIR="$1"; PORT="$2"
LABEL="dev.myclaude.server"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
# the main Applications folder (what Finder and Launchpad show) when it's writable, else your own
if [ -w /Applications ]; then APP="/Applications/My Claude.app"; else APP="$HOME/Applications/My Claude.app"; fi
rm -rf "$HOME/Applications/My Claude.app"    # an earlier install put it here
DATA="${XDG_CONFIG_HOME:-$HOME/.config}/my-claude"
LOG="$HOME/Library/Logs/my-claude.log"
mkdir -p "$HOME/Library/LaunchAgents" "$DATA" "$HOME/Library/Logs"
[ -w /Applications ] || mkdir -p "$HOME/Applications"
echo "$PORT" > "$DATA/port"
sed "s/__PORT__/$PORT/" "$DIR/macos/starting.html" > "$DATA/starting.html"

# 1. login item. PATH is set because launchd starts with a bare one, and the app calls git, gh,
#    security (keychain) and osascript (notifications).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/run.sh</string><string>--serve</string><string>$PORT</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PLIST
# bootout returns before the old job is gone; bootstrapping too early fails with error 5
for _ in $(seq 1 20); do launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break; sleep 0.5; done
for _ in 1 2 3 4 5; do launchctl bootstrap "gui/$(id -u)" "$AGENT" 2>/dev/null && break; sleep 1; done
launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || { echo "Could not start the login item. See ~/Library/Logs/my-claude.log"; exit 1; }

# 2. the app. Built with osacompile: macOS on this version refuses an app whose main program is a
#    plain shell script (LaunchServices error -10669), but runs an AppleScript applet, which then
#    runs launcher.sh. Re-signed locally (ad hoc) after the icon and name are changed.
rm -rf "$APP"
osacompile -o "$APP" -e 'do shell script quoted form of (POSIX path of (path to home folder)) & ".my-claude/macos/launcher.sh"'
cp "$DIR/macos/AppIcon.icns" "$APP/Contents/Resources/applet.icns"
# osacompile also writes Assets.car with the default script icon, and macOS prefers it over applet.icns
rm -f "$APP/Contents/Resources/Assets.car"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$APP/Contents/Info.plist" 2>/dev/null || true
PB=/usr/libexec/PlistBuddy
$PB -c "Set :CFBundleName My Claude" "$APP/Contents/Info.plist" 2>/dev/null || $PB -c "Add :CFBundleName string My Claude" "$APP/Contents/Info.plist"
$PB -c "Add :CFBundleIdentifier string dev.myclaude.app" "$APP/Contents/Info.plist" 2>/dev/null || $PB -c "Set :CFBundleIdentifier dev.myclaude.app" "$APP/Contents/Info.plist"
codesign --force --deep -s - "$APP" >/dev/null 2>&1
touch "$APP"   # so Finder and Spotlight pick up the new icon
