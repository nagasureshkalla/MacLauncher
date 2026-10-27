"""Logging setup: a small rotating file log plus stderr."""

from __future__ import annotations

import logging
import logging.handlers

from . import paths

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(debug: bool = False) -> logging.Logger:
    root = logging.getLogger("nova")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    if root.handlers:
        for h in root.handlers:
            h.setLevel(root.level)
        return root

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(stream)

    try:
        log_dir = paths.state_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        # Bounded: at most 3 x 512 KiB on disk.
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "nova.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(fh)
    except OSError as exc:  # read-only home, full disk, ...
        root.warning("file logging disabled: %s", exc)
    return root


def set_debug(enabled: bool) -> None:
    logging.getLogger("nova").setLevel(logging.DEBUG if enabled else logging.INFO)
