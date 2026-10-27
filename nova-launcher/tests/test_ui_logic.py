"""Grid organisation, dock magnification, quick-launcher results, placement."""

from nova_launcher.config import Settings
from nova_launcher.core.app_index import AppIndex, UsageHistory
from nova_launcher.core.desktop_entry import AppRecord
from nova_launcher.core.file_manager import Place
from nova_launcher.system.monitor_support import MonitorInfo, Rect, centered, choose_monitor, dock_origin
from nova_launcher.ui.dock import magnify
from nova_launcher.ui.grid_model import GridModel
from nova_launcher.ui.launcher_window import build_results


def recs(*names, cats=()):
    return [AppRecord(id=f"{n.lower()}.desktop", path="", name=n, categories=cats) for n in names]


def test_grid_orders_alphabetically_and_respects_custom_order():
    m = GridModel(recs("Beta", "Alpha", "Gamma"), ["app:gamma.desktop", "app:missing.desktop"], {})
    assert m.keys == ["app:gamma.desktop", "app:alpha.desktop", "app:beta.desktop"]


def test_grid_move_and_pages():
    m = GridModel(recs("A", "B", "C", "D", "E"), [], {})
    m.move("app:e.desktop", "app:a.desktop", after=False)
    assert m.keys[0] == "app:e.desktop"
    m.move("app:e.desktop", "app:b.desktop", after=True)
    assert m.keys[:3] == ["app:a.desktop", "app:b.desktop", "app:e.desktop"]
    assert [len(p) for p in m.pages(2)] == [2, 2, 1]


def test_grid_folder_create_add_remove():
    m = GridModel(recs("Code", "Gedit", "Vim", cats=("Development",)), [], {})
    fid = m.merge("app:gedit.desktop", "app:code.desktop")
    assert m.folders[fid] == {"name": "Development", "apps": ["code.desktop", "gedit.desktop"]}
    assert m.keys == [f"folder:{fid}", "app:vim.desktop"]
    m.merge("app:vim.desktop", f"folder:{fid}")
    assert m.keys == [f"folder:{fid}"]
    m.remove_from_folder(fid, "vim.desktop")
    m.remove_from_folder(fid, "code.desktop")
    m.remove_from_folder(fid, "gedit.desktop")
    assert fid not in m.folders and f"folder:{fid}" not in m.keys
    assert sorted(m.keys) == ["app:code.desktop", "app:gedit.desktop", "app:vim.desktop"]


def test_grid_drops_uninstalled_apps_and_empty_folders():
    m = GridModel(recs("A"), [], {"f": {"name": "Old", "apps": ["gone.desktop"]},
                                  "g": {"name": "Keep", "apps": ["a.desktop", "gone.desktop"]}})
    assert m.keys == ["folder:g"] and m.folders["g"]["apps"] == ["a.desktop"]
    order, folders = m.to_settings()
    assert order == ["folder:g"] and "f" not in folders


def test_magnify_peaks_under_pointer_and_falls_off():
    s = magnify([0, 50, 100, 150, 400], 50, 50, 1.6)
    assert abs(s[1] - 1.6) < 1e-9
    assert s[0] == s[2] and 1.0 < s[2] < s[1]
    assert s[4] == 1.0
    assert magnify([0, 50], None, 50, 1.6) == [1.0, 1.0]
    assert magnify([0, 50], 0, 50, 1.0) == [1.0, 1.0]


def _index(tmp_path):
    idx = AppIndex(dirs=[], cache_path=tmp_path / "c.json", usage=UsageHistory(tmp_path / "u.json"))
    idx._set_records(recs("Firefox", "Files", "Downloader"))
    return idx


def test_quick_results_sections(tmp_path):
    idx = _index(tmp_path)
    s = Settings(tmp_path / "s.json")
    places = [Place("xdg-downloads", "Downloads", "file:///home/u/Downloads", "folder-download")]
    items = build_results(idx, "down", s, places)
    assert [(i.kind, i.title) for i in items] == [("app", "Downloader"), ("place", "Downloads")]
    items = build_results(idx, "settings", s, places)
    assert any(i.kind == "command" and i.payload == "settings" for i in items)
    s.set("search_places", False)
    assert all(i.kind != "place" for i in build_results(idx, "down", s, places))


def test_quick_results_empty_query_shows_recent(tmp_path):
    idx = _index(tmp_path)
    s = Settings(tmp_path / "s.json")
    assert [i.title for i in build_results(idx, "", s, [])][:1] == ["Downloader"]  # alphabetical fallback
    idx.usage.record("files.desktop")
    items = build_results(idx, "", s, [])
    assert items[0].title == "Files" and items[0].section == "Recent"


def test_monitor_choice_and_placement():
    a = MonitorInfo("DP-2", Rect(0, 167, 1280, 1024), Rect(0, 167, 1280, 1024), False)
    b = MonitorInfo("HDMI-0", Rect(1280, 0, 1920, 1080), Rect(1280, 32, 1920, 1000), True)
    assert choose_monitor("primary", [a, b]) is b
    assert choose_monitor("DP-2", [a, b]) is a
    assert choose_monitor("HDMI-9", [a, b]) is b  # unplugged -> primary
    assert choose_monitor("primary", []) is None
    assert dock_origin("bottom", b.workarea, 600, 100) == (1280 + 660, 32 + 1000 - 100)
    assert dock_origin("left", a.geometry, 100, 600) == (0, 167 + 212)
    assert dock_origin("right", b.geometry, 100, 600) == (1280 + 1820, 240)
    assert centered(b.workarea, 680, 420, 0.25)[0] == 1280 + 620
