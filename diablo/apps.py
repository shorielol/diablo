"""
Which application a person means, and what state it is in.

The one place an application's identity is decided. "open chrome" failed with
Google Chrome installed and running, because the spoken name went straight to
`open -a`, and macOS has no application called "chrome". Elsewhere the window
list stood in for the application list, so an app with no window open read as
an app that was not there, and "chrome" matched Zen's windows.

So identity comes from the applications installed on this Mac: their bundle
names, display names and bundle ids. It never comes from windows. Running and
in-front are separate facts read from the process table. A name that could be
two applications is a question, never a guess.

    resolve("chrome")      -> Google Chrome, installed, running or not
    resolve("pro")         -> AMBIGUOUS: Final Cut Pro or Logic Pro
    resolve("photoshop")   -> UNKNOWN, after a Spotlight search to be sure
"""

import os
import plistlib
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
UNKNOWN = "unknown"

# Where applications live. One folder deep as well, for vendor folders such
# as /Applications/Utilities or "Adobe Photoshop 2025/".
ROOTS = ("/Applications", "/System/Applications", os.path.expanduser("~/Applications"))
# Applications that live outside those folders and that people ask for by name.
EXTRA = ("/System/Library/CoreServices/Finder.app",)

# Names nothing in an app's own metadata can produce. Kept small on purpose:
# every other short form is derived from the installed apps themselves (word
# runs, initials). An alias whose target is not installed matches nothing.
ALIASES = {
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "system preferences": "System Settings",
    "preferences": "System Settings",
}

# Words that say "an application" without naming one.
_LEADING = {"the", "my", "a", "an", "open", "launch", "start", "run", "app"}
_TRAILING = {"app", "application", "program", "browser", "window"}

REGISTRY_TTL = 300.0


@dataclass(frozen=True)
class App:
    """An installed application, as its bundle describes it."""

    name: str                           # what Finder shows: the bundle's file name
    bundle_id: str = ""
    path: str = ""
    other_names: Tuple[str, ...] = ()   # display name, bundle name, if different

    def names(self) -> Tuple[str, ...]:
        return (self.name, *self.other_names)


@dataclass(frozen=True)
class Resolution:
    """What a reference resolved to. `app` is set only when RESOLVED."""

    reference: str
    status: str
    app: Optional[App] = None
    candidates: Tuple[App, ...] = ()
    method: str = ""                    # exact | alias | words | initials | typo
    running: bool = False
    focused: bool = False

    @property
    def installed(self) -> bool:
        return self.app is not None


# ─── Discovery ───────────────────────────────────────────────────────────────

_cache = {"at": 0.0, "apps": []}
_lock = threading.Lock()


def _read_bundle(path: str) -> App:
    stem = os.path.basename(path)[:-4]
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except Exception:               # a malformed Info.plist is no data, not a crash
        info = {}
    others = []
    for key in ("CFBundleDisplayName", "CFBundleName"):
        value = str(info.get(key) or "").strip()
        if value and value != stem and value not in others:
            others.append(value)
    return App(stem, str(info.get("CFBundleIdentifier") or ""), path, tuple(others))


def _bundles(root: str) -> Iterable[str]:
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return
    for entry in entries:
        path = os.path.join(root, entry)
        if entry.endswith(".app"):
            yield path
        elif os.path.isdir(path) and not entry.startswith("."):
            try:
                inner = sorted(os.listdir(path))
            except OSError:
                continue
            for name in inner:
                if name.endswith(".app"):
                    yield os.path.join(path, name)


def registry(refresh: bool = False) -> List[App]:
    """Every application installed in the usual places. Cached for five minutes."""
    with _lock:
        if not refresh and _cache["apps"] and time.time() - _cache["at"] < REGISTRY_TTL:
            return list(_cache["apps"])
        found, seen = [], set()
        paths = [p for root in ROOTS for p in _bundles(root)]
        paths += [p for p in EXTRA if os.path.isdir(p)]
        for path in paths:
            app = _read_bundle(path)
            key = app.bundle_id or app.path
            if key not in seen:
                seen.add(key)
                found.append(app)
        _cache.update(at=time.time(), apps=found)
        return list(found)


def _spotlight(reference: str) -> List[App]:
    """Applications anywhere on the Mac whose name contains the reference."""
    term = normalise(reference)
    if len(term) < 3:
        return []
    query = (f"kMDItemContentType == 'com.apple.application-bundle' && "
             f"kMDItemDisplayName == '*{term}*'cd")
    try:
        out = subprocess.run(["mdfind", query], capture_output=True, text=True,
                             timeout=4).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    return [_read_bundle(p) for p in out if p.endswith(".app")]


# ─── State: running, in front ────────────────────────────────────────────────

