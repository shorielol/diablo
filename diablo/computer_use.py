"""
D.I.A.B.L.O. — permission to touch the computer, and what it costs to get it.

The rule this module exists to enforce
--------------------------------------
Being allowed to LOOK at the screen is not being allowed to USE it. Those are
different powers and they are granted separately. Nothing in the vision path
can reach a click, because a click only happens through this module, and this
module does not care what the vision path concluded — it checks its own grant.

Four gates, all in code
-----------------------
The local model is 4B and has been measured ignoring instructions it was given
in the same prompt. So not one of these is a sentence in a system prompt:

    1. the master switch        off unless explicitly turned on
    2. a live grant             once, or for this task, never implicit
    3. a fresh, matching screen  the pixels under the coordinate now are the
                                 pixels that were there when it was chosen
    4. a confirmation for harm   destructive targets ask again, every time,
                                 even mid-task with a standing grant

Gate 3 is the one people skip. A coordinate is chosen from a capture; speaking
the sentence that offers the click takes two or three seconds; in that time a
notification banner can slide in over the button. Clicking the old coordinate
then clicks the banner. So the screen is re-captured immediately before the
event is posted and compared against the frame the coordinate came from. If it
moved, the click does not happen.
"""

import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config  # noqa: E402
from skills import display as _display  # noqa: E402
from skills import screen as _screen  # noqa: E402

# The master switch. Off. Turning it on is a deliberate act by the user in
# .env, not something Diablo can do for itself and not something that happens
# because a model decided it would be helpful.
ENABLED = config.env_int("DIABLO_COMPUTER_USE", 0) == 1

DENY = "deny"
ALLOW_ONCE = "allow_once"
ALLOW_FOR_TASK = "allow_for_task"

# How fresh the screen must be for a click. Far tighter than reading, because
# a stale reading is a wrong sentence and a stale click is a wrong action.
CLICK_MAX_AGE = config.env_float("DIABLO_CLICK_MAX_AGE", 1.5, low=0.2, high=10.0)

# Things that cannot be undone by clicking again. A standing grant covers
# "click the search box" for a whole task; it never covers these.
DESTRUCTIVE = (
    "delete", "remove", "erase", "trash", "discard", "wipe", "format",
    "send", "reply", "forward", "post", "publish", "tweet", "share",
    "buy", "purchase", "order", "pay", "checkout", "subscribe", "transfer",
    "submit", "confirm", "accept", "agree", "sign out", "log out",
    "deactivate", "close account", "unsubscribe", "install", "uninstall",
    "overwrite", "replace", "reset", "restore", "empty",
)


def is_destructive(label: str) -> bool:
    """Whether clicking a thing with this label could be regretted."""
    lowered = (label or "").strip().lower()
    return any(word in lowered for word in DESTRUCTIVE)


@dataclass
class Grant:
    """Permission to act, with a scope and an expiry."""

    level: str = DENY
    task: str = ""
    granted_at: float = 0.0
    uses: int = 0

    def covers(self, task: str) -> bool:
        if self.level == ALLOW_FOR_TASK:
            # A standing grant belongs to one task and dies with it. It also
            # expires on its own, so a task that stalls does not leave the
            # computer clickable for the rest of the day.
            return bool(task) and task == self.task and (time.time() - self.granted_at) < 600
        if self.level == ALLOW_ONCE:
            return self.uses == 0
        return False


_GRANT = Grant()


def revoke() -> None:
    """Drop every standing permission. Called when a task ends or is cancelled."""
    global _GRANT
    _GRANT = Grant()


def state() -> Dict[str, Any]:
    """What is currently permitted, for the user to see rather than infer."""
    return {
        "enabled": ENABLED,
        "level": _GRANT.level,
        "task": _GRANT.task,
        "age": round(time.time() - _GRANT.granted_at, 1) if _GRANT.granted_at else None,
        "uses": _GRANT.uses,
    }


def ask_for(action: str, task: str = "", scope: str = ALLOW_ONCE,
            confirm: Optional[Callable[[str], bool]] = None) -> Tuple[bool, str]:
    """
    Ask the user, out loud, for permission to do one thing.

    Returns (granted, spoken reason if not). There is no path that grants
    permission without `confirm` returning True — an absent confirm callback is
    a refusal, not a default yes, because the callback is absent exactly when
    nobody is listening.
    """
    global _GRANT
    if not ENABLED:
        return False, ("I am not allowed to control the computer. That is off "
                       "by default; you can turn it on in Diablo's settings.")
    if _GRANT.covers(task):
        return True, ""
    if confirm is None:
        return False, "I need you to confirm that before I touch anything."

    wording = (f"May I {action}? This will let me keep going for the rest of "
               f"this task." if scope == ALLOW_FOR_TASK else f"May I {action}?")
    if not confirm(wording):
        revoke()
        return False, "Left it alone."

    _GRANT = Grant(level=scope, task=task, granted_at=time.time(), uses=0)
    return True, ""


# ─── Posting the event ───────────────────────────────────────────────────────

def _post_click(x: float, y: float) -> bool:
    """
    A left click at a Quartz point.

    Quartz event coordinates are the same top-left global space CGWindowList
    reports, so a click on a display at negative x needs no special case. That
    is the payoff for keeping one canonical space.
    """
    try:
        import Quartz
    except ImportError:
        return False
    try:
        point = Quartz.CGPointMake(float(x), float(y))
        for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            event = Quartz.CGEventCreateMouseEvent(
                None, kind, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.02)
        return True
    except Exception:
        return False


