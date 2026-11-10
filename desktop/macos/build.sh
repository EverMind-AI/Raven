#!/bin/sh
# Build ~/Applications/Raven.app from these sources. Re-runnable.
# Run from anywhere; all paths are relative to this script.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${RAVEN_DESKTOP_APP:-$HOME/Applications/Raven.app}"
CACHE="$HERE/.modulecache"
mkdir -p "$CACHE" "$(dirname "$APP")" "$HERE/build"

echo "[1/4] compile native window"
/usr/bin/swiftc -O -framework Cocoa -framework WebKit \
    -module-cache-path "$CACHE" \
    -o "$HERE/build/RavenDesktop" "$HERE/RavenDesktop.swift"

echo "[2/4] build icon from the raven mark"
# The mark ships with the web frontend in this repository. Do not keep a
# second copy under desktop/: the PR asset gate rejects .svg outside the
# frontend trees, and one source of truth is enough.
SVG="$HERE/../../ui-web/src/assets/raven-mark.svg"
if [ ! -f "$SVG" ]; then
    SVG="$HERE/Resources/raven-mark.svg"   # standalone checkout fallback
fi
if [ ! -f "$SVG" ]; then
    echo "raven-mark.svg not found" >&2
    exit 1
fi

OFFICIAL="$HERE/build/AppIcon.png"
if [ ! -f "$OFFICIAL" ]; then
    /usr/bin/swiftc -O -framework Cocoa -framework WebKit \
        -module-cache-path "$CACHE" -o "$HERE/build/render_svg" "$HERE/render_svg.swift"
    "$HERE/build/render_svg" "$SVG" "$OFFICIAL.raw.png"
    /usr/bin/sips -z 1024 1024 "$OFFICIAL.raw.png" --out "$OFFICIAL" >/dev/null
fi
ICONSET="$HERE/build/Raven.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
for px in 16 32 128 256 512; do
    /usr/bin/sips -z "$px" "$px" "$OFFICIAL" --out "$ICONSET/icon_${px}x${px}.png" >/dev/null
    d=$((px * 2))
    /usr/bin/sips -z "$d" "$d" "$OFFICIAL" --out "$ICONSET/icon_${px}x${px}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$HERE/build/Raven.icns"

echo "[3/4] assemble bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/scripts"
cp "$HERE/build/RavenDesktop" "$APP/Contents/MacOS/RavenDesktop"
cp "$HERE/build/Raven.icns" "$APP/Contents/Resources/Raven.icns"
cp "$HERE/scripts/launch.sh" "$APP/Contents/Resources/scripts/launch.sh"
chmod +x "$APP/Contents/Resources/scripts/launch.sh"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Raven</string>
    <key>CFBundleDisplayName</key><string>Raven</string>
    <key>CFBundleIdentifier</key><string>local.raven.desktop</string>
    <key>CFBundleExecutable</key><string>RavenDesktop</string>
    <key>CFBundleIconFile</key><string>Raven.icns</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSSupportsAutomaticTermination</key><false/>
    <key>NSAppTransportSecurity</key>
    <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

echo "[4/4] codesign (ad-hoc)"
/usr/bin/codesign --force --deep --sign - "$APP" 2>/dev/null || true
echo "done: $APP"
