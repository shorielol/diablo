"""
D.I.A.B.L.O. Screen Context
===========================
Lets Diablo see what you are looking at.

The idea is taken from Bitshank-2338/clicky-windows, which captures the screen
and hands it to a vision model, and keeps context separated per window so what
is in Chrome never bleeds into what is in VS Code. That separation is the good
part and it is what this module reproduces.

The implementation is different, because macOS gives you far better than a
screenshot:

  Quartz CGWindowList   every on-screen window, its owner app, title, bounds
                        and window id — structured, free, instant
  screencapture -l      a pixel-perfect capture of ONE window by id, which is
                        what keeps Chrome's context out of Premiere's
  Vision.framework      Apple's on-device OCR. Better than Tesseract, ships with
                        the OS, no binary to install, no network
  AppleScript           for Safari and Chrome, the actual tab URL and title
                        straight from the browser rather than read off pixels

Everything here is local. Nothing is uploaded.

Optional fourth layer: if you install a vision model in Ollama
(`ollama pull qwen2.5vl:7b`), `describe_visually` sends the window image to it
for questions text alone cannot answer — layout, charts, images, "which button
is greyed out".
"""

import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

# The package parent, so `from skills import x` resolves whether this module
# is imported as part of the package or run directly as a script. Importing
# bare `x` here instead created a SECOND copy of each module with its own
# globals, which is why patching local_brain.OLLAMA_HOST in one place had no
# effect in the other. One module, one instance.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config  # noqa: E402

# Resolve the Quartz symbols now, on one thread, rather than lazily on
# whichever thread happens to ask first.
#
# PyObjC loads framework functions on first access and its loader is not
# thread-safe: two threads reaching for CGWindowListCopyWindowInfo at the same
# moment raced and one of them got KeyError from inside the lazy importer.
# Diablo reads windows from the voice loop, the tutor and the overlay watcher,
# so that race is reachable.
try:
    import Quartz as _Quartz
    for _symbol in ("CGWindowListCopyWindowInfo", "CGWindowListCreateImage",
                    "kCGWindowListOptionOnScreenOnly", "kCGNullWindowID",
                    "kCGWindowListOptionIncludingWindow",
                    "kCGWindowImageBoundsIgnoreFraming"):
        getattr(_Quartz, _symbol, None)
except Exception:
    pass
from skills import local_brain  # noqa: E402

VISION_MODEL = os.getenv("DIABLO_VISION_MODEL", "qwen2.5vl:7b")
MAX_OCR_CHARS = 6000

# Windows that are never what you mean by "what I'm looking at".
CHROME_IGNORE = {
    "Window Server", "Control Center", "Notification Centre", "Notification Center",
    "Dock", "SystemUIServer", "Spotlight", "loginwindow", "TextInputMenuAgent",
    "Wallpaper", "Screenshot", "universalaccessd",
}

# Diablo's own windows, which are never what "what am I looking at" means.
#
# Measured, not theorised: with Diablo's own NSWindow frontmost,
# frontmost_window() returned it, so "what's on my screen" read Diablo's own
# interface back to the user. The panel escapes by accident — it sits at window
# layer 25 and the layer filter drops it — but the main window is an ordinary
# titled NSWindow at layer 0 and did not.
#
# Two mechanisms, because one process is not the whole of Diablo: the owner PID
# catches the process doing the asking, and the names catch the UI when it runs
# as a separate process from the agent.
DIABLO_APPS = {"diablo", "diablo.app"}


def _own_pids() -> set:
    """This process and its parent, whose windows are Diablo's own."""
    pids = {os.getpid()}
    try:
        pids.add(os.getppid())
    except Exception:
        pass
    return pids


def is_own_window(entry_owner: str, entry_pid: int, title: str = "") -> bool:
    """Whether a window belongs to Diablo itself."""
    if entry_pid and entry_pid in _own_pids():
        return True
    name = (entry_owner or "").strip().lower()
    if name in DIABLO_APPS:
        return True
    # The development case: run from a venv, the owner is "Python" and only the
    # window title says Diablo. Narrow on purpose — a browser tab called
    # "Improve Diablo GUI" is a real window the user may well be asking about,
    # so the title only counts when the owner is a bare interpreter.
    if name in ("python", "python3", "python3.11", "python3.12", "python3.13"):
        return (title or "").strip().lower() in DIABLO_APPS
    return False


# Never capture these, whatever is asked. A screenshot of a password manager is
# a screenshot of a password. clicky does this too and it is the right call.
SENSITIVE_APPS = {
    "1password", "1password 7", "1password 8", "bitwarden", "keychain access",
    "lastpass", "dashlane", "enpass", "nordpass", "proton pass", "keeper",
    "authy", "banking", "gpg keychain",
}


# ─── Window enumeration ──────────────────────────────────────────────────────

