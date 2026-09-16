"""
D.I.A.B.L.O. — a screen capture that knows what it is.

The problem
-----------
`screen_context.capture_window` returns a path to a PNG. A path is not enough
to reason about safely, because four things that matter are missing from it:

    when        a coordinate derived from a ten-second-old capture points at
                whatever has since moved into that spot
    where       which display, at what scale, with what origin
    whether     a capture of a window on another Space is a valid PNG of
                solid black, and it returns successfully
    what        the app, the title, the URL — structure the model can use

So every capture in this module comes back as a `ScreenFrame` carrying its own
provenance, and the checks that used to be a caller's responsibility (and were
therefore skipped) happen once, here.

On black frames
---------------
`screencapture -l` on a window that is minimised, on another Space, or occluded
by a fullscreen app returns exit status 0 and writes a well-formed PNG that is
entirely one colour. Nothing downstream could tell that from a real screen: OCR
finds no text, and "there is no text in this window" is a plausible-sounding
answer to give about a window that is in fact full of text. That is the worst
class of failure Diablo has — confident, wrong, and indistinguishable from
success. Detecting it costs 19ms against a 272ms capture — under seven per cent
of a capture that would otherwise be worthless.
"""

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config  # noqa: E402
from skills import display as _display  # noqa: E402
from skills import screen_context as _sc  # noqa: E402

# How old a capture may be before a coordinate taken from it is untrustworthy.
#
# Not a guess: a walkthrough step is spoken in two to three seconds, and the
# user then has to move a hand to the mouse. Anything under about four seconds
# is almost certainly still the same screen; beyond that, ask again. Clicking
# is held to a much tighter bound — see computer_use.
STALE_AFTER = config.env_float("DIABLO_SCREEN_STALE_AFTER", 4.0, low=0.5, high=60.0)

# A capture whose sampled pixels span less than this is one flat colour.
BLANK_SPREAD = config.env_float("DIABLO_SCREEN_BLANK_SPREAD", 6.0, low=0.0, high=64.0)


# ─── Is this image actually a screen? ────────────────────────────────────────

def _samples(path: str, across: int = 24, down: int = 16) -> List[Tuple[int, int, int]]:
    """A grid of pixels from the capture. Cheap enough to run on every frame."""
    try:
        from AppKit import NSBitmapImageRep
        from Foundation import NSData
    except ImportError:
        return []
    try:
        rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(path))
        if rep is None:
            return []
        width, height = int(rep.pixelsWide()), int(rep.pixelsHigh())
        if width < 2 or height < 2:
            return []
        out = []
        for i in range(across):
            x = int((i + 0.5) * width / across)
            for j in range(down):
                y = int((j + 0.5) * height / down)
                colour = rep.colorAtX_y_(x, y)
                if colour is None:
                    continue
                try:
                    out.append((int(colour.redComponent() * 255),
                                int(colour.greenComponent() * 255),
                                int(colour.blueComponent() * 255)))
                except Exception:
                    continue
        return out
    except Exception:
        return []


def validity(path: str) -> Tuple[bool, str]:
    """
    Whether a capture is worth reading, and if not, why not in plain words.

    Returns (ok, reason). The reason is written to be said out loud, because it
    reaches the user through the ordinary answer path rather than a log.
    """
    if not path or not os.path.exists(path):
        return False, "the capture did not produce an image"
    if os.path.getsize(path) < 1024:
        return False, "the capture came back empty"

    pixels = _samples(path)
    if not pixels:
        # Could not inspect it. Not proof of a problem, so it passes: refusing
        # on an unreadable sample would break every machine without AppKit.
        return True, ""

    flat = [sum(p) / 3.0 for p in pixels]
    spread = max(flat) - min(flat)
    if spread <= BLANK_SPREAD:
        mean = sum(flat) / len(flat)
        if mean < 24:
            return False, ("that window captured as solid black, which usually "
                           "means it is minimised or on another desktop")
        if mean > 231:
            return False, "that window captured as a blank white rectangle"
        return False, "that window captured as one flat colour with nothing in it"
    return True, ""


