#!/bin/bash
# Build FairPlay.app (student exam app).   cd ~/code/fairplay/mac && bash build.sh
#
# Dev build (default): local server, public dev key, dev overrides (ServerURL default,
# safety auto-unlock), http allowed for local addresses. Pair with FAIRPLAY_DEV=1 on the server.
#
# Release build (audit 2026-10-12 fix order #2, #3, #6):
#   FAIRPLAY_BUILD=release \
#   FAIRPLAY_SERVER=https://exam.example.edu \
#   FAIRPLAY_APP_KEY=<same value as the server's FAIRPLAY_APP_KEY> \
#   FAIRPLAY_PINS=<base64 sha256 of a CA cert, comma-separated; see pin.sh>   (optional, recommended)
#   FAIRPLAY_SIGN_ID="Developer ID Application: Your Uni (TEAMID)"           (optional)
#   FAIRPLAY_NOTARY_PROFILE=<notarytool keychain profile>                     (optional)
#   bash build.sh
# Release builds: https only, no ATS exceptions, no UserDefaults/env overrides, no
# safety unlock, hardened runtime; notarized when a profile is given.
set -euo pipefail
cd "$(dirname "$0")"
APP="Proctora"; BUNDLE="FairPlay.app"   # executable keeps its internal name
# Audit M-19: one version source (Config.swift).
VERSION="$(sed -n 's/.*static let version = "\(.*\)".*/\1/p' Sources/Proctora/Config.swift | head -1)"
[ -n "$VERSION" ] || { echo "could not read the version from Config.swift"; exit 1; }
MODE="${FAIRPLAY_BUILD:-dev}"
DEV_KEY="fairplay-dev-app-key-not-secret"

