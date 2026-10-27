#!/usr/bin/env bash
# Build dist/nova-launcher_<version>_all.deb with dpkg-deb (no debhelper needed).
# Install with:  sudo apt install ./dist/nova-launcher_<version>_all.deb
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(python3 -c "import sys; sys.path.insert(0, '$SRC/src'); import nova_launcher; print(nova_launcher.__version__)")
BUILD="$(mktemp -d)"
trap 'rm -rf -- "$BUILD"' EXIT
ROOT="$BUILD/nova-launcher_${VERSION}_all"

subst() { sed -e "s|@BINDIR@|/usr/bin|g" -e "s|@PYLIBDIR@|/usr/lib/nova-launcher|g" "$1" > "$2"; }

install -d "$ROOT/DEBIAN" "$ROOT/usr/bin" "$ROOT/usr/lib/nova-launcher" \
    "$ROOT/usr/share/applications" "$ROOT/usr/share/icons/hicolor/scalable/apps" \
    "$ROOT/usr/share/dbus-1/services" "$ROOT/usr/share/doc/nova-launcher"
cp -r "$SRC/src/nova_launcher" "$ROOT/usr/lib/nova-launcher/"
find "$ROOT" -name '__pycache__' -prune -exec rm -rf {} +
subst "$SRC/packaging/nova-launcher.in" "$ROOT/usr/bin/nova-launcher"
chmod 755 "$ROOT/usr/bin/nova-launcher"
subst "$SRC/packaging/io.github.nova.Launcher.desktop" "$ROOT/usr/share/applications/io.github.nova.Launcher.desktop"
subst "$SRC/packaging/io.github.nova.Launcher.service" "$ROOT/usr/share/dbus-1/services/io.github.nova.Launcher.service"
cp "$SRC/src/nova_launcher/resources/icons/io.github.nova.Launcher.svg" "$ROOT/usr/share/icons/hicolor/scalable/apps/"
cp "$SRC/README.md" "$SRC/LICENSE" "$ROOT/usr/share/doc/nova-launcher/"

SIZE=$(du -sk "$ROOT/usr" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$SRC/packaging/debian/control" > "$ROOT/DEBIAN/control"
install -m 755 "$SRC/packaging/debian/postinst" "$ROOT/DEBIAN/postinst"
install -m 755 "$SRC/packaging/debian/prerm" "$ROOT/DEBIAN/prerm"
install -m 755 "$SRC/packaging/debian/postrm" "$ROOT/DEBIAN/postrm"
find "$ROOT" -type d -exec chmod 755 {} +
find "$ROOT/usr" -type f ! -path '*/bin/*' -exec chmod 644 {} +

mkdir -p "$SRC/dist"
dpkg-deb --root-owner-group --build "$ROOT" "$SRC/dist/" >/dev/null
echo "built $SRC/dist/nova-launcher_${VERSION}_all.deb"
