"""
D.I.A.B.L.O. — what the application says about itself.

Why this exists
---------------
OCR can only find a control that has text on it. Ask where the gear icon is
and there is nothing to match, so `pointing.locate` returns nothing and Diablo
says it cannot find it. That is the largest single class of failure in screen
guidance, and it is not fixable by reading harder: the information is not in
the pixels.

macOS applications publish their controls. A button knows it is a button, that
its description is "Settings", that it is enabled, and exactly where it is.
This module asks.

Accessibility is a CANDIDATE SOURCE, not truth. Applications lie by omission
and by staleness: Electron apps often expose one giant AXGroup, some windows
report positions a frame behind, and a control can be present in the tree and
invisible on screen. So everything here comes back as evidence to be weighed
against what the screen actually shows, never as a coordinate to click.

Through ctypes, deliberately
----------------------------
pyobjc-framework-ApplicationServices is not installed here — only Cocoa,
Quartz, Vision, WebKit and CoreML are. permissions.py already hit this and
recorded the lesson: `from ApplicationServices import AXIsProcessTrusted`
inside an `except BaseException` raised every time, was swallowed, and made the
app report accessibility as denied for months. The frameworks themselves are
part of the OS and always present, so this talks to them directly.

Coordinates
-----------
AXPosition is already in the same top-left global space CGWindowList reports,
which is Diablo's canonical Quartz space. Verified against a window whose known
bounds were compared with its AX position. No conversion, and none should be
added.
"""

import ctypes
import ctypes.util
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config  # noqa: E402

# A tree can be enormous — a browser's rendered page is tens of thousands of
# nodes — and walking all of it costs seconds. These bound the walk. Both are
# generous for a toolbar or a dialog, which is what Diablo actually points at.
MAX_ELEMENTS = config.env_int("DIABLO_AX_MAX_ELEMENTS", 1200, low=50, high=20000)
MAX_DEPTH = config.env_int("DIABLO_AX_MAX_DEPTH", 14, low=2, high=40)

_UTF8 = 0x08000100

# Roles worth offering as something to click. A walk that returns every
# AXGroup and AXStaticText buries the four buttons the user meant.
ACTIONABLE_ROLES = {
    "AXButton", "AXMenuItem", "AXMenuBarItem", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXLink", "AXTab", "AXTabGroup", "AXToolbarButton",
    "AXDisclosureTriangle", "AXSlider", "AXIncrementor", "AXComboBox",
    "AXTextField", "AXTextArea", "AXSearchField", "AXSegmentedControl",
    "AXCell", "AXRow", "AXOutline", "AXMenuButton", "AXStepper",
}

_as = None
_cf = None


def _load():
    global _as, _cf
    if _as is not None:
        return
    _as = ctypes.cdll.LoadLibrary(
        ctypes.util.find_library("ApplicationServices")
        or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    _cf = ctypes.cdll.LoadLibrary(
        ctypes.util.find_library("CoreFoundation")
        or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_long, ctypes.c_uint32]
    _cf.CFGetTypeID.restype = ctypes.c_ulong
    _cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    _cf.CFStringGetTypeID.restype = ctypes.c_ulong
    _cf.CFArrayGetTypeID.restype = ctypes.c_ulong
    _cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
    _cf.CFNumberGetTypeID.restype = ctypes.c_ulong
    _cf.CFArrayGetCount.restype = ctypes.c_long
    _cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    _cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    _cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    _cf.CFBooleanGetValue.restype = ctypes.c_bool
    _cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]

    _as.AXIsProcessTrusted.restype = ctypes.c_bool
    _as.AXUIElementCreateApplication.restype = ctypes.c_void_p
    _as.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
    _as.AXUIElementCopyAttributeValue.restype = ctypes.c_int
    _as.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_void_p)]
    _as.AXValueGetValue.restype = ctypes.c_bool
    _as.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


def available() -> bool:
    """Whether macOS is letting Diablo read other applications' controls."""
    try:
        _load()
        return bool(_as.AXIsProcessTrusted())
    except Exception:
        return False


def _cfstr(text: str):
    return _cf.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)


def _read_string(ref) -> Optional[str]:
    if not ref:
        return None
    try:
        if _cf.CFGetTypeID(ref) != _cf.CFStringGetTypeID():
            return None
        buffer = ctypes.create_string_buffer(4096)
        if _cf.CFStringGetCString(ref, buffer, 4096, _UTF8):
            return buffer.value.decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def _attribute(element, name: str):
    out = ctypes.c_void_p()
    try:
        err = _as.AXUIElementCopyAttributeValue(element, _cfstr(name), ctypes.byref(out))
    except Exception:
        return None
    return out.value if err == 0 else None


def _string_attribute(element, name: str) -> str:
    return _read_string(_attribute(element, name)) or ""


def _bool_attribute(element, name: str) -> Optional[bool]:
    ref = _attribute(element, name)
    if not ref:
        return None
    try:
        if _cf.CFGetTypeID(ref) == _cf.CFBooleanGetTypeID():
            return bool(_cf.CFBooleanGetValue(ref))
    except Exception:
        pass
    return None


def _frame(element) -> Optional[Tuple[float, float, float, float]]:
    """The element's rectangle in Quartz space, or None if it will not say."""
    position, size = _attribute(element, "AXPosition"), _attribute(element, "AXSize")
    if not position or not size:
        return None
    point, extent = _Point(), _Size()
    # 1 is kAXValueCGPointType, 2 is kAXValueCGSizeType.
    if not _as.AXValueGetValue(position, 1, ctypes.byref(point)):
        return None
    if not _as.AXValueGetValue(size, 2, ctypes.byref(extent)):
        return None
    return (point.x, point.y, extent.width, extent.height)