case "$MODE" in
  dev)
    SERVER="${FAIRPLAY_SERVER:-http://127.0.0.1:8000}"
    KEY="${FAIRPLAY_APP_KEY:-$DEV_KEY}"
    SWIFT_FLAGS=(-Xswiftc -DFAIRPLAY_DEV)
    ;;
  release)
    SERVER="${FAIRPLAY_SERVER:?set FAIRPLAY_SERVER=https://your-exam-server}"
    KEY="${FAIRPLAY_APP_KEY:?set FAIRPLAY_APP_KEY (same as the server)}"
    [[ "$SERVER" == https://* ]] || { echo "Release builds need an https:// server"; exit 1; }
    [[ "$KEY" != "$DEV_KEY" && ${#KEY} -ge 32 ]] || { echo "FAIRPLAY_APP_KEY must be a private value of 32+ characters"; exit 1; }
    # Audit M-04: without pins any CA an admin installs can read and change the traffic
    # (and fake the hand-in). Release builds must pin the server's CA (see pin.sh).
    [[ -n "${FAIRPLAY_PINS:-}" ]] || { echo "Release builds need FAIRPLAY_PINS (run: bash pin.sh https://your-exam-server)"; exit 1; }
    SWIFT_FLAGS=()
    ;;
  *) echo "FAIRPLAY_BUILD must be dev or release"; exit 1 ;;
esac
PINS="${FAIRPLAY_PINS:-}"
# Dev builds write a readable trace (clicks, errors, lock, quits) to <repo>/logs/events.log.
if [ "$MODE" = dev ]; then
  DEBUG_LOG="${FAIRPLAY_DEBUG_LOG:-$(cd .. && pwd)/logs/events.log}"
  [[ "$DEBUG_LOG" != *'"'* && "$DEBUG_LOG" != *'\'* ]] || { echo "bad FAIRPLAY_DEBUG_LOG path"; exit 1; }
else
  DEBUG_LOG=""
fi
[[ "$SERVER" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$ ]] || { echo "bad FAIRPLAY_SERVER: $SERVER"; exit 1; }
[[ "$KEY" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || { echo "FAIRPLAY_APP_KEY: use letters, digits and ._~+/=- only"; exit 1; }
PIN_LIST=()
IFS=',' read -ra PIN_ARR <<< "$PINS"
for p in "${PIN_ARR[@]:-}"; do
  p="$(echo "$p" | tr -d ' ')"; [ -z "$p" ] && continue
  [[ "$p" =~ ^[A-Za-z0-9+/]{43}=$ ]] || { echo "bad pin (want base64 SHA-256): $p"; exit 1; }
  PIN_LIST+=("$p")
done

# Audit M-14: the source tree is never rewritten. Server, app key, pins and the trace path
# go into Contents/Resources/FairPlayBuild.plist inside the (then signed) app bundle;
# BuildConfig.swift only holds dev defaults. A crash mid-build can't leave a key behind.
if [ -n "${FAIRPLAY_UNIVERSAL:-}" ] || [ "$MODE" = release ]; then
  ARCH_FLAGS=(--arch arm64 --arch x86_64)    # Intel and Apple Silicon (needs full Xcode)
else
  ARCH_FLAGS=()
fi
echo "==> Compiling ($MODE build, server $SERVER${ARCH_FLAGS[*]:+, universal})..."
swift build -c release ${ARCH_FLAGS[@]+"${ARCH_FLAGS[@]}"} ${SWIFT_FLAGS[@]+"${SWIFT_FLAGS[@]}"} || {
  if [ ${#ARCH_FLAGS[@]} -gt 0 ]; then
    echo "Universal build failed (it needs full Xcode, not only the Command Line Tools)."
    echo "Install Xcode, or for a single-architecture test build: FAIRPLAY_UNIVERSAL= bash build.sh"
  fi
  exit 1
}
BIN="$(swift build -c release ${ARCH_FLAGS[@]+"${ARCH_FLAGS[@]}"} --show-bin-path)/$APP"
[ -f "$BIN" ] || { echo "no binary at $BIN"; exit 1; }

echo "==> Assembling $BUNDLE..."
rm -rf "$BUNDLE"; mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cp "$BIN" "$BUNDLE/Contents/MacOS/$APP"
CONF="$BUNDLE/Contents/Resources/FairPlayBuild.plist"
plutil -create xml1 "$CONF"
plutil -insert ServerURL -string "$SERVER" "$CONF"
plutil -insert AppKey -string "$KEY" "$CONF"
plutil -insert DebugLogPath -string "$DEBUG_LOG" "$CONF"
plutil -insert Pins -array "$CONF"
i=0
for p in "${PIN_LIST[@]:-}"; do
  [ -n "$p" ] || continue
  plutil -insert "Pins.$i" -string "$p" "$CONF"; i=$((i + 1))
done
chmod 600 "$CONF"
if [ "$MODE" = dev ]; then
  # Dev: plain http to local addresses only (127.0.0.1, .local, LAN IPs). No arbitrary loads.
  ATS='<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>'
else
  ATS=''   # release: default App Transport Security (https only)
fi
cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>FairPlay</string>
  <key>CFBundleDisplayName</key><string>FairPlay</string>
  <key>CFBundleIdentifier</key><string>ai.proctora.student</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>$APP</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.education</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  $ATS
</dict></plist>
PLIST

if [ -n "${FAIRPLAY_SIGN_ID:-}" ]; then
  echo "==> Signing with '$FAIRPLAY_SIGN_ID' (hardened runtime)..."
  codesign --force --options runtime --timestamp --sign "$FAIRPLAY_SIGN_ID" "$BUNDLE"
  if [ -n "${FAIRPLAY_NOTARY_PROFILE:-}" ]; then
    echo "==> Notarizing..."
    ditto -c -k --keepParent "$BUNDLE" FairPlay.zip
    # Audit M-19: notarytool exits 0 even when Apple says "Invalid"; check the status.
    NOTARY_OUT="$(xcrun notarytool submit FairPlay.zip --keychain-profile "$FAIRPLAY_NOTARY_PROFILE" --wait 2>&1)"
    echo "$NOTARY_OUT"
    echo "$NOTARY_OUT" | grep -q "status: Accepted" || { echo "Notarization was NOT accepted"; rm -f FairPlay.zip; exit 1; }
    xcrun stapler staple "$BUNDLE"
    rm -f FairPlay.zip
  fi
elif security find-identity -p codesigning 2>/dev/null | grep -q "Proctora Dev"; then
  # Accessibility permission is tied to the code signature: the self-signed
  # "Proctora Dev" certificate keeps the grant across rebuilds.
  echo "==> Signing with 'Proctora Dev'..."
  codesign --force --options runtime --sign "Proctora Dev" "$BUNDLE"
else
  echo "==> Ad-hoc signing (Accessibility will be asked again after each rebuild)..."
  codesign --force --options runtime --sign - "$BUNDLE"
fi
[ "$MODE" = release ] && [ -z "${FAIRPLAY_SIGN_ID:-}" ] && \
  echo "WARNING: release build without Developer ID signing/notarization (FAIRPLAY_SIGN_ID)."
echo "==> Done: $BUNDLE ($MODE)"
if [ -n "$DEBUG_LOG" ]; then echo "    Debug trace: $DEBUG_LOG"; fi