def is_running(app: App) -> bool:
    """Whether the application has a process, from LaunchServices, not windows."""
    try:
        if app.bundle_id:
            out = subprocess.run(["lsappinfo", "info", "-only", "pid", "-app", app.bundle_id],
                                 capture_output=True, text=True, timeout=3).stdout
            return bool(re.search(r"pid\s*=\s*\d+", out))
        if app.path:
            return subprocess.run(["pgrep", "-f", f"{app.path}/Contents/MacOS/"],
                                  capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def front_bundle() -> str:
    """The bundle id of the application in front, or ""."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                             timeout=3).stdout.strip()
        out = subprocess.run(["lsappinfo", "info", "-only", "bundleid", asn],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    found = re.search(r'bundleID="([^"]+)"', out)
    return found.group(1) if found else ""


# ─── Matching ────────────────────────────────────────────────────────────────

def normalise(name: str) -> str:
    """Lower case, no punctuation, no ".app", single spaces."""
    text = (name or "").lower().replace("’", "'").strip()
    text = re.sub(r"\.app\b", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _cleaned(reference: str) -> Tuple[str, str]:
    """The reference without filler: (as given, and without generic trailing words)."""
    words = normalise(reference).split()
    while words and words[0] in _LEADING:
        words = words[1:]
    full = " ".join(words)
    while len(words) > 1 and words[-1] in _TRAILING:
        words = words[:-1]
    if len(words) == 1 and words[0] in _TRAILING:
        words = []
    return full, " ".join(words)


def _osa(a: str, b: str) -> int:
    """Edit distance counting one swap of neighbouring letters as one edit."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1,
                             rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[-1][-1]


def _run_in(words: List[str], name_words: List[str]) -> bool:
    n = len(words)
    return any(name_words[i:i + n] == words for i in range(len(name_words) - n + 1))


def _tiers(ref: str) -> List[Tuple[str, Callable[[str], bool]]]:
    """Ways a normalised reference can match a normalised app name, strictest first."""
    words = ref.split()
    compact = ref.replace(" ", "")
    alias = normalise(ALIASES.get(ref, ""))

    def initials(name: str) -> bool:
        parts = name.split()
        return (2 <= len(compact) <= 4 and " " not in ref and len(parts) >= 2
                and "".join(p[0] for p in parts) == compact)

    def typo(name: str) -> bool:
        if len(compact) < 5:
            return False
        allowed = 1 if len(compact) < 8 else 2
        return (_osa(compact, name.replace(" ", "")) <= allowed
                or any(len(w) >= 5 and _osa(compact, w) <= allowed for w in name.split()))

    return [
        ("exact", lambda name: name == ref),
        ("alias", lambda name: bool(alias) and name == alias),
        ("words", lambda name: len(compact) >= 3 and _run_in(words, name.split())),
        ("initials", initials),
        ("typo", typo),
    ]


def _match(ref: str, apps: Sequence[App]) -> Tuple[str, List[App]]:
    named = [(app, [normalise(n) for n in app.names()]) for app in apps]
    for method, test in _tiers(ref):
        hits = [app for app, names in named
                if any(test(n) for n in names)
                or (method == "exact" and ref == app.bundle_id.lower())]
        if hits:
            return method, hits
    return "", []


def resolve(reference: str, registry: Optional[Sequence[App]] = None,
            running: Optional[Callable[[App], bool]] = None,
            front: Optional[Callable[[], str]] = None) -> Resolution:
    """
    The application a reference names. Deterministic: exact names first, then
    the small alias table, then word runs ("chrome" in "Google Chrome"), then
    initials ("fcp"), then a one-letter typo. A tier that matches more than
    one application makes the answer AMBIGUOUS, with the candidates.
    """
    injected = registry is not None
    apps = list(registry) if injected else _registry()
    running = running or is_running
    front = front or front_bundle
    full, bare = _cleaned(reference)

    method, hits = "", []
    for ref in dict.fromkeys(r for r in (full, bare) if r):
        method, hits = _match(ref, apps)
        if hits:
            break
    if not hits and not injected and bare:
        # Nothing here by that name. Look again, everywhere, before saying so.
        method, hits = _match(bare, _registry(refresh=True) + _spotlight(bare))

    unique = list({(a.bundle_id or a.path): a for a in hits}.values())
    if not unique:
        return Resolution(reference, UNKNOWN)
    if len(unique) > 1:
        return Resolution(reference, AMBIGUOUS, candidates=tuple(unique), method=method)
    app = unique[0]
    is_up = bool(running(app))
    return Resolution(reference, RESOLVED, app=app, method=method, running=is_up,
                      focused=is_up and bool(app.bundle_id) and front() == app.bundle_id)


_registry = registry


# Words around a one-word name that say it is being used as an app's name:
# "in Notes", "on chrome", "the Zen browser", "Preview app".
_BEFORE_APP = {"in", "on", "into", "from", "using", "with", "inside", "open", "launch",
               "via", "through"}
_AFTER_APP = {"app", "application", "browser", "window"}
_SELF = {"diablo"}


def mentioned(text: str, registry: Optional[Sequence[App]] = None) -> List[App]:
    """
    The installed applications a request names, most specific first.

    A name of two words or more counts wherever it appears, as do its first
    two words ("final cut" for Final Cut Pro). A one-word name is often an
    ordinary word too ("notes", "shortcuts", "preview"), so it counts only
    where the sentence uses it as a name: capitalised mid-sentence, after
    "in"/"on"/"open", or before "app"/"browser".
    """
    tokens = re.findall(r"[A-Za-z0-9]+", (text or "").replace("’", "'"))
    words = [t.lower() for t in tokens]
    found = []
    for app in (registry if registry is not None else _registry()):
        # "Hey Diablo, how do I…" names the assistant, not the task's app.
        if normalise(app.name) in _SELF:
            continue
        best = 0
        for name in app.names():
            parts = normalise(name).split()
            if len(parts) >= 2:
                for run in (parts, parts[:2]):
                    if len(run) >= 2 and _run_in(run, words):
                        best = max(best, len(run))
            elif parts and len(parts[0]) >= 3:
                for i, word in enumerate(words):
                    if word != parts[0]:
                        continue
                    named = ((i > 0 and tokens[i][0].isupper())
                             or (i > 0 and words[i - 1] in _BEFORE_APP)
                             or (i + 1 < len(words) and words[i + 1] in _AFTER_APP))
                    if named:
                        best = max(best, 1)
        if best:
            found.append((best, len(app.name), app))
    found.sort(key=lambda entry: (-entry[0], -entry[1]))
    return [app for _w, _l, app in found]


# ─── The app in use ──────────────────────────────────────────────────────────
#
# "open chrome", then "open a new tab": the second request names no app, and
# Diablo asked "which browser?" twice. Whatever Diablo last opened, raised or
# acted in is remembered here for a few minutes, as the app a request that
# names none is most likely about.

IN_USE_SECONDS = 300.0
_in_use = {"app": None, "at": 0.0}


def note_in_use(app: Optional[App]) -> None:
    if app is not None:
        _in_use.update(app=app, at=time.time())


def in_use(max_age: float = IN_USE_SECONDS) -> Optional[App]:
    """The app Diablo last opened, raised or acted in, if that was recent."""
    if _in_use["app"] is not None and time.time() - _in_use["at"] <= max_age:
        return _in_use["app"]
    return None


# ─── Saying it ───────────────────────────────────────────────────────────────

def _either(apps: Sequence[App]) -> str:
    names = [a.name for a in apps[:4]]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def describe(resolution: Resolution) -> str:
    """What to tell a person about a resolution, in their terms."""
    said = (resolution.reference or "").strip()
    if resolution.status == AMBIGUOUS:
        return f"Did you mean {_either(resolution.candidates)}?"
    if resolution.status == UNKNOWN:
        if not _cleaned(said)[1]:
            return "I couldn't tell which app you meant."
        return f"I couldn't find an app called “{said}” on this Mac."
    name = resolution.app.name
    if resolution.focused:
        return f"{name} is open and in front."
    if resolution.running:
        return f"{name} is open."
    return f"{name} is installed but not open right now."


def _launch(app: App) -> bool:
    command = ["open", "-b", app.bundle_id] if app.bundle_id else ["open", app.path]
    try:
        return subprocess.run(command, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_app(reference: str, registry: Optional[Sequence[App]] = None,
             launcher: Optional[Callable[[App], bool]] = None,
             running: Optional[Callable[[App], bool]] = None,
             wait: float = 8.0) -> str:
    """
    Resolve, launch by bundle id, and say what happened, checked against the
    process table: "Opened" only once the application is actually running.
    """
    running = running or is_running
    resolution = resolve(reference, registry=registry, running=running,
                         front=(lambda: "") if registry is not None else None)
    if resolution.status != RESOLVED:
        return describe(resolution)
    app = resolution.app
    if not (launcher or _launch)(app):
        return f"I couldn't open {app.name}."
    deadline = time.time() + wait
    while True:
        if running(app):
            note_in_use(app)
            return (f"Brought {app.name} to the front." if resolution.running
                    else f"Opened {app.name}.")
        if time.time() >= deadline:
            return (f"I asked macOS to open {app.name}, but it hasn't started yet. "
                    f"It may still be loading.")
        time.sleep(0.25)
