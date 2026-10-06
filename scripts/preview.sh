#!/usr/bin/env bash
# Renders the extension in a throwaway headless GNOME Shell and saves screenshots.
#   scripts/preview.sh [output-dir]          sample data (default output: build/preview)
#   scripts/preview.sh --live [output-dir]   your linked accounts and real usage (read-only)
# Nothing touches your session: private D-Bus, temporary XDG dirs and keyfile settings.
set -euo pipefail

LIVE=0
if [[ ${1:-} == --live ]]; then
    LIVE=1
    shift
fi
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(realpath -m "${1:-$ROOT/build/preview}")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

EXTENSIONS=$WORK/data/gnome-shell/extensions
mkdir -p "$EXTENSIONS" "$WORK/config/quotax" "$WORK/config/glib-2.0/settings" "$WORK/share/gnome-shell/modes" "$WORK/runtime"
chmod 700 "$WORK/runtime"
mkdir -p "$EXTENSIONS/quotax@turinglabs.org/backend"
cp -r "$ROOT"/extension/. "$EXTENSIONS/quotax@turinglabs.org"/
cp -r "$ROOT/quotax" "$EXTENSIONS/quotax@turinglabs.org/backend/"
glib-compile-schemas "$EXTENSIONS/quotax@turinglabs.org/schemas"
cp -r "$ROOT/scripts/preview/quotax-preview@turinglabs.org" "$EXTENSIONS/"

# Ubuntu's Yaru theme without Ubuntu's own extensions.
cat > "$WORK/share/gnome-shell/modes/quotax-preview.json" <<'EOF'
{
    "parentMode": "user",
    "stylesheetName": "Yaru/gnome-shell.css",
    "colorScheme": "prefer-light",
    "themeResourceName": "theme/Yaru/gnome-shell-theme.gresource",
    "iconsResourceName": "theme/Yaru/gnome-shell-icons.gresource",
    "enabledExtensions": []
}
EOF

SAMPLE=1
if ((LIVE)); then
    SAMPLE=
    cp "${XDG_CONFIG_HOME:-$HOME/.config}/quotax/accounts.json" "$WORK/config/quotax/"
    ln -s "${XDG_DATA_HOME:-$HOME/.local/share}/quotax" "$WORK/data/quotax"
else
cat > "$WORK/config/quotax/accounts.json" <<'EOF'
{"version": 1, "accounts": [
  {"id": "0B6B3E0C-6B83-4C8B-9E0A-1E7A8E6C0001", "provider": "claude", "source": "cli", "email": "name@example.com", "plan": "Team"},
  {"id": "0B6B3E0C-6B83-4C8B-9E0A-1E7A8E6C0004", "provider": "claude", "source": "managed", "email": "work@example.com", "plan": "Max 5x"},
  {"id": "0B6B3E0C-6B83-4C8B-9E0A-1E7A8E6C0002", "provider": "codex", "source": "cli", "email": "name@example.com", "plan": "Plus"},
  {"id": "0B6B3E0C-6B83-4C8B-9E0A-1E7A8E6C0003", "provider": "grok", "source": "managed", "email": "name@example.com", "plan": "SuperGrok"}
]}
EOF
fi

for variant in dark light; do
    scheme=$([[ $variant == dark ]] && echo prefer-dark || echo default)
    cat > "$WORK/config/glib-2.0/settings/keyfile" <<EOF
[org/gnome/shell]
enabled-extensions=['quotax@turinglabs.org', 'quotax-preview@turinglabs.org']
disable-user-extensions=false
welcome-dialog-last-shown-version='999'

[org/gnome/desktop/interface]
color-scheme='$scheme'
EOF
    env -i HOME="$HOME" PATH="/usr/bin:/bin" LANG="${PREVIEW_LANG:-${LANG:-en_US.UTF-8}}" \
        XDG_RUNTIME_DIR="$WORK/runtime" XDG_DATA_HOME="$WORK/data" XDG_CONFIG_HOME="$WORK/config" \
        XDG_STATE_HOME="$WORK/state" XDG_CACHE_HOME="$WORK/cache" XDG_DATA_DIRS="$WORK/share:/usr/local/share:/usr/share" \
        GSETTINGS_BACKEND=keyfile QUOTAX_SAMPLE=$SAMPLE QUOTAX_PREVIEW_DIR="$OUT" QUOTAX_PREVIEW_VARIANT="$variant" \
        timeout 90 dbus-run-session -- gnome-shell --headless --wayland --no-x11 --mode=quotax-preview \
        --virtual-monitor 1280x1200 --wayland-display=quotax-preview >"$OUT/shell-$variant.log" 2>&1 || true
    grep -E -A3 'JS ERROR|JS WARNING|Quotax' "$OUT/shell-$variant.log" | head -20 || true
done
ls -1 "$OUT"
