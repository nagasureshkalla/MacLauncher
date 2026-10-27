import json

import pytest

from nova_launcher.config import SCHEMA, Settings


def test_defaults_without_file(tmp_path):
    s = Settings(tmp_path / "s.json").load()
    assert s["dock_position"] == "bottom" and s.load_errors == []


def test_persistence_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    s = Settings(p).load()
    s.set("dock_icon_size", 64)
    s.set("pinned_apps", ["a.desktop", "b.desktop", "a.desktop"])
    s.set_folders({"f1": {"name": "Dev", "apps": ["x.desktop"]}})
    s2 = Settings(p).load()
    assert s2["dock_icon_size"] == 64
    assert s2["pinned_apps"] == ["a.desktop", "b.desktop"]  # de-duplicated
    assert s2.folders == {"f1": {"name": "Dev", "apps": ["x.desktop"]}}


def test_invalid_values_fall_back_to_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"dock_icon_size": 9999, "dock_position": "top", "theme": 3,
                             "dock_autohide": "yes", "grid_columns": True, "unknown": 1,
                             "dock_magnification": 2, "folders": {"bad": "x", "ok": {"name": "A", "apps": ["a", 5]}}}))
    s = Settings(p).load()
    assert s["dock_icon_size"] == SCHEMA["dock_icon_size"].default
    assert s["dock_position"] == "bottom"
    assert s["grid_columns"] == SCHEMA["grid_columns"].default  # bool is not int
    assert s["dock_magnification"] == 2.0  # int accepted for float
    assert s.folders == {"ok": {"name": "A", "apps": ["a"]}}
    assert len(s.load_errors) >= 5


def test_corrupt_file_is_moved_aside_not_deleted(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ not json")
    s = Settings(p).load()
    assert s["theme"] == "system"
    assert (tmp_path / "s.json.corrupt").read_text() == "{ not json"
    assert "corrupt" in s.load_errors[0]


def test_set_rejects_invalid_values(tmp_path):
    s = Settings(tmp_path / "s.json")
    with pytest.raises(ValueError):
        s.set("dock_position", "diagonal")
    with pytest.raises(ValueError):
        s.set("grid_rows", 100)


def test_listeners_and_failing_listener(tmp_path):
    s = Settings(tmp_path / "s.json")
    seen = []
    s.connect(lambda k: 1 / 0)  # must not break other listeners
    s.connect(seen.append)
    s.set("theme", "dark")
    s.set("theme", "dark")  # unchanged: no notification
    assert seen == ["theme"]


def test_import_validates(tmp_path):
    s = Settings(tmp_path / "s.json")
    assert s.import_dict([1, 2]) == ["imported config is not an object"]
    s.import_dict({"grid_rows": 4, "grid_columns": "many"})
    assert s["grid_rows"] == 4 and s["grid_columns"] == SCHEMA["grid_columns"].default


def test_returned_lists_are_copies(tmp_path):
    s = Settings(tmp_path / "s.json")
    s["pinned_apps"].append("evil.desktop")
    assert "evil.desktop" not in s["pinned_apps"]
