"""Read-only system diagnostics for slow app / file-manager startup.

Every check only *reads* system state.  Each finding records whether it was
verified (directly observed) or is a possible cause (inferred), the evidence,
and a recommendation.  ``repair()`` is a separate, interactive step that
shows each proposed change and asks before applying it.

Run:  nova-launcher --diagnose [--report FILE]
      nova-launcher --repair
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

OK, INFO, WARN, PROBLEM = "ok", "info", "warning", "problem"
_RANK = {PROBLEM: 0, WARN: 1, INFO: 2, OK: 3}


@dataclass
class Finding:
    area: str
    title: str
    status: str = OK
    verified: bool = True  # False => "possible cause", not directly observed
    evidence: list[str] = field(default_factory=list)
    likely_cause: str = ""
    recommendation: str = ""


@dataclass
class RepairAction:
    id: str
    title: str
    impact: str
    commands: list[list[str]]
    undo: str


def run(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    """Run a read-only command; never raises.  rc 127 = missing utility."""
    if shutil.which(cmd[0]) is None:
        return 127, f"{cmd[0]}: not installed"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C"})
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)}: timed out after {timeout}s"
    except OSError as exc:
        return 126, str(exc)


MISSING_HINTS = {
    "iostat": "sudo apt install sysstat",
    "tracker3": "sudo apt install tracker",
    "gnome-extensions": "part of gnome-shell",
    "nvidia-smi": "only present with the NVIDIA proprietary driver",
    "journalctl": "part of systemd",
}


class Diagnostics:
    def __init__(self, journal_since: str = "-3days"):
        self.findings: list[Finding] = []
        self.missing: list[str] = []
        self.journal_since = journal_since
        self._journal_cache: str | None = None
        self.started = time.time()

    # -- helpers -----------------------------------------------------------
    def _cmd(self, cmd: list[str], timeout: float = 15) -> tuple[int, str]:
        rc, out = run(cmd, timeout)
        if rc == 127 and cmd[0] not in self.missing:
            self.missing.append(cmd[0])
        return rc, out

    def _journal_warnings(self) -> str:
        if self._journal_cache is None:
            _, self._journal_cache = self._cmd(
                ["journalctl", "--user", "-b", "-p", "warning", "--no-pager", "-q", "-o", "short-iso"], timeout=30)
            _, system = self._cmd(["journalctl", "-b", "-p", "warning", "--no-pager", "-q", "-o", "short-iso"], timeout=30)
            # --user may be empty on some setups; merge both, dedupe lines.
            self._journal_cache = "\n".join(dict.fromkeys((self._journal_cache + "\n" + system).splitlines()))
        return self._journal_cache

    def add(self, f: Finding) -> Finding:
        self.findings.append(f)
        return f

    # -- checks ------------------------------------------------------------
    def check_session(self) -> None:
        st = os.environ.get("XDG_SESSION_TYPE", "unknown")
        de = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
        _, shell_ver = self._cmd(["gnome-shell", "--version"])
        self.add(Finding("session", "Desktop session", INFO, evidence=[f"session={st}", f"desktop={de}", shell_ver]))

    def check_memory(self) -> None:
        mem = _meminfo()
        f = Finding("memory", "Memory and swap")
        total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        swap_total, swap_free = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
        f.evidence.append(f"RAM {avail / 1048576:.1f} GiB available of {total / 1048576:.1f} GiB")
        if swap_total:
            used_pct = 100 * (swap_total - swap_free) / swap_total
            f.evidence.append(f"swap {used_pct:.0f}% used of {swap_total / 1048576:.1f} GiB")
        psi = _read("/proc/pressure/memory")
        if psi:
            f.evidence.append("PSI memory: " + psi.splitlines()[0])
            m = re.search(r"avg300=([\d.]+)", psi)
            if m and float(m.group(1)) > 5:
                f.status, f.likely_cause = PROBLEM, "Sustained memory pressure stalls every process, including Nautilus."
        vm = _vmstat()
        if vm.get("pswpout", 0) > 250_000:
            f.evidence.append(f"pages swapped out since boot: {vm['pswpout']:,} ({vm['pswpout'] * 4 / 1048576:.1f} GiB)")
            if f.status == OK:
                f.status = INFO
                f.likely_cause = ("Idle desktop processes have been swapped out; their first interaction after a "
                                  "long idle period pays a (usually sub-second on NVMe) swap-in cost.")
        self.add(f)

    def check_disk(self) -> None:
        f = Finding("disk", "Disk space and I/O")
        for mount in ("/", str(Path.home())):
            try:
                st = os.statvfs(mount)
                free = st.f_bavail * st.f_frsize / 1e9
                pct = 100 - 100 * st.f_bavail / max(st.f_blocks, 1)
                f.evidence.append(f"{mount}: {pct:.0f}% used, {free:.0f} GB free")
                if free < 5:
                    f.status, f.recommendation = PROBLEM, "Free disk space; GLib/GTK caches and the tracker DB fail when full."
            except OSError:
                pass
        psi = _read("/proc/pressure/io")
        if psi:
            f.evidence.append("PSI io: " + psi.splitlines()[0])
            m = re.search(r"avg300=([\d.]+)", psi)
            if m and float(m.group(1)) > 10:
                f.status = WARN
                f.likely_cause = "Processes are frequently stalled on I/O."
        rc, out = self._cmd(["iostat", "-dx", "1", "2"], timeout=10)
        if rc == 0:
            last = out.split("Device")[-1].splitlines()
            hdr = ("Device" + last[0]).split()
            for line in last[1:]:
                cols = line.split()
                if len(cols) == len(hdr) and "%util" in hdr and not cols[0].startswith(("loop", "ram", "zram")):
                    util = float(cols[hdr.index("%util")])
                    if util > 50:
                        f.status = WARN
                    f.evidence.append(f"{cols[0]}: util {util:.0f}%")
        self.add(f)

    def check_cpu_hogs(self) -> None:
        f = Finding("cpu", "Desktop process CPU / state")
        rc, out = self._cmd(["ps", "-eo", "pid,pcpu,rss,stat,comm", "--sort=-pcpu"])
        if rc != 0:
            return
        lines = out.splitlines()[1:]
        f.evidence.append("top: " + "; ".join(" ".join(l.split()[1:2] + l.split()[4:5]) + "%" for l in lines[:5]))
        for line in lines:
            cols = line.split(None, 4)
            if len(cols) < 5:
                continue
            pid, cpu, rss, stat, comm = cols
            if comm in ("gnome-shell", "nautilus", "tracker-miner-f", "tracker-extract", "Xorg", "gvfsd"):
                if "D" in stat:
                    f.status = PROBLEM
                    f.evidence.append(f"{comm} ({pid}) is in uninterruptible I/O wait (D state)")
                if float(cpu) > 50:
                    f.status = WARN if f.status == OK else f.status
                    f.evidence.append(f"{comm} ({pid}) at {cpu}% CPU")
        self.add(f)

    def check_dbus(self) -> None:
        f = Finding("dbus", "D-Bus responsiveness of desktop services")
        services = ["org.gnome.Shell", "org.gnome.Nautilus", "org.freedesktop.FileManager1",
                    "org.freedesktop.Tracker3.Miner.Files", "org.freedesktop.portal.Desktop", "org.gtk.vfs.Daemon"]
        for svc in services:
            t0 = time.perf_counter()
            rc, out = self._cmd(["gdbus", "call", "--session", "--dest", svc, "--object-path", "/",
                                 "--method", "org.freedesktop.DBus.Peer.Ping", "--timeout", "10"], timeout=15)
            ms = (time.perf_counter() - t0) * 1000
            if rc == 0:
                f.evidence.append(f"{svc}: {ms:.0f} ms")
                if ms > 1000:
                    f.status = PROBLEM
                    f.likely_cause = f"{svc} is slow to answer D-Bus calls; apps that depend on it will block."
            elif "ServiceUnknown" in out or "not activatable" in out:
                f.evidence.append(f"{svc}: not running/activatable")
            else:
                f.status = PROBLEM
                f.evidence.append(f"{svc}: FAILED after {ms:.0f} ms ({out[:120]})")
        # Historical GApplication single-instance hangs.
        hangs = [l for l in self._journal_warnings().splitlines()
                 if "Unable to register the application" in l or "Timeout was reached" in l]
        if hangs:
            f.status = WARN if f.status == OK else f.status
            f.evidence.append(f"{len(hangs)} 'Unable to register'/'Timeout was reached' errors this boot, e.g.: "
                              + hangs[-1][:180])
            f.likely_cause = ("An app's existing instance stopped answering D-Bus; a second launch then waits "
                              "for the D-Bus timeout (25 s per call) before failing. This is the classic "
                              "'click and nothing happens for minutes' pattern.")
        self.add(f)

    def check_nautilus(self) -> None:
        f = Finding("nautilus", "Files (Nautilus)")
        rc, out = self._cmd(["pgrep", "-a", "-x", "nautilus"])
        procs = out.splitlines() if rc == 0 else []
        f.evidence.append(f"running instances: {len(procs)}")
        for p in procs:
            pid = p.split()[0]
            status = _read(f"/proc/{pid}/status")
            state = re.search(r"State:\s+(\S+)", status or "")
            rss = re.search(r"VmRSS:\s+(\d+)", status or "")
            f.evidence.append(f"pid {pid}: state {state.group(1) if state else '?'}, "
                              f"RSS {int(rss.group(1)) // 1024 if rss else '?'} MiB")
            if state and state.group(1) == "D":
                f.status = PROBLEM
                f.likely_cause = "Nautilus is blocked in the kernel on I/O (often an unreachable mount)."
        warns = [l for l in self._journal_warnings().splitlines() if "nautilus" in l.lower()]
        if warns:
            kinds = Counter(re.sub(r"^\S+ \S+ \S+ ", "", l).split(":", 1)[-1].strip()[:60] for l in warns)
            per_min = Counter(l[:16] for l in warns)
            burst_min, burst = per_min.most_common(1)[0]
            f.evidence.append(f"{len(warns)} Nautilus warnings this boot; peak {burst}/min at {burst_min}")
            for k, n in kinds.most_common(3):
                f.evidence.append(f"  {n}x {k}")
            if burst > 200:
                f.status = WARN if f.status == OK else f.status
                f.verified = True
                f.likely_cause = ("Nautilus is logging hundreds of GTK warnings per minute while browsing "
                                  "large project trees. Each warning is a synchronous journal write; the flood "
                                  "indicates heavy UI churn and makes the main loop slower.")
                f.recommendation = ("Avoid keeping huge source/asset trees on ~/Desktop (Nautilus and the "
                                    "Desktop Icons extension watch it). Close Nautilus windows on those folders.")
        self.add(f)

    def check_tracker(self) -> list[RepairAction]:
        repairs: list[RepairAction] = []
        f = Finding("indexing", "File indexing (Tracker/LocalSearch)")
        rc, status = self._cmd(["tracker3", "status"], timeout=20)
        if rc == 127:
            f.status = INFO
            f.evidence.append("tracker3 not installed")
            self.add(f)
            return repairs
        head = status.splitlines()[:4]
        f.evidence.extend(head)
        m = re.search(r"(\d+) recorded failures", status)
        failures = int(m.group(1)) if m else 0
        _, rec = self._cmd(["gsettings", "get", "org.freedesktop.Tracker3.Miner.Files", "index-recursive-directories"])
        f.evidence.append(f"recursively indexed: {rec}")
        crashes = [l for l in self._journal_warnings().splitlines() if "Extractor subprocess died" in l]
        if crashes:
            f.evidence.append(f"tracker extractor crashed {len(crashes)} times this boot "
                              f"(first {crashes[0][:19]}, last {crashes[-1][:19]})")
        big = []
        if "&DESKTOP" in rec or "$HOME" in rec:
            desk = Path(_user_dir("DESKTOP") or Path.home() / "Desktop")
            count = _count_files(desk, limit=500_000, budget_s=8)
            big.append((desk, count))
            f.evidence.append(f"{desk} contains {'>' if count >= 500_000 else ''}{count:,} files (sampled with time budget)")
        if failures > 500 or len(crashes) > 100 or any(c > 100_000 for _, c in big):
            f.status = PROBLEM
            f.likely_cause = (
                "The file indexer is configured to recursively index a very large tree of developer "
                "projects, datasets and media. Its metadata extractor crashes repeatedly on some files "
                "and restarts, generating continuous disk I/O and D-Bus traffic. Nautilus (search, "
                "starred files, recent) and the Desktop Icons extension both talk to the indexer, so "
                "they are slowed down while it churns.")
            f.recommendation = ("Stop indexing ~/Desktop recursively (keep Documents/Music/Pictures/Videos), "
                                "then reset the broken index so it is rebuilt cleanly.")
            repairs.append(RepairAction(
                "tracker-scope", "Stop recursive indexing of ~/Desktop",
                "Files search will no longer find files inside ~/Desktop subfolders. No files are touched.",
                [["gsettings", "set", "org.freedesktop.Tracker3.Miner.Files", "index-recursive-directories",
                  "['&DOCUMENTS', '&MUSIC', '&PICTURES', '&VIDEOS']"]],
                "gsettings reset org.freedesktop.Tracker3.Miner.Files index-recursive-directories"))
            repairs.append(RepairAction(
                "tracker-reset", "Reset the tracker file index (cache only)",
                "Deletes only tracker's own index database in ~/.cache/tracker3; it is rebuilt automatically. "
                "Your files are not touched.",
                [["tracker3", "reset", "--filesystem"]],
                "Nothing to undo: the index is rebuilt on next login."))
        elif failures:
            f.status = INFO
        self.add(f)
        return repairs

    def check_extensions(self) -> None:
        f = Finding("extensions", "GNOME Shell extensions", INFO)
        rc, out = self._cmd(["gnome-extensions", "list", "--enabled"])
        if rc == 0:
            exts = out.split()
            f.evidence.append(f"enabled: {', '.join(exts) or 'none'}")
            if "ding@rastersoft.com" in exts:
                f.evidence.append("Desktop Icons (DING) watches ~/Desktop and renders every item on it")
                f.verified = False
                f.likely_cause = ("Possible contributor: DING monitors ~/Desktop. With large project folders "
                                  "there this is cheap at the top level, but it also queries tracker metadata.")
            third_party = [e for e in exts if not e.endswith("@ubuntu.com") and e != "ding@rastersoft.com"]
            if third_party:
                f.status = WARN
                f.recommendation = "Temporarily disable third-party extensions one at a time to test their impact."
        self.add(f)

    def check_mounts(self) -> None:
        f = Finding("mounts", "Network / FUSE mounts")
        mounts = _read("/proc/mounts") or ""
        net = [l.split()[:3] for l in mounts.splitlines()
               if l.split()[2:3] and re.match(r"(nfs|cifs|smb|sshfs|fuse\.sshfs|fuse\.rclone|davfs)", l.split()[2])]
        f.evidence.append(f"network mounts: {len(net)}")
        for dev, mnt, typ in net:
            t0 = time.perf_counter()
            # Stat in a subprocess with a timeout so a dead server cannot hang us.
            rc, _ = self._cmd(["stat", "-t", mnt], timeout=3)
            ms = (time.perf_counter() - t0) * 1000
            f.evidence.append(f"{mnt} ({typ}): {'unreachable' if rc else f'{ms:.0f} ms'}")
            if rc or ms > 1000:
                f.status = PROBLEM
                f.likely_cause = "An unreachable network mount blocks any app that lists it (Files sidebar)."
        bookmarks = _read(str(Path.home() / ".config/gtk-3.0/bookmarks")) or ""
        remote = [b for b in bookmarks.splitlines() if re.match(r"(smb|sftp|nfs|dav|ftp)s?://", b)]
        if remote:
            f.status = WARN if f.status == OK else f.status
            f.evidence.append(f"remote bookmarks in Files sidebar: {len(remote)}")
        self.add(f)

    def check_gpu(self) -> None:
        f = Finding("gpu", "Graphics driver")
        rc, out = self._cmd(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
        if rc == 0:
            f.evidence.append(out)
        egl = [l for l in self._journal_warnings().splitlines() if "failed to create dri2 screen" in l]
        if egl:
            f.status = INFO
            f.evidence.append(f"{len(egl)} 'libEGL: failed to create dri2 screen' warnings (GTK falling back "
                              "from the NVIDIA DRI path; harmless but adds startup work)")
        self.add(f)

    def check_boot(self) -> None:
        f = Finding("boot", "Boot / session services", INFO)
        rc, out = self._cmd(["systemd-analyze", "blame", "--no-pager"], timeout=20)
        if rc == 0:
            f.evidence.append("slowest units: " + "; ".join(l.strip() for l in out.splitlines()[:5]))
        rc, out = self._cmd(["systemd-analyze", "--user", "blame", "--no-pager"], timeout=20)
        if rc == 0:
            f.evidence.append("slowest user units: " + "; ".join(l.strip() for l in out.splitlines()[:5]))
        self.add(f)

    def check_desktop_entries(self) -> None:
        f = Finding("apps", "Desktop entries")
        try:
            from ..core import app_discovery

            t0 = time.perf_counter()
            recs, rep = app_discovery.scan()
            ms = (time.perf_counter() - t0) * 1000
        except Exception as exc:
            f.status, f.evidence = WARN, [f"scan failed: {exc}"]
            self.add(f)
            return
        broken = {p: r for p, r in rep.skipped.items() if r.startswith(("invalid", "unavailable"))}
        f.evidence.append(f"{rep.scanned} desktop files -> {rep.shown} visible apps, scanned in {ms:.0f} ms")
        if broken:
            f.status = INFO
            f.evidence.append(f"{len(broken)} broken/unavailable entries (skipped safely):")
            f.evidence.extend(f"  {p}: {r}" for p, r in list(broken.items())[:10])
        self.add(f)

    # -- orchestration -----------------------------------------------------
    def run_all(self, progress: Callable[[str], None] | None = None) -> list[RepairAction]:
        repairs: list[RepairAction] = []
        steps: list[tuple[str, Callable]] = [
            ("session", self.check_session), ("memory", self.check_memory), ("disk", self.check_disk),
            ("cpu", self.check_cpu_hogs), ("dbus", self.check_dbus), ("nautilus", self.check_nautilus),
            ("indexing", self.check_tracker), ("extensions", self.check_extensions),
            ("mounts", self.check_mounts), ("gpu", self.check_gpu), ("boot", self.check_boot),
            ("apps", self.check_desktop_entries),
        ]
        for name, fn in steps:
            if progress:
                progress(name)
            try:
                r = fn()
                if isinstance(r, list):
                    repairs.extend(r)
            except Exception as exc:  # a failing check must not abort the report
                self.add(Finding(name, f"{name} check failed", WARN, evidence=[repr(exc)]))
        return repairs

    def report_markdown(self, repairs: list[RepairAction] | None = None) -> str:
        out = ["# Nova Launcher — System Diagnostic Report", "",
               f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} in {time.time() - self.started:.1f}s. "
               "All checks are read-only.", ""]
        problems = [f for f in self.findings if f.status in (PROBLEM, WARN)]
        out.append("## Summary")
        if not problems:
            out.append("No problems detected.")
        for f in sorted(problems, key=lambda f: _RANK[f.status]):
            tag = "VERIFIED" if f.verified else "POSSIBLE"
            out.append(f"- **[{f.status.upper()} / {tag}] {f.title}** — {f.likely_cause or 'see details'}")
        out.append("")
        out.append("## Details")
        for f in sorted(self.findings, key=lambda f: _RANK[f.status]):
            out.append(f"### {f.title} — {f.status}{'' if f.verified else ' (possible cause, not verified)'}")
            for e in f.evidence:
                out.append(f"- `{e}`" if not e.startswith("  ") else f"  - `{e.strip()}`")
            if f.likely_cause:
                out.append(f"\n**Likely cause:** {f.likely_cause}")
            if f.recommendation:
                out.append(f"\n**Recommendation:** {f.recommendation}")
            out.append("")
        if self.missing:
            out.append("## Missing utilities")
            for m in self.missing:
                out.append(f"- `{m}` — {MISSING_HINTS.get(m, 'not installed')} (checks using it were skipped)")
            out.append("")
        if repairs:
            out.append("## Optional repairs (run `nova-launcher --repair`; each asks first)")
            for r in repairs:
                out.append(f"- **{r.title}** — {r.impact}  \n  Command: `{' '.join(_q(c) for c in r.commands[0])}`  \n  Undo: `{r.undo}`")
        return "\n".join(out) + "\n"

    def to_json(self) -> str:
        return json.dumps([f.__dict__ for f in self.findings], indent=2)


def repair(repairs: list[RepairAction], ask: Callable[[str], str] = input) -> list[str]:
    """Interactively apply repairs.  Nothing runs without an explicit 'y'."""
    log = []
    if not repairs:
        print("No repairs to offer.")
        return log
    for r in repairs:
        print(f"\n== {r.title}\n   Impact: {r.impact}\n   Undo:   {r.undo}")
        for c in r.commands:
            print("   Command: " + " ".join(_q(x) for x in c))
        if ask("   Apply this change? [y/N] ").strip().lower() != "y":
            log.append(f"skipped {r.id}")
            continue
        for c in r.commands:
            rc, out = run(c, timeout=60)
            log.append(f"{r.id}: {' '.join(c)} -> rc={rc} {out[:200]}")
            print(f"   -> exit {rc} {out[:200]}")
    return log


# -- small helpers -----------------------------------------------------------
def _q(s: str) -> str:
    return f'"{s}"' if any(c in s for c in " '[]&$") else s


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _meminfo() -> dict[str, int]:
    out = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        try:
            out[k] = int(v.split()[0])
        except (ValueError, IndexError):
            pass
    return out


def _vmstat() -> dict[str, int]:
    out = {}
    for line in (_read("/proc/vmstat") or "").splitlines():
        k, _, v = line.partition(" ")
        if v.isdigit():
            out[k] = int(v)
    return out


def _user_dir(name: str) -> str | None:
    text = _read(str(Path.home() / ".config/user-dirs.dirs")) or ""
    m = re.search(rf'XDG_{name}_DIR="([^"]+)"', text)
    return m.group(1).replace("$HOME", str(Path.home())) if m else None


def _count_files(root: Path, limit: int, budget_s: float) -> int:
    """Bounded file count (stops at ``limit`` entries or ``budget_s`` seconds)."""
    n = 0
    deadline = time.monotonic() + budget_s
    stack = [str(root)]
    while stack and n < limit and time.monotonic() < deadline:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    n += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return n
