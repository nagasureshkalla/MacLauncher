"""The single application index shared by every Nova window.

Startup path (designed so the UI never waits on the filesystem):

1. ``load_cache()`` reads one small JSON file -> UI can render immediately.
2. ``refresh_async()`` re-scans desktop entries on a worker thread and hands
   the result back on the GLib main loop.
3. ``Gio.FileMonitor`` watches the application directories; a change only
   re-parses the affected desktop IDs (incremental refresh), debounced.

There is no polling: when nothing changes, the index does no work.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Callable

from gi.repository import Gio, GLib

from .. import paths
from . import app_discovery
from .desktop_entry import AppRecord, ScanReport, load_entry
from .search_engine import SearchEngine

log = logging.getLogger("nova.index")

CACHE_VERSION = 2
USAGE_MAX_ENTRIES = 300
USAGE_HALF_LIFE_DAYS = 14.0


class UsageHistory:
    """Local-only launch history used for ranking and the dock's recent apps."""

    def __init__(self, path: Path | None = None, enabled: bool = True):
        self.path = path or (paths.state_dir() / "usage.json")
        self.enabled = enabled
        self._data: dict[str, dict] = {}

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {
                    k: {"count": int(v.get("count", 0)), "last": float(v.get("last", 0))}
                    for k, v in raw.items()
                    if isinstance(k, str) and isinstance(v, dict)
                }
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.warning("usage history unreadable, starting fresh: %s", exc)
            self._data = {}

    def record(self, app_id: str, now: float | None = None) -> None:
        if not self.enabled:
            return
        now = now or time.time()
        e = self._data.setdefault(app_id, {"count": 0, "last": 0.0})
        e["count"] += 1
        e["last"] = now
        if len(self._data) > USAGE_MAX_ENTRIES:  # bounded
            for k, _ in sorted(self._data.items(), key=lambda kv: kv[1]["last"])[: len(self._data) - USAGE_MAX_ENTRIES]:
                del self._data[k]
        self.save()

    def save(self) -> None:
        try:
            paths.atomic_write(self.path, json.dumps(self._data))
        except OSError as exc:
            log.warning("cannot save usage history: %s", exc)

    def clear(self) -> None:
        self._data = {}
        self.save()

    def score(self, app_id: str, now: float | None = None) -> float:
        e = self._data.get(app_id)
        if not e:
            return 0.0
        age_days = max(0.0, ((now or time.time()) - e["last"]) / 86400)
        return math.log1p(e["count"]) * 0.5 ** (age_days / USAGE_HALF_LIFE_DAYS) * 4

    def recent(self, n: int) -> list[str]:
        return [k for k, _ in sorted(self._data.items(), key=lambda kv: -kv[1]["last"])[:n]]


