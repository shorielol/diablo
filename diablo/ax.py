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
    _cf.CFNumberGetValue.restype = ctypes.c_bool
    _cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]

    _as.AXIsProcessTrusted.restype = ctypes.c_bool
    _as.AXUIElementCreateApplication.restype = ctypes.c_void_p
    _as.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
    _as.AXUIElementCopyAttributeValue.restype = ctypes.c_int
    _as.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_void_p)]
    _as.AXValueGetValue.restype = ctypes.c_bool
    _as.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    _as.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
    _as.AXUIElementCreateSystemWide.argtypes = []
    _as.AXUIElementCopyElementAtPosition.restype = ctypes.c_int
    _as.AXUIElementCopyElementAtPosition.argtypes = [ctypes.c_void_p, ctypes.c_float,
                                                     ctypes.c_float,
                                                     ctypes.POINTER(ctypes.c_void_p)]
    _as.AXUIElementGetPid.restype = ctypes.c_int
    _as.AXUIElementGetPid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    _as.AXUIElementPerformAction.restype = ctypes.c_int
    _as.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


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


def _number_attribute(element, name: str) -> Optional[float]:
    ref = _attribute(element, name)
    if not ref:
        return None
    try:
        if _cf.CFGetTypeID(ref) == _cf.CFNumberGetTypeID():
            out = ctypes.c_double()
            # 13 is kCFNumberDoubleType.
            if _cf.CFNumberGetValue(ref, 13, ctypes.byref(out)):
                return float(out.value)
        if _cf.CFGetTypeID(ref) == _cf.CFBooleanGetTypeID():
            return 1.0 if _cf.CFBooleanGetValue(ref) else 0.0
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
    # A checkbox's or radio button's on/off, read as a number. Kept out of
    # `value`, because `label` falls back to `value` and a control's name
    # must not change when it is ticked.
    state: Optional[float] = None

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
                        state=(_number_attribute(node, "AXValue")
                               if role in TOGGLE_ROLES else None),
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


@dataclass
class Hit:
    """
    What macOS says is under a point right now.

    `element` is the nearest actionable control at or above what was hit, or
    None when the point lands on scenery. `pid` and `window_frame` belong to
    whatever is frontmost there, which is how a point on a window that covers
    the one being asked about is told apart from a point on that window.
    """

    element: Optional[Element] = None
    deepest_role: str = ""
    pid: int = 0
    window_title: str = ""
    window_frame: Optional[Tuple[float, float, float, float]] = None


# Controls whose on/off is their AXValue.
TOGGLE_ROLES = {"AXCheckBox", "AXRadioButton"}


def _element(node, pid: int, window_title: str) -> Optional[Element]:
    rect = _frame(node)
    if not rect or rect[2] <= 0 or rect[3] <= 0:
        return None
    role = _string_attribute(node, "AXRole")
    return Element(
        role=role,
        state=_number_attribute(node, "AXValue") if role in TOGGLE_ROLES else None,
        subrole=_string_attribute(node, "AXSubrole"),
        title=_string_attribute(node, "AXTitle"),
        description=_string_attribute(node, "AXDescription"),
        value=_string_attribute(node, "AXValue")[:120],
        help=_string_attribute(node, "AXHelp"),
        enabled=_bool_attribute(node, "AXEnabled"),
        focused=_bool_attribute(node, "AXFocused"),
        selected=_bool_attribute(node, "AXSelected"),
        x=rect[0], y=rect[1], width=rect[2], height=rect[3],
        pid=pid, window_title=window_title)


# How far up from the deepest element to look for the control. A button's
# title cell or a checkbox's label is one or two levels below the control.
CLIMB = 4


def hit_test(x: float, y: float) -> Optional[Hit]:
    """
    The control under a Quartz point, asked of the system rather than of one
    window's tree.

    `element_at` searches the tree of the window it is given, so it cannot see
    another window lying on top of that point, and a click goes to whatever is
    on top. This asks the window server's view of the screen. Returns None when
    accessibility is unavailable, which is "cannot tell", never "nothing there".
    """
    if not available():
        return None
    try:
        _load()
        system = _as.AXUIElementCreateSystemWide()
        hit = ctypes.c_void_p()
        err = _as.AXUIElementCopyElementAtPosition(system, float(x), float(y),
                                                   ctypes.byref(hit))
    except Exception:
        return None
    if err != 0 or not hit.value:
        return Hit()

    pid = ctypes.c_int(0)
    try:
        _as.AXUIElementGetPid(hit.value, ctypes.byref(pid))
    except Exception:
        pass
    window = _attribute(hit.value, "AXWindow")
    window_title = _string_attribute(window, "AXTitle") if window else ""
    result = Hit(deepest_role=_string_attribute(hit.value, "AXRole"), pid=int(pid.value),
                 window_title=window_title,
                 window_frame=_frame(window) if window else None)

    node = hit.value
    for _ in range(CLIMB + 1):
        role = _string_attribute(node, "AXRole")
        if role in ACTIONABLE_ROLES:
            result.element = _element(node, result.pid, window_title)
            break
        if role in ("AXWindow", "AXApplication", "AXSheet", "AXDrawer", ""):
            break
        node = _attribute(node, "AXParent")
        if not node:
            break
    return result


