"""
Which browser is actually installed, asked once.

Every tool that opens a URL had the string "Zen Browser" written into it. The
application is called Zen — `/Applications/Zen.app` — so every one of those
calls failed with "Unable to find application named 'Zen Browser'", and the
tools around them reported success anyway because `open` fails quietly and
nobody checked its exit code.

Hardcoding a browser name is the bug, not the particular name that was wrong.
This asks the system.
"""

import os
import plistlib
import subprocess
from typing import List, Optional

# Preference order, not a whitelist: anything installed can be used, these are
# just the ones to reach for first. Zen is first because it was the one the
# project was written against.
PREFERRED = ["Zen", "Arc", "Dia", "Google Chrome", "Brave Browser",
             "Firefox", "Microsoft Edge", "Safari"]

_cached: Optional[str] = None


def installed() -> List[str]:
    """Every browser-looking application present, in preference order."""
    found = []
    for name in PREFERRED:
        for root in ("/Applications", os.path.expanduser("~/Applications")):
            if os.path.isdir(os.path.join(root, f"{name}.app")):
                found.append(name)
                break
    return found


def default() -> str:
    """
    The browser to use. DIABLO_BROWSER wins, then the system default, then
    whatever is installed. Safari is the last resort because it is always
    there, so falling back to it can never fail.
    """
    global _cached
    if _cached:
        return _cached

    chosen = (os.getenv("DIABLO_BROWSER") or "").strip()
    if chosen and _exists(chosen):
        _cached = chosen
        return _cached

    system = _system_default()
    if system and _exists(system):
        _cached = system
        return _cached

    here = installed()
    _cached = here[0] if here else "Safari"
    return _cached


def _exists(name: str) -> bool:
    return any(os.path.isdir(os.path.join(root, f"{name}.app"))
               for root in ("/Applications", os.path.expanduser("~/Applications")))


def _system_default() -> Optional[str]:
    """
    Whatever macOS opens an https link with.

    Read from LaunchServices rather than guessed. The handler is a bundle id,
    so it is turned back into an application name with mdfind.
    """
    plist = os.path.expanduser(
        "~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist")
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return None
    bundle = None
    for handler in data.get("LSHandlers", []):
        if handler.get("LSHandlerURLScheme") == "https":
            bundle = handler.get("LSHandlerRoleAll")
            break
    if not bundle:
        return None
    try:
        out = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == '{bundle}'"],
            capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
    except Exception:
        return None
    for path in out:
        if path.endswith(".app"):
            return os.path.basename(path)[:-4]
    return None


def open_url(url: str, browser: Optional[str] = None) -> bool:
    """
    Open a URL and say whether it actually opened.

    Returns a bool because `open -a` exits non-zero on a missing application
    and every caller here used to ignore that, which is how a browser that has
    not existed for the life of the project went unnoticed.
    """
    name = browser or default()
    try:
        result = subprocess.run(["open", "-a", name, url],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return True
        # Named browser gone: let macOS pick rather than failing the request.
        return subprocess.run(["open", url], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False


if __name__ == "__main__":
    print("installed :", ", ".join(installed()) or "(none found)")
    print("system    :", _system_default() or "(unreadable)")
    print("using     :", default())
