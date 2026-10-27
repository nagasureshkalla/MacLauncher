# Nova Launcher — System Diagnostic Report

Generated 2026-10-26 21:50:36 in 5.4s. All checks are read-only.

## Summary
- **[PROBLEM / VERIFIED] File indexing (Tracker/LocalSearch)** — The file indexer is configured to recursively index a very large tree of developer projects, datasets and media. Its metadata extractor crashes repeatedly on some files and restarts, generating continuous disk I/O and D-Bus traffic. Nautilus (search, starred files, recent) and the Desktop Icons extension both talk to the indexer, so they are slowed down while it churns.
- **[WARNING / VERIFIED] Files (Nautilus)** — Nautilus is logging hundreds of GTK warnings per minute while browsing large project trees. Each warning is a synchronous journal write; the flood indicates heavy UI churn and makes the main loop slower.

## Details
### File indexing (Tracker/LocalSearch) — problem
- `Currently indexed: 0 files, 5 folders`
- `Remaining space on database partition: 786.2?GB (39.96%)`
- `All data miners are idle, indexing complete`
- `3592 recorded failures`
- `recursively indexed: ['&DESKTOP', '&DOCUMENTS', '&MUSIC', '&PICTURES', '&VIDEOS']`
- `tracker extractor crashed 1897 times this boot (first 2026-10-26T17:29:04, last 2026-10-26T19:02:41)`
- `/home/nagasureshkalla/Desktop contains >505,698 files (sampled with time budget)`

**Likely cause:** The file indexer is configured to recursively index a very large tree of developer projects, datasets and media. Its metadata extractor crashes repeatedly on some files and restarts, generating continuous disk I/O and D-Bus traffic. Nautilus (search, starred files, recent) and the Desktop Icons extension both talk to the indexer, so they are slowed down while it churns.

**Recommendation:** Stop indexing ~/Desktop recursively (keep Documents/Music/Pictures/Videos), then reset the broken index so it is rebuilt cleanly.

### Files (Nautilus) — warning
- `running instances: 1`
- `pid 149253: state S, RSS 364 MiB`
- `5617 Nautilus warnings this boot; peak 449/min at 2026-10-26T21:20`
  - `693x duplicate child name in GtkStack: Media`
  - `634x duplicate child name in GtkStack: Content`
  - `369x duplicate child name in GtkStack: Config`

**Likely cause:** Nautilus is logging hundreds of GTK warnings per minute while browsing large project trees. Each warning is a synchronous journal write; the flood indicates heavy UI churn and makes the main loop slower.

**Recommendation:** Avoid keeping huge source/asset trees on ~/Desktop (Nautilus and the Desktop Icons extension watch it). Close Nautilus windows on those folders.

### Desktop session — info
- `session=x11`
- `desktop=ubuntu:GNOME`
- `GNOME Shell 46.0`

### Memory and swap — info
- `RAM 56.3 GiB available of 62.7 GiB`
- `swap 56% used of 2.0 GiB`
- `PSI memory: some avg10=0.00 avg60=0.00 avg300=0.00 total=53518943`
- `pages swapped out since boot: 1,122,878 (4.3 GiB)`

**Likely cause:** Idle desktop processes have been swapped out; their first interaction after a long idle period pays a (usually sub-second on NVMe) swap-in cost.

### GNOME Shell extensions — info (possible cause, not verified)
- `enabled: ding@rastersoft.com, ubuntu-appindicators@ubuntu.com, ubuntu-dock@ubuntu.com`
- `Desktop Icons (DING) watches ~/Desktop and renders every item on it`

**Likely cause:** Possible contributor: DING monitors ~/Desktop. With large project folders there this is cheap at the top level, but it also queries tracker metadata.

### Boot / session services — info
- `slowest units: 30.190s systemd-rfkill.service; 30.129s fstrim.service; 5.951s NetworkManager-wait-online.service; 4.329s plymouth-quit-wait.service; 3.640s apt-daily-upgrade.service`
- `slowest user units: 2min 52.022s evergy-rates.service; 2.217s snap.snapd-desktop-integration.snapd-desktop-integration.service; 1.308s tracker-miner-fs-3.service; 909ms org.gnome.Shell@x11.service; 415ms xdg-desktop-portal.service`