@dataclass(frozen=True)
class MenuItem:
    """
    One command in an application's menu bar, as the application publishes it.

    `path` runs from the menu bar title to the item: ("File", "New Tab"), or
    ("File", "Export", "PDF…") for a submenu. `cmd_mods` is the accessibility
    modifier mask: 1 Shift, 2 Option, 4 Control, 8 means no Command.
    """

    path: Tuple[str, ...]
    enabled: bool = True
    cmd_char: str = ""
    cmd_mods: int = 0
    node: Optional[int] = field(default=None, compare=False, repr=False)
    # Where the menu's title sits in the menu bar, in Quartz points: what to
    # point at when the command itself is inside a closed menu.
    bar_frame: Optional[Tuple[float, float, float, float]] = field(
        default=None, compare=False, repr=False)

    @property
    def title(self) -> str:
        return self.path[-1] if self.path else ""


# How much of a menu bar is read: deep enough for File › Export › PDF…, and
# bounded, because some apps publish hundreds of items (fonts, recent files).
MENU_DEPTH = 3
MENU_LIMIT = 1500


def menu_tree(pid: int) -> List[MenuItem]:
    """
    Every command in an application's menu bar, without opening a menu.

    The Apple menu is skipped: it belongs to the system, not the app. Works
    for an application in the background, since the bar is read from the
    application, not from the screen.
    """
    if not pid or not available():
        return []
    try:
        _load()
        bar = _attribute(_as.AXUIElementCreateApplication(pid), "AXMenuBar")
    except Exception:
        return []
    out: List[MenuItem] = []

    def children(node) -> List[int]:
        ref = _attribute(node, "AXChildren")
        if not ref:
            return []
        try:
            return [_cf.CFArrayGetValueAtIndex(ref, i)
                    for i in range(min(_cf.CFArrayGetCount(ref), 400))]
        except Exception:
            return []

    def walk(menu, path: Tuple[str, ...], depth: int, bar) -> None:
        for item in children(menu):
            if len(out) >= MENU_LIMIT:
                return
            title = _string_attribute(item, "AXTitle").strip()
            if not title:
                continue                          # a separator
            here = path + (title,)
            submenus = [c for c in children(item)
                        if _string_attribute(c, "AXRole") == "AXMenu"]
            if submenus and depth < MENU_DEPTH:
                walk(submenus[0], here, depth + 1, bar)
                continue
            mods = _number_attribute(item, "AXMenuItemCmdModifiers")
            out.append(MenuItem(here, _bool_attribute(item, "AXEnabled") is not False,
                                _string_attribute(item, "AXMenuItemCmdChar"),
                                int(mods or 0), item, bar))

    for index, top in enumerate(children(bar) if bar else []):
        title = _string_attribute(top, "AXTitle").strip()
        if index == 0 or not title or title == "Apple":
            continue
        bar_frame = _frame(top)
        for menu in children(top):
            if _string_attribute(menu, "AXRole") == "AXMenu":
                walk(menu, (title,), 1, bar_frame)
    return out


def window_titles(pid: int) -> Tuple[str, ...]:
    """The titles of an application's windows, front first, from the app itself."""
    if not pid or not available():
        return ()
    try:
        _load()
        windows = _attribute(_as.AXUIElementCreateApplication(pid), "AXWindows")
        if not windows:
            return ()
        return tuple(_string_attribute(_cf.CFArrayGetValueAtIndex(windows, i), "AXTitle")
                     for i in range(min(_cf.CFArrayGetCount(windows), 50)))
    except Exception:
        return ()


def press(node: Optional[int]) -> bool:
    """Press a control or menu item through accessibility. True if the app accepted it."""
    if not node or not available():
        return False
    try:
        _load()
        return _as.AXUIElementPerformAction(node, _cfstr("AXPress")) == 0
    except Exception:
        return False


# Controls that take typed text. Keystrokes sent to anything else are lost,
# or worse: a space bar press on a focused button presses it.
EDITABLE_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}


def focused_element(pid: int) -> Optional[Element]:
    """
    The control that has keyboard focus in an application, or None when
    nothing has it or accessibility cannot say. Both mean "do not type".
    """
    if not pid or not available():
        return None
    try:
        _load()
        node = _attribute(_as.AXUIElementCreateApplication(pid), "AXFocusedUIElement")
    except Exception:
        return None
    if not node:
        return None
    window = _attribute(node, "AXWindow")
    return _element(node, pid, _string_attribute(window, "AXTitle") if window else "")


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
