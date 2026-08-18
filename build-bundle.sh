#!/usr/bin/env bash
# Rebuild my-claude-dashboard.zip — the payload install.sh downloads — from the live skill.
#
#   ./build-bundle.sh [path-to-skill]     (default: ~/.claude/skills/My-Claude)
#
# Only the files a user needs to run the app are copied. Personal state (config.json), the venv,
# the on-disk caches and the internal SKILL.md notes are never included. The script refuses to
# build if a credential-shaped string turns up in the payload.
set -euo pipefail

SRC="${1:-$HOME/.claude/skills/My-Claude}"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)"
ZIP="$OUT_DIR/my-claude-dashboard.zip"
NAME="my-claude-dashboard"

[ -f "$SRC/myclaude.py" ] || { echo "No myclaude.py in $SRC"; exit 1; }

STAGE="$(mktemp -d)/$NAME"
mkdir -p "$STAGE"
trap 'rm -rf "$(dirname "$STAGE")"' EXIT

# what ships
cp "$SRC/myclaude.py" "$SRC/run.sh" "$SRC/requirements.txt" "$SRC/requirements-dev.txt" \
   "$SRC/README.md" "$SRC/.gitignore" "$SRC/links.json" "$STAGE/"
cp -R "$SRC/static" "$SRC/templates" "$SRC/tests" "$STAGE/"
chmod +x "$STAGE/run.sh"

# what must never ship
rm -rf "$STAGE"/**/__pycache__ "$STAGE"/__pycache__ "$STAGE"/tests/__pycache__ 2>/dev/null || true
find "$STAGE" -name '.DS_Store' -o -name '*.pyc' -o -name 'config.json' -o -name '.mc_*' | xargs rm -rf 2>/dev/null || true

# refuse to ship anything credential-shaped
if grep -rIlEq 'sk-ant-[A-Za-z0-9]|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|"accessToken"[[:space:]]*:[[:space:]]*"' "$STAGE" 2>/dev/null; then
  echo "Refusing to build: credential-shaped string found in the payload."; exit 1
fi

rm -f "$ZIP"
( cd "$(dirname "$STAGE")" && zip -qr "$ZIP" "$NAME" )

echo "Built $ZIP"
echo "  $(unzip -l "$ZIP" | tail -1 | awk '{print $2}') files · $(du -h "$ZIP" | cut -f1)"
echo "  myclaude.py $(wc -l < "$SRC/myclaude.py" | tr -d ' ') lines · tests $(ls "$SRC/tests"/test_*.py | wc -l | tr -d ' ') file(s)"
