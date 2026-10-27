from conftest import app_entry, write_desktop

from nova_launcher.core import app_discovery
from nova_launcher.core.desktop_entry import AppRecord, load_entry


def test_discovers_apps_and_reads_metadata(tmp_path):
    d = tmp_path / "share/applications"
    write_desktop(d, "org.example.Editor.desktop", app_entry(
        "Example Editor", "true %F", "accessories-text-editor",
        "GenericName=Text Editor\nComment=Edit text\nKeywords=notes;code;\nCategories=Utility;TextEditor;\n"
        "Actions=new;\n\n[Desktop Action new]\nName=New Window\nExec=true --new"))
    recs, rep = app_discovery.scan([d])
    assert [r.id for r in recs] == ["org.example.Editor.desktop"]
    r = recs[0]
    assert r.name == "Example Editor"
    assert r.generic_name == "Text Editor"
    assert r.executable == "true"
    assert "notes" in r.keywords and "TextEditor" in r.categories
    assert r.actions[0].id == "new" and r.actions[0].name == "New Window"
    assert rep.scanned == 1 and rep.shown == 1


def test_subdirectory_ids_use_dashes(tmp_path):
    d = tmp_path / "apps"
    write_desktop(d / "kde", "konsole.desktop", app_entry("Konsole"))
    recs, _ = app_discovery.scan([d])
    assert recs[0].id == "kde-konsole.desktop"


def test_user_override_shadows_system_entry(tmp_path):
    user, system = tmp_path / "user", tmp_path / "system"
    write_desktop(system, "foo.desktop", app_entry("Foo System"))
    write_desktop(user, "foo.desktop", app_entry("Foo Custom"))
    recs, rep = app_discovery.scan([user, system])
    assert [r.name for r in recs] == ["Foo Custom"]
    assert str(system / "foo.desktop") in rep.shadowed


def test_hidden_override_removes_system_app(tmp_path):
    user, system = tmp_path / "user", tmp_path / "system"
    write_desktop(system, "foo.desktop", app_entry("Foo"))
    write_desktop(user, "foo.desktop", "Type=Application\nName=Foo\nExec=true\nHidden=true")
    recs, rep = app_discovery.scan([user, system])
    assert recs == []
    assert rep.skipped[str(user / "foo.desktop")] == "hidden"


def test_nodisplay_and_non_application_entries_are_skipped(tmp_path):
    d = tmp_path / "apps"
    write_desktop(d, "a.desktop", app_entry("A", extra="NoDisplay=true"))
    write_desktop(d, "link.desktop", "Type=Link\nName=Web\nURL=https://example.com")
    write_desktop(d, "b.desktop", app_entry("B"))
    recs, rep = app_discovery.scan([d])
    assert [r.name for r in recs] == ["B"]
    assert rep.skipped[str(d / "a.desktop")] == "nodisplay"


def test_onlyshowin_other_desktop_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    d = tmp_path / "apps"
    write_desktop(d, "kde.desktop", app_entry("KDE Only", extra="OnlyShowIn=KDE;"))
    recs, _ = app_discovery.scan([d])
    assert recs == []


def test_malformed_and_missing_binary_entries_do_not_break_scan(tmp_path):
    d = tmp_path / "apps"
    d.mkdir()
    (d / "garbage.desktop").write_bytes(b"\x00\xff not a key file [[[")
    write_desktop(d, "noexec.desktop", "Type=Application\nName=No Exec")
    write_desktop(d, "tryexec.desktop", app_entry("Missing", extra="TryExec=/nonexistent/binary-xyz"))
    write_desktop(d, "ok.desktop", app_entry("Works"))
    recs, rep = app_discovery.scan([d])
    assert [r.name for r in recs] == ["Works"]
    assert rep.skipped[str(d / "garbage.desktop")].startswith("invalid")
    assert rep.skipped[str(d / "tryexec.desktop")].startswith("unavailable")


def test_duplicate_apps_from_two_sources_are_collapsed(tmp_path):
    d = tmp_path / "apps"
    write_desktop(d, "org.foo.App.desktop", app_entry("Foo", "true"))
    write_desktop(d, "foo_foo.desktop", app_entry("Foo", "true --snap"))
    write_desktop(d, "other.desktop", app_entry("Foo", "false"))  # same name, different program
    recs, rep = app_discovery.scan([d])
    assert len(recs) == 2
    assert len(rep.duplicates) == 1


def test_missing_directories_are_ignored(tmp_path):
    recs, rep = app_discovery.scan([tmp_path / "nope"])
    assert recs == [] and rep.scanned == 0


def test_record_roundtrip():
    r = AppRecord(id="x.desktop", path="/x.desktop", name="X", keywords=("a",), categories=("B",))
    assert AppRecord.from_dict(r.to_dict()) == r


def test_winner_for_id_resolves_subdirectories(tmp_path):
    d = tmp_path / "apps"
    write_desktop(d / "kde", "konsole.desktop", app_entry("Konsole"))
    assert app_discovery.winner_for_id("kde-konsole.desktop", [d]) == d / "kde" / "konsole.desktop"


def test_missing_icon_is_not_an_error(tmp_path):
    d = tmp_path / "apps"
    p = write_desktop(d, "x.desktop", app_entry("X", icon="this-icon-does-not-exist-anywhere"))
    rec, reason = load_entry(str(p), "x.desktop")
    assert reason == "ok" and rec.icon == "this-icon-does-not-exist-anywhere"


def test_real_system_scan_is_fast():
    """Realistic data set: the machine's own desktop entries."""
    import time

    t = time.perf_counter()
    recs, rep = app_discovery.scan()
    elapsed = time.perf_counter() - t
    assert rep.scanned > 0
    assert elapsed < 1.0, f"scan took {elapsed:.2f}s"
