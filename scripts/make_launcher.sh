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

# The bundle executable: resolve the repo (the .app sits in the repo root) and
# open Terminal running the launch script. Using the app's own location keeps it
# working no matter where the repo is cloned.
cat > "$MACOS/launcher" <<'EOF'
#!/bin/bash
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
open -a Terminal "$REPO/scripts/start_kb.sh"
EOF
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
