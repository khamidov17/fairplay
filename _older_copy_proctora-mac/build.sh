#!/bin/bash
# Build ProctoraLockdown.app.  cd ~/code/proctora-mac && ./build.sh
set -euo pipefail
APP="ProctoraLockdown"; BUNDLE="$APP.app"

echo "==> Compiling (release)..."
swift build -c release
BIN=".build/release/$APP"; [ -f "$BIN" ] || { echo "no binary at $BIN"; exit 1; }

echo "==> Assembling $BUNDLE..."
rm -rf "$BUNDLE"; mkdir -p "$BUNDLE/Contents/MacOS"
cp "$BIN" "$BUNDLE/Contents/MacOS/$APP"
cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$APP</string>
  <key>CFBundleDisplayName</key><string>Proctora Lockdown</string>
  <key>CFBundleIdentifier</key><string>ai.proctora.lockdown</string>
  <key>CFBundleVersion</key><string>0.2</string>
  <key>CFBundleShortVersionString</key><string>0.2</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>$APP</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

# Accessibility permission is tied to the code signature. An ad-hoc signature
# changes on every build, so macOS would ask again each time. A self-signed
# "Proctora Dev" certificate keeps it stable. Create it once in Keychain Access
# (Certificate Assistant > Create a Certificate, type: Code Signing).
if security find-identity -v -p codesigning 2>/dev/null | grep -q "Proctora Dev"; then
  echo "==> Signing with 'Proctora Dev' (stable identity)..."
  codesign --force --deep --sign "Proctora Dev" "$BUNDLE"
else
  echo "==> Ad-hoc signing (Accessibility permission will be asked again after each rebuild)..."
  codesign --force --deep --sign - "$BUNDLE"
fi
echo "==> Done: $BUNDLE"