def _fingerprint(path: str) -> List[int]:
    """
    A coarse signature of what the screen looked like.

    Sixteen buckets of average brightness. Enough to answer "did anything move
    at all" without keeping the image, which is the point: the frame is
    released and this survives.
    """
    pixels = _samples(path, across=16, down=16)
    if not pixels:
        return []
    per = max(1, len(pixels) // 16)
    return [int(sum(sum(p) / 3.0 for p in pixels[i:i + per]) / max(1, len(pixels[i:i + per])))
            for i in range(0, len(pixels), per)][:16]


# ─── The frame ───────────────────────────────────────────────────────────────

@dataclass
class ScreenFrame:
    """One capture, and everything known about it."""

    path: Optional[str]
    captured_at: float
    source: str                      # window | display | region | desktop
    ok: bool = True
    problem: str = ""

    app: str = ""
    title: str = ""
    url: str = ""
    window_id: int = 0

    display_id: int = 0
    display_index: int = 0
    display_scale: float = 1.0
    display_name: str = ""

    # Quartz, top-left origin: the rectangle this frame shows.
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    pixel_width: int = 0
    pixel_height: int = 0
    fingerprint: List[int] = field(default_factory=list)

    def age(self) -> float:
        return time.time() - self.captured_at

    def is_stale(self, max_age: float = None) -> bool:
        return self.age() > (STALE_AFTER if max_age is None else max_age)

    def to_quartz(self, u: float, v: float) -> Tuple[float, float]:
        """A normalised point inside this frame to a point on the desktop."""
        return (self.x + u * self.width, self.y + v * self.height)

    def release(self) -> None:
        """Drop the image. The structure survives; the pixels do not."""
        if self.path and os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass
        self.path = None

    def context(self) -> Dict[str, Any]:
        """The structured form, for a model or a log. Never the image."""
        return {
            "app": self.app,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "display": self.display_name,
            "display_index": self.display_index,
            "scale": self.display_scale,
            "bounds": f"{int(self.width)}x{int(self.height)} at "
                      f"({int(self.x)},{int(self.y)})",
            "captured": f"{self.age():.1f}s ago",
            "ok": self.ok,
            "problem": self.problem,
        }


def _failed(source: str, problem: str) -> ScreenFrame:
    return ScreenFrame(path=None, captured_at=time.time(), source=source,
                       ok=False, problem=problem)


def _measure(frame: ScreenFrame) -> ScreenFrame:
    """Fill in pixel dimensions, validity and fingerprint for a written frame."""
    ok, reason = validity(frame.path)
    frame.ok, frame.problem = ok, reason
    if not ok:
        return frame
    try:
        from AppKit import NSBitmapImageRep
        from Foundation import NSData
        rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(frame.path))
        if rep is not None:
            frame.pixel_width, frame.pixel_height = int(rep.pixelsWide()), int(rep.pixelsHigh())
    except Exception:
        pass
    frame.fingerprint = _fingerprint(frame.path)
    return frame


# ─── Taking one ──────────────────────────────────────────────────────────────

def capture_window(target: str = "") -> ScreenFrame:
    """
    The window the user means, with its provenance.

    Refusals and failures come back as a frame with `ok=False` and a spoken
    reason rather than as None or an "Error:" string, so the caller has one
    shape to handle and cannot accidentally treat a refusal as an empty screen.
    """
    window = _sc.find_window(target) if target else _sc.frontmost_window()
    if not window:
        if not _sc.list_windows():
            return _failed("window", "I cannot see any windows. Diablo needs Screen "
                                     "Recording permission in System Settings")
        return _failed("window", f"I could not find a window matching '{target}'")
    return capture_this_window(window)


