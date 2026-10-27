#!/usr/bin/env python3
"""Measure Nova Launcher performance on this machine.

  cold start   new process -> first painted frame of the quick launcher
  warm open    `gapplication action … toggle-quick` -> first painted frame
               (the path a GNOME keyboard shortcut takes)
  search       in-process query latency over the real installed apps
  idle         CPU time and RSS of the resident process while hidden

Timestamps come from CLOCK_MONOTONIC in both processes, written by the app
when NOVA_BENCH_FILE is set.  Results: median and p95.

Usage: scripts/benchmark.py [--runs N] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "io.github.nova.Launcher"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
CMD = [sys.executable, "-m", "nova_launcher.main"]


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def summary(values):
    return {"n": len(values), "median_ms": round(statistics.median(values), 1),
            "p95_ms": round(pct(values, 95), 1), "min_ms": round(min(values), 1), "max_ms": round(max(values), 1)}


def running() -> bool:
    return subprocess.run(["gdbus", "call", "--session", "--dest", APP_ID, "--object-path", "/",
                           "--method", "org.freedesktop.DBus.Peer.Ping"], capture_output=True).returncode == 0


def quit_app():
    subprocess.run(CMD + ["--quit"], env=ENV, capture_output=True, timeout=20)
    for _ in range(100):
        if not running():
            return
        time.sleep(0.05)


def wait_line(bench: Path, n: int, timeout=20.0) -> list[str]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        lines = bench.read_text().splitlines() if bench.exists() else []
        if len(lines) >= n:
            return lines
        time.sleep(0.005)
    raise TimeoutError("no frame painted")


def proc_stats(pid: int) -> tuple[float, int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    ticks = int(fields[11]) + int(fields[12])
    rss_kb = int([l for l in Path(f"/proc/{pid}/status").read_text().splitlines() if l.startswith("VmRSS")][0].split()[1])
    return ticks / os.sysconf("SC_CLK_TCK"), rss_kb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=15)
    ap.add_argument("--out", default=str(ROOT / "docs" / "benchmark-results.json"))
    args = ap.parse_args()
    if running():
        print("Nova is already running; quitting it for a clean measurement.")
        quit_app()

    results = {"machine": os.uname().nodename, "session": os.environ.get("XDG_SESSION_TYPE"),
               "python": sys.version.split()[0], "date": time.strftime("%Y-%m-%d %H:%M")}
    bench = Path(tempfile.mkdtemp()) / "frames.tsv"
    env = {**ENV, "NOVA_BENCH_FILE": str(bench)}

    # -- cold start ----------------------------------------------------------
    cold = []
    for i in range(max(3, args.runs // 3)):
        bench.unlink(missing_ok=True)
        t0 = time.monotonic_ns()
        p = subprocess.Popen(CMD + ["--quick", "--no-dock"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        line = wait_line(bench, 1)[0].split("\t")
        cold.append((int(line[2]) - t0) / 1e6)
        quit_app()
        p.wait(timeout=10)
    results["cold_start_to_first_frame"] = summary(cold)
    print("cold start      ", results["cold_start_to_first_frame"])

    # -- warm open via gapplication (keyboard-shortcut path) ------------------
    bench.unlink(missing_ok=True)
    p = subprocess.Popen(CMD + ["--background", "--no-dock"], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    for _ in range(200):
        if running():
            break
        time.sleep(0.05)
    time.sleep(1.5)  # let the background scan settle
    warm, inproc = [], []
    for i in range(args.runs):
        n = len(bench.read_text().splitlines()) if bench.exists() else 0
        t0 = time.monotonic_ns()
        subprocess.run(["gapplication", "action", APP_ID, "toggle-quick"], check=True)
        line = wait_line(bench, n + 1)[-1].split("\t")
        warm.append((int(line[2]) - t0) / 1e6)
        inproc.append((int(line[2]) - int(line[1])) / 1e6)
        time.sleep(0.15)
        subprocess.run(["gapplication", "action", APP_ID, "toggle-quick"], check=True)  # hide
        time.sleep(0.15)
    results["warm_open_shortcut_to_first_frame"] = summary(warm)
    results["warm_open_in_process_present_to_frame"] = summary(inproc)
    print("warm open       ", results["warm_open_shortcut_to_first_frame"])
    print("  (in-process)  ", results["warm_open_in_process_present_to_frame"])

    # -- idle cost while hidden ----------------------------------------------------
    cpu0, _ = proc_stats(p.pid)
    time.sleep(10)
    cpu1, rss = proc_stats(p.pid)
    results["idle_hidden"] = {"cpu_percent_over_10s": round((cpu1 - cpu0) / 10 * 100, 2), "rss_mb": round(rss / 1024, 1)}
    print("idle (hidden)   ", results["idle_hidden"])
    quit_app()
    p.wait(timeout=10)

    # -- search latency (in-process, real apps) ------------------------------------
    sys.path.insert(0, str(ROOT / "src"))
    from nova_launcher.core import app_discovery
    from nova_launcher.core.search_engine import SearchEngine

    t = time.perf_counter()
    recs, rep = app_discovery.scan()
    results["discovery_scan"] = {"ms": round((time.perf_counter() - t) * 1000, 1),
                                 "desktop_files": rep.scanned, "apps": rep.shown}
    engine = SearchEngine(recs)
    queries = ["f", "fi", "fir", "fire", "firefx", "term", "code", "files", "setings", "calc", "zzzz", "libre wr"]
    lat = []
    for _ in range(20):
        for q in queries:
            t = time.perf_counter()
            engine.search(q, 20)
            lat.append((time.perf_counter() - t) * 1000)
    results["search_latency"] = summary(lat)
    t = time.perf_counter()
    for _ in range(1000):
        engine._entries[len(engine._entries) // 2]
    big = recs * max(1, 1000 // max(1, len(recs)))
    engine_big = SearchEngine(big)
    lat_big = []
    for q in queries:
        t = time.perf_counter()
        engine_big.search(q, 20)
        lat_big.append((time.perf_counter() - t) * 1000)
    results["search_latency_1000_apps"] = summary(lat_big)
    print("discovery       ", results["discovery_scan"])
    print("search          ", results["search_latency"])
    print("search (1000)   ", results["search_latency_1000_apps"])

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
