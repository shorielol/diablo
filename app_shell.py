"""
D.I.A.B.L.O. — the Mac app.

Three surfaces, and the split between them is the whole design:

  menu bar    always there, costs nothing
  the pill    a small floating capsule near your cursor showing what Diablo is
              doing right now. Drawn natively in AppKit.
  the panel   the full window — conversation, agents, trace, health, settings.
              A WKWebView, created the first time you open it and not before.

The pill is native rather than a second web view on purpose. It is the surface
that is visible all day, and a WKWebView content process costs about 33 MB on
this machine. Drawing a rounded rectangle and twenty-eight bars in Core
Graphics costs nothing measurable. The rich panel earns its 33 MB because you
only open it when you want it, and it closes again.

The shape is borrowed from Clicky, which got it right: menu-bar only with no
Dock icon, a non-activating always-on-top pill that never steals focus, and a
live waveform so you can see it hearing you. What is different is what the pill
says — Diablo does work, not just dictation, so it reports which agent is
running and what tool it is in.

Threading: AppKit owns the main thread, full stop. The voice loop moves to a
background thread, which is the opposite of how voice_bridge ran before and the
only arrangement macOS allows.
"""

import json
import os
import sys
import threading
import time
import traceback
import urllib.request
from typing import Any, Callable, Dict, List, Optional

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
# Set DIABLO_APP_DEBUG=1 to trace the pill. Verifying this by counting red
# pixels in a screenshot produced two false positives in a row — a stop button
# elsewhere on screen is also crimson — so the app says what it is doing.
DEBUG = os.getenv("DIABLO_APP_DEBUG", "0") not in ("0", "", "false")


def _log(*parts):
    if DEBUG:
        print("  [shell]", *parts, flush=True)
sys.path.insert(0, HOME)

import objc  # noqa: E402
from AppKit import (  # noqa: E402
    NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
    NSBezierPath, NSColor, NSEvent, NSFont, NSFontAttributeName,
    NSForegroundColorAttributeName, NSImage, NSMakeRect, NSMakeSize, NSMenu,
    NSMenuItem, NSPoint, NSScreen, NSStatusBar, NSString, NSTimer, NSView,
    NSWindow, NSWindowStyleMaskBorderless, NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView, NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowTitleHidden,
    NSPanel, NSWindowStyleMaskNonactivatingPanel, NSGraphicsContext,
)
from Foundation import NSOperationQueue, NSURL, NSURLRequest  # noqa: E402
from WebKit import WKWebView, WKWebViewConfiguration  # noqa: E402

# ─── Look ────────────────────────────────────────────────────────────────────
# The same tokens the web UI uses, so the two surfaces match exactly.

CRIMSON = (0.878, 0.137, 0.235)
GOLD = (0.949, 0.761, 0.188)
BLUE = (0.239, 0.663, 0.988)
GREY = (0.290, 0.290, 0.333)
RED = (1.0, 0.420, 0.420)

STATE_LOOK = {
    "idle":       (GREY,    "ready"),
    "listening":  (CRIMSON, "listening"),
    "processing": (GOLD,    "thinking"),
    "responding": (BLUE,    "speaking"),
    "error":      (RED,     "something broke"),
    "init":       (GREY,    "waking"),
    "cold":       (GREY,    "asleep"),
    "shutdown":   (GREY,    "shutting down"),
}

PILL_W, PILL_H = 300.0, 56.0
BARS = 28


def _colour(rgb, alpha=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, alpha)


# ─── The pill ────────────────────────────────────────────────────────────────