@dataclass
class Element:
    """One control, as the application describes it."""

    role: str = ""
    subrole: str = ""
    title: str = ""
    description: str = ""
    value: str = ""
    help: str = ""
    enabled: Optional[bool] = None
    focused: Optional[bool] = None
    selected: Optional[bool] = None

    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    app: str = ""
    pid: int = 0
    window_title: str = ""
    depth: int = 0

    @property
    def centre(self) -> Tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    @property
    def label(self) -> str:
        """The best name this control has, in the order a person would read it."""
        for candidate in (self.title, self.description, self.help, self.value):
            if candidate and candidate.strip():
                return candidate.strip()
        return ""

    @property
    def named(self) -> bool:
        return bool(self.label)

    def describe(self) -> str:
        kind = (self.subrole or self.role or "control").replace("AX", "")
        name = self.label or "unlabelled"
        return f"{kind} “{name}”" if self.named else f"unlabelled {kind}"


def _pid_for_window(window: Dict[str, Any]) -> int:
    """The owning process of a CGWindowList entry."""
    if window.get("pid"):
        return int(window["pid"])
    try:
        import Quartz
        for entry in Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
            if int(entry.get("kCGWindowNumber", 0)) == int(window.get("id", 0)):
                return int(entry.get("kCGWindowOwnerPID") or 0)
    except Exception:
        pass
    return 0


def elements_for_window(window: Dict[str, Any],
                        actionable_only: bool = True) -> List[Element]:
    """
    Every control the application publishes for this window.

    Breadth-first and bounded, because a tree is not a list: a rendered web
    page is tens of thousands of nodes and walking it all costs seconds that
    the user is waiting through.
    """
    if not available() or not window:
        return []
    pid = _pid_for_window(window)
    if not pid:
        return []

    try:
        _load()
        app_element = _as.AXUIElementCreateApplication(pid)
    except Exception:
        return []
    if not app_element:
        return []

    app_name = window.get("app", "")
    wanted_title = (window.get("title") or "").strip()

    windows_ref = _attribute(app_element, "AXWindows")
    roots = []
    if windows_ref:
        try:
            count = _cf.CFArrayGetCount(windows_ref)
            for i in range(min(count, 24)):
                node = _cf.CFArrayGetValueAtIndex(windows_ref, i)
                title = _string_attribute(node, "AXTitle")
                # Prefer the window the caller actually named. Falling back to
                # every window is deliberate: many applications give their
                # windows no title at all.
                if not wanted_title or not title or title.strip() == wanted_title:
                    roots.append((node, title))
        except Exception:
            pass
    if not roots:
        roots = [(app_element, wanted_title)]

    found: List[Element] = []
    seen = 0
    for root, title in roots:
        queue = [(root, 0)]
        while queue and seen < MAX_ELEMENTS:
            node, depth = queue.pop(0)
            seen += 1
            role = _string_attribute(node, "AXRole")
            if depth > 0 and (not actionable_only or role in ACTIONABLE_ROLES):
                rect = _frame(node)
                if rect and rect[2] > 0 and rect[3] > 0:
                    found.append(Element(
                        role=role,
                        subrole=_string_attribute(node, "AXSubrole"),
                        title=_string_attribute(node, "AXTitle"),
                        description=_string_attribute(node, "AXDescription"),
                        value=_string_attribute(node, "AXValue")[:120],
                        help=_string_attribute(node, "AXHelp"),
                        enabled=_bool_attribute(node, "AXEnabled"),
                        focused=_bool_attribute(node, "AXFocused"),
                        selected=_bool_attribute(node, "AXSelected"),
                        x=rect[0], y=rect[1], width=rect[2], height=rect[3],
                        app=app_name, pid=pid, window_title=title, depth=depth))
            if depth >= MAX_DEPTH:
                continue
            children = _attribute(node, "AXChildren")
            if not children:
                continue
            try:
                count = _cf.CFArrayGetCount(children)
            except Exception:
                continue
            for i in range(min(count, 250)):
                child = _cf.CFArrayGetValueAtIndex(children, i)
                if child:
                    queue.append((child, depth + 1))
    return found


def element_at(x: float, y: float, window: Dict[str, Any]) -> Optional[Element]:
    """
    The smallest published control containing a point.

    Smallest, not first: controls nest, and the button inside the toolbar
    inside the window is the one the user is pointing at.
    """
    best, smallest = None, float("inf")
    for element in elements_for_window(window):
        if not (element.x <= x <= element.x + element.width
                and element.y <= y <= element.y + element.height):
            continue
        area = element.width * element.height
        if area < smallest:
            best, smallest = element, area
    return best


if __name__ == "__main__":
    from skills import screen_context
    print("accessibility available:", available())
    window = screen_context.frontmost_window()
    if window:
        print(f"{window['app']} — {window['title'][:50]}")
        import time
        started = time.time()
        elements = elements_for_window(window)
        print(f"  {len(elements)} actionable controls in {(time.time()-started)*1000:.0f}ms")
        for element in elements[:15]:
            print(f"    {element.describe():54} "
                  f"({element.x:.0f},{element.y:.0f}) {element.width:.0f}x{element.height:.0f}"
                  f"{'' if element.enabled is not False else '  [disabled]'}")
        unlabelled = [e for e in elements if not e.named]
        print(f"  of which unlabelled (invisible to OCR): {len(unlabelled)}")
