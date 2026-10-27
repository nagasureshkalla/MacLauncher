"""Live GTK tests: keyboard navigation, shortcut actions, startup without cache.

They create real (briefly shown) windows, so they need a graphical session.
A unique application ID keeps them away from a real running Nova instance.
"""

import itertools
import os
import time

import pytest
from conftest import app_entry, needs_display, write_desktop

pytestmark = needs_display
_ids = itertools.count()


@pytest.fixture
def app(tmp_path, monkeypatch):
    apps = tmp_path / "data/applications"
    for n in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta"):
        write_desktop(apps, f"{n.lower()}.desktop", app_entry(n, "true"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "nosys"))

    import nova_launcher.application as appmod
    from gi.repository import Gio

    class TestApp(appmod.NovaApplication):
        def __init__(self):
            super().__init__()
            self.set_application_id(f"io.github.nova.Test{os.getpid()}n{next(_ids)}")
            self.set_flags(Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.NON_UNIQUE)

    a = TestApp()
    a.settings.set("dock_enabled", False)
    a.settings.set("grid_columns", 3)
    a.settings.set("grid_rows", 2)
    a.register(None)  # runs do_startup
    yield a
    a.quit_app()


def spin(cond, timeout=5.0):
    from gi.repository import GLib

    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)
    return cond()


def key(window, keyval):
    window._on_key(None, keyval, 0, 0)


def test_index_loads_without_cache(app):
    assert spin(lambda: len(app.index) == 8)


def test_grid_keyboard_navigation_and_paging(app):
    from gi.repository import Gdk

    assert spin(lambda: len(app.index) == 8)
    g = app.grid
    g.open()
    assert spin(lambda: g.get_visible())
    assert g.carousel.get_n_pages() == 2  # 8 apps, 6 per page
    key(g, Gdk.KEY_Right)
    assert g.selected == 0
    key(g, Gdk.KEY_Right)
    key(g, Gdk.KEY_Down)
    assert g.selected == 4  # moved one row down (3 columns)
    key(g, Gdk.KEY_Down)
    assert g.selected == 4  # stays on page at the bottom row
    for _ in range(3):
        key(g, Gdk.KEY_Right)
    assert g.selected == 7  # crossed onto page 2, clamped at the end
    assert spin(lambda: round(g.carousel.get_position()) == 1)
    key(g, Gdk.KEY_Escape)
    assert spin(lambda: not g.get_visible())


def test_grid_search_filters_and_enter_launches_first(app, monkeypatch):
    assert spin(lambda: len(app.index) == 8)
    launched = []
    monkeypatch.setattr(app, "launch_app", lambda rec, source=None, action=None: (
        launched.append(rec.id), type("R", (), {"ok": True, "error": ""})())[1])
    g = app.grid
    g.open()
    g.search.set_text("gam")
    assert g.result_keys == ["app:gamma.desktop"]
    g.activate_selected()
    assert launched == ["gamma.desktop"]


def test_quick_launcher_arrows_enter_escape(app, monkeypatch):
    from gi.repository import Gdk

    assert spin(lambda: len(app.index) == 8)
    launched = []
    monkeypatch.setattr(app, "launch_app", lambda rec, source=None, action=None: (
        launched.append(rec.id), type("R", (), {"ok": True, "error": ""})())[1])
    q = app.quick
    q.open()
    q.entry.set_text("eta")
    assert [i.title for i in q.items][:2] == ["Eta", "Beta"] or q.items[0].title == "Eta"
    key(q, Gdk.KEY_Down)
    assert q.selected_index() == 1
    key(q, Gdk.KEY_Up)
    key(q, Gdk.KEY_Return)
    assert launched == ["eta.desktop"]
    q.open()
    key(q, Gdk.KEY_Escape)
    assert not q.get_visible()


def test_shortcut_actions_toggle_windows(app):
    assert spin(lambda: len(app.index) == 8)
    app.activate_action("toggle-quick", None)
    assert spin(lambda: app.quick.get_visible())
    app.activate_action("toggle-quick", None)
    assert spin(lambda: not app.quick.get_visible())
    app.activate_action("toggle-grid", None)
    assert spin(lambda: app.grid.get_visible())
    app.activate_action("toggle-grid", None)
    assert spin(lambda: not app.grid.get_visible())


def test_commands_widget_runs_commands(app):
    from nova_launcher.system import x11

    if not app.dock_supported():
        pytest.skip("commands widget needs X11")
    assert spin(lambda: len(app.index) == 8)
    app.settings.set("widget_enabled", True)
    app.update_widget_visibility()
    w = app.widget
    assert spin(lambda: w.get_visible())
    assert w.list.get_row_at_index(0).command_id == "grid"
    w.run("grid")
    assert spin(lambda: app.grid.get_visible())
    app.grid.dismiss()
    app.settings.set("widget_collapsed", True)
    assert not w.body.get_reveal_child()
    app.settings.set("widget_enabled", False)
    assert spin(lambda: not w.get_visible())