class PillView(NSView):
    """
    What Diablo is doing, in 300 by 56 points.

    Redrawn only when something changes, plus a slow tick while the waveform is
    live. A view that redraws at 60fps for a dot that is not moving is how a
    menu bar app ends up in the battery report.
    """

    def initWithFrame_(self, frame):
        self = objc.super(PillView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.state = "idle"
        self.headline = "ready"
        self.subtitle = ""
        self.levels: List[float] = [0.04] * BARS
        self.accent = GREY
        self.on_click = None
        return self

    def isOpaque(self):
        return False

    def mouseDown_(self, event):
        if self.on_click is not None:
            self.on_click()

    def update_(self, payload):
        state = payload.get("state", self.state)
        self.state = state
        self.accent, default_line = STATE_LOOK.get(state, (GREY, state))
        self.headline = payload.get("headline") or default_line
        self.subtitle = payload.get("subtitle", "")
        self.setNeedsDisplay_(True)

    def pushLevel_(self, level):
        self.levels = self.levels[1:] + [max(0.04, min(1.0, float(level)))]
        if self.state in ("listening", "responding"):
            self.setNeedsDisplay_(True)

    _draws = 0

    def drawRect_(self, rect):
        PillView._draws += 1
        bounds = self.bounds()
        w, h = bounds.size.width, bounds.size.height

        # Body. Near-black at 92% with a hairline, which is what makes it read
        # as a floating object rather than a hole in the screen.
        body = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(1, 1, w - 2, h - 2), 16, 16)
        _colour((0.043, 0.043, 0.051),
                0.72 if self.state in ("idle", "cold") else 0.94).set()
        body.fill()
        _colour((1, 1, 1), 0.09).set()
        body.setLineWidth_(1.0)
        body.stroke()

        # Badge: the character, reduced to what survives at fifteen points —
        # a dark hair arc, two lit eyes, a grin. The suit, tie and gloved hand
        # are in the full mascot and none of them read at this size.
        #
        # Built from explicit paths rather than clipping. The first version
        # clipped a rectangle to a circle for the hair and the clip leaked,
        # leaving a flat bar with a streak sticking out of the top of the head.
        cx, cy, r = 26.0, h / 2, 15.0
        _colour(self.accent, 0.18).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - r, cy - r, r * 2, r * 2)).fill()

        # Face
        _colour((0.937, 0.878, 0.824), 0.96).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - 9, cy - 9, 18, 18)).fill()

        # Hair: a half-disc across the top, as one closed arc.
        hair = NSBezierPath.bezierPath()
        hair.moveToPoint_(NSPoint(cx - 9, cy + 1))
        # Clockwise, explicitly. The default is counter-clockwise, which took
        # the arc from 180 degrees round through the bottom — the hair drew
        # across the chin instead of over the head.
        hair.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            NSPoint(cx, cy + 1), 9.0, 180.0, 0.0, True)
        hair.closePath()
        # Lighter than the pill body on purpose. At near-black the hair was the
        # same tone as the background behind it, so the head lost its top half
        # and read as a bowl with a face in it.
        _colour((0.18, 0.18, 0.22), 1.0).set()
        hair.fill()

        # The streak: a short stroke inside the hair, which is what tells the
        # four agents apart at a glance.
        streak = NSBezierPath.bezierPath()
        streak.moveToPoint_(NSPoint(cx - 2.6, cy + 1.4))
        streak.lineToPoint_(NSPoint(cx - 1.8, cy + 8.4))
        streak.setLineWidth_(2.0)
        _colour(self.accent, 1.0).set()
        streak.stroke()

        # Eyes
        _colour((0.082, 0.082, 0.102), 1.0).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - 5.4, cy - 3.6, 4.0, 3.6)).fill()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx + 1.4, cy - 3.6, 4.0, 3.6)).fill()
        iris = self.accent if self.state not in ("idle", "cold") else (0.62, 0.62, 0.68)
        _colour(iris, 1.0).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - 4.4, cy - 3.1, 1.8, 2.6)).fill()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx + 2.4, cy - 3.1, 1.8, 2.6)).fill()

        # Grin: a thin arc, not a filled blob.
        grin = NSBezierPath.bezierPath()
        grin.moveToPoint_(NSPoint(cx - 4.0, cy - 5.0))
        grin.curveToPoint_controlPoint1_controlPoint2_(
            NSPoint(cx + 4.0, cy - 5.0),
            NSPoint(cx - 1.6, cy - 8.2), NSPoint(cx + 1.6, cy - 8.2))
        grin.setLineWidth_(1.6)
        _colour((1, 1, 1), 0.92).set()
        grin.stroke()

        # Text block. Two lines when there is a subtitle, one centred line when
        # there is not, so a short status does not float above empty space.
        head_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(13.0, 0.3),
            NSForegroundColorAttributeName: _colour((0.95, 0.95, 0.96), 1.0),
        }
        sub_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(11.0, 0.0),
            NSForegroundColorAttributeName: _colour((1, 1, 1), 0.55),
        }
        text_x = 50.0
        if self.subtitle:
            NSString.stringWithString_(self.subtitle).drawAtPoint_withAttributes_(
                NSPoint(text_x, cy - 15), sub_attrs)
            NSString.stringWithString_(self.headline).drawAtPoint_withAttributes_(
                NSPoint(text_x, cy - 1), head_attrs)
        else:
            NSString.stringWithString_(self.headline).drawAtPoint_withAttributes_(
                NSPoint(text_x, cy - 8), head_attrs)

        # Waveform, right-aligned. Only while there is sound to show — a static
        # waveform is a lie about what the microphone is doing.
        if self.state in ("listening", "responding"):
            bw, gap = 2.5, 2.0
            total = BARS * bw + (BARS - 1) * gap
            x = w - 18 - total
            _colour(self.accent, 0.9).set()
            for level in self.levels:
                bar_h = max(bw, level * 26.0)
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x, cy - bar_h / 2, bw, bar_h), bw / 2, bw / 2).fill()
                x += bw + gap


