#!/usr/bin/env python3
"""
A capture of his screen lives exactly as long as the question that needed it.

`screencapture` can only write to a file, so a capture is a PNG in the private
per-user temp folder for the second or two it takes to read it. These checks
drive every way out of that window of time — success, OCR failure, an
exception mid-read, a vision check that raises — and assert the file is gone
afterwards. They also cover what a crash leaves behind: found on this machine,
a window capture two days old.

The temp folder is redirected to a sandbox and capture, OCR and the model are
stubbed, so no real screen is captured and nothing outside the sandbox is
touched.
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

BASE = tempfile.mkdtemp(prefix="diablo-capture-lifetime-")
os.environ["DIABLO_TRACE"] = os.path.join(BASE, "trace.jsonl")

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import local_brain, screen, screen_context as sc  # noqa: E402

WINDOW = {"id": 31, "app": "Mail", "title": "Inbox", "x": 0, "y": 0,
          "width": 800, "height": 600, "sensitive": False}


class Stubs:
    """Capture writes a real (tiny) file into the sandbox; everything else is fake."""

    def __init__(self, ocr=None):
        region = {"label": "Main", "span": (0, 0), "bounds": None, "lines": ["Inbox"],
                  "headings": [], "chars": 5}
        self.ocr = ocr if ocr is not None else (lambda path, window: [dict(region)])
        self.made = []
        self._undo = []

    def capture(self, window_id):
        path = os.path.join(BASE, f"diablo_win_{window_id}_{len(self.made)}.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG not really")
        self.made.append(path)
        return path

    def patch(self, owner, name, value):
        self._undo.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def __enter__(self):
        self.patch(tempfile, "tempdir", BASE)
        self.patch(sc, "frontmost_window", lambda: dict(WINDOW))
        self.patch(sc, "find_window", lambda target: dict(WINDOW))
        self.patch(sc, "browser_tab", lambda app: None)
        self.patch(sc, "capture_window", self.capture)
        self.patch(sc, "ocr_regions", self.ocr)
        self.patch(sc, "salient_lines", lambda regions, limit=8: [])
        self.patch(screen, "validity", lambda path: (True, ""))
        return self

    def __exit__(self, *exc):
        for owner, name, value in reversed(self._undo):
            setattr(owner, name, value)

    def left(self):
        return [p for p in self.made if os.path.exists(p)]


def run():
    r = Results()

    r.head("Every way out of reading a window")
    with guard(r, "OCR fails with a message"):
        with Stubs(ocr=lambda path, window: "OCR unavailable") as s:
            info = sc.structured()
        r.check("the error is reported", info.get("error") == "OCR unavailable")
        r.check("and the capture is gone", not s.left(), str(s.left()))

    with guard(r, "OCR raises"):
        def boom(path, window):
            raise RuntimeError("vision framework crashed")
        with Stubs(ocr=boom) as s:
            try:
                sc.structured()
                raised = False
            except RuntimeError:
                raised = True
        r.check("the exception still reaches the caller", raised)
        r.check("and the capture does not outlive it", not s.left(), str(s.left()))

    with guard(r, "a successful read hands the capture on, and explain() deletes it"):
        saved_chat = local_brain.chat
        local_brain.chat = lambda *a, **k: {"content": "An inbox."}
        try:
            with Stubs() as s:
                s.patch(sc, "vision_available", lambda: False)
                info = sc.structured()
                handed = info.pop("_capture", None)
                held = bool(handed) and os.path.exists(handed)
                sc._discard(handed)
                answer = sc.explain("what is this")
        finally:
            local_brain.chat = saved_chat
        r.check("structured() keeps it for the next reader", held)
        r.check("explain() answers", answer == "An inbox.", answer)
        r.check("and nothing is left", not s.left(), str(s.left()))

    with guard(r, "the vision check raises inside explain()"):
        def broken(question, info):
            raise RuntimeError("model list unavailable")
        with Stubs() as s:
            s.patch(sc, "needs_vision", broken)
            try:
                sc.explain("what colour is the chart")
                raised = False
            except RuntimeError:
                raised = True
        r.check("the failure is not swallowed", raised)
        r.check("and the capture is still deleted", not s.left(), str(s.left()))

    with guard(r, "a walkthrough that stops before its first step"):
        # Found by an independent review: tutor.walk captured the window and
        # only started cleaning up once the steps began, so its six early
        # returns left the capture behind.
        from skills import tutor
        with Stubs() as s:
            s.patch(sc, "_ocr_fragments", lambda path, window=None: [])
            s.patch(tutor, "menu_bar_targets", lambda app: [])
            said = tutor.walk("walk me through this", speak=lambda *a, **k: None)
        r.check("it says it cannot read the window", "can't read" in said, said[:60])
        r.check("and the capture is gone", s.made and not s.left(), str(s.left()))

    r.head("What a crash leaves behind")
    with guard(r, "the sweep"):
        old = time.time() - sc.STALE_CAPTURE_SECONDS - 60
        files = {
            "diablo_win_9_1.png": old,          # orphaned by a crash: goes
            "diablo_main_2.png_big.png": old,   # derived name, still Diablo's: goes
            "diablo_win_9_2.png": time.time(),  # a read in progress: stays
            "holiday.png": old,                 # not Diablo's: stays
        }
        for name, mtime in files.items():
            path = os.path.join(BASE, name)
            open(path, "wb").close()
            os.utime(path, (mtime, mtime))
        os.makedirs(os.path.join(BASE, "diablo-outcomes-x"), exist_ok=True)
        saved_dir = tempfile.tempdir
        tempfile.tempdir = BASE
        try:
            removed = sc.sweep_stale_captures()
        finally:
            tempfile.tempdir = saved_dir
        present = set(os.listdir(BASE))
        r.check("old Diablo captures are removed", removed == 2
                and "diablo_win_9_1.png" not in present
                and "diablo_main_2.png_big.png" not in present, f"removed {removed}")
        r.check("a capture still being read is left alone", "diablo_win_9_2.png" in present)
        r.check("a file that is not Diablo's is left alone", "holiday.png" in present)
        r.check("and folders are left alone", "diablo-outcomes-x" in present)

    with guard(r, "the sweep runs when the screen subsystem starts"):
        import companion_manager
        calls = []
        saved_sweep = sc.sweep_stale_captures
        sc.sweep_stale_captures = lambda *a, **k: calls.append(1) or 0
        try:
            companion_manager.ScreenSubsystem(None).start()
        finally:
            sc.sweep_stale_captures = saved_sweep
        r.check("start() sweeps once", calls == [1], str(calls))

    return r.report("capture lifetime")


if __name__ == "__main__":
    try:
        sys.exit(run())
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
