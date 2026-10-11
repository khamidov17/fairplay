#!/bin/bash
# Build FairPlay.app (student exam app).   cd ~/code/proctora/mac && bash build.sh
set -euo pipefail
cd "$(dirname "$0")"
APP="Proctora"; BUNDLE="FairPlay.app"   # executable keeps its internal name

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
  <key>CFBundleName</key><string>FairPlay</string>
  <key>CFBundleDisplayName</key><string>FairPlay</string>
  <key>CFBundleIdentifier</key><string>ai.proctora.student</string>
  <key>CFBundleVersion</key><string>0.4</string>
  <key>CFBundleShortVersionString</key><string>0.4</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>$APP</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict>
    <key>NSAllowsArbitraryLoads</key><true/>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict></plist>
PLIST

# Accessibility permission is tied to the code signature: sign with the
# self-signed "Proctora Dev" certificate so the grant survives rebuilds.
if security find-identity -p codesigning 2>/dev/null | grep -q "Proctora Dev"; then
  echo "==> Signing with 'Proctora Dev'..."
  codesign --force --deep --sign "Proctora Dev" "$BUNDLE"
else
  echo "==> Ad-hoc signing (Accessibility will be asked again after each rebuild)..."
  codesign --force --deep --sign - "$BUNDLE"
fi
echo "==> Done: $BUNDLE"