def list_windows(min_width: int = 200, min_height: int = 120) -> List[Dict[str, Any]]:
    """
    Every real, on-screen application window, frontmost first.

    Layer 0 is the ordinary application layer; menu bar extras, the Dock and
    notification banners live above it and are dropped, along with anything too
    small to be something you are reading.
    """
    try:
        import Quartz
    except ImportError:
        return []

    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) or []

    windows = []
    for entry in raw:
        if entry.get("kCGWindowLayer") != 0:
            continue
        owner = entry.get("kCGWindowOwnerName") or ""
        if not owner or owner in CHROME_IGNORE:
            continue
        title = entry.get("kCGWindowName") or ""
        if is_own_window(owner, int(entry.get("kCGWindowOwnerPID") or 0), title):
            continue
        bounds = entry.get("kCGWindowBounds") or {}
        width, height = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
        if width < min_width or height < min_height:
            continue
        windows.append({
            "id": int(entry.get("kCGWindowNumber", 0)),
            "app": owner,
            "title": entry.get("kCGWindowName") or "",
            # The origin, in screen points, top-left. Kept because it is what
            # turns a normalised OCR box into a place on the desktop you can
            # actually point at. Negative x is normal: it means a display to
            # the left of the primary one.
            "x": float(bounds.get("X", 0)),
            "y": float(bounds.get("Y", 0)),
            "width": width,
            "height": height,
            "sensitive": owner.lower() in SENSITIVE_APPS,
        })

    # Which screen each window is on. Attached here rather than looked up by
    # callers because every spatial consumer needs it and none of them should
    # be re-deriving display geometry.
    try:
        from skills import display as _display
        for window in windows:
            screen = _display.for_window(window)
            if screen:
                window["display_id"] = screen.id
                window["display_index"] = screen.index
                window["display_scale"] = screen.scale
                window["display"] = screen.describe()
    except Exception:
        pass
    return windows


def window_by_id(window_id: int) -> Optional[Dict[str, Any]]:
    """
    A window's bounds as they are NOW, by the id you already hold.

    Needed because a window is not a fixed rectangle. A walkthrough resolves
    one window at the start and then talks for a minute, and in that minute the
    user can drag it, resize it, or move it to the other display. Every marker
    after that is drawn from the bounds it had when the lesson began.
    """
    if not window_id:
        return None
    for window in list_windows():
        if window["id"] == int(window_id):
            return window
    return None


def has_moved(window: Dict[str, Any], slack: float = 2.0) -> Optional[Dict[str, Any]]:
    """
    The window's current bounds if it has shifted, None if it is where it was.

    Returns the whole window rather than a boolean so the caller can re-anchor
    from it without a second lookup.
    """
    now = window_by_id(window.get("id", 0))
    if now is None:
        return None
    if (abs(now["x"] - window.get("x", 0)) > slack
            or abs(now["y"] - window.get("y", 0)) > slack
            or abs(now["width"] - window.get("width", 0)) > slack
            or abs(now["height"] - window.get("height", 0)) > slack):
        return now
    return None


def frontmost_window() -> Optional[Dict[str, Any]]:
    """The window you are actually looking at. Quartz returns them front to back."""
    windows = list_windows()
    return windows[0] if windows else None


# Words too common to identify a window on their own. Not a spelling list —
# each one of these was a real substring of a real application name.
_WEAK_WORDS = {
    "not", "and", "the", "for", "with", "you", "your", "was", "are", "has",
    "had", "can", "one", "two", "all", "any", "out", "off", "own", "new",
    "old", "how", "why", "who", "but", "its", "see", "get", "got", "now",
    "open", "close", "show", "find", "look", "read", "that", "this", "there",
    "here", "what", "which", "where", "when", "definitely", "really", "just",
    "some", "thing", "stuff", "screen", "display", "please",
}


def find_window(query: str) -> Optional[Dict[str, Any]]:
    """
    Resolve a spoken phrase to a window.

    "my chrome tab" and "the browser" both have to land on the browser without
    the user knowing it is called Zen. Matching runs app name first, then title,
    then a small table of the names people actually say.
    """
    if not query or query.strip().lower() in ("screen", "my screen", "this", "here"):
        return frontmost_window()

    windows = list_windows()
    if not windows:
        return None

    needle = query.lower().strip()
    for filler in ["my ", "the ", "that ", "this ", " window", " tab", " app"]:
        needle = needle.replace(filler, " ")
    needle = " ".join(needle.split())

    aliases = {
        "browser": ["zen", "chrome", "safari", "firefox", "arc", "edge"],
        "chrome": ["chrome", "zen", "arc"],
        "code": ["code", "visual studio code", "cursor", "xcode"],
        "editor": ["code", "visual studio code", "cursor", "obsidian", "notes"],
        "terminal": ["terminal", "iterm", "iterm2", "warp", "ghostty"],
        "spreadsheet": ["excel", "numbers"],
        "doc": ["word", "pages", "docs"],
        "email": ["mail", "spark", "outlook"],
        "chat": ["messages", "whatsapp", "discord", "slack"],
    }

    def hit(window, term):
        return term in window["app"].lower() or term in window["title"].lower()

    for window in windows:
        if hit(window, needle):
            return window
    for key, candidates in aliases.items():
        if key in needle:
            for candidate in candidates:
                for window in windows:
                    if hit(window, candidate):
                        return window
    # Last resort: one distinctive word, matched at a word boundary.
    #
    # This pass used to take any word of three characters or more and look for
    # it anywhere inside an app name or title. Measured: "a window that is
    # definitely not open" returned Notes, because "not" is a substring of
    # "Notes". A question about a window that is not open came back as a
    # confident reading of an unrelated application — the worst available
    # outcome, and indistinguishable from a correct answer.
    #
    # So common words cannot carry a match on their own, and a match has to
    # begin at a word boundary rather than land in the middle of one.
    for word in needle.split():
        if len(word) < 3 or word in _WEAK_WORDS:
            continue
        pattern = re.compile(r"\b" + re.escape(word), re.IGNORECASE)
        for window in windows:
            if pattern.search(window["app"]) or pattern.search(window["title"]):
                return window
    return None


