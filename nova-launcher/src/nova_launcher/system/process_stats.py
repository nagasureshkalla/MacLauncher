"""Per-application CPU, GPU, RAM and VRAM usage (shown on Dynamic Island tiles).

Every figure is a percentage of the machine: CPU of all cores, RAM of total
memory, GPU busy time, VRAM of total video memory.

An app's usage is the sum over its whole process tree, so Chrome's renderer
and GPU processes count towards Chrome.  CPU and RAM come from /proc; GPU and
VRAM from ``nvidia-smi pmon`` on NVIDIA, else from the kernel's DRM fdinfo
(amdgpu, i915, xe, ...).

The parsers and formatters are pure and unit-tested; ``UsageSampler`` polls
in a background thread and hands each sample back on the GLib main loop.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

log = logging.getLogger("nova.stats")

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
PRIME_S = 0.3  # first CPU delta, so numbers appear right away
INTERVAL_S = 1.5


@dataclass(frozen=True)
class Usage:
    cpu: float  # percent of all cores
    ram: float  # percent of total memory
    gpu: float | None  # percent; None when no GPU backend is available
    vram: float | None  # percent of total VRAM; None when it is unknown


@dataclass(frozen=True)
class ProcStat:
    ppid: int
    ticks: int  # utime + stime
    rss: int  # bytes


# -- pure helpers (unit-tested) ------------------------------------------------------------
def parse_stat(text: str) -> ProcStat | None:
    """One /proc/<pid>/stat line; the command name may contain spaces and ')'."""
    rest = text[text.rfind(")") + 2:].split()
    if len(rest) < 22:
        return None
    return ProcStat(int(rest[1]), int(rest[11]) + int(rest[12]), int(rest[21]) * PAGE)


def descendants(ppids: dict[int, int], root: int) -> set[int]:
    """``root`` and every process below it."""
    children: dict[int, list[int]] = {}
    for pid, ppid in ppids.items():
        children.setdefault(ppid, []).append(pid)
    tree, todo = set(), [root]
    while todo:
        pid = todo.pop()
        if pid in tree:
            continue
        tree.add(pid)
        todo.extend(children.get(pid, ()))
    return tree


def parse_pmon(text: str) -> dict[int, tuple[float, int]]:
    """``nvidia-smi pmon -c 1 -s um`` -> {pid: (sm %, framebuffer bytes)}, summed over GPUs."""
    cols: list[str] = []
    out: dict[int, tuple[float, int]] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            if not cols:
                cols = line.lstrip("#").split()
            continue
        parts = line.split()
        if not cols or len(parts) < len(cols) - 1:  # the command column may be empty
            continue
        row = dict(zip(cols, parts))

        def num(key: str) -> float:
            try:
                return float(row.get(key, "-"))
            except ValueError:
                return 0.0  # "-": no sample this period

        try:
            pid = int(row["pid"])
        except (KeyError, ValueError):
            continue
        sm, fb = out.get(pid, (0.0, 0))
        out[pid] = (sm + num("sm"), fb + int(num("fb") * 1024 * 1024))
    return out


_UNITS = {"": 1, "B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}


def parse_fdinfo(text: str) -> tuple[str, int, int] | None:
    """DRM fdinfo -> (client key, busy ns summed over engines, VRAM bytes); None if not a GPU client."""
    fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    if "drm-client-id" not in fields:
        return None
    key = f"{fields.get('drm-pdev', '')}/{fields['drm-client-id']}"
    busy = sum(int(v.split()[0]) for k, v in fields.items()
               if k.startswith("drm-engine-") and not k.startswith("drm-engine-capacity") and v.endswith(" ns"))
    vram = 0
    for name in ("drm-resident-vram", "drm-total-vram", "drm-memory-vram"):
        if name in fields:
            m = re.fullmatch(r"(\d+)\s*(\w*)", fields[name])
            if m:
                vram = int(m.group(1)) * _UNITS.get(m.group(2), 1)
            break
    return key, busy, vram


def percent(part: float, whole: float) -> float:
    return min(max(100 * part / whole, 0.0), 100.0) if whole > 0 else 0.0


def format_percent(p: float | None) -> str:
    """Two digits, like a system monitor: '01%', '13%', '100%'; '--%' when unknown."""
    if p is None:
        return "--%"
    return f"{round(min(max(p, 0.0), 100.0)):02d}%"


# -- sampling ------------------------------------------------------------------------------
def mem_total() -> int:
    """Total RAM in bytes, from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def read_procs() -> dict[int, ProcStat]:
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                st = parse_stat(f.read())
        except OSError:
            continue  # exited
        if st is not None:
            procs[int(name)] = st
    return procs


