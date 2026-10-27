import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch):
    """Never touch the real user's config/cache/state during tests."""
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                     ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        d = tmp_path / sub
        d.mkdir()
        monkeypatch.setenv(var, str(d))
    yield


def write_desktop(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text("[Desktop Entry]\n" + body.strip() + "\n")
    return p


APP = """
Type=Application
Name={name}
Exec={exec}
Icon={icon}
{extra}
"""


def app_entry(name, exec_="true", icon="utilities-terminal", extra=""):
    return APP.format(name=name, exec=exec_, icon=icon, extra=extra)


needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"),
                                   reason="needs a graphical session")
