#!/usr/bin/env python3
"""
The tools that reach outside the sandbox and touch the machine itself.

These have a property the filesystem tools do not: running the test changes the
state of the Mac. Volume and brightness are saved and put back in a finally
block, screenshots are deleted, and nothing here opens an app or a browser tab
unless DIABLO_TEST_INTRUSIVE=1, because a test suite that steals focus while you
are working is worse than no test suite.

What is being checked is not "does osascript work" but "does the tool return a
sentence the model can act on, and does it clamp the arguments a 14B model will
eventually get wrong".
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from skills import agent_tools as T  # noqa: E402

INTRUSIVE = os.getenv("DIABLO_TEST_INTRUSIVE") == "1"
r = Results()


def current_volume():
    try:
        out = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip())
    except Exception:
        return None


def run():
    r.head("System status")
    with guard(r, "system_status"):
        out = T.execute("system_status", {})
        r.check("returns a readable status", isinstance(out, str) and len(out) > 10,
                out[:70].replace("\n", " "))
        r.check("mentions something numeric", any(c.isdigit() for c in out))

    # ── volume, restored afterwards ─────────────────────────────────────────
    r.head("Volume")
    original = current_volume()
    if original is None:
        r.skip("volume tools", "cannot read the current volume")
    else:
        try:
            with guard(r, "set_volume sets"):
                T.execute("set_volume", {"level": 30})
                time.sleep(0.4)
                r.check("volume actually changed", abs((current_volume() or -1) - 30) <= 2,
                        f"now {current_volume()}")

            with guard(r, "set_volume clamps high"):
                out = T.execute("set_volume", {"level": 9999})
                time.sleep(0.4)
                r.check("above 100 clamps to 100", (current_volume() or 0) == 100, out[:40])

            with guard(r, "set_volume clamps low"):
                out = T.execute("set_volume", {"level": -400})
                time.sleep(0.4)
                # Assert on what the tool reports, not on reading the volume
                # back: macOS keeps a separate mute flag, so a device that is
                # muted can report a non-zero output volume and the read-back
                # tells you about CoreAudio rather than about the clamp.
                r.check("below 0 clamps to 0", "0%" in out, out[:40])

            with guard(r, "set_volume rejects nonsense"):
                out = T.execute("set_volume", {"level": "very loud"})
                r.check("a non-number is an error, not a crash",
                        isinstance(out, str) and "Error" in out, out[:55])
        finally:
            subprocess.run(["osascript", "-e", f"set volume output volume {original}"],
                           capture_output=True)
            r.check("volume was restored", abs((current_volume() or -1) - original) <= 2,
                    f"back to {original}%")

    # ── brightness ──────────────────────────────────────────────────────────
    r.head("Brightness")
    with guard(r, "set_brightness"):
        out = T.execute("set_brightness", {"level": 70})
        r.check("returns a sentence either way", isinstance(out, str) and len(out) > 5,
                out[:65])
        if "unavailable" in out or "Error" in out:
            r.skip("brightness actually changes", "no brightness control on this Mac")

    # ── screenshot ──────────────────────────────────────────────────────────
    r.head("Screenshot")
    before = set(os.listdir(os.path.expanduser("~/Desktop")))
    with guard(r, "take_screenshot"):
        out = T.execute("take_screenshot", {})
        time.sleep(1.5)
        after = set(os.listdir(os.path.expanduser("~/Desktop")))
        new = [f for f in (after - before) if f.lower().endswith(".png")]
        r.check("reports success", isinstance(out, str) and "Error" not in out, out[:60])
        if r.check("a PNG landed on the Desktop", bool(new), ", ".join(new) or "nothing new"):
            path = os.path.join(os.path.expanduser("~/Desktop"), new[0])
            r.check("and it is not empty", os.path.getsize(path) > 1000,
                    f"{os.path.getsize(path)} bytes")
            os.unlink(path)
            r.ok("test screenshot cleaned up")

    # ── window sight ────────────────────────────────────────────────────────
    r.head("Seeing the screen")
    with guard(r, "list_windows"):
        out = T.execute("list_windows", {})
        r.check("lists real windows",
                isinstance(out, str) and "Error" not in out and "window" in out.lower(),
                out[:70].replace("\n", " "))

    with guard(r, "read_screen on the frontmost window"):
        out = T.execute("read_screen", {"target": ""})
        r.check("returns window context",
                isinstance(out, str) and "Error" not in out and "Window" in out,
                out[:60].replace("\n", " "))

    with guard(r, "read_screen with a nonsense target"):
        out = T.execute("read_screen", {"target": "zzqqxx-not-an-app"})
        r.check("does not silently grab the wrong window",
                isinstance(out, str) and ("could not" in out.lower() or "no " in out.lower()
                                          or "Error" in out),
                out[:60].replace("\n", " "))

    # ── things that steal focus ─────────────────────────────────────────────
    r.head("Apps, browser and media")
    intrusive = [
        ("open_app",       {"name": "Calculator"}),
        ("open_url",       {"url": "https://example.com"}),
        ("play_youtube",   {"query": "test"}),
        ("web_search",     {"query": "test"}),
        ("play_music",     {"query": ""}),
        ("music_transport", {"action": "pause"}),
    ]
    if not INTRUSIVE:
        for name, _ in intrusive:
            r.skip(name, "would steal focus; set DIABLO_TEST_INTRUSIVE=1")
        # Still prove they are wired to something, without firing them.
        for name, _ in intrusive:
            with guard(r, f"{name} is registered and has a handler"):
                tool = T.TOOLS.get(name)
                r.check(f"{name} is wired up", tool is not None and callable(tool["fn"]))
    else:
        for name, args in intrusive:
            with guard(r, f"{name} runs"):
                out = T.execute(name, args)
                r.check(f"{name} returns a sentence",
                        isinstance(out, str) and len(out) > 3, out[:55])
                time.sleep(1)
        subprocess.run(["osascript", "-e", 'quit app "Calculator"'], capture_output=True)

    # ── offline gating ──────────────────────────────────────────────────────
    r.head("Offline mode is honoured")
    with guard(r, "network tools are marked"):
        networked = [n for n, t in T.TOOLS.items() if t["network"]]
        r.check("some tools are flagged as needing the internet", bool(networked),
                ", ".join(sorted(networked)))

    was = os.environ.get("DIABLO_OFFLINE")
    os.environ["DIABLO_OFFLINE"] = "1"
    try:
        with guard(r, "offline blocks networked tools"):
            blocked = []
            for name in networked:
                out = T.execute(name, {k: "x" for k in
                                       T.TOOLS[name]["schema"]["function"]["parameters"]["required"]})
                if "offline mode" not in out:
                    blocked.append(f"{name}->{out[:30]}")
            r.check("every networked tool refuses while offline", not blocked,
                    "; ".join(blocked) or f"{len(networked)} refused")

        with guard(r, "offline does not block local tools"):
            out = T.execute("system_status", {})
            r.check("local tools still work offline", "offline mode" not in out, out[:50])
    finally:
        if was is None:
            os.environ.pop("DIABLO_OFFLINE", None)
        else:
            os.environ["DIABLO_OFFLINE"] = was

    # ── contacts ────────────────────────────────────────────────────────────
    r.head("Contacts")
    with guard(r, "search_contact"):
        out = T.execute("search_contact", {"name": "a"})
        r.check("returns something rather than raising", isinstance(out, str),
                out[:60].replace("\n", " "))

    with guard(r, "contact aliases resolve"):
        r.check("'mom' maps to a real contact name",
                T.CONTACT_ALIASES.get("mom") is not None, T.CONTACT_ALIASES.get("mom"))

    r.head("Capabilities")
    with guard(r, "list_capabilities"):
        out = T.execute("list_capabilities", {})
        r.check("reports the built-in tool count", "built-in tools" in out,
                out.splitlines()[0] if out else "")
        r.check("reports MCP state", "MCP" in out or "mcp" in out,
                " / ".join(out.splitlines()[1:3]))

    return r.report("system")


if __name__ == "__main__":
    sys.exit(run())