def capture_this_window(window: Dict[str, Any]) -> ScreenFrame:
    """Capture a window already resolved, so a caller does not resolve twice."""
    if window.get("sensitive"):
        return _failed("window", f"{window['app']} looks like a password manager "
                                 f"or banking window, so I will not capture it")

    screen = _display.for_window(window)
    at = time.time()
    path = _sc.capture_window(window["id"])
    if not path:
        return _failed("window", f"I could not capture the {window['app']} window")

    frame = ScreenFrame(
        path=path, captured_at=at, source="window",
        app=window.get("app", ""), title=window.get("title", ""),
        window_id=int(window.get("id", 0)),
        x=float(window.get("x", 0)), y=float(window.get("y", 0)),
        width=float(window.get("width", 0)), height=float(window.get("height", 0)),
        display_id=screen.id if screen else 0,
        display_index=screen.index if screen else 0,
        display_scale=screen.scale if screen else 1.0,
        display_name=screen.describe() if screen else "",
    )
    tab = _sc.browser_tab(frame.app)
    if tab:
        frame.url = tab.get("url", "")
        if tab.get("title"):
            frame.title = tab["title"]
    return _measure(frame)


def capture_display(which: Optional[int] = None) -> ScreenFrame:
    """
    One whole display. `which` is the 1-based index, main first.

    There is deliberately no "capture the screen" that returns a single image
    of everything, because on this machine that silently meant the main display
    only. Callers that want the whole desktop ask for every display.
    """
    screens = _display.displays()
    if not screens:
        return _failed("display", "I cannot see any displays")
    chosen = next((d for d in screens if d.index == which), screens[0]) if which else screens[0]

    at = time.time()
    path = _display.capture_display(chosen)
    if not path:
        return _failed("display", f"I could not capture the {chosen.describe()}")
    return _measure(ScreenFrame(
        path=path, captured_at=at, source="display",
        app="", title=chosen.describe(),
        x=chosen.x, y=chosen.y, width=chosen.width, height=chosen.height,
        display_id=chosen.id, display_index=chosen.index,
        display_scale=chosen.scale, display_name=chosen.describe()))


def capture_region(x: float, y: float, width: float, height: float) -> ScreenFrame:
    """A rectangle of the desktop, in Quartz points."""
    screen = _display.for_point(x + width / 2.0, y + height / 2.0)
    at = time.time()
    path = _display.capture_region(x, y, width, height)
    if not path:
        return _failed("region", "I could not capture that part of the screen")
    return _measure(ScreenFrame(
        path=path, captured_at=at, source="region",
        title="a region of the screen",
        x=x, y=y, width=width, height=height,
        display_id=screen.id if screen else 0,
        display_index=screen.index if screen else 0,
        display_scale=screen.scale if screen else 1.0,
        display_name=screen.describe() if screen else ""))


CURSOR_REGION = config.env_int("DIABLO_CURSOR_REGION", 420, low=80, high=1600)


def capture_around_cursor(size: int = None) -> ScreenFrame:
    """
    A square centred on the pointer, clipped to the display it is on.

    This is what "what's this?" means when someone is hovering over something.
    Sending the whole screen for that question is worse on every axis: slower,
    less private, and the model has to find the thing before it can answer.
    """
    spot = _display.cursor()
    if not spot:
        return _failed("region", "I cannot tell where your pointer is")
    screen = _display.for_point(*spot)
    if not screen:
        return _failed("region", "your pointer is not on any display I can see")

    side = float(size or CURSOR_REGION)
    left = max(screen.x, spot[0] - side / 2.0)
    top = max(screen.y, spot[1] - side / 2.0)
    width = min(side, screen.right - left)
    height = min(side, screen.bottom - top)
    frame = capture_region(left, top, width, height)
    if frame.ok:
        frame.title = "around your pointer"
        window = window_under_cursor()
        if window:
            frame.app = window.get("app", "")
    return frame


def window_under_cursor() -> Optional[Dict[str, Any]]:
    """
    The frontmost window containing the pointer.

    list_windows is ordered front to back, so the first window whose bounds
    contain the point is the one actually visible there — the one behind it
    contains the point too, and is not what is being pointed at.
    """
    spot = _display.cursor()
    if not spot:
        return None
    x, y = spot
    for window in _sc.list_windows():
        if (window["x"] <= x < window["x"] + window["width"]
                and window["y"] <= y < window["y"] + window["height"]):
            return window
    return None


