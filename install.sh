#!/bin/sh
# Install Route C on the Mac: RouteC.app (camera identity), launchd agent, `routec` command.
#   ./install.sh               install or update (keeps RouteC.app, so the Camera grant survives)
#   ./install.sh --rebuild-app rebuild the launcher too (macOS will ask for Camera permission again)
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
APP="$HOME/Applications/RouteC.app"
SUPPORT="$HOME/Library/Application Support/RouteC"
AGENT="$HOME/Library/LaunchAgents/com.ekorre.routec.plist"
LABEL=com.ekorre.routec
UIDN=$(id -u)

say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || die "run this on the Mac (not in a Linux VM / device_bash)"
[ -x "$PY" ] || die "missing $PY (the Python 3.13 framework build with pyserial + Pillow)"
"$PY" -c "import serial, PIL" 2>/dev/null || die "$PY lacks pyserial or Pillow: $PY -m pip install pyserial pillow"
"$PY" -c "import Vision, Quartz" 2>/dev/null || say "warning: PyObjC Vision missing; OCR will run DEGRADED (tesseract)"
[ -x /opt/homebrew/bin/ffmpeg ] || die "missing /opt/homebrew/bin/ffmpeg (brew install ffmpeg)"

mkdir -p "$SUPPORT" "$HOME/Library/Logs/RouteC" "$HOME/Applications" "$HOME/Library/LaunchAgents"

# 1. The launcher app. Built once; rebuilding changes its signature and loses the Camera grant.
if [ ! -x "$APP/Contents/MacOS/RouteC" ] || [ "${1:-}" = "--rebuild-app" ]; then
  say "building RouteC.app"
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS"
  cp "$ROOT/macos/Info.plist" "$APP/Contents/Info.plist"
  xcrun clang -fobjc-arc -O2 -framework Foundation -framework AVFoundation \
    -o "$APP/Contents/MacOS/RouteC" "$ROOT/macos/launcher.m"
  codesign --force --sign - --identifier "$LABEL" "$APP"
else
  say "RouteC.app already built (kept, so its Camera grant stays)"
fi

# 2. What the launcher runs. Lives outside the app so code updates never touch its signature.
cat > "$SUPPORT/start.sh" <<SH
#!/bin/sh
cd "$ROOT"
export PYTHONPATH="$ROOT"
exec "$PY" -m routec.server
SH
chmod 755 "$SUPPORT/start.sh"

# 3. The command line.
BIN=/opt/homebrew/bin
[ -w "$BIN" ] || BIN="$HOME/.local/bin"
mkdir -p "$BIN"
ln -sf "$ROOT/bin/routec" "$BIN/routec"
say "routec -> $BIN/routec"

# 4. The launchd agent (starts at login, restarts if it dies).
sed "s#__HOME__#$HOME#g" "$ROOT/macos/com.ekorre.routec.plist" > "$AGENT"
launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
# A daemon started by hand (routec start --here) holds the port; stop it first.
"$ROOT/bin/routec" stop >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UIDN" "$AGENT"
launchctl kickstart -k "gui/$UIDN/$LABEL"

# 5. Camera permission, asked for by RouteC itself.
RES="$SUPPORT/camera-status"
rm -f "$RES"
open -W -g -n -a "$APP" --args --camera-status-file "$RES" || true
AUTH=$(cat "$RES" 2>/dev/null || echo unknown)
say "camera permission for RouteC: $AUTH"
if [ "$AUTH" != authorized ]; then
  say ""
  say "ACTION NEEDED (once): allow RouteC to use the camera."
  say "  A macOS prompt may be on screen now: click Allow. Otherwise open"
  say "  System Settings > Privacy & Security > Camera and switch RouteC on, then run: routec restart"
  open -g -n -a "$APP" --args --request-camera-file "$RES" || true
fi

say ""
for i in 1 2 3 4 5 6 7 8 9 10; do
  "$ROOT/bin/routec" status >/dev/null 2>&1 && break
  sleep 1
done
"$ROOT/bin/routec" status || true
say ""
say "Live view (what Claude sees): $("$ROOT/bin/routec" view)"
