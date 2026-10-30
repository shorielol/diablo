"""
An application's own menus: where a command is, and how to reach it.

Every Mac application publishes its commands in its menu bar, under their own
names and with their shortcuts, whether or not anything in the window looks
like them. Asked how to open settings in Zen, Diablo looked for a gear, found
none and gave up, when Zen › Settings… (⌘,) was there the whole time. This is
the route that does not depend on recognising an icon.

    find(app, "settings")   -> [Zen › Settings…]
    route(item)             -> "Zen › Settings… (⌘,)"

Reading the menus changes nothing. Choosing one is an action and lives in
computer_use.choose_menu, behind the same permission as a click.
"""

import re
import subprocess
from typing import List, Optional, Sequence, Set

from skills import apps, ax

# Words that ask for a command without naming it.
_FILLER = {"a", "an", "the", "my", "me", "to", "how", "do", "i", "can", "you", "please",
           "open", "show", "go", "get", "find", "where", "is", "are", "in", "on", "of",
           "as", "for", "up", "into", "this", "that", "it", "menu", "option", "command",
           "button", "icon", "control", "link", "thing", "there", "here",
           "hey", "hi", "ok", "okay", "diablo", "could", "would", "will", "want", "need",
           "like", "know", "tell", "teach", "help", "what", "which", "when", "why", "let",
           "us", "we", "your", "be", "able", "just", "now", "again"}
# One rename every Mac user meets: macOS 13 renamed Preferences to Settings,
# and applications still use either. Not a phrase list; one equivalence.
_SAME = {"preferences": "settings", "prefs": "settings", "options": "settings",
         # Final Cut Pro, Keynote and others say captions where people say
         # subtitles: "add subtitles" was matched to File › Import › Captions…
         # by the model instead of Edit › Captions › Add Caption.
         "subtitles": "captions", "subtitle": "caption"}


def _words(text: str) -> List[str]:
    return [_SAME.get(word, word)
            for word in re.findall(r"[a-z0-9]+", (text or "").lower().replace("…", " "))]


def _content(text: str) -> Set[str]:
    return {w for w in _words(text) if w not in _FILLER}


def _pid(app: apps.App) -> int:
    if not app.bundle_id:
        return 0
    try:
        out = subprocess.run(["lsappinfo", "info", "-only", "pid", "-app", app.bundle_id],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    found = re.search(r"pid\s*=\s*(\d+)", out)
    return int(found.group(1)) if found else 0


def menu_items(app: apps.App) -> List[ax.MenuItem]:
    """The commands in a running application's menu bar. Empty if it is not running."""
    return ax.menu_tree(_pid(app))


def find(app: apps.App, what: str,
         items: Optional[Sequence[ax.MenuItem]] = None) -> List[ax.MenuItem]:
    """
    The menu commands that do what was asked, best first.

    Every word of the request has to appear somewhere on the item's path, and
    at least one in the item's own title: "history" is Show Full History, not
    every item in the History menu. Nothing is returned rather than the
    nearest thing, so a command the app does not have is never invented.
    """
    wanted = _content(what)
    if not wanted:
        return []
    found = []
    for item in (items if items is not None else menu_items(app)):
        title = set(_words(item.title))
        path = {w for part in item.path for w in _words(part)}
        if not wanted <= path or not wanted & title:
            continue
        # Best: the title says exactly what was asked; then the most of the
        # request in the title; then the shortest title; then enabled.
        exact = _content(item.title) == wanted
        found.append(((not exact, -len(wanted & title), len(title), not item.enabled), item))
    return [item for _key, item in sorted(found, key=lambda pair: pair[0])]


def shortcut(item: ax.MenuItem) -> str:
    """The keyboard shortcut in the order a Mac prints it: ⌃⌥⇧⌘."""
    if not item.cmd_char:
        return ""
    mods = item.cmd_mods or 0
    keys = ("⌃" if mods & 4 else "") + ("⌥" if mods & 2 else "") + ("⇧" if mods & 1 else "")
    keys += "" if mods & 8 else "⌘"
    return keys + item.cmd_char.upper()


def route(item: ax.MenuItem) -> str:
    """Where a command is: "File › New Tab (⌘T)"."""
    keys = shortcut(item)
    return " › ".join(item.path) + (f" ({keys})" if keys else "")


def _press(item: ax.MenuItem) -> bool:
    return ax.press(item.node)