def under_cursor(radius: float = 60.0) -> Optional[Dict[str, Any]]:
    """
    The nearest readable thing to the pointer, with how far away it is.

    Answers "what is this?" and "what's under my cursor?" by measurement rather
    than by asking a model to guess what someone is hovering over.
    """
    window = window_under_cursor()
    if not window or window.get("sensitive"):
        return None
    spot = _display.cursor()
    if not spot:
        return None

    frame = capture_this_window(window)
    try:
        # Inside the try, not before it. A rejected frame is still a file on
        # disk, and returning here used to skip the release below — so the one
        # capture Diablo had decided it could not trust was the one it left in
        # /var/folders. Caught by the privacy check in test_screen.
        if not frame.ok:
            return None
        fragments = _sc._ocr_fragments(frame.path, window)
    finally:
        frame.release()
    if isinstance(fragments, str) or not fragments:
        return None

    best, shortest = None, float("inf")
    for fragment in fragments:
        cx, cy = frame.to_quartz((fragment["left"] + fragment["right"]) / 2.0,
                                 fragment["top"] + fragment["height"] / 2.0)
        distance = ((cx - spot[0]) ** 2 + (cy - spot[1]) ** 2) ** 0.5
        if distance < shortest:
            best, shortest = fragment, distance
            best_point = (cx, cy)
    if best is None or shortest > radius * 4:
        return None
    return {
        "text": best["text"],
        "confidence": best["confidence"],
        "distance": round(shortest, 1),
        "near": shortest <= radius,
        "x": best_point[0], "y": best_point[1],
        "app": window.get("app", ""),
        "title": window.get("title", ""),
    }


# ─── What changed ────────────────────────────────────────────────────────────

def _lines_of(frame: ScreenFrame, window: Optional[dict] = None) -> List[str]:
    if not frame.ok or not frame.path:
        return []
    fragments = _sc._ocr_fragments(frame.path, window)
    if isinstance(fragments, str):
        return []
    return [f["text"].strip() for f in fragments if f.get("text", "").strip()]


@dataclass
class ScreenChange:
    """The difference between two moments, in words rather than pixels."""

    moved: bool = False
    appeared: List[str] = field(default_factory=list)
    vanished: List[str] = field(default_factory=list)
    same: bool = True
    app_changed: bool = False
    before_app: str = ""
    after_app: str = ""
    seconds: float = 0.0

    def describe(self) -> str:
        """One or two sentences, for speaking."""
        if self.app_changed:
            return f"You moved from {self.before_app} to {self.after_app}."
        if self.same and not self.appeared and not self.vanished:
            return "Nothing I can read has changed."
        parts = []
        if self.appeared:
            parts.append("new on screen: " + ", ".join(
                f'"{t[:40]}"' for t in self.appeared[:4]))
        if self.vanished:
            parts.append("gone: " + ", ".join(
                f'"{t[:40]}"' for t in self.vanished[:4]))
        if not parts:
            return "Something moved, but the text on screen is the same."
        return "; ".join(parts).capitalize() + "."


def compare(before: ScreenFrame, after: ScreenFrame,
            window: Optional[dict] = None) -> ScreenChange:
    """
    What is different between two captures.

    Text sets, not pixels. A pixel diff of a screen with a blinking cursor or a
    clock in the corner reports a change every time and is therefore useless
    for "did the dialog open". What someone means by "what changed" is almost
    always which words appeared or went away.

    The fingerprint still gets consulted, because it is the only thing that can
    tell "nothing changed" from "something changed that I cannot read".
    """
    change = ScreenChange(before_app=before.app, after_app=after.app,
                          seconds=max(0.0, after.captured_at - before.captured_at))
    if before.app and after.app and before.app != after.app:
        change.app_changed = True
        change.same = False
        return change

    if before.fingerprint and after.fingerprint and len(before.fingerprint) == len(after.fingerprint):
        drift = sum(abs(a - b) for a, b in zip(before.fingerprint, after.fingerprint))
        change.moved = drift > 8 * len(before.fingerprint) / 4
    else:
        change.moved = True

    was, now = set(_lines_of(before, window)), set(_lines_of(after, window))
    # Short fragments churn — a frame counter, a clock, a character count — so
    # they are excluded from "what appeared" while still counting as movement.
    change.appeared = sorted(t for t in (now - was) if len(t) > 3)
    change.vanished = sorted(t for t in (was - now) if len(t) > 3)
    change.same = not change.moved and not change.appeared and not change.vanished
    return change