# ─── Capture ─────────────────────────────────────────────────────────────────

def capture_window(window_id: int) -> Optional[str]:
    """One window, by id, to a temp PNG. `-o` drops the drop shadow."""
    path = os.path.join(tempfile.gettempdir(), f"diablo_win_{window_id}_{int(time.time())}.png")
    proc = subprocess.run(["screencapture", "-l", str(window_id), "-x", "-o", path],
                          capture_output=True, timeout=30)
    if proc.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    return path


def capture_screen() -> Optional[str]:
    """
    The main display, and only the main display. Named so on purpose.

    This used to be called as though it captured "the screen". Measured on a
    two-display machine, `screencapture -x out.png` writes ONE file containing
    the main display, exits 0, and says nothing about the display it dropped.
    Anything built on it lost half the desktop and could not tell.

    For the whole desktop use `display.capture_everything()`, which returns one
    capture per display with each display's own scale factor intact.
    """
    from skills import display as _display
    main = _display.main_display()
    if main is None:
        return None
    return _display.capture_display(main)


# ─── Reading the pixels ──────────────────────────────────────────────────────

def ocr_available() -> bool:
    try:
        import Vision  # noqa: F401
        return True
    except ImportError:
        return False


# How much to enlarge a capture before reading it.
#
# Measured, on a window on a non-Retina display showing "3x + 5 = 20":
#
#     x1   '3x*5'        'QuEStlon 3'   'ShDW all WDrklny.'
#     x2   '3x+5'        'Questthn 3'   'ShDw 4ll WDrklThy.'
#     x3   '3x+5'        'Question 3'   'Slww all workang.'
#
# The plus sign read as a multiplication sign at native size, which is not a
# typo — it is a different equation, and the tutor built on top of this taught
# multiplication for several minutes before anyone noticed. Three times costs
# about 40ms and fixes it.
OCR_UPSCALE = config.env_float("DIABLO_OCR_UPSCALE", 3.0, low=1.0, high=6.0)
# Below this many pixels per point a capture is worth enlarging. A Retina
# capture is already 2x and gains little.
OCR_UPSCALE_BELOW = config.env_float("DIABLO_OCR_UPSCALE_BELOW", 1.5)


def _enlarge(path: str, factor: float, smooth: bool = True) -> Optional[str]:
    """
    A bigger copy of the capture, or None if it could not be made.

    `smooth` decides how the pixels in between are filled in, and it is not a
    detail — it changes what Vision reads. Measured over 20 renderings at 10px
    and 12px, three upscale factors:

                      prose (mean similarity)   equations solved
        smooth              0.84                    2 of 5
        nearest             0.83                    4 of 5

    Smoothing interpolates across the thin strokes that separate a 2 from a 7
    and a 1 from an l, so "5x - 12 = 23" comes back as "5x-l?=". Nearest
    neighbour keeps the strokes hard and loses a little on letterforms, which
    prose survives and arithmetic does not.

    So prose keeps the smoothing, because anchoring a marker depends on reading
    labels, and the maths path asks for nearest separately.
    """
    try:
        from AppKit import (NSBitmapImageRep, NSGraphicsContext, NSImage,
                            NSPNGFileType)
        from Foundation import NSData, NSMakeRect
    except ImportError:
        return None
    try:
        source = NSImage.alloc().initWithContentsOfFile_(path)
        original = NSBitmapImageRep.imageRepWithData_(
            NSData.dataWithContentsOfFile_(path))
        width = int(original.pixelsWide() * factor)
        height = int(original.pixelsHigh() * factor)
        if width <= 0 or height <= 0 or width * height > 80_000_000:
            return None
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, width, height, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)
        rep.setSize_((width, height))
        context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(context)
        # NSImageInterpolationHigh (3) or NSImageInterpolationNone (1).
        context.setImageInterpolation_(3 if smooth else 1)
        source.drawInRect_(NSMakeRect(0, 0, width, height))
        NSGraphicsContext.currentContext().flushGraphics()
        NSGraphicsContext.restoreGraphicsState()
        out = path.replace(".png", "_big.png" if smooth else "_sharp.png")
        rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(out, True)
        return out
    except Exception:
        return None


def _pixels_per_point(path: str, window: Optional[dict]) -> float:
    if not window or not window.get("width"):
        return 1.0
    try:
        from AppKit import NSBitmapImageRep
        from Foundation import NSData
        rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(path))
        return float(rep.pixelsWide()) / float(window["width"])
    except Exception:
        return 1.0


def _ocr_fragments(path: str, window: Optional[dict] = None):
    """
    Raw OCR fragments with their boxes, or a string if OCR is unavailable.

    Vision returns observations in no useful order and with normalised,
    bottom-left-origin boxes. They come back here flipped to a top-left origin
    so everything downstream can think in reading order.

    The boxes are normalised, so enlarging the image first does not move
    anything: a fragment at 30% across is at 30% across whatever the pixel
    dimensions are. That is what makes the upscale free for every caller.
    """
    scaled = None
    if OCR_UPSCALE > 1.0 and _pixels_per_point(path, window) < OCR_UPSCALE_BELOW:
        scaled = _enlarge(path, OCR_UPSCALE)
        if scaled:
            path = scaled
    try:
        return _ocr_fragments_at(path)
    finally:
        if scaled:
            try:
                os.unlink(scaled)
            except OSError:
                pass


