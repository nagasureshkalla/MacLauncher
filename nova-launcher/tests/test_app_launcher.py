import os
import time

from conftest import app_entry, write_desktop

from nova_launcher.core import app_launcher
from nova_launcher.core.desktop_entry import AppRecord, load_entry


def _record(tmp_path, body, name="t.desktop"):
    p = write_desktop(tmp_path / "apps", name, body)
    rec, reason = load_entry(str(p), name)
    assert rec is not None, reason
    return rec


def test_launch_runs_program_without_shell(tmp_path):
    marker = tmp_path / "launched"
    rec = _record(tmp_path, app_entry("Toucher", f"touch {marker}"))
    res = app_launcher.launch(rec)
    assert res.ok, res.error
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.02)
    assert marker.exists()


def test_shell_metacharacters_are_not_interpreted(tmp_path):
    marker = tmp_path / "pwned"
    # If Exec were run through a shell, "; touch …" would create the marker.
    rec = _record(tmp_path, app_entry("Evil", f"true ; touch {marker} && touch {marker}"))
    app_launcher.launch(rec)
    time.sleep(0.3)
    assert not marker.exists()


def test_missing_desktop_file_reports_failure(tmp_path):
    rec = AppRecord(id="gone-xyz.desktop", path=str(tmp_path / "gone.desktop"), name="Gone")
    res = app_launcher.launch(rec)
    assert not res.ok and "no longer installed" in res.error


def test_program_removed_after_indexing_reports_failure(tmp_path):
    exe = tmp_path / "bin-tool"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    rec = _record(tmp_path, app_entry("Tool", str(exe)))
    exe.unlink()
    res = app_launcher.launch(rec)
    assert not res.ok


def test_unknown_action_is_rejected(tmp_path):
    rec = _record(tmp_path, app_entry("A"))
    res = app_launcher.launch(rec, action="nope")
    assert not res.ok and "no action" in res.error


def test_desktop_action_launch(tmp_path):
    marker = tmp_path / "action-ran"
    rec = _record(tmp_path, app_entry("A", extra=f"Actions=go;\n\n[Desktop Action go]\nName=Go\nExec=touch {marker}"))
    assert app_launcher.launch(rec, action="go").ok
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.02)
    assert marker.exists()


def test_launch_returns_quickly_for_slow_apps(tmp_path):
    rec = _record(tmp_path, app_entry("Slow", "sleep 5"))
    t = time.perf_counter()
    assert app_launcher.launch(rec).ok
    assert time.perf_counter() - t < 0.5  # never waits for the app
    os.system("pkill -f '^sleep 5$' >/dev/null 2>&1")
