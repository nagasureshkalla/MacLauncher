#!/usr/bin/env bash
# Remove a per-user Nova Launcher install.
#
# Removes only the paths listed in the install manifest, plus Nova's own
# autostart entry and GNOME shortcuts (only those whose path contains
# "nova-launcher").  Settings/cache/history are kept unless --purge is given,
# and even then only Nova's own directories are removed.
set -euo pipefail

APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}"
MANIFEST="${APPDIR}/nova-launcher/install-manifest.txt"
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

nova-launcher --quit 2>/dev/null || true

if [[ -f "$MANIFEST" ]]; then
    while IFS= read -r path; do
        [[ -z "$path" ]] && continue
        case "$path" in
            "$HOME"/.local/*|"$APPDIR"/*) ;;
            *) echo "skipping unexpected path: $path"; continue ;;
        esac
        if [[ -d "$path" ]]; then
            echo "removing $path/"; rm -rf -- "$path"
        elif [[ -e "$path" ]]; then
            echo "removing $path"; rm -f -- "$path"
        fi
    done < "$MANIFEST"
    rm -f -- "$MANIFEST"
    rmdir --ignore-fail-on-non-empty "${APPDIR}/nova-launcher" 2>/dev/null || true
else
    echo "no install manifest found at $MANIFEST (nothing installed per-user?)"
fi

# Refresh (not delete) the shared caches so they no longer mention Nova.
command -v update-desktop-database >/dev/null && update-desktop-database -q "${APPDIR}/applications" 2>/dev/null || true
[[ -f "${APPDIR}/icons/hicolor/icon-theme.cache" ]] && command -v gtk-update-icon-cache >/dev/null \
    && gtk-update-icon-cache -q -t "${APPDIR}/icons/hicolor" 2>/dev/null || true

AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/nova-launcher.desktop"
[[ -f "$AUTOSTART" ]] && { echo "removing $AUTOSTART"; rm -f -- "$AUTOSTART"; }

# GNOME custom shortcuts: drop only Nova's entries from the list.
if command -v gsettings >/dev/null; then
    KEY=org.gnome.settings-daemon.plugins.media-keys
    current=$(gsettings get "$KEY" custom-keybindings)
    if [[ "$current" == *nova-launcher-* ]]; then
        kept=$(python3 -c "
import ast,sys
s=sys.argv[1].replace('@as ','')
items=[p for p in ast.literal_eval(s) if '/nova-launcher-' not in p]
print('@as []' if not items else str(items))" "$current")
        for p in $(python3 -c "import ast,sys; print(' '.join(p for p in ast.literal_eval(sys.argv[1].replace('@as ','')) if '/nova-launcher-' in p))" "$current"); do
            for k in name command binding; do
                gsettings reset "$KEY.custom-keybinding:$p" "$k" || true
            done
        done
        gsettings set "$KEY" custom-keybindings "$kept"
        echo "removed Nova keyboard shortcuts (other shortcuts untouched)"
    fi
fi

if ((PURGE)); then
    for d in "${XDG_CONFIG_HOME:-$HOME/.config}/nova-launcher" \
             "${XDG_CACHE_HOME:-$HOME/.cache}/nova-launcher" \
             "${XDG_STATE_HOME:-$HOME/.local/state}/nova-launcher"; do
        [[ -d "$d" ]] && { echo "removing $d/"; rm -rf -- "$d"; }
    done
else
    echo "kept settings/history (run with --purge to remove ~/.config|.cache|.local/state/nova-launcher)"
fi
echo "Nova Launcher uninstalled."
