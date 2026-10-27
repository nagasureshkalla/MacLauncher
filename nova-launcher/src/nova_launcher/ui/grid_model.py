"""Pure ordering/folder logic for the Launchpad grid (no GTK; unit-tested).

Top-level items are keys: ``app:<desktop id>`` or ``folder:<folder id>``.
"""

from __future__ import annotations

import itertools
import time

from ..core.desktop_entry import AppRecord

APP, FOLDER = "app:", "folder:"


def app_key(app_id: str) -> str:
    return APP + app_id


def folder_key(fid: str) -> str:
    return FOLDER + fid


class GridModel:
    def __init__(self, records: list[AppRecord], order: list[str], folders: dict[str, dict]):
        self.records = {r.id: r for r in records}
        self.folders = {fid: {"name": f["name"], "apps": [a for a in f["apps"] if a in self.records]}
                        for fid, f in folders.items()}
        # Folders left with < 1 installed app disappear.
        self.folders = {fid: f for fid, f in self.folders.items() if f["apps"]}
        in_folders = {a for f in self.folders.values() for a in f["apps"]}
        keys: list[str] = []
        seen: set[str] = set()
        for k in order:
            if k in seen:
                continue
            if k.startswith(APP) and k[len(APP):] in self.records and k[len(APP):] not in in_folders:
                keys.append(k)
            elif k.startswith(FOLDER) and k[len(FOLDER):] in self.folders:
                keys.append(k)
            seen.add(k)
        # New apps / folders not yet ordered go to the end, alphabetically.
        for fid in self.folders:
            if folder_key(fid) not in seen:
                keys.append(folder_key(fid))
        for r in sorted(self.records.values(), key=lambda r: r.name.casefold()):
            k = app_key(r.id)
            if k not in seen and r.id not in in_folders:
                keys.append(k)
        self.keys = keys

    # -- queries ---------------------------------------------------------------
    def title(self, key: str) -> str:
        if key.startswith(FOLDER):
            return self.folders[key[len(FOLDER):]]["name"]
        return self.records[key[len(APP):]].name

    def folder_records(self, fid: str) -> list[AppRecord]:
        return [self.records[a] for a in self.folders.get(fid, {}).get("apps", []) if a in self.records]

    def pages(self, per_page: int) -> list[list[str]]:
        per_page = max(1, per_page)
        return [self.keys[i:i + per_page] for i in range(0, len(self.keys), per_page)] or [[]]

    # -- edits ---------------------------------------------------------------
    def move(self, key: str, target: str, after: bool) -> None:
        if key == target or key not in self.keys or target not in self.keys:
            return
        self.keys.remove(key)
        i = self.keys.index(target) + (1 if after else 0)
        self.keys.insert(i, key)

    def merge(self, key: str, target: str) -> str | None:
        """Drop ``key`` (an app) onto ``target``.  Returns the folder id used."""
        if key == target or not key.startswith(APP) or key not in self.keys:
            return None
        app_id = key[len(APP):]
        if target.startswith(FOLDER):
            fid = target[len(FOLDER):]
            if app_id not in self.folders[fid]["apps"]:
                self.folders[fid]["apps"].append(app_id)
            self.keys.remove(key)
            return fid
        if target.startswith(APP) and target in self.keys:
            other = target[len(APP):]
            fid = self._new_folder_id()
            self.folders[fid] = {"name": self._suggest_name(other, app_id), "apps": [other, app_id]}
            pos = self.keys.index(target)
            self.keys[pos] = folder_key(fid)
            self.keys.remove(key)
            return fid
        return None

    def remove_from_folder(self, fid: str, app_id: str) -> None:
        folder = self.folders.get(fid)
        if not folder or app_id not in folder["apps"]:
            return
        folder["apps"].remove(app_id)
        pos = self.keys.index(folder_key(fid)) + 1 if folder_key(fid) in self.keys else len(self.keys)
        self.keys.insert(pos, app_key(app_id))
        if len(folder["apps"]) == 0:
            self.keys.remove(folder_key(fid))
            del self.folders[fid]

    def rename_folder(self, fid: str, name: str) -> None:
        if fid in self.folders and name.strip():
            self.folders[fid]["name"] = name.strip()[:64]

    def _new_folder_id(self) -> str:
        base = f"f{int(time.time())}"
        for n in itertools.count():
            fid = f"{base}-{n}"
            if fid not in self.folders:
                return fid
        raise AssertionError

    def _suggest_name(self, a: str, b: str) -> str:
        ca = set(self.records[a].categories)
        cb = set(self.records[b].categories)
        generic = {"GTK", "GNOME", "Qt", "KDE", "Application", "X-GNOME-Utilities", "Core"}
        common = [c for c in self.records[a].categories if c in cb and c not in generic and not c.startswith("X-")]
        names = {"AudioVideo": "Media", "Development": "Development", "Graphics": "Graphics", "Network": "Internet",
                 "Office": "Office", "Settings": "Settings", "System": "System", "Utility": "Utilities",
                 "Game": "Games", "Education": "Education", "WebBrowser": "Browsers", "TextEditor": "Editors"}
        for c in common:
            if c in names:
                return names[c]
        return common[0] if common else ("Utilities" if ca & cb else "Folder")

    def to_settings(self) -> tuple[list[str], dict[str, dict]]:
        return list(self.keys), {fid: {"name": f["name"], "apps": list(f["apps"])} for fid, f in self.folders.items()}