class Pill:
    """The panel the pill lives in. Never takes focus, never eats a click."""

    def __init__(self):
        frame = NSMakeRect(0, 0, PILL_W, PILL_H)
        # alloc() and init...() must be chained. Calling init on the allocated
        # object and throwing the return value away leaves you holding an
        # uninitialised panel that accepts every setter and draws nothing —
        # silently, with no error anywhere, which cost two rounds of debugging.
        # NonactivatingPanel is not optional. Without it, a borderless NSPanel
        # belonging to an accessory app that has never been activated is
        # created, reports isVisible() as true, runs drawRect_, and is never
        # composited — it does not even appear in CGWindowList. Every API call
        # succeeds and nothing is on screen.
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        self.panel.setFloatingPanel_(True)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setLevel_(25)                       # above windows, below alerts
        # Clickable, unlike Clicky's, which is purely informational. On a full
        # menu bar macOS hides new status items with no error and no way to
        # override it, so the pill has to be reachable — otherwise the app has
        # no entry point at all on a machine like this one.
        self.panel.setIgnoresMouseEvents_(False)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.view = PillView.alloc().initWithFrame_(frame)
        self.panel.setContentView_(self.view)
        self.visible = False
        self.on_click = None

    def position(self):
        """
        Bottom-right of whichever screen the cursor is on.

        Clicky puts its pill at the cursor. Diablo's is up for whole tasks
        rather than the moment you hold a key, so it sits in a corner instead —
        a capsule following the mouse around for ninety seconds is maddening.
        """
        mouse = NSEvent.mouseLocation()
        screen = next((s for s in NSScreen.screens()
                       if _contains(s.frame(), mouse)), NSScreen.mainScreen())
        visible = screen.visibleFrame()
        x = visible.origin.x + visible.size.width - PILL_W - 24
        y = visible.origin.y + 24
        self.panel.setFrameOrigin_(NSPoint(x, y))

    def show(self, **payload):
        self.view.on_click = self.on_click
        self.view.update_(payload)
        if not self.visible:
            self.position()
            self.panel.orderFrontRegardless()
            self.visible = True

    def hide(self):
        if self.visible:
            _log("pill.hide()")
            self.panel.orderOut_(None)
            self.visible = False


def _contains(frame, point):
    return (frame.origin.x <= point.x <= frame.origin.x + frame.size.width
            and frame.origin.y <= point.y <= frame.origin.y + frame.size.height)


# ─── The panel ───────────────────────────────────────────────────────────────

