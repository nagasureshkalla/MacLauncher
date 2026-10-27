#!/usr/bin/env bash
# Per-user install of Nova Launcher into ~/.local (no root needed).
#
# Every file written is recorded in a manifest; scripts/uninstall.sh removes
# exactly those files and nothing else.
#
#   scripts/install.sh            install / upgrade
#   scripts/install.sh --dry-run  show what would be installed
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${HOME}/.local"
LIBDIR="${PREFIX}/lib/nova-launcher"
BINDIR="${PREFIX}/bin"
APPDIR="${XDG_DATA_HOME:-$PREFIX/share}"
MANIFEST="${APPDIR}/nova-launcher/install-manifest.txt"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

echo "== Checking dependencies"
missing=()
python3 - <<'PY' || missing+=("python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-gi-cairo")
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
gi.require_foreign("cairo")
PY
command -v gapplication >/dev/null || missing+=("libglib2.0-bin")
if ((${#missing[@]})); then
    echo "Missing packages. Install them with:"
    echo "    sudo apt install ${missing[*]}"
    exit 1
fi
echo "   ok (python3 $(python3 -c 'import sys;print(sys.version.split()[0])'), GTK 4, libadwaita)"

declare -a FILES=(
    "${BINDIR}/nova-launcher"
    "${APPDIR}/applications/io.github.nova.Launcher.desktop"
    "${APPDIR}/icons/hicolor/scalable/apps/io.github.nova.Launcher.svg"
    "${APPDIR}/dbus-1/services/io.github.nova.Launcher.service"
)
echo "== Will install"
echo "   ${LIBDIR}/ (Python package)"
printf '   %s\n' "${FILES[@]}"
((DRY)) && exit 0

subst() { sed -e "s|@BINDIR@|${BINDIR}|g" -e "s|@PYLIBDIR@|${LIBDIR}|g" "$1" > "$2"; }

mkdir -p "${LIBDIR}" "${BINDIR}" "${APPDIR}/applications" "${APPDIR}/icons/hicolor/scalable/apps" \
         "${APPDIR}/dbus-1/services" "$(dirname "$MANIFEST")"
rm -rf "${LIBDIR}/nova_launcher"
cp -r "${SRC}/src/nova_launcher" "${LIBDIR}/"
find "${LIBDIR}" -name '__pycache__' -prune -exec rm -rf {} +
python3 -m compileall -q "${LIBDIR}/nova_launcher"

subst "${SRC}/packaging/nova-launcher.in" "${BINDIR}/nova-launcher"
chmod 755 "${BINDIR}/nova-launcher"
subst "${SRC}/packaging/io.github.nova.Launcher.desktop" "${APPDIR}/applications/io.github.nova.Launcher.desktop"
cp "${SRC}/src/nova_launcher/resources/icons/io.github.nova.Launcher.svg" "${APPDIR}/icons/hicolor/scalable/apps/"
subst "${SRC}/packaging/io.github.nova.Launcher.service" "${APPDIR}/dbus-1/services/io.github.nova.Launcher.service"

{ echo "${LIBDIR}"; printf '%s\n' "${FILES[@]}"; } > "$MANIFEST"

command -v update-desktop-database >/dev/null && update-desktop-database -q "${APPDIR}/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "${APPDIR}/icons/hicolor" 2>/dev/null || true

echo "== Installed. Manifest: ${MANIFEST}"
echo "Start it:         nova-launcher --quick        (or 'Nova Launcher' in the app grid)"
echo "Optional extras (each is opt-in and reversible, in Nova Settings):"
echo "  - Start at login (Settings > General)"
echo "  - Keyboard shortcuts Super+/ and Super+Shift+A (Settings > General > Install shortcuts)"
