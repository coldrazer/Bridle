#!/usr/bin/env bash
# Signed, notarized release with a Sparkle update feed.
#
# One-time setup:
#   1. A "Developer ID Application" certificate in your keychain (Apple Developer Program).
#   2. Notary credentials:  xcrun notarytool store-credentials bridle-notary --apple-id YOU --team-id TEAM
#   3. Sparkle keys:        app/.build/artifacts/sparkle/Sparkle/bin/generate_keys   (prints the public key;
#                           the private key stays in your keychain)
#
# Each release:
#   DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)" \
#   NOTARY_PROFILE=bridle-notary \
#   BRIDLE_FEED_URL=https://your.site/bridle/appcast.xml \
#   BRIDLE_SPARKLE_PUBLIC_KEY=<public key> \
#   scripts/release.sh
#
# Output: dist/release/Bridle-<version>.zip and dist/release/appcast.xml. Upload both to the
# location BRIDLE_FEED_URL points at (appcast.xml at that URL, the zip next to it).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Preflight only: report what a signed release still needs, change nothing.
if [ "${1:-}" = "--check" ]; then
  missing=0
  if security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application"; then
    echo "ok      Developer ID Application certificate"
  else
    echo "MISSING Developer ID Application certificate (Apple Developer Program)"; missing=1
  fi
  for var in DEVELOPER_ID NOTARY_PROFILE BRIDLE_FEED_URL BRIDLE_SPARKLE_PUBLIC_KEY; do
    if [ -n "${!var:-}" ]; then echo "ok      $var is set"; else echo "MISSING $var (export it, see header)"; missing=1; fi
  done
  if [ -n "${NOTARY_PROFILE:-}" ] && xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
    echo "ok      notary profile works"
  elif [ -n "${NOTARY_PROFILE:-}" ]; then
    echo "MISSING notary profile '$NOTARY_PROFILE' does not validate"; missing=1
  fi
  if find "$ROOT/app/.build/artifacts" -name generate_appcast -type f -print -quit 2>/dev/null | grep -q .; then
    echo "ok      Sparkle generate_appcast"
  else
    echo "MISSING Sparkle tools (run 'swift package resolve' in app/)"; missing=1
  fi
  [ -f "$ROOT/assets/Aria/config.json" ] && echo "ok      Aria bundle" || { echo "MISSING Aria bundle (scripts/fetch_aria.sh)"; missing=1; }
  [ "$missing" = 0 ] && echo "Release preflight: READY" || echo "Release preflight: NOT READY"
  exit "$missing"
fi

: "${DEVELOPER_ID:?set DEVELOPER_ID to your 'Developer ID Application: …' identity}"
: "${NOTARY_PROFILE:?set NOTARY_PROFILE to a notarytool keychain profile}"
: "${BRIDLE_FEED_URL:?set BRIDLE_FEED_URL to your appcast URL}"
: "${BRIDLE_SPARKLE_PUBLIC_KEY:?set BRIDLE_SPARKLE_PUBLIC_KEY (from generate_keys)}"

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/engine/bridle_engine/__init__.py")"
DIST="$ROOT/dist"
RELEASE="$DIST/release"
APP="$DIST/Bridle.app"
mkdir -p "$RELEASE"

BRIDLE_SKIP_ADHOC_SIGN=1 "$ROOT/scripts/build_app.sh" "$DIST"

echo "Signing with $DEVELOPER_ID (hardened runtime)…"
sign() { codesign --force --timestamp --options runtime --sign "$DEVELOPER_ID" "$@"; }
FW="$APP/Contents/Frameworks/Sparkle.framework"
if [ -d "$FW" ]; then
  # Inside-out: Sparkle's helpers first, then the framework, then the app.
  for item in "$FW/Versions/B/XPCServices/"*.xpc "$FW/Versions/B/Autoupdate" "$FW/Versions/B/Updater.app"; do
    [ -e "$item" ] && sign "$item"
  done
  sign "$FW"
fi
# The app itself carries the entitlements (microphone under the hardened runtime).
sign --entitlements "$ROOT/app/Bridle.entitlements" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -d --entitlements - "$APP" 2>/dev/null | grep -q "com.apple.security.device.audio-input" \
  || { echo "error: signed app is missing the audio-input entitlement" >&2; exit 1; }

echo "Notarizing (this can take a few minutes)…"
ZIP="$RELEASE/Bridle-$VERSION.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$APP"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"     # re-zip with the stapled ticket
spctl --assess --type execute --verbose "$APP"

echo "Generating appcast…"
GEN="$(find "$ROOT/app/.build/artifacts" -name generate_appcast -type f -print -quit)"
if [ -z "$GEN" ]; then
  echo "generate_appcast not found (run 'swift package resolve' in app/ first)" >&2
  exit 1
fi
"$GEN" --download-url-prefix "$(dirname "$BRIDLE_FEED_URL")/" "$RELEASE"
echo
echo "Release ready:"
echo "  $ZIP"
echo "  $RELEASE/appcast.xml  -> upload to $BRIDLE_FEED_URL"
