"""Command-line entry point.

Diagnostics run without starting the GUI (and without GTK), so they work
even when the desktop itself is misbehaving.
"""

from __future__ import annotations

import argparse
import sys


def _cli_tools(argv: list[str]) -> int | None:
    if any(a in argv for a in ("--set-default", "--restore-default")):
        return _default_launcher("--set-default" in argv)
    if not any(a in argv for a in ("--diagnose", "--repair", "--version")):
        return None
    p = argparse.ArgumentParser(prog="nova-launcher")
    p.add_argument("--diagnose", action="store_true", help="run read-only system diagnostics")
    p.add_argument("--repair", action="store_true", help="diagnose, then offer fixes (asks before each)")
    p.add_argument("--report", metavar="FILE", help="write the Markdown report to FILE")
    p.add_argument("--json", action="store_true", help="print findings as JSON")
    p.add_argument("--version", action="store_true")
    args, _ = p.parse_known_args(argv[1:])

    from . import __version__

    if args.version:
        print(f"Nova Launcher {__version__}")
        return 0

    from .system.diagnostics import Diagnostics, repair

    d = Diagnostics()
    repairs = d.run_all(progress=lambda name: print(f"  checking {name}…", file=sys.stderr))
    if args.json:
        print(d.to_json())
    else:
        report = d.report_markdown(repairs)
        if args.report:
            with open(args.report, "w", encoding="utf-8") as fh:
                fh.write(report)
            print(f"report written to {args.report}", file=sys.stderr)
        else:
            print(report)
    if args.repair:
        repair(repairs)
    return 0


def _default_launcher(enable: bool) -> int:
    """Make Nova the default launcher (or undo it), then restart Nova."""
    import subprocess

    from . import APP_ID
    from .config import Settings
    from .system import desktop_integration as integ

    # The running instance owns settings.json; stop it so it cannot overwrite our changes.
    import time

    def running() -> bool:
        return subprocess.run(["gdbus", "call", "--session", "--dest", APP_ID, "--object-path", "/",
                               "--method", "org.freedesktop.DBus.Peer.Ping"], capture_output=True).returncode == 0

    if running():
        subprocess.run(["gapplication", "action", APP_ID, "quit"], capture_output=True, timeout=10)
        for _ in range(100):
            if not running():
                break
            time.sleep(0.05)
    settings = Settings().load()
    lines = integ.set_as_default(settings) if enable else integ.restore_default(settings)
    for line in lines:
        print(" -", line)
    if enable:
        subprocess.Popen([sys.argv[0] if sys.argv[0].endswith("nova-launcher") else "nova-launcher", "--background"],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("Nova restarted. Undo everything with:  nova-launcher --restore-default")
    else:
        print("Nova stopped. Start it again any time with:  nova-launcher")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    rc = _cli_tools(argv)
    if rc is not None:
        return rc
    from .application import NovaApplication

    return NovaApplication().run(argv)


if __name__ == "__main__":
    sys.exit(main())
