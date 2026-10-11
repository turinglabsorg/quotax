#!/usr/bin/env bash
# Installs the Quotax GNOME Shell extension (with its Python backend) and the `quotax` command.
#   scripts/install.sh              install or update
#   scripts/install.sh --uninstall  remove the extension and the command (linked accounts are kept)
set -euo pipefail

UUID=quotax@turinglabs.org
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TARGET=${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID
COMMAND=$HOME/.local/bin/quotax

# Adds or removes the extension in org.gnome.shell enabled-extensions, keeping every other entry.
set_enabled() {
    python3 - "$UUID" "$1" <<'PY'
import ast, subprocess, sys
uuid, enable = sys.argv[1], sys.argv[2] == "1"
raw = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], capture_output=True, text=True, check=True).stdout
current = ast.literal_eval(raw.strip().removeprefix("@as "))
wanted = current + [uuid] if enable and uuid not in current else [item for item in current if enable or item != uuid]
if wanted != current:
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(wanted)], check=True)
PY
}

if [[ ${1:-} == --uninstall ]]; then
    gnome-extensions disable "$UUID" 2>/dev/null || true
    set_enabled 0 || true
    rm -rf "$TARGET" "$COMMAND"
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/turinglabs.quotax"
    echo "Quotax removed. Linked accounts are still in ~/.config/quotax and ~/.local/share/quotax."
    exit 0
fi

# gnome or wayland. QUOTAX_SURFACE wins, then --gnome/--wayland, then the session.
surface=${QUOTAX_SURFACE:-}
case "${1:-}" in
    --gnome) surface=gnome; shift ;;
    --wayland) surface=wayland; shift ;;
esac
if [[ -z $surface ]]; then
    desktop="${XDG_CURRENT_DESKTOP:-} ${XDG_SESSION_DESKTOP:-}"
    if [[ -n ${OMARCHY_PATH:-} || -d /usr/share/omarchy || $desktop == *Hyprland* ]]; then
        surface=wayland
    else
        surface=gnome
    fi
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Quotax needs Python 3.10 or later." >&2; exit 1; }

install_command() {
    local backend="$1"
    mkdir -p "$(dirname "$COMMAND")"
    cat > "$COMMAND" <<EOF
#!/bin/sh
PYTHONPATH="$backend\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -B -m quotax "\$@"
EOF
    chmod +x "$COMMAND"
}

if [[ $surface == wayland ]]; then
    plugin="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/turinglabs.quotax"
    rm -rf "$plugin"
    mkdir -p "$plugin/backend"
    cp -a "$ROOT"/omarchy/. "$plugin"/
    cp -a "$ROOT/quotax" "$plugin/backend/"
    find "$plugin" -name __pycache__ -prune -exec rm -rf {} +
    install_command "$plugin/backend"
    if command -v omarchy-shell >/dev/null; then
        omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
    fi
    if command -v omarchy >/dev/null; then
        omarchy plugin enable turinglabs.quotax --section right >/dev/null 2>&1 || true
    fi
    echo "Installed the Quotax bar widget and $COMMAND."
    echo "Link an account with: quotax link claude"
    exit 0
fi

command -v glib-compile-schemas >/dev/null || { echo "glib-compile-schemas is missing (package libglib2.0-bin)." >&2; exit 1; }

rm -rf "$TARGET"
mkdir -p "$TARGET/backend"
cp -r "$ROOT"/extension/. "$TARGET"/
cp -r "$ROOT/quotax" "$TARGET/backend/"
find "$TARGET" -name __pycache__ -prune -exec rm -rf {} +
glib-compile-schemas "$TARGET/schemas"

install_command "$TARGET/backend"

set_enabled 1
if [[ $(gsettings get org.gnome.shell disable-user-extensions) == true ]]; then
    echo "Note: user extensions are disabled in GNOME (org.gnome.shell disable-user-extensions)."
fi

echo "Installed $UUID and $COMMAND."
if gnome-extensions info "$UUID" 2>/dev/null | grep -q -E 'State: (ACTIVE|ENABLED)'; then
    echo "GNOME Shell keeps running the previous version until you log out and back in."
else
    echo "Log out and back in to load it: GNOME Shell on Wayland only discovers new extensions at login."
fi
