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
            ("org.gnome.mutter", "overlay-key"): "'Super_L'",
            ("org.gnome.shell.keybindings", "toggle-overview"): "@as []",
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
    assert g.db[("org.gnome.mutter", "overlay-key")] == ""  # tap no longer opens the overview...
    assert g.db[("org.gnome.shell.keybindings", "toggle-overview")] == "['<Super>s']"  # ...it is on Windows+S
    assert s["windows_key_action"] == "grid"
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
    assert s["windows_key_action"] == "overview"
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


def test_windows_key_setting_can_be_switched_back(monkeypatch, tmp_path):
    g = _patch(monkeypatch)
    integ.set_as_default(Settings(tmp_path / "s.json"))
    assert "Quick Search" in integ.apply_windows_key("quick")
    assert g.db[("org.gnome.mutter", "overlay-key")] == ""
    integ.apply_windows_key("overview")
    assert g.db[("org.gnome.mutter", "overlay-key")] == "Super_L"


def test_old_backup_gains_new_keys_without_losing_originals(monkeypatch, tmp_path):
    """A backup made before the Windows-key feature must still restore overlay-key."""
    import json

    g = _patch(monkeypatch)
    integ.backup_path().parent.mkdir(parents=True, exist_ok=True)
    integ.backup_path().write_text(json.dumps({"toggle_application_view": "['<Super>a']"}))
    integ.apply_windows_key("grid")
    data = json.loads(integ.backup_path().read_text())
    assert data["toggle_application_view"] == "['<Super>a']"
    assert data["overlay_key"] == "'Super_L'"
    integ.restore_default(Settings(tmp_path / "s.json"))
    assert g.db[("org.gnome.mutter", "overlay-key")] == "'Super_L'"


def _detector(times):
    taps = []
    it = iter(times)
    d = x11.SuperTapDetector({133, 134}, lambda: taps.append(1), clock=lambda: next(it))
    return d, taps


def test_windows_key_tap_detection():
    P, R, B = x11.XI_RawKeyPress, x11.XI_RawKeyRelease, x11.XI_RawButtonPress
    d, taps = _detector([0.0, 0.1])
    d.feed(P, 133); d.feed(R, 133)
    assert taps == [1]  # clean tap

    d, taps = _detector([0.0, 0.2])
    d.feed(P, 133); d.feed(P, 38); d.feed(R, 38); d.feed(R, 133)
    assert taps == []  # Windows+A is a shortcut, not a tap

    d, taps = _detector([0.0, 0.2])
    d.feed(P, 134); d.feed(B, 1); d.feed(R, 134)
    assert taps == []  # Windows+click (window drag)

    d, taps = _detector([0.0, 1.5])
    d.feed(P, 133); d.feed(R, 133)
    assert taps == []  # held too long

    d, taps = _detector([0.0, 0.3])
    d.feed(P, 133); d.feed(P, 133); d.feed(P, 133); d.feed(R, 133)
    assert taps == [1]  # auto-repeat ignored

    d, taps = _detector([0.0])
    d.feed(R, 133); d.feed(P, 38); d.feed(R, 38)
    assert taps == []  # stray release / other keys alone do nothing
