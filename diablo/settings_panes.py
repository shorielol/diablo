"""
The panes of System Settings, read from the Mac, and which one a request is about.

"can you show me how to connect a new Bluetooth device?" names no app, and
Diablo taught against whatever window was in front. Each pane of System
Settings is an installed extension with a display name and a bundle id, and
System Settings opens one from a link. So a request that names a system
feature resolves to its pane by reading what is installed:

    mentioned("connect a new bluetooth device")  -> [Bluetooth]
    url(Bluetooth)  -> "x-apple.systempreferences:com.apple.BluetoothSettings"

Matching uses each pane's name and the titles of the settings inside it (from
the pane's own search index), as whole phrases. The index's loose keywords
("on", "power", "usage") are not used: they would match anything.
"""

import glob
import os
import plistlib
import re
import threading
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

ROOT = "/System/Library/ExtensionKit/Extensions"
SETTINGS_POINT = "com.apple.Settings.extension.ui"

# Suffixes Apple's internal bundle names carry that the pane's title does not.
_SUFFIXES = ("SettingPane_macOS", "SettingsExtension", "SettingsIntents", "Settings Extension",
             "Extension", "Settings", "Preferences", "Ext")


@dataclass(frozen=True)
class Pane:
    name: str
    bundle_id: str
    titles: Tuple[str, ...] = ()        # settings inside the pane, by their own titles


_cache: List[Pane] = []
_lock = threading.Lock()


def _readable(name: str) -> str:
    """'WiFiSettings' -> 'Wi Fi', 'LockScreen' -> 'Lock Screen'."""
    name = name.replace("_macOS", "")
    for suffix in _SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return " ".join(name.split())


def _load(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
    except Exception:               # a malformed file is no data, not a crash
        return {}
    return data if isinstance(data, dict) else {}


def _pane(appex: str) -> Optional[Pane]:
    info = _load(os.path.join(appex, "Contents", "Info.plist"))
    point = (info.get("EXAppExtensionAttributes") or {}).get("EXExtensionPointIdentifier")
    if point != SETTINGS_POINT or not info.get("CFBundleIdentifier"):
        return None
    resources = os.path.join(appex, "Contents", "Resources")
    table = _load(os.path.join(resources, "InfoPlist.loctable"))
    english = table.get("en") or table.get("English") or {}
    name = (english.get("CFBundleDisplayName") or info.get("CFBundleDisplayName")
            or _readable(info.get("CFBundleName") or os.path.basename(appex)[:-6]))
    titles = []
    for terms in glob.glob(os.path.join(resources, "en.lproj", "*.searchTerms")):
        for entry in _load(terms).values():
            for item in (entry or {}).get("localizableStrings", []):
                title = str(item.get("title") or "").strip()
                if title and title not in titles:
                    titles.append(title)
    return Pane(_readable(str(name)), str(info["CFBundleIdentifier"]), tuple(titles))


def discover() -> List[Pane]:
    """Every System Settings pane installed on this Mac. Read once per process."""
    with _lock:
        if not _cache:
            found = [_pane(p) for p in sorted(glob.glob(os.path.join(ROOT, "*.appex")))]
            _cache.extend(p for p in found if p)
        return list(_cache)


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _says(text: str, phrase: str) -> bool:
    """The phrase as whole words in the text, spaces optional: 'wifi' says 'Wi Fi'."""
    phrase = _norm(phrase)
    if not phrase:
        return False
    if re.search(rf"\b{re.escape(phrase)}\b", text):
        return True
    return " " in phrase and phrase.replace(" ", "") in text.split()


def mentioned(text: str, panes: Optional[Sequence[Pane]] = None) -> List[Pane]:
    """The panes a request is about: named outright first, then by a setting inside."""
    said = _norm(text)
    if not said:
        return []
    candidates = list(panes) if panes is not None else discover()
    by_name = [p for p in candidates if _says(said, p.name)]
    if by_name:
        return sorted(by_name, key=lambda p: -len(p.name))
    by_title = [(max(len(t) for t in p.titles if _says(said, t)), p)
                for p in candidates if any(_says(said, t) for t in p.titles)]
    return [p for _length, p in sorted(by_title, key=lambda pair: -pair[0])]


def url(pane: Pane) -> str:
    """The link that opens this pane in System Settings."""
    return f"x-apple.systempreferences:{pane.bundle_id}"
