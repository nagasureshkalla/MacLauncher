# Nova Launcher — Test Report

Date: 2026-10-26 · Ubuntu 24.04.4 LTS, GNOME Shell 46.0, **X11** session,
NVIDIA RTX 3090 (driver 580.178.04), Python 3.12.3, GTK 4.14.5, libadwaita 1.5.0.
Only results that were actually executed are listed.

## Automated tests — `pytest`: 72 passed, 0 failed (3 consecutive runs)

| Area (requirement) | Tests | File |
|---|---|---|
| Desktop-entry discovery, metadata, actions, subdirectory IDs | 3 | test_app_discovery.py |
| Duplicate resolution (user override, cross-source duplicates) | 2 | test_app_discovery.py |
| Hidden / NoDisplay / OnlyShowIn / non-application entries | 3 | test_app_discovery.py |
| Malformed files, missing Exec, missing TryExec binary | 1 | test_app_discovery.py |
| Invalid/missing icons don't count as failures | 1 | test_app_discovery.py |
| Realistic data set (this machine's 206 entries) in < 1 s | 1 | test_app_discovery.py |
| Search ranking, exact > prefix > substring, keyword/exec/category | 4 | test_search_engine.py |
| Fuzzy + typo matching ("firefx", "setings", "trminal", "vsc") | 2 | test_search_engine.py |
| Usage boost within a tier only; empty/shell-like queries | 2 | test_search_engine.py |
| Search latency with ~1,000 apps < 50 ms | 1 | test_search_engine.py |
| Launch works, no shell interpretation, desktop actions | 3 | test_app_launcher.py |
| Launch failures: missing file, binary removed, unknown action | 3 | test_app_launcher.py |
| Launch never waits for the app | 1 | test_app_launcher.py |
| Cache creation, reload, corruption recovery, stale dirs | 3 | test_app_index.py |
| Incremental refresh (add/change/remove) + live inotify | 2 | test_app_index.py |
| Background worker failure keeps cached apps | 1 | test_app_index.py |
| UI responsiveness during a 1 s indexing scan (gap < 100 ms) | 1 | test_app_index.py |
| Usage history bounded, ranked, disabled, corrupt file | 2 | test_app_index.py |
| Settings persistence, validation, corrupt file moved aside, import | 8 | test_config.py |
| Grid order, paging, folders create/add/remove, uninstalled apps | 4 | test_ui_logic.py |
| Dock magnification curve | 1 | test_ui_logic.py |
| Quick-launcher results: apps, folders, commands, recents | 2 | test_ui_logic.py |
| Multi-monitor choice/fallback and dock/centre placement | 1 | test_ui_logic.py |
| File manager: places without disk access, mounts never raise | 2 | test_file_manager.py |
| Unresponsive file manager: times out, reports, open() never blocks | 1 | test_file_manager.py |
| Diagnostics: missing utility, timeout, tracker detection, full run | 4 | test_diagnostics.py |
| Repair mode does nothing without explicit "y" | 1 | test_diagnostics.py |
| Shortcut install keeps other shortcuts; removal only Nova's | 2 | test_diagnostics.py |
| Autostart toggle | 1 | test_diagnostics.py |
| **Live GTK**: index without cache, grid keyboard nav + paging + Esc | 2 | test_windows.py |
| **Live GTK**: grid search + Enter, quick ↑/↓/Enter/Esc | 2 | test_windows.py |
| **Live GTK**: shortcut actions toggle-quick / toggle-grid | 1 | test_windows.py |

Bugs found by these tests and fixed: entries without `Exec` were shown;
launching an app whose binary was removed after indexing raised instead of
reporting; `x11` helpers could dereference libX11 before loading it.

## Manual / integration checks (executed)

| Check | Result |
|---|---|
| Per-user install, `nova-launcher --version` | ✅ |
| Cold D-Bus activation via `gapplication action … toggle-quick` (no instance running) | ✅ started in ~0.5 s, window opened |
| Dock window type on X11 | ✅ `_NET_WM_WINDOW_TYPE_DOCK` |
| Focus on shortcut open (X11, unlocked session) | ✅ 4/4 focused within 150 ms; focus returned on close |
| Uninstall in a throwaway HOME | ✅ only manifest files removed; unrelated apps, tools, documents and settings kept |
| `.deb` build + `desktop-file-validate` | ✅ (`lintian` not installed; `.deb` not installed system-wide — needs sudo) |
| Screenshot tour (7 views rendered from the live app) | ✅ docs/screenshots/ |

## Benchmarks — see README "Performance" and docs/benchmark-results.json

Warm open 38–54 ms median (p95 59–150 ms under heavy background load), search
0.7 ms (90 apps) / 7.6 ms (1,080 apps), idle CPU 0.0 %, RSS 157 MB of which
44 MB private (target < 100 MB RSS not met; explained in README).

## Not tested

* **Wayland session** — this machine runs X11 (NVIDIA). The code paths are
  designed to degrade (no dock, compositor placement) but were not executed
  under Wayland.
* **Network disconnected / unavailable network mount** — no network mounts
  exist here; the code never touches mounts beyond the volume monitor's
  in-memory list, and diagnostics stat mounts in a subprocess with a 3 s
  timeout, but this was not exercised against a real dead server.
* **Mouse-driven** drag-and-drop in the grid/dock, dock magnification and
  auto-hide by hand — logic is unit-tested, interaction not exercised.
* Login autostart across a real re-login (toggle writes the file; tested).
* Applying `--repair` (intentionally not run: it changes your tracker settings).
