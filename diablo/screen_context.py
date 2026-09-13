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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_brain  # noqa: E402

VISION_MODEL = os.getenv("DIABLO_VISION_MODEL", "qwen2.5vl:7b")
MAX_OCR_CHARS = 6000

# Windows that are never what you mean by "what I'm looking at".
CHROME_IGNORE = {
    "Window Server", "Control Center", "Notification Centre", "Notification Center",
    "Dock", "SystemUIServer", "Spotlight", "loginwindow", "TextInputMenuAgent",
    "Wallpaper", "Screenshot", "universalaccessd",
}

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
        bounds = entry.get("kCGWindowBounds") or {}
        width, height = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
        if width < min_width or height < min_height:
            continue
        windows.append({
            "id": int(entry.get("kCGWindowNumber", 0)),
            "app": owner,
            "title": entry.get("kCGWindowName") or "",
            "width": width,
            "height": height,
            "sensitive": owner.lower() in SENSITIVE_APPS,
        })
    return windows


def frontmost_window() -> Optional[Dict[str, Any]]:
    """The window you are actually looking at. Quartz returns them front to back."""
    windows = list_windows()
    return windows[0] if windows else None


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
    for word in needle.split():
        if len(word) < 3:
            continue
        for window in windows:
            if hit(window, word):
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
    path = os.path.join(tempfile.gettempdir(), f"diablo_screen_{int(time.time())}.png")
    proc = subprocess.run(["screencapture", "-x", path], capture_output=True, timeout=30)
    if proc.returncode != 0 or not os.path.exists(path):
        return None
    return path


# ─── Reading the pixels ──────────────────────────────────────────────────────

def ocr_available() -> bool:
    try:
        import Vision  # noqa: F401
        return True
    except ImportError:
        return False


def _ocr_fragments(path: str):
    """
    Raw OCR fragments with their boxes, or a string if OCR is unavailable.

    Vision returns observations in no useful order and with normalised,
    bottom-left-origin boxes. They come back here flipped to a top-left origin
    so everything downstream can think in reading order.
    """
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


def ocr_regions(path: str):
    """OCR one window into labelled, spatially coherent regions."""
    fragments = _ocr_fragments(path)
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
            "lines": lines,
            "headings": headings[:6],
            "chars": sum(len(line) for line in lines),
        })

    # Biggest block of text first: that is almost always what is being looked at.
    regions.sort(key=lambda r: -r["chars"])
    return regions


def ocr_image(path: str) -> str:
    """Backwards-compatible flat text, now in correct reading order."""
    regions = ocr_regions(path)
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
    try:
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()
    finally:
        try:
            os.unlink(path)
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
        return f"Error: vision model unavailable ({e})."


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
    try:
        regions = ocr_regions(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if isinstance(regions, str):
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

    ask = question.strip() or "What is on this screen? Say what he is looking at and what matters about it."
    try:
        message = local_brain.chat(
            [{"role": "system", "content": EXPLAIN_SYSTEM},
             {"role": "user", "content": f"{_as_prompt(info)}\n\n---\nHis question: {ask}"}],
            num_predict=220, temperature=0.3,
        )
    except local_brain.BrainOffline as e:
        return f"Error: my local model is not responding ({e})."

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
]


def wants_screen(text: str) -> bool:
    """Cheap pre-filter for whether to attach screen context to a reply."""
    lowered = text.lower()
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