if __name__ == "__main__":
    frame = capture_window()
    print("frame:", frame.context())
    frame.release()
    spot = _display.cursor()
    print("cursor:", spot)
    hit = under_cursor()
    print("under cursor:", hit)


# ─── What was on screen a moment ago ─────────────────────────────────────────
# Enough to answer "what changed?" and no more. One previous moment, kept as
# structure with the pixels already dropped, replaced each time Diablo looks.
# Not a screenshot history: there is nowhere for a thousand screenshots to
# accumulate because there is exactly one slot and it holds text, not an image.

@dataclass
class Moment:
    """What a screen looked like, after the picture has been thrown away."""

    at: float
    app: str
    title: str
    url: str
    display_name: str
    lines: List[str] = field(default_factory=list)
    fingerprint: List[int] = field(default_factory=list)

    def age(self) -> float:
        return time.time() - self.at


_LAST: Optional[Moment] = None

# Beyond this, "what changed" is not a question about a moment any more.
MEMORY_SECONDS = config.env_float("DIABLO_SCREEN_MEMORY", 900.0, low=10.0, high=7200.0)


def remember(frame: ScreenFrame, lines: Optional[List[str]] = None) -> None:
    """Keep the shape of this moment. The image is not kept and is not wanted."""
    global _LAST
    if not frame.ok:
        return
    _LAST = Moment(at=frame.captured_at, app=frame.app, title=frame.title,
                   url=frame.url, display_name=frame.display_name,
                   lines=list(lines or []), fingerprint=list(frame.fingerprint))


def last_moment() -> Optional[Moment]:
    if _LAST is None or _LAST.age() > MEMORY_SECONDS:
        return None
    return _LAST


def forget() -> None:
    """Drop the remembered moment. Called on reset and when the user asks."""
    global _LAST
    _LAST = None


def what_changed(target: str = "") -> str:
    """
    Compare the screen now against the last time Diablo looked at it.

    Says plainly when there is nothing to compare against, rather than
    describing the current screen and letting that pass for a comparison —
    which is the failure mode that makes this feature untrustworthy.
    """
    previous = last_moment()
    frame = capture_window(target)
    try:
        # See under_cursor: the rejection has to happen inside the try or the
        # rejected capture never reaches release().
        if not frame.ok:
            return frame.problem.capitalize() + "."
        fragments = _sc._ocr_fragments(frame.path, {"width": frame.width,
                                                    "height": frame.height})
        lines = ([f["text"].strip() for f in fragments if f.get("text", "").strip()]
                 if not isinstance(fragments, str) else [])
        if previous is None:
            remember(frame, lines)
            return ("I had not looked at your screen before this, Sir, so I have "
                    "nothing to compare it against. I have it now — ask me again "
                    "after something changes.")

        if previous.app and frame.app and previous.app != frame.app:
            was, now = previous.app, frame.app
            remember(frame, lines)
            return f"You have moved from {was} to {now} since I last looked."

        was, now = set(previous.lines), set(lines)
        appeared = sorted(t for t in (now - was) if len(t) > 3)
        vanished = sorted(t for t in (was - now) if len(t) > 3)
        ago = previous.age()
        remember(frame, lines)

        if not appeared and not vanished:
            return (f"Nothing I can read has changed in {frame.app or 'that window'} "
                    f"in the last {int(ago)} seconds.")
        parts = []
        if appeared:
            parts.append("new: " + ", ".join(f'"{t[:45]}"' for t in appeared[:4]))
        if vanished:
            parts.append("gone: " + ", ".join(f'"{t[:45]}"' for t in vanished[:4]))
        more = ""
        if len(appeared) > 4 or len(vanished) > 4:
            more = f" ({len(appeared)} added, {len(vanished)} removed in all)"
        return "; ".join(parts).capitalize() + more + "."
    finally:
        frame.release()