def _ocr_fragments_at(path: str):
    """The Vision call itself, on whatever image it is handed."""
    try:
        import Vision
        from Foundation import NSURL
    except ImportError:
        return ("Error: on-device OCR is not installed. Run:\n"
                "  ~/.diablo/venv/bin/pip install pyobjc-framework-Vision")

    url = NSURL.fileURLWithPath_(path)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(1)          # accurate
    request.setUsesLanguageCorrection_(True)

    success, error = handler.performRequests_error_([request], None)
    if not success:
        return f"Error: OCR failed ({error})."

    fragments = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates or len(candidates) == 0:
            continue
        box = observation.boundingBox()
        fragments.append({
            "text": candidates[0].string(),
            "left": float(box.origin.x),
            "right": float(box.origin.x + box.size.width),
            "top": 1.0 - float(box.origin.y) - float(box.size.height),
            "height": float(box.size.height),
            "confidence": float(candidates[0].confidence()),
        })
    return fragments


def sharp_fragments(path: str, factor: float = 3.0):
    """
    The same capture read again with the strokes kept hard.

    Second opinion, not a replacement. It costs one more Vision pass — around
    200ms — and it is only worth asking for when the answer is arithmetic,
    where a blurred digit is the difference between an answer and a refusal.
    """
    scaled = _enlarge(path, factor, smooth=False)
    if not scaled:
        return []
    try:
        result = _ocr_fragments_at(scaled)
        return [] if isinstance(result, str) else result
    finally:
        try:
            os.unlink(scaled)
        except OSError:
            pass


def _find_columns(fragments, x_bins: int = 140, y_bins: int = 60,
                  min_gap: float = 0.025, occupancy: float = 0.06):
    """
    Find the vertical gutters that separate panels.

    This is the fix for the thing that made screen reading useless: sorting
    fragments by height alone splices a chat panel, a sidebar and a web page
    into one line, because they share a y coordinate.

    A flat projection of every fragment onto the x-axis does not work either —
    one full-width toolbar covers every bin and hides all the gutters. So the
    window is treated as a 2D occupancy grid and a gutter is a column of x that
    is empty across nearly the whole *height*. A toolbar only occupies a band or
    two, which leaves the real gutters visible underneath it.
    """
    if not fragments:
        return [(0.0, 1.0)]

    grid = [[False] * y_bins for _ in range(x_bins)]
    for fragment in fragments:
        x0 = max(0, min(x_bins - 1, int(fragment["left"] * x_bins)))
        x1 = max(0, min(x_bins - 1, int(fragment["right"] * x_bins)))
        y0 = max(0, min(y_bins - 1, int(fragment["top"] * y_bins)))
        y1 = max(0, min(y_bins - 1, int((fragment["top"] + fragment["height"]) * y_bins)))
        for xi in range(x0, x1 + 1):
            row = grid[xi]
            for yi in range(y0, y1 + 1):
                row[yi] = True

    threshold = max(1, int(occupancy * y_bins))
    filled = [sum(1 for cell in grid[xi] if cell) >= threshold for xi in range(x_bins)]

    gap_bins = max(1, int(min_gap * x_bins))
    columns, run_start, gap_run = [], 0, 0
    for i, is_filled in enumerate(filled):
        if is_filled:
            if gap_run >= gap_bins and i - gap_run > run_start:
                columns.append((run_start / x_bins, (i - gap_run) / x_bins))
                run_start = i
            gap_run = 0
        else:
            gap_run += 1
    columns.append((run_start / x_bins, 1.0))

    # Slivers are icon strips and scrollbars, not panels worth separating.
    columns = [c for c in columns if c[1] - c[0] > 0.05]
    return columns or [(0.0, 1.0)]


def _lines_from(fragments):
    """Group fragments into reading lines within a single column."""
    fragments = sorted(fragments, key=lambda f: (round(f["top"], 3), f["left"]))
    lines, current, last_top = [], [], None
    for fragment in fragments:
        tolerance = max(0.006, fragment["height"] * 0.6)
        if last_top is not None and abs(fragment["top"] - last_top) > tolerance:
            lines.append(" ".join(current))
            current = []
        current.append(fragment["text"])
        last_top = fragment["top"]
    if current:
        lines.append(" ".join(current))
    return [line for line in lines if line.strip()]


def _label_for(span, fragments_in_span) -> str:
    """A name a person would use for where this text is."""
    centre = (span[0] + span[1]) / 2
    width = span[1] - span[0]
    if width > 0.75:
        return "main area"
    if centre < 0.28:
        return "left panel"
    if centre > 0.72:
        return "right panel"
    return "main area"


def _extent(fragments):
    """
    The box a group of fragments occupies, normalised inside the capture.

    The fragments already carry their boxes; this only takes the outside of
    them. A region knew where it was across the window (`span`) but not how
    far down it ran, so nothing above could outline it or stand beside it.
    (u0, v0, u1, v1) — left, top, right, bottom, each 0..1.
    """
    if not fragments:
        return None
    return (min(f["left"] for f in fragments),
            min(f["top"] for f in fragments),
            max(f["right"] for f in fragments),
            max(f["top"] + f["height"] for f in fragments))


