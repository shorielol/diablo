"""
A photograph of the interface, at a size you choose.

Section 23 of the redesign brief says to run the application and look at it
rather than judging the UI from source. This makes that repeatable: it starts
the real server, loads the real app in a real WKWebView at a given size, waits
for it to settle, and captures that window by its window id — so what lands in
the PNG is what the compositor drew, not a guess.

    python3 tests/ui_shot.py --width 392 --height 560 --out /tmp/compact.png
    python3 tests/ui_shot.py --width 1040 --height 680 --hash '#health'

Captures one window by id, so nothing else on screen ends up in the frame and
the shot is identical whatever is behind it.
"""

import argparse
import os
import sys
import threading
import time

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

try:
    from companion_manager import load_env
    load_env()
except Exception:
    pass


def _seed(what: str) -> None:
    """
    Put state in front of the camera, in memory only.

    Deliberately not through JobStore.add(): that flushes to ~/.diablo/jobs.json,
    and a screenshot is not a reason to write invented work into the real
    record. Appending to the in-memory list is what the server reads, and it
    dies with this process.
    """
    import time as _time
    if what in ("talk", "both"):
        from skills import conversation
        conversation.session.turns.extend([
            {"role": "user", "content": "how do I export this as a PDF?"},
            {"role": "assistant",
             "content": "It is under the share menu rather than file, which is "
                        "where most people look first. I will walk you through "
                        "it \u2014 watch the screen."},
        ])
    if what in ("work", "both"):
        from skills import delegation_tool
        delegation_tool.store._jobs.append({
            "id": "demo", "task": "Find current UK apprenticeship deadlines",
            "agent": "vassago", "status": "running",
            "created_at": _time.time(),
            "parts": [{"agent": "vassago",
                       "task": "Find current UK apprenticeship deadlines",
                       "status": "running"}],
        })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=float, default=392.0)
    parser.add_argument("--height", type=float, default=560.0)
    parser.add_argument("--hash", default="")
    parser.add_argument("--out", default="/tmp/diablo_ui.png")
    parser.add_argument("--settle", type=float, default=2.6)
    parser.add_argument("--demo", default="",
                        help="seed state so a surface can be photographed: "
                             "'talk' for a conversation, 'work' for a job in "
                             "flight, 'both' for both")
    parser.add_argument("--titled", action="store_true",
                        help="real window chrome, so the traffic lights are in "
                             "the shot where they will be in the product")
    args = parser.parse_args()

    from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                        NSBackingStoreBuffered, NSMakeRect, NSWindow,
                        NSWindowStyleMaskBorderless)
    from Foundation import NSURL, NSURLRequest
    from WebKit import WKWebView, WKWebViewConfiguration

    from skills import server

    if args.demo:
        _seed(args.demo)

    url = server.start()
    if not url:
        print("could not start the server", file=sys.stderr)
        return 1

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    frame = NSMakeRect(120, 120, args.width, args.height)
    if args.titled:
        from AppKit import (NSColor, NSWindowStyleMaskClosable,
                            NSWindowStyleMaskFullSizeContentView,
                            NSWindowStyleMaskMiniaturizable,
                            NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
                            NSWindowTitleHidden)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskFullSizeContentView)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False)
        window.setTitle_("Diablo")
        window.setTitlebarAppearsTransparent_(True)
        window.setTitleVisibility_(NSWindowTitleHidden)
        window.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.043, 0.043, 0.051, 1.0))
    else:
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        window.setOpaque_(False)
        window.setHasShadow_(False)

    config = WKWebViewConfiguration.alloc().init()
    view = WKWebView.alloc().initWithFrame_configuration_(
        NSMakeRect(0, 0, args.width, args.height), config)
    view.setValue_forKey_(False, "drawsBackground")
    window.setContentView_(view)
    view.loadRequest_(NSURLRequest.requestWithURL_(
        NSURL.URLWithString_(url + args.hash)))
    window.orderFrontRegardless()

    def shoot():
        time.sleep(args.settle)
        import Quartz
        from skills import screen_context
        number = None
        for entry in Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
            if int(entry.get("kCGWindowOwnerPID") or 0) != os.getpid():
                continue
            bounds = entry.get("kCGWindowBounds") or {}
            if abs(bounds.get("Width", 0) - args.width) < 2:
                number = int(entry.get("kCGWindowNumber", 0))
                break
        if not number:
            print("could not find the window to capture", file=sys.stderr)
            os._exit(1)
        path = screen_context.capture_window(number)
        if not path:
            print("capture failed", file=sys.stderr)
            os._exit(1)
        import shutil
        shutil.move(path, args.out)
        print(args.out)
        sys.stdout.flush()
        os._exit(0)

    threading.Thread(target=shoot, daemon=True).start()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