@dataclass
class ActionResult:
    ok: bool
    spoken: str
    verified: Optional[bool] = None
    change: Optional[Any] = None


def click(x: float, y: float, label: str = "", frame: Optional[_screen.ScreenFrame] = None,
          task: str = "", confirm: Optional[Callable[[str], bool]] = None,
          expect: str = "") -> ActionResult:
    """
    Click a point, having earned the right to, and then check it worked.

    `frame` is the capture the coordinate was read from. It is not optional in
    spirit: without it there is no way to know whether the screen still looks
    like the screen the coordinate describes, and gate 3 cannot run.
    """
    if not ENABLED:
        return ActionResult(False, "I am not allowed to control the computer, Sir. "
                                   "That is switched off.")

    # Gate: is this even a place on a screen?
    if not _display.on_any_display(x, y):
        return ActionResult(False, "That point is not on any display I can see, "
                                   "so I will not click it.")

    # Gate: the window we are about to click into.
    screen_at = _display.for_point(x, y)
    window = None
    from skills import screen_context as _sc
    for candidate in _sc.list_windows():
        if (candidate["x"] <= x < candidate["x"] + candidate["width"]
                and candidate["y"] <= y < candidate["y"] + candidate["height"]):
            window = candidate
            break
    if window and window.get("sensitive"):
        return ActionResult(False, f"That is inside {window['app']}. I will not "
                                   f"click inside a password manager.")

    # Gate: harm needs its own yes, standing grant or not.
    if is_destructive(label):
        if confirm is None:
            return ActionResult(False, f"'{label}' looks like it cannot be undone. "
                                       f"I need you to confirm that one out loud.")
        if not confirm(f"click '{label[:60]}'? That one may not be undoable"):
            return ActionResult(False, "Left it alone.")
    else:
        granted, why = ask_for(f"click '{label[:60]}'" if label else "click that",
                               task=task, confirm=confirm)
        if not granted:
            return ActionResult(False, why)

    # Gate: is the screen still the screen the coordinate came from?
    if frame is not None:
        if frame.is_stale(CLICK_MAX_AGE):
            fresh = (_screen.capture_this_window(window) if window
                     else _screen.capture_display(screen_at.index if screen_at else None))
            try:
                if fresh.ok and frame.fingerprint and fresh.fingerprint:
                    drift = sum(abs(a - b) for a, b in
                                zip(frame.fingerprint, fresh.fingerprint))
                    if drift > 6 * len(frame.fingerprint):
                        return ActionResult(
                            False, "The screen has changed since I looked, Sir. "
                                   "Let me look again before I click anything.")
            finally:
                fresh.release()

    before = (_screen.capture_this_window(window) if window
              else _screen.capture_display(screen_at.index if screen_at else None))

    _GRANT.uses += 1
    if not _post_click(x, y):
        before.release()
        return ActionResult(False, "I could not send the click. Diablo needs "
                                   "Accessibility permission in System Settings.")

    # Verify, rather than report success because no exception was raised.
    time.sleep(0.6)
    after = (_screen.capture_this_window(window) if window
             else _screen.capture_display(screen_at.index if screen_at else None))
    try:
        change = _screen.compare(before, after, window)
        if change.same:
            return ActionResult(True, f"I clicked {label or 'it'}, but nothing on "
                                      f"screen changed. That may not have worked.",
                                verified=False, change=change)
        if expect:
            landed = any(expect.lower() in text.lower() for text in change.appeared)
            if not landed:
                return ActionResult(True, f"I clicked {label or 'it'} and the screen "
                                          f"changed, but I cannot see {expect}.",
                                    verified=False, change=change)
        return ActionResult(True, f"Clicked {label or 'it'}. {change.describe()}",
                            verified=True, change=change)
    finally:
        before.release()
        after.release()


def type_text(text: str, task: str = "",
              confirm: Optional[Callable[[str], bool]] = None) -> ActionResult:
    """
    Type into whatever has focus, having asked first.

    Deliberately says what it is about to type in the confirmation. "May I
    type?" is not a question anyone can answer responsibly.
    """
    if not ENABLED:
        return ActionResult(False, "I am not allowed to control the computer, Sir.")
    if not text:
        return ActionResult(False, "There is nothing to type.")

    from skills import screen_context as _sc
    front = _sc.frontmost_window()
    if front and front.get("sensitive"):
        return ActionResult(False, f"{front['app']} is in front. I will not type "
                                   f"into a password manager.")

    granted, why = ask_for(f"type \"{text[:80]}\" into {front['app'] if front else 'the front window'}",
                           task=task, confirm=confirm)
    if not granted:
        return ActionResult(False, why)

    try:
        import Quartz
    except ImportError:
        return ActionResult(False, "I cannot send keystrokes on this machine.")
    try:
        for chunk in [text[i:i + 20] for i in range(0, len(text), 20)]:
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            Quartz.CGEventKeyboardSetUnicodeString(up, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
            time.sleep(0.03)
    except Exception:
        return ActionResult(False, "I could not send those keystrokes. Diablo needs "
                                   "Accessibility permission in System Settings.")
    _GRANT.uses += 1
    return ActionResult(True, f"Typed that into {front['app'] if front else 'the front window'}.")


if __name__ == "__main__":
    print("computer use:", state())
    for label in ("Continue", "Delete all messages", "Send", "Search box", "Buy now"):
        print(f"  {label:24} destructive={is_destructive(label)}")