### Desktop entries — info
- `206 desktop files -> 90 visible apps, scanned in 52 ms`
- `5 broken/unavailable entries (skipped safely):`
  - `/usr/share/ubuntu/applications/texdoctk.desktop: unavailable: missing Exec or TryExec binary not installed`
  - `/usr/share/applications/nsight.desktop: unavailable: missing Exec or TryExec binary not installed`
  - `/usr/share/applications/openjdk-8-java.desktop: unavailable: missing Exec or TryExec binary not installed`
  - `/usr/share/applications/openjdk-21-java.desktop: unavailable: missing Exec or TryExec binary not installed`
  - `/usr/share/applications/vim.desktop: unavailable: missing Exec or TryExec binary not installed`

### Disk space and I/O — ok
- `/: 60% used, 786 GB free`
- `/home/nagasureshkalla: 60% used, 786 GB free`
- `PSI io: some avg10=0.00 avg60=0.00 avg300=0.11 total=68398276`
- `loop0: util 0%`
- `loop1: util 0%`
- `loop10: util 0%`
- `loop11: util 0%`
- `loop12: util 0%`
- `loop13: util 0%`
- `loop14: util 0%`
- `loop15: util 0%`
- `loop16: util 0%`
- `loop17: util 0%`
- `loop18: util 0%`
- `loop19: util 0%`
- `loop2: util 0%`
- `loop20: util 0%`
- `loop21: util 0%`
- `loop22: util 0%`
- `loop23: util 0%`
- `loop24: util 0%`
- `loop25: util 0%`
- `loop26: util 0%`
- `loop27: util 0%`
- `loop28: util 0%`
- `loop29: util 0%`
- `loop3: util 0%`
- `loop30: util 0%`
- `loop31: util 0%`
- `loop32: util 0%`
- `loop33: util 0%`
- `loop34: util 0%`
- `loop4: util 0%`
- `loop5: util 0%`
- `loop6: util 0%`
- `loop7: util 0%`
- `loop8: util 0%`
- `loop9: util 0%`
- `nvme0n1: util 0%`
- `sda: util 0%`
- `sdb: util 0%`

### Desktop process CPU / state — ok
- `top: 32.4 chrome%; 7.0 chrome%; 4.7 gnome-shell%; 4.4 claude%; 4.1 ollama%`

### D-Bus responsiveness of desktop services — ok
- `org.gnome.Shell: 6 ms`
- `org.gnome.Nautilus: 5 ms`
- `org.freedesktop.FileManager1: 4 ms`
- `org.freedesktop.Tracker3.Miner.Files: 4 ms`
- `org.freedesktop.portal.Desktop: 5 ms`
- `org.gtk.vfs.Daemon: 4 ms`

### Network / FUSE mounts — ok
- `network mounts: 0`

### Graphics driver — ok
- `NVIDIA GeForce RTX 3090, 580.178.04`

## Optional repairs (run `nova-launcher --repair`; each asks first)
- **Stop recursive indexing of ~/Desktop** — Files search will no longer find files inside ~/Desktop subfolders. No files are touched.  
  Command: `gsettings set org.freedesktop.Tracker3.Miner.Files index-recursive-directories "['&DOCUMENTS', '&MUSIC', '&PICTURES', '&VIDEOS']"`  
  Undo: `gsettings reset org.freedesktop.Tracker3.Miner.Files index-recursive-directories`
- **Reset the tracker file index (cache only)** — Deletes only tracker's own index database in ~/.cache/tracker3; it is rebuilt automatically. Your files are not touched.  
  Command: `tracker3 reset --filesystem`  
  Undo: `Nothing to undo: the index is rebuilt on next login.`

## Analyst notes (manual follow-up, 2026-10-26)

- **Ruled out — `evergy-rates.service` (2 min 52 s):** a `Type=oneshot` unit started by
  `evergy-rates.timer`; nothing is ordered after it (`list-dependencies --reverse` is empty),
  so it does not delay the session or other apps.
- **History:** "Unable to register the application … NoReply" for Nautilus (Oct 24) and
  Calculator (Oct 22): a second launch waited on a hung primary instance over D-Bus. This
  matches the "click and nothing happens for minutes" symptom.
- **Not observed live:** no hang reproduced during this session; D-Bus answered in < 10 ms.
  The tracker finding is verified as a fault; that it *causes* the multi-minute delays is
  the most likely explanation, not a proven one.
- **Suggested order:** run `nova-launcher --repair` (two confirmations), log out/in, and
  move large project trees off `~/Desktop` (e.g. to `~/Projects`) if Files is still slow.