class _Gpu:
    """Per-process GPU busy % and VRAM, from nvidia-smi or DRM fdinfo."""

    def __init__(self):
        self.nvidia = shutil.which("nvidia-smi")
        self.drm = os.path.isdir("/dev/dri")
        self._prev: dict[str, int] = {}  # fdinfo client -> busy ns
        self._prev_t = 0.0
        self.vram_total = self._vram_total()

    def _vram_total(self) -> int:
        """Total video memory in bytes, summed over GPUs; 0 when unknown (e.g. integrated graphics)."""
        if self.nvidia:
            try:
                out = subprocess.run([self.nvidia, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout
                return sum(int(float(v)) for v in out.split()) * 1024 * 1024
            except (OSError, subprocess.SubprocessError, ValueError):
                log.debug("nvidia-smi memory query failed", exc_info=True)
                return 0
        total = 0
        try:
            cards = [c for c in os.listdir("/sys/class/drm") if re.fullmatch(r"card\d+", c)]
        except OSError:
            return 0
        for card in cards:  # amdgpu exposes this; other drivers have no dedicated VRAM
            try:
                with open(f"/sys/class/drm/{card}/device/mem_info_vram_total") as f:
                    total += int(f.read())
            except (OSError, ValueError):
                continue
        return total

    @property
    def available(self) -> bool:
        return bool(self.nvidia or self.drm)

    def sample(self, pids: set[int]) -> dict[int, tuple[float, int]]:
        if self.nvidia:
            try:
                out = subprocess.run([self.nvidia, "pmon", "-c", "1", "-s", "um"], capture_output=True,
                                     text=True, timeout=5).stdout
                return parse_pmon(out)
            except (OSError, subprocess.SubprocessError):
                log.debug("nvidia-smi pmon failed", exc_info=True)
                return {}
        return self._sample_drm(pids) if self.drm else {}

    def _sample_drm(self, pids: set[int]) -> dict[int, tuple[float, int]]:
        now = time.monotonic()
        wall = (now - self._prev_t) * 1e9 if self._prev_t else 0
        busy_now: dict[str, int] = {}
        out: dict[int, tuple[float, int]] = {}
        for pid in pids:
            try:
                fds = os.listdir(f"/proc/{pid}/fd")
            except OSError:
                continue
            for fd in fds:
                try:
                    if not os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                        continue
                    with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                        info = parse_fdinfo(f.read())
                except OSError:
                    continue
                if info is None or info[0] in busy_now:
                    continue  # one client can sit behind several fds
                key, busy, vram = info
                busy_now[key] = busy
                pct = 100 * (busy - self._prev[key]) / wall if wall and key in self._prev else 0.0
                gpu, mem = out.get(pid, (0.0, 0))
                out[pid] = (gpu + max(pct, 0.0), mem + vram)
        self._prev, self._prev_t = busy_now, now
        return out


class UsageSampler:
    """Polls usage for a set of root pids while ``watch``ed; stops on ``stop``."""

    def __init__(self, on_sample: Callable[[dict[int, Usage]], None]):
        self.on_sample = on_sample
        self._roots: frozenset[int] = frozenset()
        self._stop = threading.Event()
        self._stop.set()
        self._gpu = _Gpu()
        self._cores = os.cpu_count() or 1
        self._mem = mem_total()

    def watch(self, roots: Iterable[int]) -> None:
        self._roots = frozenset(p for p in roots if p > 0)
        if self._stop.is_set():
            # A fresh event: a thread told to stop may still be finishing its wait.
            self._stop = stop = threading.Event()
            threading.Thread(target=self._run, args=(stop,), name="nova-usage", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, stop: threading.Event) -> None:
        from gi.repository import GLib

        prev, prev_t = read_procs(), time.monotonic()
        stop.wait(PRIME_S)
        while not stop.is_set():
            try:
                procs, now = read_procs(), time.monotonic()
                sample = self._measure(prev, procs, now - prev_t)
                prev, prev_t = procs, now
            except Exception:
                log.exception("usage sample failed")
                sample = {}
            if not stop.is_set():
                GLib.idle_add(self._deliver, stop, sample)
            stop.wait(INTERVAL_S)

    def _deliver(self, stop: threading.Event, sample: dict[int, Usage]) -> bool:
        if not stop.is_set():
            self.on_sample(sample)
        return False

    def _measure(self, prev: dict[int, ProcStat], procs: dict[int, ProcStat], dt: float) -> dict[int, Usage]:
        ppids = {pid: st.ppid for pid, st in procs.items()}
        trees = {root: descendants(ppids, root) for root in self._roots if root in procs}
        gpu = self._gpu.sample(set().union(*trees.values())) if trees else {}
        has_gpu = self._gpu.available
        vram_total = self._gpu.vram_total
        out = {}
        for root, tree in trees.items():
            ticks = sum(procs[p].ticks - prev[p].ticks for p in tree if p in prev)
            out[root] = Usage(
                cpu=percent(ticks / CLK_TCK, dt * self._cores),
                ram=percent(sum(procs[p].rss for p in tree), self._mem),
                gpu=min(sum(gpu[p][0] for p in tree if p in gpu), 100.0) if has_gpu else None,
                vram=percent(sum(gpu[p][1] for p in tree if p in gpu), vram_total) if vram_total else None,
            )
        return out
