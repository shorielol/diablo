"""
What macOS will actually let Diablo do, and how to ask for more.

Written against the C APIs through ctypes rather than through PyObjC, for a
reason worth recording. The accessibility check used to be:

    from ApplicationServices import AXIsProcessTrusted
    result["accessibility"] = bool(AXIsProcessTrusted())

wrapped in `except BaseException`. That module is part of pyobjc-framework-
ApplicationServices, which is not installed here — only Cocoa, Quartz, Vision,
WebKit and CoreML are. So the import raised, the exception was swallowed, and
the answer was False every single time. The app has been reporting
accessibility as denied while the real API says it is trusted.

A permission check that cannot fail loudly will eventually lie quietly. These
go through ctypes to the frameworks themselves, which are always present
because they are part of the OS, and each one reports its own failure rather
than folding it into a False.
"""

import ctypes
import ctypes.util
import os
import subprocess
from typing import Dict, Optional

APP_SERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

# The pane each permission lives in, for when macOS declines to ask again.
PANES = {
    "accessibility":
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "screen":
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "mic":
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}

_services = None
_cf = None


def _load():
    global _services, _cf
    if _services is None:
        _services = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("ApplicationServices") or APP_SERVICES)
        _services.AXIsProcessTrusted.restype = ctypes.c_bool
        _services.AXIsProcessTrusted.argtypes = []
        _services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        _services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    if _cf is None:
        _cf = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("CoreFoundation") or CORE_FOUNDATION)
        _cf.CFDictionaryCreate.restype = ctypes.c_void_p
        _cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p]
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
    return _services, _cf


# ─── Accessibility ───────────────────────────────────────────────────────────

def accessibility(prompt: bool = False) -> bool:
    """
    Whether this process may drive other apps' interfaces.

    With `prompt`, macOS raises its own dialog — but only the first time it is
    asked by a given app. After you have answered once it returns the remembered
    answer in silence, which is why the caller also needs `open_pane`.
    """
    services, cf = _load()
    if not prompt:
        return bool(services.AXIsProcessTrusted())

    # {kAXTrustedCheckOptionPrompt: true}, built by hand because there is no
    # bridge here to build it for us.
    key = ctypes.c_void_p.in_dll(services, "kAXTrustedCheckOptionPrompt")
    yes = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
    key_callbacks = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
    value_callbacks = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
    keys = (ctypes.c_void_p * 1)(key)
    values = (ctypes.c_void_p * 1)(yes)
    options = cf.CFDictionaryCreate(
        None, keys, values, 1,
        ctypes.byref(key_callbacks), ctypes.byref(value_callbacks))
    try:
        return bool(services.AXIsProcessTrustedWithOptions(options))
    finally:
        if options:
            cf.CFRelease(options)


# ─── Screen recording ────────────────────────────────────────────────────────

def screen(prompt: bool = False) -> bool:
    """
    Whether window contents are readable.

    The old check looked for a window title belonging to another app and
    inferred permission from finding one. That works, but it is a heuristic
    reading of a question the OS answers directly.
    """
    import Quartz
    if prompt and hasattr(Quartz, "CGRequestScreenCaptureAccess"):
        return bool(Quartz.CGRequestScreenCaptureAccess())
    if hasattr(Quartz, "CGPreflightScreenCaptureAccess"):
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    return any(w.get("kCGWindowName") for w in windows)


# ─── Microphone ──────────────────────────────────────────────────────────────

def microphone(prompt: bool = False) -> Optional[bool]:
    """
    Whether audio input is permitted — or None when it cannot be established.

    AVFoundation is the API that answers this and its PyObjC binding is not
    installed, so there is no honest yes or no available here without pulling
    in a dependency for one boolean. None means "not known", and the caller
    should say that rather than guessing, because the thing that actually
    proves it is a microphone returning samples that are not all zero, which
    the mic subsystem already reports separately.

    Prompting still works: the first capture attempt raises the dialog, and the
    pane is the fallback for every attempt after that.
    """
    try:
        import AVFoundation  # noqa: F401
    except Exception:
        return None
    import AVFoundation
    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
        AVFoundation.AVMediaTypeAudio)
    if prompt and status == 0:   # not determined
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, lambda granted: None)
    return status == 3           # authorized


# ─── All of them ─────────────────────────────────────────────────────────────

def snapshot() -> Dict[str, object]:
    """Every permission, with an honest None where the answer is not knowable."""
    result: Dict[str, object] = {}
    try:
        result["accessibility"] = accessibility()
    except Exception as e:
        result["accessibility"] = None
        result["accessibility_error"] = f"{type(e).__name__}: {e}"
    try:
        result["screen"] = screen()
    except Exception as e:
        result["screen"] = None
        result["screen_error"] = f"{type(e).__name__}: {e}"
    result["mic"] = microphone()
    return result


def open_pane(kind: str) -> bool:
    """Open the System Settings page where the switch for `kind` lives."""
    url = PANES.get(kind)
    if not url:
        return False
    try:
        subprocess.run(["open", url], check=False, timeout=5)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    for name, value in snapshot().items():
        print(f"{name:>22}  {value}")
    print(f"{'process':>22}  {os.getpid()}")
