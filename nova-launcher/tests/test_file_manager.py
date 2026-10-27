import time

from gi.repository import Gio, GLib

from nova_launcher.core import file_manager as fm


def _spin(cond, timeout=5.0):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)
    return cond()


def test_standard_places_do_not_touch_disk():
    places = fm.standard_places()
    assert places[0].id == "home" and places[0].uri.startswith("file://")
    assert any(p.uri == "trash:///" for p in places)


def test_mounts_listing_never_raises():
    class Broken:
        def get_mounts(self):
            raise RuntimeError("gvfs died")

    assert fm.mounted_places(Broken()) == []
    assert isinstance(fm.mounted_places(), list)


class _FakeBus:
    """Stands in for a file manager that never answers (the 5-minute hang)."""

    def call(self, *args):
        callback = args[-1]
        timeout_ms = args[7]

        def fire():
            class Res:
                pass

            callback(self, Res())
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(timeout_ms, fire)

    def call_finish(self, _res):
        raise GLib.Error.new_literal(Gio.io_error_quark(), "Timeout was reached", Gio.IOErrorEnum.TIMED_OUT)


def test_unresponsive_file_manager_times_out_and_reports(monkeypatch):
    monkeypatch.setattr(Gio, "bus_get_sync", lambda *_: _FakeBus())
    launched = []
    monkeypatch.setattr(fm.FileManager, "_launch_default", lambda self, uri, done, reason="": (
        launched.append(uri), done(True, reason)))
    slow, done = [], []
    m = fm.FileManager(timeout_ms=300, on_slow=slow.append)
    t = time.monotonic()
    m.open("file:///tmp", done=lambda ok, msg: done.append((ok, msg)))
    assert time.monotonic() - t < 0.1  # open() itself never blocks
    assert _spin(lambda: done)
    assert slow and "did not respond" in slow[0]
    # Either a fallback launch happened, or (default FM is the stuck Nautilus) a clear failure.
    assert launched == ["file:///tmp"] or done[0][0] is False
