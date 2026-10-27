"""Blurred desktop wallpaper, used to paint frosted-glass surfaces.

GNOME does not let applications blur what is behind their windows.  But the
commands widget always sits on the desktop (it stays below windows) and the
dock lives in screen space that windows do not enter, so painting them with
the matching slice of a blurred copy of the wallpaper looks like real glass.
The Launchpad uses the whole blurred wallpaper as its backdrop, like macOS.

The blur is computed once per monitor size on a worker thread and cached in
~/.cache/nova-launcher; it is recomputed only when the wallpaper changes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Callable

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib  # noqa: E402

from .. import paths  # noqa: E402

log = logging.getLogger("nova.wallpaper")

BG_SCHEMA = "org.gnome.desktop.background"
IFACE_SCHEMA = "org.gnome.desktop.interface"
BLUR_VERSION = 2  # bump when the look of the blur changes


def wallpaper_path(dark: bool) -> str | None:
    try:
        s = Gio.Settings.new(BG_SCHEMA)
    except Exception:
        return None
    uri = s.get_string("picture-uri-dark" if dark else "picture-uri") or s.get_string("picture-uri")
    if not uri:
        return None
    path = GLib.filename_from_uri(uri)[0] if uri.startswith("file://") else uri
    return path if os.path.isfile(path) else None


def render_blur(src: str, width: int, height: int, out: str) -> None:
    """Cover-crop ``src`` to width x height, blur heavily, add vibrancy (pure; tested)."""
    from PIL import Image, ImageEnhance, ImageFilter

    with Image.open(src) as im:
        im = im.convert("RGB")
        # "zoom" placement: scale to cover, centre crop (what GNOME draws).
        scale = max(width / im.width, height / im.height)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.BILINEAR)
        left, top = (im.width - width) // 2, (im.height - height) // 2
        im = im.crop((left, top, left + width, top + height))
    # Blurring at 1/4 size is ~16x cheaper and indistinguishable after upscaling.
    small = im.resize((max(1, width // 4), max(1, height // 4)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(radius=14))
    small = ImageEnhance.Color(small).enhance(1.35)  # macOS-style vibrancy
    small.resize((width, height), Image.BICUBIC).save(out, "PNG", compress_level=1)


class BlurredWallpaper:
    """Per-monitor blurred wallpaper textures, with change notification."""

    def __init__(self, is_dark: Callable[[], bool]):
        self.is_dark = is_dark
        self._textures: dict[tuple[int, int], Gdk.Texture] = {}
        self._pending: set[tuple[int, int]] = set()
        self._listeners: list[Callable[[], None]] = []
        self._generation = 0
        try:
            self._settings = Gio.Settings.new(BG_SCHEMA)
            for key in ("picture-uri", "picture-uri-dark", "picture-options"):
                self._settings.connect(f"changed::{key}", lambda *_: self.invalidate())
        except Exception:
            self._settings = None

    def connect(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def invalidate(self) -> None:
        self._generation += 1
        self._textures.clear()
        for cb in list(self._listeners):
            cb()

    def texture(self, width: int, height: int) -> Gdk.Texture | None:
        """Blurred wallpaper at this size, or None while it is being made."""
        key = (width, height)
        tex = self._textures.get(key)
        if tex is None and key not in self._pending:
            self._start(key)
        return tex

    def _cache_file(self, src: str, width: int, height: int) -> str:
        st = os.stat(src)
        h = hashlib.sha1(f"{src}:{st.st_mtime_ns}:{st.st_size}:{width}x{height}:{BLUR_VERSION}".encode()).hexdigest()[:16]
        return str(paths.cache_dir() / f"wallpaper-blur-{h}.png")

    def _start(self, key: tuple[int, int]) -> None:
        src = wallpaper_path(self.is_dark())
        if src is None:
            return
        self._pending.add(key)
        gen = self._generation

        def work():
            out = None
            try:
                out = self._cache_file(src, *key)
                if not os.path.exists(out):
                    paths.cache_dir().mkdir(parents=True, exist_ok=True)
                    tmp = out + f".{os.getpid()}.tmp.png"
                    render_blur(src, key[0], key[1], tmp)
                    os.replace(tmp, out)
                    self._prune(keep=out)
            except Exception as exc:  # missing Pillow, unreadable image, ...
                log.warning("cannot blur wallpaper: %s", exc)
                out = None
            GLib.idle_add(lambda: (self._done(key, out, gen), GLib.SOURCE_REMOVE)[1])

        threading.Thread(target=work, name="nova-wallpaper", daemon=True).start()

    def _done(self, key, out, gen) -> None:
        self._pending.discard(key)
        if gen != self._generation or out is None:
            return
        try:
            self._textures[key] = Gdk.Texture.new_from_filename(out)
        except GLib.Error as exc:
            log.warning("cannot load blurred wallpaper: %s", exc.message)
            return
        for cb in list(self._listeners):
            cb()

    @staticmethod
    def _prune(keep: str, max_files: int = 6) -> None:
        """Bounded cache: keep only the newest few blurred images."""
        d = paths.cache_dir()
        files = sorted(d.glob("wallpaper-blur-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[max_files:]:
            if str(p) != keep:
                p.unlink(missing_ok=True)
