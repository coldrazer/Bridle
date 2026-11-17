#!/usr/bin/env bash
# Builds Bridle.app (release) with the Python engine sources bundled in Resources/engine and
# Sparkle.framework in Frameworks.
# Usage: scripts/build_app.sh [output-dir]   (default: ./dist)
#
# Optional environment for release builds (scripts/release.sh sets these):
#   BRIDLE_FEED_URL            Sparkle appcast URL, e.g. https://example.com/bridle/appcast.xml
#   BRIDLE_SPARKLE_PUBLIC_KEY  EdDSA public key printed by Sparkle's generate_keys
#   BRIDLE_BUILD_NUMBER        CFBundleVersion (defaults to a timestamp)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
APP="$OUT/Bridle.app"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/engine/bridle_engine/__init__.py")"
BUILD="${BRIDLE_BUILD_NUMBER:-$(date +%Y%m%d%H%M)}"

echo "Building Swift app (release)…"
(cd "$ROOT/app" && swift build -c release --arch arm64)
BIN_DIR="$(cd "$ROOT/app" && swift build -c release --arch arm64 --show-bin-path)"

rm -rf "$APP"   # always rebuilt from scratch; nothing stale survives
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine" "$APP/Contents/Frameworks"
cp "$BIN_DIR/Bridle" "$APP/Contents/MacOS/Bridle"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/engine/bridle_engine" "$APP/Contents/Resources/engine/"
cp "$ROOT/engine/requirements.txt" "$ROOT/engine/requirements-extras.txt" \
   "$ROOT/engine/requirements-lock.txt" "$ROOT/engine/requirements-extras-lock.txt" \
   "$ROOT/engine/requirements-speech-lock.txt" "$APP/Contents/Resources/engine/"
cp "$ROOT/app/Sources/Bridle/Resources/"{AppIcon.icns,BridleLogo.png,BridleMark.png} "$APP/Contents/Resources/"
# Bundled models must match their download manifest exactly (every listed file present,
# SHA-256 equal). A partial bundle would ship a model that cannot load, so fail the build.
verify_bundle() {
  /usr/bin/env python3 - "$1" <<'PY' || { echo "error: $1 does not match its .bridle.json manifest; re-run its fetch script" >&2; exit 1; }
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
manifest = json.loads((root / ".bridle.json").read_text())
bad = []
for name, digest in manifest["file_hashes"].items():
    f = root / name
    if not f.is_file():
        bad.append(f"missing {name}")
        continue
    h = hashlib.sha256()
    with f.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != digest:
        bad.append(f"hash mismatch {name}")
for line in bad:
    print(f"  {root.name}: {line}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
}

# Aria's bundled helper model (scripts/fetch_aria.sh populates assets/Aria).
if [ -f "$ROOT/assets/Aria/config.json" ]; then
  verify_bundle "$ROOT/assets/Aria"
  mkdir -p "$APP/Contents/Resources/Aria"
  cp -R "$ROOT/assets/Aria/." "$APP/Contents/Resources/Aria/"
else
  echo "warning: assets/Aria is missing; this build will not include Aria (run scripts/fetch_aria.sh)" >&2
fi
# Bundled Onyx Whisper speech model for Aria's voice input.
if [ -f "$ROOT/assets/OnyxWhisper/config.json" ]; then
  verify_bundle "$ROOT/assets/OnyxWhisper"
  mkdir -p "$APP/Contents/Resources/OnyxWhisper"
  cp -R "$ROOT/assets/OnyxWhisper/." "$APP/Contents/Resources/OnyxWhisper/"
else
  echo "warning: assets/OnyxWhisper is missing; Aria voice input will need a downloaded speech model (run scripts/fetch_onyx.sh)" >&2
fi

# Sparkle.framework: SwiftPM leaves it next to the binary (or in the artifacts cache).
SPARKLE="$(find "$BIN_DIR" -maxdepth 2 -name Sparkle.framework -print -quit 2>/dev/null || true)"
if [ -z "$SPARKLE" ]; then
  SPARKLE="$(find "$ROOT/app/.build/artifacts" -path '*macos*' -name Sparkle.framework -print -quit 2>/dev/null || true)"
fi
if [ -n "$SPARKLE" ]; then
  ditto "$SPARKLE" "$APP/Contents/Frameworks/Sparkle.framework"
else
  echo "warning: Sparkle.framework not found; the app will not launch without it" >&2
fi

SPARKLE_KEYS=""
if [ -n "${BRIDLE_FEED_URL:-}" ] && [ -n "${BRIDLE_SPARKLE_PUBLIC_KEY:-}" ]; then
  SPARKLE_KEYS="  <key>SUFeedURL</key><string>${BRIDLE_FEED_URL}</string>
  <key>SUPublicEDKey</key><string>${BRIDLE_SPARKLE_PUBLIC_KEY}</string>
  <key>SUEnableAutomaticChecks</key><true/>"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.bridle.app</string>
  <key>CFBundleName</key><string>Bridle</string>
  <key>CFBundleDisplayName</key><string>Bridle</string>
  <key>CFBundleExecutable</key><string>Bridle</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${BUILD}</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSServices</key><array><dict>
    <key>NSMenuItem</key><dict><key>default</key><string>Ask Bridle</string></dict>
    <key>NSMessage</key><string>askBridle</string>
    <key>NSPortName</key><string>Bridle</string>
    <key>NSSendTypes</key><array><string>public.utf8-plain-text</string></array>
  </dict></array>
  <key>NSMicrophoneUsageDescription</key><string>Record your voice when you choose Talk, then transcribe it on this Mac.</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>Bridle serves your local models to other devices on your network when sharing is turned on, and finds other Bridle servers.</string>
  <key>NSBonjourServices</key><array><string>_bridle._tcp</string></array>
${SPARKLE_KEYS}
</dict>
</plist>
PLIST

if [ -z "${BRIDLE_SKIP_ADHOC_SIGN:-}" ]; then
  # Ad-hoc signature so Gatekeeper lets a locally built app run. release.sh signs with Developer ID.
  codesign --force --deep --sign - "$APP"
  codesign --force --sign - --entitlements "$ROOT/app/Bridle.entitlements" "$APP"
  codesign --verify --deep --strict "$APP" || { echo "error: built app fails codesign verification" >&2; exit 1; }
fi
echo "Built $APP ($VERSION, build $BUILD)"