class Panel:
    """
    The full window. Built the first time it is opened, and not before.

    That laziness is the point of the whole split: until you press the menu bar
    icon there is no web view and no 33 MB.
    """

    def __init__(self, url: str):
        self.url = url
        self.window = None
        self.webview = None

    def _build(self):
        frame = NSMakeRect(0, 0, 880, 620)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskFullSizeContentView)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False)
        self.window.setTitle_("Diablo")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(NSWindowTitleHidden)
        self.window.setBackgroundColor_(_colour((0.043, 0.043, 0.051), 1.0))
        self.window.setMinSize_(NSMakeSize(560, 420))
        self.window.center()

        config = WKWebViewConfiguration.alloc().init()
        self.webview = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        self.webview.setValue_forKey_(False, "drawsBackground")
        self.window.setContentView_(self.webview)
        self.webview.loadRequest_(
            NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url)))

    def toggle(self):
        if self.window is None:
            self._build()
        if self.window.isVisible():
            self.window.orderOut_(None)
        else:
            self.window.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def close(self):
        if self.window is not None:
            self.window.orderOut_(None)



def _status_icon():
    """
    The menu bar glyph: a small ring with a notch, drawn once.

    Template mode is what makes it follow the menu bar's tint instead of
    staying a fixed colour when the wallpaper is light.
    """
    size = 18.0
    image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    image.lockFocus()
    NSColor.blackColor().set()
    ring = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(2.5, 2.5, 13, 13))
    ring.setLineWidth_(2.0)
    ring.stroke()
    NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(7, 7, 4, 4)).fill()
    image.unlockFocus()
    image.setTemplate_(True)
    return image


# ─── The app ─────────────────────────────────────────────────────────────────

class Shell:
    def __init__(self, url: str, manager=None):
        self.url = url
        self.manager = manager
        self.app = NSApplication.sharedApplication()
        # Accessory policy: lives in the menu bar, no Dock icon, no app switcher
        # entry. An assistant you talk to should not be something you alt-tab to.
        self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        _log("Shell.__init__ start")
        self.pill = Pill()
        self.pill.on_click = lambda: _on_main(self.panel.toggle)
        _log("  pill built")
        self.panel = Panel(url)
        self._build_menu()
        _log("  menu built, status item:", self.item, "button:", self.item.button())
        self._hide_at: Optional[float] = None

        try:
            from skills import server as _server
            _server.attach(panel_toggle=lambda: _on_main(self.panel.toggle))
        except Exception:
            pass
        if manager is not None:
            manager.on("*", self._on_event)
        _log("Shell.__init__ done")

    def _build_menu(self):
        bar = NSStatusBar.systemStatusBar()
        self.item = bar.statusItemWithLength_(-1.0)          # variable length
        self.item.setVisible_(True)
        button = self.item.button()
        # A drawn template image rather than a text glyph. Text status items
        # are wider, and a template image picks up the menu bar's own tint in
        # light and dark automatically.
        button.setImage_(_status_icon())
        button.setToolTip_("Diablo")
        _log("status item visible =", self.item.isVisible(),
             "length =", self.item.length())

        menu = NSMenu.alloc().init()
        self.status_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Ready", None, "")
        self.status_item.setEnabled_(False)
        menu.addItem_(self.status_item)
        menu.addItem_(NSMenuItem.separatorItem())

        self._add(menu, "Open Diablo", "togglePanel:", "d")
        self._add(menu, "Stop speaking", "interrupt:", ".")
        self._add(menu, "Forget this conversation", "resetChat:", "")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Quit Diablo", "quitApp:", "q")
        self.item.setMenu_(menu)

    def _add(self, menu, title, selector, key):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, objc.selector(getattr(self, selector.rstrip(":")),
                                 signature=b"v@:@"), key)
        item.setTarget_(self)
        menu.addItem_(item)

    # ── menu actions ─────────────────────────────────────────────────────
    def togglePanel(self, sender):
        self.panel.toggle()

    def interrupt(self, sender):
        _post("/api/control", {"action": "interrupt"})

    def resetChat(self, sender):
        _post("/api/control", {"action": "reset"})

    def quitApp(self, sender):
        os._exit(0)

    # ── state ────────────────────────────────────────────────────────────
    def _on_event(self, payload: Dict[str, Any]) -> None:
        """
        Manager events arrive on whatever thread produced them. AppKit is not
        thread-safe, so the actual drawing is bounced to the main thread.
        """
        try:
            event = payload.get("event")
            _log("event", event, payload.get("new_state", ""))
            if event == "state_changed":
                self._apply_state(payload.get("new_state", "idle"))
            elif event == "wake_detected":
                self._apply_state("listening")
            elif event == "tool":
                self._set_pill(headline=f"Running {payload.get('tool', '')}",
                               subtitle="Working", state="processing")
        except Exception:
            traceback.print_exc()

    def _apply_state(self, state: str) -> None:
        _, words = STATE_LOOK.get(state, (GREY, state))
        if state in ("idle", "cold"):
            # It stays up, dimmed. Going away entirely would leave nothing to
            # click, and something that is always in the same corner is easier
            # to find than something that appears only while it is busy.
            self._hide_at = None
            self._set_pill(headline="Hey Diablo", subtitle="ready", state="idle")
        else:
            self._hide_at = None
            self._set_pill(headline=words, state=state)
        self.status_item.setTitle_(words.capitalize())

    def _set_pill(self, **payload):
        _log("_set_pill", payload)

        def apply():
            _log("  on main thread, showing")
            self.pill.show(**payload)
            _log("  visible =", self.pill.visible,
                 "isVisible =", self.pill.panel.isVisible(),
                 "frame =", self.pill.panel.frame(),
                 "screen =", self.pill.panel.screen(),
                 "alpha =", self.pill.panel.alphaValue())
        _on_main(apply)

    _ticks = 0

    def _tick(self, timer):
        """Once a second: retire a finished pill, and feed the waveform."""
        Shell._ticks += 1
        if Shell._ticks % 50 == 1:
            _log("tick", Shell._ticks, "pill.visible =", self.pill.visible)
        if self._hide_at and time.time() > self._hide_at:
            self._hide_at = None
            _on_main(self.pill.hide)
        if self.manager is not None and self.pill.visible:
            try:
                level = self.manager.mic.audio_power_level
                _on_main(lambda: self.pill.view.pushLevel_(level * 3.2))
            except Exception:
                pass

    def run(self):
        _log("run(): starting the tick thread and entering the AppKit loop")
        # Show it immediately rather than waiting for the first state change,
        # so there is something to click from the moment the app opens.
        _on_main(lambda: self.pill.show(
            headline="Hey Diablo", subtitle="ready", state="idle"))
        # A plain background thread rather than NSTimer. NSTimer wants an
        # Objective-C target and Shell is an ordinary Python object, so the
        # timer was accepted and never fired once.
        threading.Thread(target=self._tick_forever, daemon=True).start()
        self.app.run()

    def _tick_forever(self):
        while True:
            time.sleep(0.1)
            try:
                self._tick(None)
            except Exception:
                pass


