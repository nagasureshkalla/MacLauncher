"""'Make Nova the default' must be fully reversible and leave other settings alone."""

from nova_launcher.config import Settings
from nova_launcher.system import desktop_integration as integ
from nova_launcher.system import x11


class FakeGnome:
    def __init__(self):
        self.db = {
            ("org.gnome.shell.keybindings", "toggle-application-view"): "['<Super>a']",
            ("org.gnome.desktop.wm.keybindings", "switch-input-source"): "['<Super>space', 'XF86Keyboard']",
            ("org.gnome.desktop.wm.keybindings", "switch-input-source-backward"): "['<Shift><Super>space', '<Shift>XF86Keyboard']",
            (integ.MEDIA_KEYS, "custom-keybindings"): "['/org/user/own-shortcut/']",
        }
        self.extensions = {"ubuntu-dock@ubuntu.com", "ding@rastersoft.com"}

    def gsettings(self, *args):
        op, schema, key = args[0], args[1], args[2]
        if op == "get":
            return 0, self.db.get((schema, key), "@as []")
        if op == "set":
            self.db[(schema, key)] = args[3]
        elif op == "reset":
            self.db.pop((schema, key), None)
        return 0, ""

    def run(self, cmd):
        if cmd[:2] == ["gnome-extensions", "list"]:
            return 0, "\n".join(sorted(self.extensions))
        if cmd[:2] == ["gnome-extensions", "disable"]:
            self.extensions.discard(cmd[2])
        if cmd[:2] == ["gnome-extensions", "enable"]:
            self.extensions.add(cmd[2])
        return 0, ""


def _patch(monkeypatch):
    g = FakeGnome()
    monkeypatch.setattr(integ, "_gsettings", g.gsettings)
    monkeypatch.setattr(integ, "_run", g.run)
    return g


def test_set_default_then_restore_roundtrip(monkeypatch, tmp_path):
    g = _patch(monkeypatch)
    original = dict(g.db)
    s = Settings(tmp_path / "s.json")
    s.set("dock_autohide", True)

    integ.set_as_default(s)
    assert g.db[("org.gnome.shell.keybindings", "toggle-application-view")] == "@as []"
    assert g.db[("org.gnome.desktop.wm.keybindings", "switch-input-source")] == "['XF86Keyboard']"
    assert "ubuntu-dock@ubuntu.com" not in g.extensions and "ding@rastersoft.com" in g.extensions
    customs = integ._parse_strv(g.db[(integ.MEDIA_KEYS, "custom-keybindings")])
    assert "/org/user/own-shortcut/" in customs and len(customs) == 3
    assert integ.autostart_enabled()
    assert s["dock_autohide"] is False and s["dock_reserve_space"] is True
    assert s["shortcut_quick"] == "<Super>space" and s["shortcut_grid"] == "<Super>a"

    # Running it twice must not overwrite the true originals in the backup.
    integ.set_as_default(s)

    integ.restore_default(s)
    for k, v in original.items():
        assert g.db.get(k) == v, k
    assert "ubuntu-dock@ubuntu.com" in g.extensions
    assert not integ.autostart_enabled()
    assert s["dock_autohide"] is True and s["shortcut_quick"] == "<Super>slash"
    assert not integ.backup_path().exists()


def test_restore_without_backup_is_harmless(monkeypatch, tmp_path):
    _patch(monkeypatch)
    assert "nothing to restore" in integ.restore_default(Settings(tmp_path / "s.json"))[0]


def test_strut_values_multi_monitor():
    # HDMI-0 at x=1280 (1920x1080), DP-2 at y=167 makes the X screen 3200x1191.
    v = x11.strut_values("bottom", 78, (1280, 0, 1920, 1080), (3200, 1191))
    assert v[3] == 1191 - 1080 + 78 and (v[10], v[11]) == (1280, 3199)
    v = x11.strut_values("right", 70, (1280, 0, 1920, 1080), (3200, 1191))
    assert v[1] == 70 and (v[6], v[7]) == (0, 1079)
    v = x11.strut_values("left", 70, (0, 167, 1280, 1024), (3200, 1191))
    assert v[0] == 70 and (v[4], v[5]) == (167, 1190)
    assert x11.strut_values("bottom", 0, (0, 0, 10, 10), (10, 10)) == [0] * 12