class AppIndex:
    def __init__(
        self,
        dirs: list[Path] | None = None,
        cache_path: Path | None = None,
        usage: UsageHistory | None = None,
        scan_fn: Callable[[list[Path]], tuple[list[AppRecord], ScanReport]] | None = None,
    ):
        self.dirs = dirs if dirs is not None else paths.application_dirs()
        self.cache_path = cache_path or (paths.cache_dir() / "apps.json")
        self.usage = usage or UsageHistory()
        self._scan_fn = scan_fn or app_discovery.scan
        self._records: dict[str, AppRecord] = {}
        self.engine = SearchEngine(usage_score=self.usage.score)
        self.last_report: ScanReport | None = None
        self.last_error: str | None = None
        self.ready = False  # True after the first successful scan
        self._listeners: list[Callable[[], None]] = []
        self._monitors: list[Gio.FileMonitor] = []
        self._pending_ids: set[str] = set()
        self._pending_full = False
        self._debounce_id = 0
        self._scan_lock = threading.Lock()
        self._generation = 0

    # -- queries -----------------------------------------------------------
    def get(self, app_id: str) -> AppRecord | None:
        return self._records.get(app_id)

    def all(self) -> list[AppRecord]:
        return sorted(self._records.values(), key=lambda r: r.name.casefold())

    def __len__(self) -> int:
        return len(self._records)

    def search(self, query: str, limit: int = 20) -> list[AppRecord]:
        return [r for _, r in self.engine.search(query, limit)]

    def find_by_wm_class(self, wm_class: str) -> AppRecord | None:
        """App for a window class: StartupWMClass, then desktop ID, then executable.

        Entries that declare their own StartupWMClass are never matched by
        executable: Chrome/Edge web apps (Claude, YouTube, ...) all run
        ``google-chrome`` but have their own ``crx_*`` class, and must not
        claim plain Chrome windows.
        """
        wc = wm_class.casefold()
        if not wc:
            return None
        records = self._records.values()
        for r in records:
            if r.startup_wm_class and r.startup_wm_class.casefold() == wc:
                return r
        for r in records:
            if r.id.removesuffix(".desktop").casefold() == wc:
                return r
        for r in records:
            if not r.startup_wm_class and r.executable.casefold() == wc:
                return r
        return None

    def connect(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def _changed(self) -> None:
        self.engine.rebuild(self._records.values())
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.exception("index listener failed")

    def _set_records(self, records: list[AppRecord]) -> None:
        self._records = {r.id: r for r in records}
        self._changed()

    # -- cache -------------------------------------------------------------
    def load_cache(self) -> bool:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if raw.get("version") != CACHE_VERSION or raw.get("dirs") != [str(d) for d in self.dirs]:
                log.info("app cache is stale (version/dirs changed); ignoring")
                return False
            records = [AppRecord.from_dict(d) for d in raw["apps"]]
        except FileNotFoundError:
            return False
        except Exception as exc:  # corrupt cache: ignore, it is rebuilt by the scan
            log.warning("app cache unreadable (%s); will rebuild", exc)
            return False
        self._set_records(records)
        log.debug("loaded %d apps from cache", len(records))
        return True

    def save_cache(self) -> None:
        data = {
            "version": CACHE_VERSION,
            "dirs": [str(d) for d in self.dirs],
            "apps": [r.to_dict() for r in self.all()],
        }
        try:
            paths.atomic_write(self.cache_path, json.dumps(data))
        except OSError as exc:
            log.warning("cannot write app cache: %s", exc)

    # -- scanning ------------------------------------------------------------
    def refresh_sync(self) -> bool:
        """Blocking full scan (tests, CLI tools).  Returns success."""
        try:
            records, report = self._scan_fn(self.dirs)
        except Exception as exc:
            self.last_error = f"scan failed: {exc}"
            log.exception("application scan failed")
            return False
        self._apply_scan(records, report)
        return True

    def refresh_async(self, done: Callable[[bool], None] | None = None) -> None:
        """Scan on a worker thread; apply on the main loop.  UI never blocks."""
        self._generation += 1
        gen = self._generation

        def worker():
            with self._scan_lock:
                t0 = time.perf_counter()
                try:
                    records, report = self._scan_fn(self.dirs)
                    err = None
                except Exception as exc:
                    records, report, err = None, None, exc
                    log.exception("background scan failed")
                elapsed = (time.perf_counter() - t0) * 1000

            def apply():
                if gen != self._generation:
                    return GLib.SOURCE_REMOVE  # a newer scan superseded this one
                if err is not None:
                    # Recovery: keep the cached/previous records and say why.
                    self.last_error = f"scan failed: {err}"
                    if done:
                        done(False)
                    return GLib.SOURCE_REMOVE
                self._apply_scan(records, report)
                log.info("scanned %d desktop files -> %d apps in %.1f ms", report.scanned, report.shown, elapsed)
                if done:
                    done(True)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(apply)

        threading.Thread(target=worker, name="nova-scan", daemon=True).start()

    def _apply_scan(self, records: list[AppRecord], report: ScanReport) -> None:
        self.last_report = report
        self.last_error = None
        changed = {r.id: r for r in records} != self._records
        self.ready = True
        if changed:
            self._set_records(records)
            self.save_cache()

    # -- incremental refresh ---------------------------------------------------
    def watch(self) -> None:
        """Monitor application directories (inotify; zero cost while idle)."""
        for d in self.dirs:
            try:
                gfile = Gio.File.new_for_path(str(d))
                mon = gfile.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error as exc:
                log.debug("cannot monitor %s: %s", d, exc.message)
                continue
            mon.connect("changed", self._on_dir_changed, d)
            self._monitors.append(mon)

    def unwatch(self) -> None:
        for m in self._monitors:
            m.cancel()
        self._monitors.clear()

    def _on_dir_changed(self, _mon, file, other, event, base: Path) -> None:
        for f in (file, other):
            if f is None:
                continue
            p = f.get_path()
            if not p:
                continue
            if p.endswith(".desktop"):
                try:
                    self._pending_ids.add(app_discovery.desktop_id_for(Path(p), base))
                except ValueError:
                    self._pending_full = True
            elif event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.DELETED,
                           Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.MOVED_OUT):
                self._pending_full = True  # a subdirectory appeared/vanished
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        # Package managers write many files at once: wait for quiet.
        self._debounce_id = GLib.timeout_add(600, self._flush_pending)

    def _flush_pending(self) -> bool:
        self._debounce_id = 0
        if self._pending_full or len(self._pending_ids) > 40:
            self._pending_full = False
            self._pending_ids.clear()
            self.refresh_async()
        else:
            ids, self._pending_ids = self._pending_ids, set()
            self.refresh_ids(ids)
        return GLib.SOURCE_REMOVE

    def refresh_ids(self, ids: set[str]) -> None:
        """Re-resolve only the given desktop IDs (cheap; main thread is fine)."""
        changed = False
        for app_id in ids:
            path = app_discovery.winner_for_id(app_id, self.dirs)
            record = None
            if path is not None:
                try:
                    record, _reason = load_entry(str(path), app_id)
                except Exception:
                    record = None
            old = self._records.get(app_id)
            if record is None and old is not None:
                del self._records[app_id]
                changed = True
            elif record is not None and record != old:
                self._records[app_id] = record
                changed = True
        if changed:
            log.info("incremental refresh: %s", ", ".join(sorted(ids)))
            self._changed()
            self.save_cache()
