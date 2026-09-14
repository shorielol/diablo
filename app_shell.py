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
import math
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
    NSGradient,
)
from Foundation import (  # noqa: E402
    NSAffineTransform, NSOperationQueue, NSURL, NSURLRequest,
)
from WebKit import WKWebView, WKWebViewConfiguration  # noqa: E402

# The character is defined once, in the icon builder, on the mascot's own
# 120-unit grid. The orb draws the same paths rather than a second copy of
# them, so his face cannot differ between the Dock and the screen.
from skills import make_icon  # noqa: E402

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

# ─── The orb ─────────────────────────────────────────────────────────────────
#
# It used to be a 300 by 56 capsule with his face on the left, the state in
# words and a live waveform on the right. It was legible and it was too much:
# a strip that wide sits across whatever is underneath it all day, and none of
# the three things it said were things you needed to read.
#
# So: the character, in a circle, and nothing else. State is carried by the
# ring around him and by his own face — the mouth moves while he talks, which
# is the one animation that says "this is speaking to you" without a caption.

ORB = 62.0          # the character's circle
HALO = 15.0         # room around it for the ring to breathe into
CANVAS = ORB + HALO * 2


def _colour(rgb, alpha=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, alpha)


class OrbView(NSView):
    """
    Diablo, as a face in a circle.

    Redraws on a clock only while something is actually moving. A view that
    runs at 60fps for a dot that is not moving is how a menu bar app ends up
    in the battery report, and idle is by far the most common state.
    """

    def initWithFrame_(self, frame):
        self = objc.super(OrbView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.state = "idle"
        self.accent = GREY
        self.level = 0.0          # smoothed microphone or speech amplitude
        self.phase = 0.0          # seconds since this state began
        self.on_click = None
        self.tooltip = "ready"
        return self

    def isOpaque(self):
        return False

    def mouseDown_(self, event):
        if self.on_click is not None:
            self.on_click()

    def update_(self, payload):
        state = payload.get("state", self.state)
        if state != self.state:
            self.phase = 0.0
            self.level = 0.0
        self.state = state
        self.accent, words = STATE_LOOK.get(state, (GREY, state))
        self.tooltip = payload.get("headline") or words
        self.setToolTip_(self.tooltip)
        self.setNeedsDisplay_(True)

    def animating(self) -> bool:
        return self.state in ("listening", "processing", "responding")

    def advance_(self, dt):
        """One frame. Returns nothing; the shell decides how often to call it."""
        if not self.animating():
            return
        self.phase += float(dt)
        self.setNeedsDisplay_(True)

    def pushLevel_(self, level):
        # Smoothed, because raw microphone power jitters hard enough to make
        # the ring look like it is being shaken rather than spoken to.
        target = max(0.0, min(1.0, float(level)))
        self.level += (target - self.level) * 0.35
        if self.animating():
            self.setNeedsDisplay_(True)

    # ── the face ─────────────────────────────────────────────────────────

    def _mouth_openness(self) -> float:
        """
        How far open his mouth is, this frame.

        Only while speaking. Two sine waves at unrelated frequencies rather
        than one: a single sine is a metronome, and a mouth opening on a
        metronome reads as a machine rather than as speech. The slower wave
        supplies the pauses between phrases.
        """
        if self.state != "responding":
            return 0.0
        fast = math.sin(self.phase * 11.0)
        slow = math.sin(self.phase * 2.7 + 1.1)
        shaped = (0.55 + 0.45 * fast) * (0.45 + 0.55 * (0.5 + 0.5 * slow))
        # Real amplitude when the voice can report it; the wave alone otherwise.
        return max(0.0, min(1.0, shaped * (0.55 + 0.9 * self.level)))

    _draws = 0

    def drawRect_(self, rect):
        OrbView._draws += 1
        cx = cy = CANVAS / 2.0
        r = ORB / 2.0
        context = NSGraphicsContext.currentContext()

        self._draw_ring(cx, cy, r)

        # The disc he sits in.
        disc = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - r, cy - r, r * 2, r * 2))
        context.saveGraphicsState()
        disc.addClip()
        # Pitched at his hair rather than darker than it. A disc noticeably
        # darker than the character leaves the edge of his head visible as a
        # seam inside the circle, and once you have seen it you cannot stop
        # seeing it. Matched, he reads as filling the circle.
        NSGradient.alloc().initWithStartingColor_endingColor_(
            _colour((0.067, 0.067, 0.082), 0.97),
            _colour((0.098, 0.098, 0.118), 0.97)).drawInRect_angle_(
                NSMakeRect(cx - r, cy - r, r * 2, r * 2), 90.0)

        # The character, drawn from make_icon so that the orb, the app icon and
        # the window are one drawing rather than three that drift apart.
        # Framed with room around him. The first version scaled the head to
        # 1.58 of the circle and cropped the top of his eyes against the rim,
        # which made every state look faintly alarmed.
        scale = ORB * 1.38 / 120.0
        transform = NSAffineTransform.transform()
        transform.translateXBy_yBy_(cx - 60 * scale, cy + 49 * scale)
        transform.scaleXBy_yBy_(scale, -scale)
        context.saveGraphicsState()
        transform.concat()
        # His streak stays crimson in every state. Recolouring it to match the
        # ring was the obvious thing to try and it is wrong: the streak is how
        # you recognise him, and a blue one is somebody else. State belongs on
        # the ring, which is not part of him.
        make_icon.draw_head(detail=True, mouth=self._mouth_openness())
        context.restoreGraphicsState()
        context.restoreGraphicsState()

        # A hairline, so he keeps an edge against a light background.
        _colour((1, 1, 1), 0.14).set()
        disc.setLineWidth_(1.0)
        disc.stroke()

    def _draw_ring(self, cx, cy, r):
        """State, as everything outside the circle."""
        if self.state in ("idle", "cold"):
            return

        if self.state == "listening":
            # Two rings pushing outward, on the voice. Crimson.
            for index in range(2):
                offset = (self.phase * 0.9 + index * 0.5) % 1.0
                spread = 3.0 + offset * (HALO - 2.0) * (0.5 + self.level)
                fade = (1.0 - offset) * 0.55 * (0.35 + 0.65 * self.level)
                if fade <= 0.02:
                    continue
                _colour(self.accent, fade).set()
                ring = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(cx - r - spread, cy - r - spread,
                               (r + spread) * 2, (r + spread) * 2))
                ring.setLineWidth_(2.0)
                ring.stroke()
            return

        if self.state == "responding":
            # A steady halo that breathes with the mouth, rather than ripples.
            # Ripples while he is talking read as "still listening", which is
            # the one thing it must not say.
            glow = 4.0 + 5.0 * self._mouth_openness()
            _colour(self.accent, 0.30).set()
            ring = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - r - glow / 2, cy - r - glow / 2,
                           (r + glow / 2) * 2, (r + glow / 2) * 2))
            ring.setLineWidth_(glow)
            ring.stroke()
            return

        if self.state == "processing":
            # One arc going round. The oldest idiom there is for "working",
            # and it cannot be mistaken for sound.
            _colour(self.accent, 0.16).set()
            track = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - r - 5, cy - r - 5, (r + 5) * 2, (r + 5) * 2))
            track.setLineWidth_(2.4)
            track.stroke()
            sweep = NSBezierPath.bezierPath()
            head = (-self.phase * 200.0) % 360.0
            sweep.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                NSPoint(cx, cy), r + 5, head, head - 78, True)
            sweep.setLineWidth_(2.4)
            sweep.setLineCapStyle_(1)          # round
            _colour(self.accent, 0.95).set()
            sweep.stroke()
            return

        # error, waking, shutting down: a plain ring, no movement.
        _colour(self.accent, 0.65).set()
        ring = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - r - 4, cy - r - 4, (r + 4) * 2, (r + 4) * 2))
        ring.setLineWidth_(2.0)
        ring.stroke()


