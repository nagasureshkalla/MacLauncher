from nova_launcher.system import diagnostics as dg
from nova_launcher.system import desktop_integration as integ


def test_run_reports_missing_utility():
    rc, out = dg.run(["definitely-not-a-real-binary-xyz"])
    assert rc == 127 and "not installed" in out


def test_run_times_out():
    rc, out = dg.run(["sleep", "5"], timeout=0.3)
    assert rc == 124


def test_tracker_problem_detected_and_repairs_offered(monkeypatch, tmp_path):
    def fake_run(cmd, timeout=15):
        if cmd[0] == "tracker3":
            return 0, "Currently indexed: 0 files, 5 folders\n3592 recorded failures"
        if cmd[:2] == ["gsettings", "get"]:
            return 0, "['&DESKTOP', '&DOCUMENTS']"
        if cmd[0] == "journalctl":
            return 0, "\n".join(f"2026-10-26T17:{i // 60:02d}:{i % 60:02d} host tracker-miner-f[1]: Extractor subprocess died "
                                "unexpectedly" for i in range(150))
        return 127, "missing"

    monkeypatch.setattr(dg, "run", fake_run)
    monkeypatch.setattr(dg, "_user_dir", lambda name: str(tmp_path))
    d = dg.Diagnostics()
    repairs = d.check_tracker()
    f = d.findings[-1]
    assert f.status == dg.PROBLEM and f.verified
    assert any("crashed 150 times" in e for e in f.evidence)
    assert [r.id for r in repairs] == ["tracker-scope", "tracker-reset"]
    report = d.report_markdown(repairs)
    assert "PROBLEM / VERIFIED" in report and "gsettings reset" in report


def test_full_run_is_read_only_and_produces_report():
    d = dg.Diagnostics()
    repairs = d.run_all()
    md = d.report_markdown(repairs)
    assert md.startswith("# Nova Launcher") and "## Details" in md
    assert {f.area for f in d.findings} >= {"memory", "disk", "dbus", "nautilus", "indexing", "apps"}


def test_repair_does_nothing_without_yes(monkeypatch):
    ran = []
    monkeypatch.setattr(dg, "run", lambda cmd, timeout=60: (ran.append(cmd), (0, ""))[1])
    action = dg.RepairAction("x", "X", "impact", [["echo", "hi"]], "undo")
    log = dg.repair([action], ask=lambda _p: "")
    assert ran == [] and log == ["skipped x"]
    dg.repair([action], ask=lambda _p: "y")
    assert ran == [["echo", "hi"]]


def test_strv_parsing_roundtrip():
    assert integ._parse_strv("@as []") == []
    assert integ._parse_strv("['/a/', '/b/']") == ["/a/", "/b/"]
    assert integ._strv([]) == "@as []"
    assert integ._strv(["/a/"]) == "['/a/']"


def test_shortcut_install_keeps_other_custom_shortcuts(monkeypatch):
    store = {"custom-keybindings": "['/org/other/']"}
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "get" and args[2] == "custom-keybindings":
            return 0, store["custom-keybindings"]
        if args[0] == "set" and args[2] == "custom-keybindings":
            store["custom-keybindings"] = args[3]
        return 0, ""

    monkeypatch.setattr(integ, "_gsettings", fake)
    msg = integ.install_shortcut("quick", "<Super>slash", "toggle-quick")
    assert "gapplication action io.github.nova.Launcher toggle-quick" in msg
    assert "/org/other/" in store["custom-keybindings"] and "nova-launcher-quick" in store["custom-keybindings"]
    integ.remove_shortcuts()
    assert integ._parse_strv(store["custom-keybindings"]) == ["/org/other/"]


def test_autostart_toggle(tmp_path):
    assert not integ.autostart_enabled()
    integ.set_autostart(True)
    text = integ.autostart_path().read_text()
    assert "nova-launcher --background" in text and str(tmp_path) in str(integ.autostart_path())
    integ.set_autostart(False)
    assert not integ.autostart_enabled()
