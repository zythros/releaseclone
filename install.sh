#!/bin/sh
# Install releaseclone for the current user.
#
#   ./install.sh               install (or update) to ~/.local
#   ./install.sh --prefix DIR  install to DIR/lib/releaseclone, command in DIR/bin
#   ./install.sh --uninstall   remove the program (your config and downloads are kept)
set -eu

PREFIX="$HOME/.local"
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --prefix=*) PREFIX="${1#--prefix=}"; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,7s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
done

APP_DIR="$PREFIX/lib/releaseclone"  # not share/: that holds the state file
BIN="$PREFIX/bin/releaseclone"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/releaseclone"
FILES="releaseclone.py releaseclone_tui.py"

if [ "$UNINSTALL" = 1 ]; then
    rm -f "$BIN"
    # Only delete what we installed, never the whole directory.
    for f in $FILES config.example.toml; do rm -f "$APP_DIR/$f"; done
    rm -rf "$APP_DIR/__pycache__"
    rmdir "$APP_DIR" 2>/dev/null || true
    echo "Removed $BIN and $APP_DIR"
    echo "Kept your config ($CONFIG_DIR) and state (${XDG_DATA_HOME:-$HOME/.local/share}/releaseclone)."
    exit 0
fi

# --- requirements
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found. Install Python 3.11 or newer." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "error: Python 3.11+ is required (found $(python3 -V 2>&1))." >&2
    exit 1
fi
if ! python3 -c 'import curses' 2>/dev/null; then
    echo "warning: Python's curses module is missing, so 'releaseclone tui' won't work." >&2
fi

# --- install
SRC="$(cd "$(dirname "$0")" && pwd)"
for f in $FILES config.example.toml; do
    [ -f "$SRC/$f" ] || { echo "error: $f not found next to install.sh" >&2; exit 1; }
done
mkdir -p "$APP_DIR" "$PREFIX/bin"
install -m 755 "$SRC/releaseclone.py" "$APP_DIR/releaseclone.py"
install -m 644 "$SRC/releaseclone_tui.py" "$APP_DIR/releaseclone_tui.py"
install -m 644 "$SRC/config.example.toml" "$APP_DIR/config.example.toml"
ln -sf "$APP_DIR/releaseclone.py" "$BIN"
echo "Installed releaseclone to $APP_DIR"
echo "Command: $BIN"

if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    mkdir -p "$CONFIG_DIR"
    cp "$SRC/config.example.toml" "$CONFIG_DIR/config.toml"
    echo "Created $CONFIG_DIR/config.toml from the example. Edit it, or use 'releaseclone tui'."
else
    echo "Kept existing config $CONFIG_DIR/config.toml"
fi

# --- optional extras
command -v notify-send >/dev/null 2>&1 || echo "note: notify-send not found; desktop notifications are disabled (install libnotify)."
command -v xdg-open >/dev/null 2>&1 || echo "note: xdg-open not found; the TUI's 'open folder' will show the path instead (install xdg-utils)."

case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) echo "note: $PREFIX/bin is not on your PATH. Add it:"
       echo "        fish:     fish_add_path $PREFIX/bin"
       echo "        bash/zsh: echo 'export PATH=\"$PREFIX/bin:\$PATH\"' >> ~/.profile" ;;
esac

echo
echo "Next: run 'releaseclone tui', or schedule 'releaseclone run' with cron (see README)."