# ─── The screen is not the document ──────────────────────────────────────────
# A screenshot of page three of a forty-page PDF contains page three. Asked
# "what is the important part of this?", a model given only that screenshot
# will answer about page three in the confident voice of something that has
# read the whole file — and the answer will be wrong in a way nobody can see.
#
# So the two are kept apart. What is visible is one source; the document behind
# it is another, usually better one; and when the better one exists, Diablo
# says so rather than quietly answering from the worse.

# Applications whose front document is a real file, and that will say where it
# is when asked in AppleScript. Deliberately short: a guess about which file a
# window is showing is worse than admitting the window is all there is.
_DOCUMENT_APPS = {
    "Preview": 'tell application "Preview" to return POSIX path of (get file of front document)',
    "TextEdit": 'tell application "TextEdit" to return POSIX path of (get file of front document)',
    "Pages": 'tell application "Pages" to return POSIX path of (get file of front document)',
    "Numbers": 'tell application "Numbers" to return POSIX path of (get file of front document)',
    "Keynote": 'tell application "Keynote" to return POSIX path of (get file of front document)',
    "Script Editor": 'tell application "Script Editor" to return POSIX path of (get file of front document)',
}


def document_behind(target: str = "") -> Dict[str, Any]:
    """
    What the visible window is a view OF, if anything.

    Returns `kind` of "page", "file" or "none", with a `source` to read it
    properly. "none" is a real answer and the common one: a terminal, a chat
    window and a settings panel are not views of a document, and the screen is
    genuinely all there is.
    """
    window = _sc.find_window(target) if target else _sc.frontmost_window()
    if not window:
        return {"kind": "none", "why": "I cannot see a window."}
    if window.get("sensitive"):
        return {"kind": "none", "why": f"{window['app']} stays private."}

    app = window.get("app", "")
    answer: Dict[str, Any] = {"app": app, "title": window.get("title", "")}

    tab = _sc.browser_tab(app)
    if tab and tab.get("url", "").startswith(("http://", "https://")):
        answer.update(kind="page", source=tab["url"],
                      name=tab.get("title") or tab["url"])
        return answer

    script = _DOCUMENT_APPS.get(app)
    if script:
        try:
            import subprocess
            proc = subprocess.run(["osascript", "-e", script], capture_output=True,
                                  text=True, timeout=8)
            path = proc.stdout.strip()
            if proc.returncode == 0 and path and os.path.exists(path):
                answer.update(kind="file", source=path,
                              name=os.path.basename(path))
                return answer
        except Exception:
            pass

    answer.update(kind="none",
                  why=f"{app} is not showing a file I can open separately")
    return answer


def visible_or_whole(target: str = "") -> str:
    """
    Say whether the whole document is available, and how much of it is on screen.

    This is the sentence that stops a screenshot being mistaken for a file. It
    does not do the reading — read_page and read_file already do that, and
    which one to use is the thing this answers.
    """
    found = document_behind(target)
    if found["kind"] == "page":
        return (f"That is a web page, so I can read the whole of it rather than "
                f"only the part you can see. The page is {found['name'][:70]} at "
                f"{found['source']}.")
    if found["kind"] == "file":
        return (f"That window is showing {found['name']}, which is a file on "
                f"disk at {found['source']}. I can read all of it rather than "
                f"only the page you have open.")
    return (f"There is no separate document behind that window — "
            f"{found.get('why', 'it is not a view of a file')}. "
            f"What is on screen is all I can read.")
