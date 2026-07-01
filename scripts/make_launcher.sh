#!/bin/bash
# Build "Start Consulting KB.app" — a double-clickable macOS launcher that opens
# Terminal and runs scripts/start_kb.sh. The .app is gitignored (machine-local),
# so run this once per machine to (re)create it:
#
#     ./scripts/make_launcher.sh
#
# Then double-click "Start Consulting KB.app" in Finder (or drag it to the Dock).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$REPO/Start Consulting KB.app"
MACOS="$APP/Contents/MacOS"

rm -rf "$APP"
mkdir -p "$MACOS"

# The bundle executable asks Terminal (via AppleScript) to open a window and run
# the launch script. osascript is far more reliable than `open -a Terminal
# file.sh`, which often opens the script in an editor or does nothing.
#
# The repo path is baked in ABSOLUTE at build time, so the .app keeps working
# after you move it (Desktop, Dock, /Applications) — a relative-to-bundle path
# would break the moment the app leaves the repo folder.
{
    echo '#!/bin/bash'
    echo "REPO=\"$REPO\""
    cat <<'EOF'
SCRIPT="$REPO/scripts/start_kb.sh"
if [ ! -f "$SCRIPT" ]; then
    /usr/bin/osascript -e "display alert \"Consulting KB\" message \"Launch script not found at $SCRIPT. Re-run scripts/make_launcher.sh in the repo.\""
    exit 1
fi
/usr/bin/osascript <<OSA
tell application "Terminal"
    activate
    do script "clear; bash \"$SCRIPT\""
end tell
OSA
EOF
} > "$MACOS/launcher"
chmod +x "$MACOS/launcher"
chmod +x "$REPO/scripts/start_kb.sh"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Start Consulting KB</string>
    <key>CFBundleDisplayName</key><string>Start Consulting KB</string>
    <key>CFBundleIdentifier</key><string>com.consultkb.launcher</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

echo "✓ Built: $APP"
echo "  Double-click it in Finder, or drag it into your Dock."
echo "  (First launch: right-click → Open, to clear the Gatekeeper prompt.)"
