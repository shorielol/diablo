#!/usr/bin/env python3
"""
The app: its API, its assets, and the parts of the shell that can be checked
without a screen.

What cannot be automated here is whether the window looks right, and the way
that was verified during the build is worth recording, because two rounds were
wasted on it: counting red pixels in a screenshot found a red Stop button
somewhere else on screen and reported the pill as working when nothing was
being drawn at all. The reliable check is CGWindowList — if the window server
does not list the window, it is not on screen, whatever isVisible() claims.

That is the check this file makes.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard, Timer  # noqa: E402


def _orb_canvas():
    """
    The orb window's edge in points, read from app_shell's own constants.

    Returns (edge, where_it_came_from). The running app is launched from the
    install, so that copy is the one that produced the window on screen; the
    repository copy is the fallback for a checkout with nothing running.
    """
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        (os.path.join(os.path.expanduser("~/.diablo"), "app_shell.py"), "the install"),
        (os.path.join(os.path.dirname(here), "app_shell.py"), "the repository"),
    ]
    for path, whence in candidates:
        try:
            source = open(path, encoding="utf-8").read()
        except OSError:
            continue
        orb = re.search(r"^ORB\s*=\s*([\d.]+)", source, re.M)
        halo = re.search(r"^HALO\s*=\s*([\d.]+)", source, re.M)
        if orb and halo:
            return float(orb.group(1)) + float(halo.group(1)) * 2, whence
    return None, "nowhere — app_shell.py was not readable"


BASE = os.getenv("DIABLO_UI_URL", "http://127.0.0.1:17494")
r = Results()


def get(path, timeout=8):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return response.status, response.read()


def post(path, body, timeout=8):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read() or b"{}")


def running():
    try:
        get("/api/state", timeout=3)
        return True
    except Exception:
        return False


def run():
    if not running():
        print(f"\n  Diablo is not running. Start it with:  open -a Diablo\n")
        return 1

    # ── the API ─────────────────────────────────────────────────────────────
    r.head("API")
    for path, shape in [("/api/state", dict), ("/api/jobs", list),
                        ("/api/trace", list), ("/api/conversation", list),
                        ("/api/settings", dict), ("/api/everything", dict)]:
        with guard(r, path):
            with Timer() as t:
                status, body = get(path)
            payload = json.loads(body)
            r.check(f"{path} answers", status == 200 and isinstance(payload, shape),
                    f"{status}, {type(payload).__name__}, {t.elapsed * 1000:.0f}ms")

    with guard(r, "/api/everything is one round trip"):
        status, body = get("/api/everything")
        payload = json.loads(body)
        for key in ("state", "jobs", "trace", "conversation", "settings"):
            r.check(f"it carries {key}", key in payload)

    with guard(r, "state has what a UI needs to render"):
        payload = json.loads(get("/api/state")[1])
        for key in ("state", "subsystems", "permissions", "degraded"):
            r.check(f"state has {key!r}", key in payload)
        r.check("the state is a known one",
                payload["state"] in ("cold", "init", "idle", "listening",
                                     "processing", "responding", "error", "shutdown"),
                payload["state"])

    with guard(r, "an unknown route is a clean 404"):
        try:
            get("/api/nonsense")
            r.bad("unknown route 404s", "it returned 200")
        except urllib.error.HTTPError as e:
            r.check("unknown route 404s", e.code == 404, str(e.code))

    # ── assets ──────────────────────────────────────────────────────────────
    r.head("The page and its assets")
    for path, needle in [
        ("/", b"<div id=\"root\">"),
        ("/static/styles.css", b"--crimson"),
        ("/static/app.js", b"function App()"),
        ("/static/mascot.js", b"export function mascot"),
        ("/static/vendor/react.js", b"react.production.min.js"),
        ("/static/vendor/react-dom.js", b"react-dom"),
        ("/static/vendor/htm.js", b"htm"),
    ]:
        with guard(r, path):
            status, body = get(path)
            r.check(f"{path} serves", status == 200 and needle in body,
                    f"{status}, {len(body)} bytes")

    with guard(r, "React is vendored, not fetched"):
        page = get("/")[1].decode()
        r.check("no external script tags", "//unpkg" not in page and "//cdn" not in page,
                "the app must work with no network")

    with guard(r, "static paths cannot escape the app directory"):
        leaked = []
        for attempt in ("/static/../.env", "/static/../../.ssh/id_rsa",
                        "/static/....//.env"):
            try:
                status, body = get(attempt)
                if status == 200 and b"DIABLO_MODEL" in body:
                    leaked.append(attempt)
            except urllib.error.HTTPError:
                pass
        r.check("traversal is refused", not leaked, ", ".join(leaked) or "3 attempts blocked")

    # ── events ──────────────────────────────────────────────────────────────
    r.head("Event stream")
    with guard(r, "SSE connects and sends"):
        request = urllib.request.Request(BASE + "/events")
        with urllib.request.urlopen(request, timeout=6) as stream:
            first = stream.readline()
            r.check("the stream opens", first.startswith(b":"), first[:30].decode())
            r.check("content type is text/event-stream",
                    "text/event-stream" in stream.headers.get("Content-Type", ""),
                    stream.headers.get("Content-Type"))

    # ── settings round trip ─────────────────────────────────────────────────
    r.head("Settings")
    with guard(r, "a dial can be changed and read back"):
        before = json.loads(get("/api/settings")[1])["humour"]["value"]
        target = 0 if before != 0 else 3
        status, result = post("/api/settings", {"humour": target})
        r.check("the write is accepted", result.get("ok"), str(result)[:60])
        after = json.loads(get("/api/settings")[1])["humour"]["value"]
        r.check("and it took effect", after == target, f"{before} -> {after}")
        post("/api/settings", {"humour": before})
        restored = json.loads(get("/api/settings")[1])["humour"]["value"]
        r.check("and it can be put back", restored == before, str(restored))

    with guard(r, "a model change is reported as needing a restart"):
        current = json.loads(get("/api/settings")[1])["model"]["value"]
        _, result = post("/api/settings", {"model": current})
        r.check("model is flagged", "model" in (result.get("needs_restart") or []),
                str(result.get("needs_restart")))

    with guard(r, "rubbish in the settings body does not break it"):
        for body in ({}, {"nonsense": 1}, {"humour": "loud"}):
            status, result = post("/api/settings", body)
            r.check(f"survives {body}", isinstance(result, dict), str(result)[:50])

    # ── control ─────────────────────────────────────────────────────────────
    r.head("The roster over the wire")
    with guard(r, "/api/agents"):
        _, raw = get("/api/agents")
        agents = json.loads(raw)
        r.check("the roster is served", isinstance(agents, list) and len(agents) >= 4,
                f"{len(agents) if isinstance(agents, list) else agents} agents")
        ids = [a.get("id") for a in agents]
        r.check("Diablo leads", ids and ids[0] == "diablo", str(ids))
        r.check("Asmo is there", "asmo" in ids, str(ids))
        # The reader, the writer and the guard were a drawing of the permission
        # gate, not agents. They are not the user's to manage and must not come
        # back as cards.
        r.check("the internal safety classes are not exposed",
                not ({"scout", "scribe", "warden"} & set(ids)), str(ids))
        r.check("every agent can be drawn",
                all(a.get("name") and a.get("accent") and a.get("mouth")
                    for a in agents))
        r.check("no routing internals leak to the window",
                all("tools" not in a and "keywords" not in a for a in agents))

    with guard(r, "the roster comes down with everything else"):
        _, raw = get("/api/everything")
        everything = json.loads(raw)
        r.check("one round trip carries the agents",
                isinstance(everything.get("agents"), list)
                and len(everything["agents"]) >= 4)

    r.head("Asking for permission")
    with guard(r, "recheck drops the cache and re-reads"):
        _, result = post("/api/control", {"action": "recheck"})
        r.check("recheck is accepted", result.get("ok"), str(result)[:60])
        perms = result.get("permissions") or {}
        r.check("it returns all three",
                set(perms) == {"mic", "screen", "accessibility"}, str(perms))

    with guard(r, "an unknown permission is refused rather than opening a pane"):
        _, result = post("/api/control", {"action": "grant:nonsense"})
        r.check("refused", result.get("ok") is False, str(result)[:70])

    r.head("Control")
    with guard(r, "interrupt and reset"):
        for action in ("interrupt", "reset"):
            _, result = post("/api/control", {"action": action})
            r.check(f"{action} is accepted", result.get("ok"), str(result)[:50])

    with guard(r, "an unknown action is refused, not run"):
        _, result = post("/api/control", {"action": "self_destruct"})
        r.check("unknown action refused", result.get("ok") is False, str(result)[:60])

    with guard(r, "an empty message is rejected"):
        try:
            post("/api/say", {"text": "   "})
            r.bad("empty text is rejected", "it was accepted")
        except urllib.error.HTTPError as e:
            r.check("empty text is rejected", e.code == 400, str(e.code))

    # ── the window server ───────────────────────────────────────────────────
    r.head("What is actually on screen")
    with guard(r, "the pill is listed by the window server"):
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
        ours = [w for w in windows
                if "python" in (w.get("kCGWindowOwnerName") or "").lower()]
        # isVisible() returning True is not evidence. A panel missing
        # NonactivatingPanel reported visible, ran drawRect_, and never
        # appeared in this list.
        r.check("a Diablo window is composited", bool(ours),
                f"{len(ours)} window(s)")
        if ours:
            pill = ours[0]
            bounds = pill.get("kCGWindowBounds") or {}
            # Square, and small. The 300x56 capsule this replaced sat across
            # whatever was underneath it all day; the point of the orb is that
            # it does not.
            #
            # Derived, not hardcoded. This assertion carried a literal 92 long
            # after the orb became 44 + 11*2 = 66, so it failed for a year's
            # worth of the wrong reason and told nobody anything. Read the
            # design constants out of the shell the running app was launched
            # from, and the test follows the design instead of arguing with it.
            edge, whence = _orb_canvas()
            r.check("it is at the orb's size",
                    edge is not None
                    and abs(bounds.get("Width", 0) - edge) < 2
                    and abs(bounds.get("Height", 0) - edge) < 2,
                    f"{bounds.get('Width')}x{bounds.get('Height')}, "
                    f"expected {edge}x{edge} from {whence}")
            r.check("above ordinary windows", pill.get("kCGWindowLayer", 0) >= 20,
                    f"layer {pill.get('kCGWindowLayer')}")

    with guard(r, "it renders pixels, not an empty frame"):
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
        ours = [w for w in windows
                if "python" in (w.get("kCGWindowOwnerName") or "").lower()]
        if not ours:
            r.skip("pixel check", "no window to capture")
        else:
            shot = "/tmp/diablo_pill_test.png"
            subprocess.run(["screencapture", "-x", "-o", "-l",
                            str(ours[0]["kCGWindowNumber"]), shot],
                           capture_output=True, timeout=20)
            from PIL import Image
            image = Image.open(shot).convert("RGB")
            distinct = len(set(image.get_flattened_data()))
            # A blank or single-colour capture means the window exists and
            # draws nothing, which is exactly the failure this guards against.
            r.check("the capture has real content", distinct > 40,
                    f"{distinct} distinct colours, {image.size[0]}x{image.size[1]}")
            os.unlink(shot)

    return r.report("app")


if __name__ == "__main__":
    sys.exit(run())
