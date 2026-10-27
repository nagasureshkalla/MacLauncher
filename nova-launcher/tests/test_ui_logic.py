"""Grid organisation, dock magnification, quick-launcher results, placement."""

from nova_launcher.config import Settings
from nova_launcher.core.app_index import AppIndex, UsageHistory
from nova_launcher.core.desktop_entry import AppRecord
from nova_launcher.core.file_manager import Place
from nova_launcher.system.monitor_support import MonitorInfo, Rect, centered, choose_monitor, dock_origin
from nova_launcher.core.running_apps import WindowInfo, group_windows, next_window
from nova_launcher.system.x11 import strut_values
from nova_launcher.ui.dock import magnify
from nova_launcher.ui.dynamic_island import island_header, island_width, reserve_thickness
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


def test_widget_commands_start_with_all_applications(tmp_path):
    from nova_launcher.ui.commands_widget import TERMINAL_COMMANDS, widget_commands

    s = Settings(tmp_path / "s.json")
    s.set("shortcut_grid", "<Super>a")
    s.set("shortcut_quick", "<Super>space")
    cmds = widget_commands(s)
    assert cmds[0].id == "grid" and cmds[0].title == "All Applications"
    assert cmds[0].shortcut == "Windows+A" and cmds[1].shortcut == "Windows+Space"
    ids = [c.id for c in cmds]
    assert ids == ["grid", "quick", "files", "downloads", "toggle-dock", "settings", "diagnose", "repair", "quit"]
    assert next(c for c in cmds if c.id == "toggle-dock").title == "Hide Dock"
    s.set("dock_enabled", False)
    assert next(c for c in widget_commands(s) if c.id == "toggle-dock").title == "Show Dock"
    assert "toggle-dock" not in [c.id for c in widget_commands(s, dock_supported=False)]
    s.set("windows_key_action", "grid")
    assert widget_commands(s)[0].shortcut == "Windows"  # a plain tap opens All Applications
    # Every CLI flag the widget advertises must exist.
    assert all(c.startswith("nova-launcher --") for c, _ in TERMINAL_COMMANDS)


def test_dock_starts_with_all_applications(tmp_path):
    from types import SimpleNamespace

    from nova_launcher.ui.dock import DockWindow

    idx = _index(tmp_path)
    s = Settings(tmp_path / "s.json")
    s.set("pinned_apps", ["files.desktop", "missing.desktop"])
    fake = SimpleNamespace(app=SimpleNamespace(settings=s, index=idx), running={})
    kinds = [(sl.kind, sl.record.name if sl.record else None) for sl in DockWindow.slots(fake)]
    assert kinds[0] == ("launchpad", None)
    assert kinds[1] == ("pinned", "Files")
    assert kinds[-1] == ("trash", None)


def test_themed_icon_candidates_for_snap_and_regular_apps():
    from nova_launcher.ui.widgets import themed_candidates

    snap = AppRecord(id="firefox_firefox.desktop", path="", name="Firefox", executable="firefox",
                     icon="/snap/firefox/current/default256.png")
    assert themed_candidates(snap)[:2] == ["firefox", "firefox_firefox"]
    regular = AppRecord(id="org.gnome.Nautilus.desktop", path="", name="Files", executable="nautilus",
                        icon="org.gnome.Nautilus")
    c = themed_candidates(regular)
    assert c[0] == "org.gnome.Nautilus" and "nautilus" in c


def test_blur_render_cover_crops_to_exact_size(tmp_path):
    from PIL import Image

    from nova_launcher.ui.wallpaper import render_blur

    src = tmp_path / "wall.png"
    im = Image.new("RGB", (400, 100), (255, 0, 0))
    for x in range(200, 400):
        for y in range(100):
            im.putpixel((x, y), (0, 0, 255))  # left half red, right half blue
    im.save(src)
    out = tmp_path / "blur.png"
    render_blur(str(src), 160, 90, str(out))
    res = Image.open(out)
    assert res.size == (160, 90)
    # Zoom/cover crops the centre: both colours present, edges dominated by each.
    assert res.getpixel((5, 45))[0] > res.getpixel((5, 45))[2]
    assert res.getpixel((154, 45))[2] > res.getpixel((154, 45))[0]


def _rec(app_id, name, wm=""):
    return AppRecord(id=app_id, path=f"/x/{app_id}", name=name, startup_wm_class=wm)


def test_running_apps_grouped_most_recent_first_with_focused_app_in_front():
    recs = {"Firefox": _rec("firefox.desktop", "Firefox", "Firefox"),
            "Code": _rec("code.desktop", "Code", "Code"),
            "nova-launcher": _rec("io.github.nova.Launcher.desktop", "Nova", "nova-launcher")}
    wins = [  # stacking order: bottom-most first
        WindowInfo(1, "navigator", "Firefox", "Mail"),
        WindowInfo(2, "code", "Code", "main.py"),
        WindowInfo(3, "navigator", "Firefox", "Docs"),
        WindowInfo(4, "nova-launcher", "nova-launcher", "Settings"),
        WindowInfo(5, "xeyes", "XEyes", ""),
    ]
    apps = group_windows(wins, active=2, resolve=lambda c, i: recs.get(c),
                         exclude={"io.github.nova.Launcher.desktop"})
    assert [a.name for a in apps] == ["Code", "XEyes", "Firefox"]  # focused first, then most recent
    assert apps[0].active
    ff = apps[2]
    assert ff.windows == [3, 1] and ff.titles == ["Docs", "Mail"]  # topmost window first
    assert apps[1].record is None and apps[1].key == "wmclass:xeyes" and apps[1].icon == "xeyes"


def test_next_window_cycles_only_when_app_is_focused():
    assert next_window([3, 1], active=9) == 3  # not focused: most recent window
    assert next_window([3, 1], active=3) == 1  # focused: cycle
    assert next_window([3, 1], active=1) == 3
    assert next_window([], active=1) == 0


def test_island_geometry():
    assert island_width(1) == island_width(0) >= 300
    assert island_width(6) == island_width(20)  # at most one row's width
    assert island_width(4) < island_width(6)
    assert reserve_thickness(32) > 32 + 36
    from nova_launcher.core.running_apps import RunningApp
    assert island_header([]) == "No apps running"
    assert island_header([RunningApp("t", "Terminal", "", None, [1, 2, 3], ["a", "b", "c"])]) == "3 windows · 1 app"
    v = strut_values("top", 76, (1280, 0, 1920, 1080), (3200, 1191))
    assert v[2] == 76 and (v[8], v[9]) == (1280, 3199) and sum(v) == 76 + 1280 + 3199
