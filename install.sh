#!/usr/bin/env bash
# Per-user installer for RSPSDR.
#
#   ./install.sh            install into ~/.local
#   ./install.sh uninstall  remove it again
#
# Nothing is written outside the user's home directory, so no root is needed.
set -euo pipefail

APP="RSPSDR"
SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
APPDIR="$PREFIX/share/rspsdr"
BINDIR="$PREFIX/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
# The icon goes in the theme, not in an absolute path: that is what makes the
# desktop entry, the file manager and the task bar all find it.
ICON_DIR="$PREFIX/share/icons/hicolor/scalable/apps"
# The application used to be called RSP1 Listener.  An install made back then
# has to be cleared away, or the menu shows two entries and the old one keeps
# starting a stale copy of the code -- which is exactly how a fixed bug came to
# look unfixed.  The old *config* directory is deliberately left alone: it holds
# the user's saved channels, and the app adopts it on first run.
OLD_NAME="rsp1-listener"

refresh_menus() {
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" \
            >/dev/null 2>&1 || true
}

uninstall() {
    rm -rf "$APPDIR" "$PREFIX/share/$OLD_NAME"
    rm -f "$BINDIR/rspsdr" "$BINDIR/$OLD_NAME"
    rm -f "$DESKTOP_DIR/rspsdr.desktop" "$DESKTOP_DIR/$OLD_NAME.desktop"
    rm -f "$ICON_DIR/rspsdr.svg"
    refresh_menus
    echo "$APP uninstalled."
}

if [ "${1:-}" = "uninstall" ]; then
    uninstall
    exit 0
fi

echo "$APP -- installing to $PREFIX"

# Check the runtime pieces before copying anything.
missing=()
python3 - <<'PY' || missing+=("python3")
import sys
assert sys.version_info >= (3, 10), "python3 >= 3.10 required"
PY
for mod in SoapySDR numpy scipy PyQt6 pyqtgraph; do
    python3 -c "import $mod" >/dev/null 2>&1 || missing+=("$mod")
done

if [ ${#missing[@]} -gt 0 ]; then
    echo
    echo "Missing Python components: ${missing[*]}"
    echo "Install them with:"
    echo
    echo "  sudo apt install python3-soapysdr python3-pyqtgraph \\"
    echo "                   python3-pyqt6.qtmultimedia python3-numpy python3-scipy"
    echo
    echo "You also need the SDRplay API and the SoapySDR sdrplay module:"
    echo "  sudo apt install soapysdr-module-sdrplay3"
    echo "  (or install the SDRplay API from https://www.sdrplay.com/downloads/)"
    echo
    echo "Re-run ./install.sh once those are present."
    exit 1
fi

# Clear any pre-rename install first.
rm -rf "$PREFIX/share/$OLD_NAME" "$BINDIR/$OLD_NAME" "$DESKTOP_DIR/$OLD_NAME.desktop"

mkdir -p "$APPDIR" "$BINDIR" "$DESKTOP_DIR" "$ICON_DIR"
rm -rf "$APPDIR/rspsdr" "$APPDIR/tests"
cp -r "$SRC/rspsdr" "$APPDIR/"
cp -r "$SRC/tests" "$APPDIR/"
cp "$SRC/rspsdr.svg" "$APPDIR/" 2>/dev/null || true
cp "$SRC/README.md" "$APPDIR/" 2>/dev/null || true
find "$APPDIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

cat > "$BINDIR/rspsdr" <<EOF
#!/bin/sh
# Launcher created by install.sh
exec python3 -m rspsdr "\$@"
EOF
# PYTHONPATH keeps the module importable without touching site-packages.
sed -i "2i PYTHONPATH=\"$APPDIR\${PYTHONPATH:+:\$PYTHONPATH}\"\nexport PYTHONPATH" \
    "$BINDIR/rspsdr"
chmod +x "$BINDIR/rspsdr"

# The name this application had before it was RSPSDR, kept working so that
# anything already pointing at it keeps launching the current build.
ln -sf rspsdr "$BINDIR/$OLD_NAME"

cp "$SRC/rspsdr.desktop" "$DESKTOP_DIR/rspsdr.desktop"
sed -i "s|^Exec=rspsdr|Exec=$BINDIR/rspsdr|" "$DESKTOP_DIR/rspsdr.desktop"
cp "$SRC/rspsdr.svg" "$ICON_DIR/rspsdr.svg"

refresh_menus

echo
echo "Installed."
echo "  launcher : $BINDIR/rspsdr  ($BINDIR/$OLD_NAME still works too)"
echo "  menu item: $DESKTOP_DIR/rspsdr.desktop"
echo "  icon     : $ICON_DIR/rspsdr.svg"
case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *) echo "  note     : $BINDIR is not on your PATH -- log out and back in," ;;
esac
echo
echo "Run it with:  rspsdr"
echo "Diagnostics:  rspsdr --check"