def ocr_regions(path: str, window: Optional[dict] = None):
    """OCR one window into labelled, spatially coherent regions."""
    fragments = _ocr_fragments(path, window)
    if isinstance(fragments, str):
        return fragments
    if not fragments:
        return []

    regions = []
    for span in _find_columns(fragments):
        inside = [f for f in fragments
                  if span[0] <= (f["left"] + f["right"]) / 2 < span[1] + 1e-9]
        if not inside:
            continue
        lines = _lines_from(inside)
        if not lines:
            continue
        heights = sorted(f["height"] for f in inside)
        median = heights[len(heights) // 2] if heights else 0.0
        headings = [f["text"].strip() for f in inside
                    if median and f["height"] > median * 1.45 and len(f["text"].strip()) > 2]

        regions.append({
            "label": _label_for(span, inside),
            "span": span,
            "bounds": _extent(inside),
            "lines": lines,
            "headings": headings[:6],
            "chars": sum(len(line) for line in lines),
        })

    # Biggest block of text first: that is almost always what is being looked at.
    regions.sort(key=lambda r: -r["chars"])
    return regions


def ocr_image(path: str, window: Optional[dict] = None) -> str:
    """Backwards-compatible flat text, now in correct reading order."""
    regions = ocr_regions(path, window)
    if isinstance(regions, str):
        return regions
    if not regions:
        return ""
    chunks = []
    for region in regions:
        chunks.append(f"[{region['label']}]\n" + "\n".join(region["lines"]))
    text = "\n\n".join(chunks)
    if len(text) > MAX_OCR_CHARS:
        text = text[:MAX_OCR_CHARS] + f"\n... [truncated, {len(text)} chars on screen]"
    return text


# ─── Browsers, straight from the source ──────────────────────────────────────

_BROWSER_SCRIPTS = {
    "Google Chrome": 'tell application "Google Chrome" to return (URL of active tab of front window) & "\\n" & (title of active tab of front window)',
    "Safari": 'tell application "Safari" to return (URL of front document) & "\\n" & (name of front document)',
    "Arc": 'tell application "Arc" to return (URL of active tab of front window) & "\\n" & (title of active tab of front window)',
    "Microsoft Edge": 'tell application "Microsoft Edge" to return (URL of active tab of front window) & "\\n" & (title of active tab of front window)',
}


def browser_tab(app: str) -> Optional[Dict[str, str]]:
    """
    The real URL and title of the front tab, asked of the browser itself.

    Reading a URL off pixels gets you "githuh.corn". Asking AppleScript gets you
    the string. Zen and other Firefox builds do not expose this, so they fall
    back to OCR like everything else.
    """
    script = _BROWSER_SCRIPTS.get(app)
    if not script:
        return None
    proc = subprocess.run(["osascript", "-e", script], capture_output=True,
                          text=True, timeout=10)
    if proc.returncode != 0:
        return None
    parts = proc.stdout.strip().split("\n")
    if not parts or not parts[0]:
        return None
    return {"url": parts[0], "title": parts[1] if len(parts) > 1 else ""}


# ─── The thing everything else calls ─────────────────────────────────────────

def look(target: str = "") -> str:
    """
    Read one window and return its context as text.

    Empty target means whatever is in front. Anything else is resolved with
    find_window, so "my chrome tab", "the browser", "VS Code" and "Excel" all
    work without the caller knowing what is open.
    """
    window = find_window(target) if target else frontmost_window()
    if not window:
        if not list_windows():
            return ("Error: I cannot enumerate windows. Diablo needs Screen "
                    "Recording permission in System Settings > Privacy & Security.")
        open_now = ", ".join(sorted({w["app"] for w in list_windows()})[:8])
        return f"Error: I could not find a window matching '{target}'. Open right now: {open_now}."

    if window["sensitive"]:
        return (f"Refused: {window['app']} looks like a password manager or "
                f"banking window. I will not capture it.")

    header = [f"Window: {window['app']}"]
    if window["title"]:
        header.append(f"Title: {window['title']}")

    tab = browser_tab(window["app"])
    if tab:
        header.append(f"URL: {tab['url']}")
        if tab["title"] and tab["title"] != window["title"]:
            header.append(f"Page: {tab['title']}")

    path = capture_window(window["id"])
    if not path:
        return "\n".join(header) + "\n\n(Could not capture this window's contents.)"

    try:
        text = ocr_image(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if text.startswith("Error:"):
        return "\n".join(header) + "\n\n" + text
    if not text.strip():
        return "\n".join(header) + "\n\n(No readable text in this window.)"
    return "\n".join(header) + "\n\n--- what is on screen ---\n" + text


def look_everything() -> str:
    """A one-line-per-window summary of the whole desktop. Cheap, no OCR."""
    windows = list_windows()
    if not windows:
        return ("Error: no windows visible, or Screen Recording permission is "
                "not granted in System Settings > Privacy & Security.")
    lines = [f"{len(windows)} windows open, frontmost first:"]
    for window in windows[:15]:
        title = f" — {window['title'][:70]}" if window["title"] else ""
        flag = "  [not captured: sensitive]" if window["sensitive"] else ""
        lines.append(f"  {window['app']}{title}{flag}")
    return "\n".join(lines)


def vision_available() -> bool:
    """Whether the vision model is actually pulled, not just configured."""
    try:
        return VISION_MODEL in local_brain.installed_models()
    except Exception:
        return False


# The longest edge a capture is sent to the vision model at.
#
# Measured on both displays of this machine, same question, same model:
#
#     window          native             at 1280px        answer
#     1920x1080       23.8s              10.6s            same
#     3024x1766       37.0s              10.4s            same
#
# The Retina capture is the striking one: a 3.6x speedup for a description
# that came back word for word the same. Going further does cost accuracy —
# at a 633px edge the model called a dark theme "white background" — so this
# sits where the time stops falling and before the reading starts breaking.
VISION_MAX_EDGE = config.env_int("DIABLO_VISION_MAX_EDGE", 1280, low=320, high=4096)


def _for_vision(path: str) -> tuple:
    """
    The capture at a size the vision model is quick on: (path, temporary).

    Returns the original path untouched when it is already small enough, so
    the common case costs nothing and no caller has to know which happened —
    the second element says whether there is a file to clean up.
    """
    if VISION_MAX_EDGE <= 0:
        return path, False
    try:
        from AppKit import NSBitmapImageRep
        from Foundation import NSData
        rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(path))
        if rep is None:
            return path, False
        longest = max(int(rep.pixelsWide()), int(rep.pixelsHigh()))
    except Exception:
        return path, False
    if longest <= VISION_MAX_EDGE:
        return path, False
    smaller = _enlarge(path, VISION_MAX_EDGE / float(longest), smooth=True)
    return (smaller, True) if smaller else (path, False)


def read_image(path: str, instruction: str, num_predict: int = 300,
               schema: Optional[dict] = None) -> str:
    """
    Ask the vision model about an image already on disk.

    Split out of describe_visually because the caller sometimes already holds
    the capture. The tutor does: it captures once, OCRs it, and decides only
    afterwards that the OCR is not good enough. Re-capturing there would grab a
    different screen — the user has had two seconds to move something — and
    the fragments it already anchored to would no longer match the picture.
    """
    sized, temporary = _for_vision(path)
    try:
        with open(sized, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()
    except OSError as e:
        # Not interpolated: an OSError carries the full path, and this string
        # is read out loud.
        print(f"  [Vision] could not read {path}: {e}")
        return "Error: I could not read the screen capture."
    finally:
        if temporary:
            try:
                os.unlink(sized)
            except OSError:
                pass
    try:
        # `schema` constrains the reply to a shape Ollama enforces. Free text
        # from a vision model is a paragraph to parse with a regular
        # expression, and that is how a misread becomes a confident wrong
        # answer nobody can audit.
        message = local_brain.chat(
            [{"role": "user", "content": instruction, "images": [encoded]}],
            model=VISION_MODEL, num_predict=num_predict,
            **({"fmt": schema} if schema else {}),
        )
        return (message.get("content") or "").strip()
    except local_brain.BrainOffline as e:
        return f"Error: {e.spoken}"


def describe_visually(question: str, target: str = "") -> str:
    """
    Ask a local vision model about a window.

    For anything OCR cannot answer: layout, a chart's shape, which control is
    disabled, what an image shows. Needs a vision model pulled into Ollama —
    the text path above covers everything else and needs nothing.
    """
    if VISION_MODEL not in local_brain.installed_models():
        return (f"Error: no vision model installed. Run: ollama pull {VISION_MODEL} "
                f"— until then I can read the text on screen but not describe images.")

    window = find_window(target) if target else frontmost_window()
    if not window:
        return f"Error: could not find a window matching '{target}'."
    if window["sensitive"]:
        return f"Refused: {window['app']} looks like a password manager."

    path = capture_window(window["id"])
    if not path:
        return "Error: could not capture that window."
    sized, temporary = _for_vision(path)
    try:
        with open(sized, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()
    finally:
        for leftover in ([sized] if temporary else []) + [path]:
            try:
                os.unlink(leftover)
            except OSError:
                pass

    try:
        message = local_brain.chat(
            [{"role": "user",
              "content": f"This is a screenshot of {window['app']}. {question}",
              "images": [encoded]}],
            model=VISION_MODEL, num_predict=600,
        )
        return (message.get("content") or "").strip() or "The vision model returned nothing."
    except local_brain.BrainOffline as e:
        return f"Error: {e.spoken}"


# ─── Understanding it, not just reading it ──────────────────────────────────
# Reading the screen and explaining the screen are different jobs. An OCR dump
# read aloud is unusable — it is thousands of characters of interleaved UI
# chrome. clicky's insight is that the model should be given structure and asked
# to explain, not handed a wall of text. This does that with the structure
# already extracted above, and answers in something a person would want to hear.

APP_KINDS = {
    "code": ("visual studio code", "xcode", "cursor", "antigravity", "sublime",
             "pycharm", "intellij", "zed", "nova"),
    "browser": ("zen", "safari", "chrome", "firefox", "arc", "edge", "brave"),
    "terminal": ("terminal", "iterm", "warp", "ghostty", "alacritty"),
    "spreadsheet": ("excel", "numbers", "tables"),
    "document": ("word", "pages", "preview", "acrobat", "obsidian", "notes"),
    "chat": ("messages", "whatsapp", "slack", "discord", "telegram", "mail"),
    "design": ("figma", "sketch", "photoshop", "illustrator", "affinity"),
}

ERROR_HINTS = ("error", "exception", "traceback", "failed", "failure", "cannot",
               "denied", "not found", "undefined", "null", "warning", "fatal",
               "refused", "timed out", "invalid", "unexpected")


def screen_kind(app: str) -> str:
    lowered = (app or "").lower()
    for kind, names in APP_KINDS.items():
        if any(name in lowered for name in names):
            return kind
    return "app"


def salient_lines(regions, limit: int = 8):
    """Lines that look like something went wrong. Worth surfacing first."""
    hits = []
    for region in regions:
        for line in region["lines"]:
            lowered = line.lower()
            if any(hint in lowered for hint in ERROR_HINTS) and len(line) > 12:
                hits.append(line.strip())
    return hits[:limit]


def structured(target: str = "") -> Dict[str, Any]:
    """Everything known about one window, in a shape a model can reason over."""
    window = find_window(target) if target else frontmost_window()
    if not window:
        return {"error": f"No window matching '{target}'." if target
                else "No window is visible."}
    if window["sensitive"]:
        return {"error": f"{window['app']} looks like a password manager. Refusing."}

    info: Dict[str, Any] = {
        "app": window["app"],
        "title": window["title"],
        "kind": screen_kind(window["app"]),
        "size": f"{window['width']}x{window['height']}",
    }

    tab = browser_tab(window["app"])
    if tab:
        info["url"] = tab["url"]
        info["page"] = tab["title"]

    path = capture_window(window["id"])
    if not path:
        info["error"] = "Could not capture that window."
        return info

    # Blank-frame check before anything reads it. A window on another Space
    # captures as valid, well-formed, solid black, and OCR then reports no text
    # — which reads exactly like an empty window and is not one.
    try:
        from skills import screen as _screen
        good, why = _screen.validity(path)
        if not good:
            try:
                os.unlink(path)
            except OSError:
                pass
            info["error"] = why.capitalize() + "."
            return info
    except Exception:
        pass

    regions = ocr_regions(path, window)
    # Kept, not deleted, so a question the text cannot answer can be put to the
    # vision model against THIS capture. Re-capturing for the second attempt
    # would ask about a screen a second older than the one just described.
    # Whoever pops it owns deleting it; explain() always does.
    info["_capture"] = path

    if isinstance(regions, str):
        # OCR itself failed. The capture is of no further use and nothing
        # downstream will pop it, so it goes now rather than sitting in /tmp.
        info.pop("_capture", None)
        try:
            os.unlink(path)
        except OSError:
            pass
        info["error"] = regions
        return info

    info["regions"] = regions
    info["headings"] = [h for r in regions for h in r.get("headings", [])][:8]
    info["problems"] = salient_lines(regions)
    return info


def _as_prompt(info: Dict[str, Any], budget: int = 2200) -> str:
    """Flatten the structure into something worth spending context on."""
    parts = [f"Application: {info['app']} ({info['kind']})"]
    if info.get("title"):
        parts.append(f"Window title: {info['title']}")
    if info.get("url"):
        parts.append(f"Open page: {info.get('page') or ''} — {info['url']}")
    if info.get("headings"):
        parts.append("Headings on screen: " + " | ".join(info["headings"]))
    if info.get("problems"):
        parts.append("Lines that mention a problem:\n  " +
                     "\n  ".join(info["problems"]))

    remaining = budget - sum(len(p) for p in parts)
    for region in info.get("regions", []):
        if remaining <= 200:
            break
        body = "\n".join(region["lines"])
        if len(body) > remaining:
            body = body[:remaining] + " ..."
        remaining -= len(body)
        parts.append(f"--- {region['label']} ---\n{body}")
    return "\n\n".join(parts)


EXPLAIN_SYSTEM = """You are Diablo, looking at Shaurya's screen and telling him what
is there. You are speaking out loud, so:

- Two or three sentences. Never a list, never markdown. It is read aloud.
- Lead with the answer. He can see the screen; he wants the point, not a tour.
- The text came from OCR, so it has character errors and stray interface labels
  in it. Read it for sense. If something is too garbled to be sure of, say so
  rather than guessing at it.
- If he asked a question, answer that question and nothing else.
- If an error is on screen, say what it is and what it most likely means.
- Never invent anything that is not in the text you were given."""


# Questions OCR cannot answer, however good the OCR is.
#
# Text extraction returns what a screen SAYS. These ask what it LOOKS like —
# shape, colour, position, state, imagery — and no amount of reading the words
# answers them. A chart with no data labels OCRs to its axis titles and a
# legend, and a confident paragraph written from that is a description of a
# chart nobody has looked at.
VISUAL_QUESTIONS = (
    "graph", "chart", "plot", "diagram", "figure", "image", "picture", "photo",
    "icon", "colour", "color", "layout", "look like", "looks like", "design",
    "greyed", "grayed", "disabled", "highlighted", "selected", "ticked",
    "checked", "shaded", "arrow", "shape", "drawing", "screenshot", "thumbnail",
    "dark mode", "light mode", "which button", "what does this show",
)

# Below this many characters, a window of any size is not mostly text. A
# video player, a design tool, a photo, a rendered chart: all of them OCR to
# almost nothing, and "there is no text in this window" is a true statement
# that answers no question anyone asked.
THIN_TEXT = config.env_int("DIABLO_SCREEN_THIN_TEXT", 140, low=0, high=4000)


def needs_vision(question: str, info: Dict[str, Any]) -> bool:
    """
    Whether this question needs the vision model rather than the text.

    Two independent triggers, because either alone misses half the cases: the
    question can be visual about a text-heavy screen ("which button is greyed
    out" in a dialog), and the question can be ordinary about a screen with no
    text in it ("what is this?" pointed at a photograph).
    """
    lowered = (question or "").lower()
    if any(cue in lowered for cue in VISUAL_QUESTIONS):
        return True
    characters = sum(len(line) for region in info.get("regions", [])
                     for line in region.get("lines", []))
    return characters < THIN_TEXT


VISION_EXPLAIN = """Look at this screenshot and answer the question about it.

You are speaking out loud, so two or three sentences, no lists and no markdown.
Lead with the answer. Describe only what is actually visible — if you cannot
see something clearly enough to be sure, say so rather than guessing at it."""


def explain(question: str = "", target: str = "") -> str:
    """
    Say what is on screen, in a sentence or two, out loud.

    This is the difference between reading a screen and understanding one. The
    raw extraction runs to thousands of characters of interleaved interface;
    this turns it into the one thing worth saying.
    """
    info = structured(target)
    if info.get("error"):
        return f"Error: {info['error']}"

    # Route to whichever model can actually answer, rather than sending
    # everything through the cheapest one and hoping.
    #
    # The text path is the default because it is ten times faster and exact
    # where it applies. The vision model is not a better version of it — it is
    # a different instrument, and it is the only one that can answer a question
    # about a picture.
    if needs_vision(question, info) and vision_available():
        capture = info.pop("_capture", None)
        if capture and os.path.exists(capture):
            try:
                context = _as_prompt(info, budget=600)
                seen = read_image(
                    capture,
                    f"{VISION_EXPLAIN}\n\nThis is {info['app']}"
                    + (f", showing {info['title'][:80]}" if info.get("title") else "")
                    + f".\n\nQuestion: {question.strip() or 'What am I looking at?'}",
                    num_predict=320)
            finally:
                try:
                    os.unlink(capture)
                except OSError:
                    pass
            if seen and not seen.startswith("Error:"):
                return seen
    capture = info.pop("_capture", None)
    if capture:
        try:
            os.unlink(capture)
        except OSError:
            pass

    ask = question.strip() or "What is on this screen? Say what he is looking at and what matters about it."
    try:
        message = local_brain.chat(
            [{"role": "system", "content": EXPLAIN_SYSTEM},
             {"role": "user", "content": f"{_as_prompt(info)}\n\n---\nHis question: {ask}"}],
            num_predict=220, temperature=0.3,
        )
    except local_brain.BrainOffline as e:
        return f"Error: {e.spoken}"

    answer = (message.get("content") or "").strip()
    return answer or "I could not make sense of what is on screen, Sir."


# ─── Does this sentence even want the screen? ────────────────────────────────

SCREEN_REFERENCES = [
    "on screen", "on my screen", "on the screen", "this page", "this tab",
    "my tab", "what i'm looking at", "what im looking at", "what am i looking at",
    "look at", "take a look", "peek", "read this", "read the screen", "see this",
    "see my", "what's this", "whats this", "what does this say", "in front of me",
    "this error", "this code", "this article", "this window", "my browser",
    "my chrome", "the browser", "currently open", "right now on",
    # Pointing at something with the mouse rather than naming it.
    "under my cursor", "under the cursor", "my cursor", "my pointer",
    "under my mouse", "hovering over", "this icon", "this button",
    # Asking about a change, which is only meaningful about a screen.
    "what changed", "what's changed", "whats changed", "did that work",
    "did it work", "did that open", "did the", "has it changed",
    "what happened", "anything change",
    # Asking to be shown where, which needs somewhere to point.
    "where do i click", "where should i click", "what do i click",
    "which button", "what button", "point it out", "show me where",
    "walk me through", "what's next", "whats next", "what do i press",
    # Stuck, in front of something.
    "what am i doing", "what am i working on", "where am i stuck",
    "what's wrong here", "whats wrong here", "why isn't this working",
    "why isnt this working", "what am i doing wrong",
    # The displays themselves.
    "my monitor", "my monitors", "my display", "my displays",
    "second screen", "other screen", "external monitor",
]

# Sentences that mention a screen word but are not about this screen. Checked
# first, because "this" is doing different work in "what does this error mean"
# and "what does this word mean in French" — and the second must not trigger a
# capture, which costs a second and puts the wrong context in front of the
# model.
NOT_THE_SCREEN = (
    "on screen actors", "screen time", "screenplay", "screen printing",
    "sunscreen", "screen door", "silver screen",
)


def wants_screen(text: str) -> bool:
    """Cheap pre-filter for whether to attach screen context to a reply."""
    lowered = (text or "").lower()
    if any(phrase in lowered for phrase in NOT_THE_SCREEN):
        return False
    return any(phrase in lowered for phrase in SCREEN_REFERENCES)


if __name__ == "__main__":
    print(look_everything())
    print()
    print(f"OCR available: {ocr_available()}")
    front = frontmost_window()
    if front:
        print(f"\nFrontmost: {front['app']} — {front['title'][:60]}")
        if ocr_available():
            print("\n" + look()[:1500])