class Pill:
    """
    The panel the orb lives in. Never takes focus, never eats a click.

    Still called Pill because everything that talks to it calls it that, and
    renaming the class would be the whole diff.
    """

    def __init__(self):
        frame = NSMakeRect(0, 0, CANVAS, CANVAS)
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
        # override it, so the orb has to be reachable — otherwise the app has
        # no entry point at all on a machine like this one.
        self.panel.setIgnoresMouseEvents_(False)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.view = OrbView.alloc().initWithFrame_(frame)
        self.panel.setContentView_(self.view)
        self.visible = False
        self.on_click = None

    def position(self):
        """
        Bottom-right of whichever screen the cursor is on.

        Clicky puts its character at the cursor. Diablo's is up for whole tasks
        rather than the moment you hold a key, so it sits in a corner instead —
        something following the mouse around for ninety seconds is maddening.
        """
        mouse = NSEvent.mouseLocation()
        screen = next((s for s in NSScreen.screens()
                       if _contains(s.frame(), mouse)), NSScreen.mainScreen())
        visible = screen.visibleFrame()
        x = visible.origin.x + visible.size.width - CANVAS - 18
        y = visible.origin.y + 18
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
            _log("orb.hide()")
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

    def _tick(self, dt):
        """
        One animation frame: advance the orb, and feed it something to move to.

        The microphone level is only meaningful while he is listening. While he
        is speaking there is no amplitude to read — the voice engine plays a
        file — so the mouth runs on its own waves and this just keeps them at a
        steady strength. Pretending the microphone is his voice would make him
        mouth along to the room instead of to himself.
        """
        Shell._ticks += 1
        if self._hide_at and time.time() > self._hide_at:
            self._hide_at = None
            _on_main(self.pill.hide)
        if not self.pill.visible:
            return

        state = self.pill.view.state
        level = None
        if state == "listening" and self.manager is not None:
            try:
                level = min(1.0, self.manager.mic.audio_power_level * 3.2)
            except Exception:
                level = None
        elif state == "responding":
            level = 0.62

        def frame():
            if level is not None:
                self.pill.view.pushLevel_(level)
            self.pill.view.advance_(dt)
        _on_main(frame)

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
        """
        The clock behind the orb.

        Twenty-four frames a second while something is moving, four when it is
        not. Idle is by far the most common state and it has nothing to draw,
        so running the same loop at animation speed all day would spend real
        battery on a picture that does not change.
        """
        last = time.time()
        while True:
            try:
                animating = self.pill.visible and self.pill.view.animating()
            except Exception:
                animating = False
            time.sleep(1.0 / 24.0 if animating else 0.25)
            now = time.time()
            dt, last = now - last, now
            try:
                self._tick(dt)
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
