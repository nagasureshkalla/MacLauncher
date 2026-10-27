import json
import time

from conftest import app_entry, write_desktop
from gi.repository import GLib

from nova_launcher.core import app_discovery
from nova_launcher.core.app_index import AppIndex, UsageHistory


def _index(tmp_path, scan_fn=None):
    d = tmp_path / "apps"
    d.mkdir(exist_ok=True)
    usage = UsageHistory(tmp_path / "usage.json")
    return AppIndex(dirs=[d], cache_path=tmp_path / "apps.json", usage=usage, scan_fn=scan_fn), d


def _spin(cond, timeout=5.0):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)
    return cond()


def test_cache_creation_and_reload(tmp_path):
    idx, d = _index(tmp_path)
    write_desktop(d, "a.desktop", app_entry("Alpha"))
    assert idx.refresh_sync()
    assert json.loads((tmp_path / "apps.json").read_text())["apps"][0]["name"] == "Alpha"
    idx2, _ = _index(tmp_path)
    assert idx2.load_cache() and idx2.get("a.desktop").name == "Alpha"
    assert idx2.search("alp")[0].id == "a.desktop"


def test_corrupt_cache_is_ignored_and_rebuilt(tmp_path):
    idx, d = _index(tmp_path)
    write_desktop(d, "a.desktop", app_entry("Alpha"))
    (tmp_path / "apps.json").write_text("{broken")
    assert not idx.load_cache()
    assert idx.refresh_sync() and len(idx) == 1
    assert json.loads((tmp_path / "apps.json").read_text())["version"]


def test_cache_for_other_directories_is_stale(tmp_path):
    idx, d = _index(tmp_path)
    write_desktop(d, "a.desktop", app_entry("Alpha"))
    idx.refresh_sync()
    other = AppIndex(dirs=[tmp_path / "elsewhere"], cache_path=tmp_path / "apps.json")
    assert not other.load_cache()


def test_incremental_refresh_add_change_remove(tmp_path):
    idx, d = _index(tmp_path)
    write_desktop(d, "a.desktop", app_entry("Alpha"))
    idx.refresh_sync()
    calls = []
    idx.connect(lambda: calls.append(1))
    write_desktop(d, "b.desktop", app_entry("Beta"))
    write_desktop(d, "a.desktop", app_entry("Alpha Two"))
    idx.refresh_ids({"a.desktop", "b.desktop"})
    assert idx.get("a.desktop").name == "Alpha Two" and idx.get("b.desktop")
    (d / "a.desktop").unlink()
    idx.refresh_ids({"a.desktop"})
    assert idx.get("a.desktop") is None
    assert idx.search("beta")[0].id == "b.desktop"
    assert len(calls) == 2


def test_file_monitor_triggers_refresh(tmp_path):
    idx, d = _index(tmp_path)
    idx.refresh_sync()
    idx.watch()
    try:
        write_desktop(d, "new.desktop", app_entry("Newcomer"))
        assert _spin(lambda: idx.get("new.desktop") is not None)
    finally:
        idx.unwatch()


def test_background_worker_failure_keeps_cached_apps(tmp_path):
    idx, d = _index(tmp_path)
    write_desktop(d, "a.desktop", app_entry("Alpha"))
    idx.refresh_sync()

    def broken(_dirs):
        raise RuntimeError("disk on fire")

    idx._scan_fn = broken
    result = []
    idx.refresh_async(result.append)
    assert _spin(lambda: result)
    assert result == [False]
    assert idx.get("a.desktop") is not None and "disk on fire" in idx.last_error


def test_ui_stays_responsive_during_slow_indexing(tmp_path):
    def slow_scan(dirs):
        time.sleep(1.0)  # e.g. a slow disk
        return app_discovery.scan(dirs)

    idx, d = _index(tmp_path, slow_scan)
    write_desktop(d, "a.desktop", app_entry("Alpha"))
    gaps = []
    last = [time.monotonic()]

    def tick():
        now = time.monotonic()
        gaps.append(now - last[0])
        last[0] = now
        return GLib.SOURCE_CONTINUE

    src = GLib.timeout_add(10, tick)
    done = []
    idx.refresh_async(done.append)
    assert _spin(lambda: done, timeout=5)
    GLib.source_remove(src)
    assert done == [True] and idx.get("a.desktop")
    assert max(gaps) < 0.1, f"main loop blocked for {max(gaps) * 1000:.0f} ms"


def test_usage_history_is_bounded_and_ranks_recent(tmp_path):
    u = UsageHistory(tmp_path / "u.json")
    for i in range(320):
        u.record(f"app{i}.desktop", now=1000 + i)
    assert len(u._data) == 300
    assert u.recent(2) == ["app319.desktop", "app318.desktop"]
    assert u.score("app319.desktop", now=1400) > u.score("app0.desktop", now=1400)
    u2 = UsageHistory(tmp_path / "u.json")
    u2.load()
    assert u2.recent(1) == ["app319.desktop"]


def test_usage_history_disabled_and_corrupt(tmp_path):
    p = tmp_path / "u.json"
    p.write_text("[1,2")
    u = UsageHistory(p, enabled=False)
    u.load()
    u.record("x.desktop")
    assert u.recent(5) == []


def test_chrome_window_is_not_claimed_by_a_chrome_web_app(tmp_path):
    """Web apps (Claude etc.) run google-chrome too, but have their own crx_ class."""
    idx, d = _index(tmp_path)
    write_desktop(d, "chrome-abc-Default.desktop",
                  app_entry("Claude", "google-chrome --profile-directory=Default --app-id=abc",
                            extra="StartupWMClass=crx_abc\n"))
    write_desktop(d, "google-chrome.desktop", app_entry("Google Chrome", "/usr/bin/google-chrome-stable %U"))
    write_desktop(d, "xterm.desktop", app_entry("XTerm", "xterm"))
    assert idx.refresh_sync()
    assert idx.get("chrome-abc-Default.desktop").executable == "google-chrome"
    assert idx.find_by_wm_class("Google-chrome").name == "Google Chrome"  # Chrome's real WM_CLASS
    assert idx.find_by_wm_class("google-chrome").name == "Google Chrome"
    assert idx.find_by_wm_class("crx_abc").name == "Claude"
    assert idx.find_by_wm_class("XTerm").name == "XTerm"  # executable match still works
    assert idx.find_by_wm_class("") is None
