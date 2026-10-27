"""XDG application discovery.

Implements the desktop-entry ID / precedence rules from the XDG Desktop Menu
spec: a file's ID is its path relative to the ``applications`` directory with
``/`` replaced by ``-``, and the first directory in precedence order that
contains an ID wins — even if that winning file is ``Hidden=true`` (that is how
users hide system apps).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

from .. import paths
from .desktop_entry import AppRecord, ScanReport, load_entry

log = logging.getLogger("nova.discovery")


def desktop_id_for(path: Path, base: Path) -> str:
    return str(path.relative_to(base)).replace(os.sep, "-")


def iter_desktop_files(base: Path) -> Iterable[tuple[str, Path]]:
    """Yield (desktop_id, path) for every .desktop file under ``base``.

    Uses os.scandir and never follows symlinked *directories*, so a bad link
    cannot send discovery into an unrelated tree.
    """
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.name.endswith(".desktop"):
                            p = Path(entry.path)
                            yield desktop_id_for(p, base), p
                    except OSError:
                        continue
        except OSError:
            continue


def resolve_winners(dirs: list[Path], report: ScanReport) -> dict[str, Path]:
    """Map each desktop ID to the highest-precedence file providing it."""
    winners: dict[str, Path] = {}
    for base in dirs:
        if not base.is_dir():
            continue
        for desktop_id, path in iter_desktop_files(base):
            report.scanned += 1
            if desktop_id in winners:
                report.shadowed[str(path)] = str(winners[desktop_id])
                continue
            winners[desktop_id] = path
    return winners


def winner_for_id(desktop_id: str, dirs: list[Path]) -> Path | None:
    """Resolve a single ID — used for incremental refresh on file changes."""
    rel = None
    for base in dirs:
        if not base.is_dir():
            continue
        # IDs map back to paths ambiguously ("a-b.desktop" may be "a/b.desktop"),
        # so try the flat name first, then subdirectory interpretations.
        candidates = [base / desktop_id]
        parts = desktop_id.split("-")
        for i in range(1, len(parts)):
            candidates.append(base / "/".join(parts[:i]) / "-".join(parts[i:]))
        for c in candidates:
            if c.is_file():
                rel = c
                break
        if rel:
            return rel
    return None


def dedupe(records: list[AppRecord], report: ScanReport) -> list[AppRecord]:
    """Collapse different IDs that are really the same app.

    Example: the same program installed as both a .deb and a Snap/Flatpak with
    identical name *and* executable.  Precedence order is preserved: the record
    seen first (user > local > system) is kept.  Apps that merely share a name
    (e.g. two different "Terminal"s) are kept.
    """
    seen: dict[tuple[str, str], AppRecord] = {}
    out = []
    for r in records:
        key = (r.name.casefold(), r.executable)
        if r.executable and key in seen:
            report.duplicates[r.id] = seen[key].id
            continue
        seen[key] = r
        out.append(r)
    return out


def scan(dirs: list[Path] | None = None) -> tuple[list[AppRecord], ScanReport]:
    """Full scan.  Safe to call from a worker thread."""
    dirs = dirs if dirs is not None else paths.application_dirs()
    report = ScanReport()
    winners = resolve_winners(dirs, report)
    records: list[AppRecord] = []
    for desktop_id, path in winners.items():
        try:
            record, reason = load_entry(str(path), desktop_id)
        except Exception as exc:  # never let one bad file kill discovery
            record, reason = None, f"invalid: {exc}"
        if record is None:
            report.skipped[str(path)] = reason
            continue
        records.append(record)
    records = dedupe(records, report)
    records.sort(key=lambda r: r.name.casefold())
    report.shown = len(records)
    log.debug("scan: %d files, %d apps, %d skipped", report.scanned, report.shown, len(report.skipped))
    return records, report