def _on_main(fn: Callable[[], None]) -> None:
    """
    Run a block on the main thread. AppKit will not tolerate anything else.

    The first version of this defined an NSObject subclass inside the function
    and called performSelectorOnMainThread on it. That registers a new
    Objective-C class named _Runner on every single call, which works exactly
    once and then silently stops — the pill never appeared and nothing was
    logged. NSOperationQueue takes a block directly and has none of that
    problem.
    """
    def guarded():
        try:
            fn()
        except Exception:
            traceback.print_exc()

    try:
        NSOperationQueue.mainQueue().addOperationWithBlock_(guarded)
    except Exception:
        traceback.print_exc()


def _post(path: str, body: Dict[str, Any]) -> None:
    url = os.getenv("DIABLO_UI_URL", "http://127.0.0.1:17494") + path
    try:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(request, timeout=3).read()
    except Exception:
        pass


def run(listen: Optional[Callable[[], None]] = None, manager=None,
        url: Optional[str] = None) -> None:
    """
    Start the app. Blocks forever on the main thread.

    `listen` is the voice loop. It moves to a background thread because AppKit
    owns the main one, which is the reverse of how the bridge ran before.
    """
    url = url or os.getenv("DIABLO_UI_URL", "http://127.0.0.1:17494")
    if listen is not None:
        threading.Thread(target=listen, name="diablo-voice", daemon=True).start()
    Shell(url, manager=manager).run()


if __name__ == "__main__":
    from companion_manager import create_manager
    from skills import server

    manager = create_manager()
    manager.start()
    served = server.start()
    server.attach(manager=manager)
    print(f"  app shell on {served}")
    run(manager=manager, url=served or None)
