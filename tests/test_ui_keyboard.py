#!/usr/bin/env python3
"""
The window, driven from the keyboard, in the real app.

Source checks said the menu had arrow keys and Escape put focus back; only the
running page can say whether they work. This loads the real app from the real
server into a WKWebView owned by this process — the same engine the shell uses
— and sends DOM key events into that page alone. No other application, window
or input device is touched, and nothing is posted to the system event stream.

  menu      Down on the closed button opens it and focuses the first item;
            Down, Up, Home and End move and wrap; Escape closes it and focus
            returns to its button
  IME       Escape while composing does nothing at all
  stop      a queued task shows Stop in the compact surface; pressing it
            cancels that task, in memory and on disk, and the row goes
  needs     the "needs you" rule is amber, not the keyboard's focus colour
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

BASE = tempfile.mkdtemp(prefix="diablo-ui-keys-")
os.environ["DIABLO_BASE_DIR"] = BASE
os.environ["DIABLO_TRACE"] = os.path.join(BASE, "trace.jsonl")
os.environ.setdefault("DIABLO_UI_PORT", "17594")

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

WIDTH, HEIGHT = 392.0, 560.0


class Page:
    """The real app in a WKWebView, and a way to ask it questions."""

    def __init__(self, url):
        from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                            NSBackingStoreBuffered, NSMakeRect, NSWindow,
                            NSWindowStyleMaskBorderless)
        from Foundation import NSURL, NSURLRequest
        from WebKit import WKWebView, WKWebViewConfiguration
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        frame = NSMakeRect(40, 40, WIDTH, HEIGHT)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        self.view = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), WKWebViewConfiguration.alloc().init())
        self.window.setContentView_(self.view)
        self.view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        self.window.orderFrontRegardless()

    def spin(self, seconds):
        from Foundation import NSDate, NSRunLoop
        end = time.time() + seconds
        while time.time() < end:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.02))

    def js(self, code, wait=5.0):
        box = {}

        def done(result, error):
            box["result"], box["error"] = result, error
        self.view.evaluateJavaScript_completionHandler_(code, done)
        end = time.time() + wait
        while "result" not in box and time.time() < end:
            self.spin(0.02)
        if box.get("error") is not None:
            raise RuntimeError(str(box["error"])[:200])
        return box.get("result")

    def until(self, code, wait=8.0):
        end = time.time() + wait
        while time.time() < end:
            if self.js(code):
                return True
            self.spin(0.1)
        return False

    def close(self):
        self.window.orderOut_(None)


FOCUSED = "(document.activeElement && (document.activeElement.textContent || '').trim())"
KEY = ("(function(k, extra){ var t = document.activeElement || document.body;"
       " var e = new KeyboardEvent('keydown', Object.assign({key: k, bubbles: true,"
       " cancelable: true}, extra || {})); t.dispatchEvent(e); return e.defaultPrevented; })")


def menu_checks(r, page):
    r.head("The menu, from the keyboard")
    with guard(r, "opening"):
        page.js("document.querySelector('.menu summary').focus();")
        page.js(KEY + "('ArrowDown')")
        r.check("Down on the closed button opens it",
                page.until("document.querySelector('.menu').open"))
        items = page.js("[...document.querySelectorAll('[role=menuitem]')]"
                        ".map(b => b.textContent.trim())") or []
        r.check("the menu has items to move between", len(items) >= 3, str(items))
        r.check("and focus lands on the first item",
                page.until(f"{FOCUSED} === {json.dumps(items[0])}"), page.js(FOCUSED))
    with guard(r, "moving"):
        page.js(KEY + "('ArrowDown')")
        r.check("Down moves to the next item", page.js(FOCUSED) == items[1], page.js(FOCUSED))
        page.js(KEY + "('End')")
        r.check("End goes to the last", page.js(FOCUSED) == items[-1], page.js(FOCUSED))
        page.js(KEY + "('ArrowDown')")
        r.check("Down from the last wraps to the first",
                page.js(FOCUSED) == items[0], page.js(FOCUSED))
        page.js(KEY + "('ArrowUp')")
        r.check("Up from the first wraps to the last",
                page.js(FOCUSED) == items[-1], page.js(FOCUSED))
        page.js(KEY + "('Home')")
        r.check("Home goes to the first", page.js(FOCUSED) == items[0], page.js(FOCUSED))
    with guard(r, "Escape while composing"):
        page.js(KEY + "('Escape', {isComposing: true})")
        page.spin(0.2)
        r.check("does nothing: the menu stays open",
                page.js("document.querySelector('.menu').open") is True)
    with guard(r, "Escape"):
        r.check("focus is inside the menu before Escape",
                page.js("document.querySelector('.menulist').contains(document.activeElement)"))
        page.js(KEY + "('Escape')")
        page.spin(0.2)
        r.check("closes the menu", page.js("document.querySelector('.menu').open") is False)
        r.check("and focus is back on its button",
                page.js("document.activeElement === document.querySelector('.menu summary')"))
        r.check("the surface itself is still there", page.js("!!document.querySelector('.stage')"))


def stop_checks(r, page, dt, server):
    r.head("Stopping a task")
    with guard(r, "a queued task"):
        job = {"id": "uitest01", "task": "Compare three laptop stands", "agent": "asmo",
               "status": "queued", "created_at": time.time(), "parts": []}
        dt.store.add(job)
        server.push("job_queued", id=job["id"], agent=job["agent"])   # as submit() does
        r.check("shows a Stop button in the compact surface",
                page.until("!!document.querySelector('.runstop')", wait=10))
        label = page.js("document.querySelector('.runstop')?.getAttribute('aria-label')")
        r.check("named for the task it stops",
                label == "Stop: Compare three laptop stands", str(label))
        page.js("document.querySelector('.runstop').click()")
        gone = page.until("!document.querySelector('.runstop')", wait=8)
        record = next(j for j in dt.store.recent(10) if j["id"] == "uitest01")
        r.check("pressing it cancels the task", record["status"] == "cancelled",
                record["status"])
        with open(dt.store.path) as f:
            on_disk = {j["id"]: j for j in json.load(f)}
        r.check("on disk as well", on_disk["uitest01"]["status"] == "cancelled")
        r.check("and the row goes", gone)


def needs_checks(r, page):
    r.head("Needs you")
    with guard(r, "the permission request"):
        r.check("is on screen", page.until("!!document.querySelector('.needs')"))
        border, warn, focus = page.js(
            "(function(){ var n = document.querySelector('.needs');"
            " var probe = document.createElement('span'); document.body.appendChild(probe);"
            " probe.style.color = 'var(--warn)'; var warn = getComputedStyle(probe).color;"
            " probe.style.color = 'var(--focus)'; var focus = getComputedStyle(probe).color;"
            " probe.remove(); return [getComputedStyle(n).borderLeftColor, warn, focus]; })()")
        r.check("its rule is the amber that means needs you", border == warn,
                f"{border} vs {warn}")
        r.check("not the keyboard's focus colour", border != focus, focus)


def run():
    r = Results()
    from skills import delegation_tool as dt
    from skills import server

    # A temporary store, and one permission reported missing so the request
    # can be seen. Nothing reaches ~/.diablo.
    dt.store = dt.JobStore(os.path.join(BASE, "jobs.json"))
    original_state = server._state

    def denied():
        state = dict(original_state())
        perms = dict(state.get("permissions") or {})
        perms["screen"] = False
        state["permissions"] = perms
        return state
    server._state = denied

    url = server.start()
    if not url:
        r.check("the app server started", False)
        return r.report("ui keyboard")
    page = Page(url)
    try:
        with guard(r, "the app loads"):
            r.check("the menu button is on the page",
                    page.until("!!document.querySelector('.menu summary')", wait=15))
        menu_checks(r, page)
        stop_checks(r, page, dt, server)
        needs_checks(r, page)
    finally:
        page.close()
        server._state = original_state
        try:
            server.stop()
        except Exception:
            pass
    return r.report("ui keyboard")


if __name__ == "__main__":
    try:
        code = run()
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
    os._exit(code)
