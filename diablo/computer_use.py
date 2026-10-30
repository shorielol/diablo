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

import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

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


# ─── What an action came to ──────────────────────────────────────────────────
#
# Every result is exactly one of five outcomes. A boolean "verified" could not
# say the thing that matters most here: that a click was sent and nothing
# proves what it did. "Something on screen changed" used to count as success,
# and a notification sliding in, a clock ticking or a capture that failed
# outright all produced a change.

CONFIRMED = "confirmed"            # sent, and the expected result is on screen now
UNVERIFIABLE = "unverifiable"      # sent, and nothing proves it did what it was for
SUSPECTED_NOOP = "suspected_noop"  # sent, and nothing readable changed
PARTIAL = "partial"                # some of it was sent: half a click, part of the text
REFUSED = "refused"                # not sent; `refusal` names the gate that stopped it
OUTCOMES = (CONFIRMED, UNVERIFIABLE, SUSPECTED_NOOP, PARTIAL, REFUSED)

# Why nothing was sent. Codes, so that a caller decides what to do next from
# a fact rather than by searching the sentence that was spoken.
SWITCHED_OFF = "switched_off"                    # the master switch
NO_CONFIRMATION = "confirmation_unavailable"     # nobody to ask
DECLINED = "declined"                            # the user said no
SENSITIVE_WINDOW = "sensitive_window"            # a password manager
OFF_DISPLAY = "off_display"                      # not a point on any screen
NO_WINDOW = "no_window"
FRAME_MISSING = "frame_missing"                  # no capture behind the coordinate
FRAME_INVALID = "frame_invalid"                  # a capture that cannot be read
SCREEN_CHANGED = "screen_changed"                # the screen moved since the capture
TARGET_NOT_FOUND = "target_not_found"
TARGET_DISABLED = "target_disabled"
TARGET_GONE = "target_gone"                      # there when chosen, gone now
TARGET_UNCERTAIN = "target_uncertain"            # a question nobody could answer
INPUT_UNAVAILABLE = "input_unavailable"          # the event could not be posted
NOTHING_TO_TYPE = "nothing_to_type"
FRAME_MISMATCH = "frame_mismatch"                # the point is not in its capture
FRAME_USED = "frame_used"                        # that capture already authorised a click
TARGET_CHANGED = "target_changed"                # not the control that was approved
ALREADY_TRIED = "already_tried"                  # pressed before without the result wanted
NOT_VALIDATED = "not_validated"                  # a point grounding_gate did not issue
WRONG_WINDOW = "wrong_window"                    # something else is on top at the point
NO_FIELD = "no_field"                            # nothing that takes text has focus

# What a click's record says when the window could not be read afterwards.
UNREAD = "I could not read the window afterwards."
REFUSALS = (SWITCHED_OFF, NO_CONFIRMATION, DECLINED, SENSITIVE_WINDOW,
            OFF_DISPLAY, NO_WINDOW, FRAME_MISSING, FRAME_INVALID,
            SCREEN_CHANGED, TARGET_NOT_FOUND, TARGET_DISABLED, TARGET_GONE,
            TARGET_UNCERTAIN, INPUT_UNAVAILABLE, NOTHING_TO_TYPE,
            FRAME_MISMATCH, FRAME_USED, TARGET_CHANGED, ALREADY_TRIED,
            NOT_VALIDATED, WRONG_WINDOW, NO_FIELD)

# Two candidates with the same name this close together are treated as the
# same control; further apart they are different controls sharing a label.
# Deliberately conservative: a wizard's "Next" in the same place on the next
# page counts as the same control, so if the first press could not be shown
# to work, the second waits for the user.
SAME_CONTROL_POINTS = 24.0
# The same label, found again by a different source, need not land on the same
# point: perception notes the accessibility and OCR centres of one control can
# sit 57pt apart. Within this distance the same name is the same control,
# whatever role it was described with.
SAME_LABEL_POINTS = 64.0


def legacy_verified(outcome: str) -> Optional[bool]:
    """
    The old three-valued `verified`, derived from an outcome.

    Only a confirmed result is True and only a suspected no-op is False.
    Unverifiable is None: not proven either way, which is what it means.
    """
    if outcome == CONFIRMED:
        return True
    if outcome == SUSPECTED_NOOP:
        return False
    return None


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
            # One click, in the task it was given for. A yes to "Dark mode" in
            # one task is not a yes to anything in the next.
            return self.uses == 0 and task == self.task
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
    granted, why, _code = _permission(action, task, scope, confirm)
    return granted, why


def _permission(action: str, task: str = "", scope: str = ALLOW_ONCE,
                confirm: Optional[Callable[[str], bool]] = None) -> Tuple[bool, str, str]:
    """`ask_for`, with the refusal code that says which answer it was."""
    global _GRANT
    if not ENABLED:
        return False, ("I am not allowed to control the computer. That is off "
                       "by default; you can turn it on in Diablo's settings."), SWITCHED_OFF
    if _GRANT.covers(task):
        return True, "", ""
    if confirm is None:
        return False, "I need you to confirm that before I touch anything.", NO_CONFIRMATION

    wording = (f"May I {action}? This will let me keep going for the rest of "
               f"this task." if scope == ALLOW_FOR_TASK else f"May I {action}?")
    if not confirm(wording):
        revoke()
        return False, "Left it alone.", DECLINED

    _GRANT = Grant(level=scope, task=task, granted_at=time.time(), uses=0)
    return True, "", ""


# ─── Posting the event ───────────────────────────────────────────────────────

CLICK_EVENTS = 2       # mouse down, mouse up


def _post_click(x: float, y: float) -> int:
    """
    A left click at a Quartz point. Returns how many of its events were posted.

    Quartz event coordinates are the same top-left global space CGWindowList
    reports, so a click on a display at negative x needs no special case. That
    is the payoff for keeping one canonical space.

    A count rather than a yes or no, because "the button went down and never
    came up" is not the same report as "nothing was sent".
    """
    try:
        import Quartz
    except ImportError:
        return 0
    posted = 0
    try:
        point = Quartz.CGPointMake(float(x), float(y))
        for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            event = Quartz.CGEventCreateMouseEvent(
                None, kind, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            posted += 1
            time.sleep(0.02)
    except Exception:
        pass
    return posted


@dataclass
class ActionResult:
    """
    What one click or one piece of typing came to.

    `ok` means the input was sent. `outcome` says what is known about its
    effect, and `refusal` says why nothing was sent when nothing was.
    `verified` is the older boolean, kept for its readers and always derived
    from `outcome` by `legacy_verified`.
    """

    ok: bool
    spoken: str
    verified: Optional[bool] = None
    change: Optional[Any] = None
    outcome: str = REFUSED
    refusal: str = ""


def _drop_unspent_once() -> None:
    """
    A one-time yes ends with the attempt it was given for.

    It used to survive the refusal that followed it and cover the next click
    on any control, in any task — so a yes given to "Dark mode" pressed
    "Airplane mode" after the target-changed check had refused the first.
    """
    if _GRANT.level == ALLOW_ONCE:
        # Spent or not. It used to count as spent the moment a post was
        # attempted, so a refusal for unavailable input left it standing.
        revoke()


def _grant_once(task: str) -> None:
    """The answer to a question already asked: one click, this task."""
    global _GRANT
    if not _GRANT.covers(task):
        _GRANT = Grant(level=ALLOW_ONCE, task=task, granted_at=time.time(), uses=0)


def _refused(code: str, spoken: str) -> ActionResult:
    _drop_unspent_once()
    return ActionResult(False, spoken, outcome=REFUSED, refusal=code)


def _sent(outcome: str, spoken: str, change: Optional[Any] = None) -> ActionResult:
    return ActionResult(True, spoken, verified=legacy_verified(outcome),
                        change=change, outcome=outcome)


def _judge(change, expect: str) -> str:
    """
    The outcome of an action, from what changed on screen.

    Only the expected words appearing confirms anything. A screen that moved
    without them is unverifiable, never success, and never a failure either:
    the click may well have worked in a way that cannot be read.
    """
    if change is None:
        return UNVERIFIABLE
    if change.same:
        return SUSPECTED_NOOP
    # A line that was already there with other numbers in it only re-rendered:
    # "Last saved 3 minutes ago" replacing "Last saved 2 minutes ago" is a
    # clock, not a save.
    before = {_shape(text) for text in getattr(change, "vanished", ()) or ()}
    if expect and any(_evidence(text, expect) for text in change.appeared
                      if _shape(text) not in before):
        return CONFIRMED
    return UNVERIFIABLE


# Words that turn a line naming the result into a line saying it did not
# happen, or has not happened yet: "Message not sent", "Dark mode could not be
# enabled", "Sent — tap to retry", "Will be sent when online".
_FAILED = re.compile(r"(?<!\w)(not|no longer|never|cannot|can't|couldn't|could not|"
                     r"didn't|did not|isn't|wasn't|won't|unable|failed|failure|"
                     r"error|denied|unsuccessful|retry|try again|pending|will be|"
                     r"waiting|queued|unsent)(?!\w)")

# macOS writes its apostrophes curly and Wi‑Fi with a non-breaking hyphen;
# OCR and the planner write the plain ones. "couldn’t" missed "couldn't", so
# "The document couldn’t be saved." confirmed "saved".
_PLAIN = {**dict.fromkeys(map(ord, "‐‑‒–—−"), "-"),
          ord("‘"): "'", ord("’"): "'"}


def _plain(text: str) -> str:
    """Lower case, single spaces, one spelling of each apostrophe and hyphen."""
    text = unicodedata.normalize("NFKC", text or "").translate(_PLAIN)
    return " ".join(text.lower().split())


def _shape(text: str) -> str:
    """A line with its numbers taken out, to recognise a re-render."""
    return re.sub(r"\d+", "#", _plain(text))


def _evidence(text: str, expect: str) -> bool:
    """The line names the expected result and does not say it failed."""
    if not _says(text, expect):
        return False
    wanted = _plain(expect)
    rest = re.sub(r"(?<!\w)" + re.escape(wanted) + r"(?!\w)", " ", _plain(text), count=1)
    return _FAILED.search(rest) is None


def _says(text: str, expect: str) -> bool:
    """
    Whether `text` contains `expect` as words, not as letters inside a word.

    The planner writes `expect`, and it is a 4B model: "on" is a plausible
    thing for it to expect after a toggle, and "on" is inside "Notification".
    """
    wanted = _plain(expect)
    if not wanted:
        return False
    return re.search(r"(?<!\w)" + re.escape(wanted) + r"(?!\w)", _plain(text)) is not None


def _label_of(described: str) -> str:
    """
    The name inside a description, without the role it was found as, and
    without the colon OCR tends to keep after a setting's name.
    """
    quoted = re.search(r"[“\"']([^”\"']+)[”\"']", described or "")
    return _plain(quoted.group(1) if quoted else described or "").strip(" :;,.")


def _same_control(described: str, x: float, y: float,
                  avoid: Optional[Iterable[Tuple[str, float, float]]]) -> bool:
    """
    Whether this control is one of `avoid`: the same name, in the same place.

    The name is compared without its role, because the same toggle can come
    back as a "switch" from accessibility and as "text" from OCR, and the
    place allows for the two sources disagreeing about its centre.
    """
    label = _label_of(described)
    for name, ax, ay in avoid or ():
        apart = math.hypot(x - ax, y - ay)
        if name == described and apart <= SAME_CONTROL_POINTS:
            return True
        if label and _label_of(name) == label and apart <= SAME_LABEL_POINTS:
            return True
    return False


# What the grounding gate's refusals mean here, so a caller decides its next
# move from the same codes act_on uses.
_GATE_REFUSALS = {
    "REJECT_DISABLED": TARGET_DISABLED,
    "REJECT_WRONG_WINDOW": WRONG_WINDOW,
    "REJECT_NO_TARGET": TARGET_GONE,
    "REJECT_UNVERIFIABLE": TARGET_UNCERTAIN,
    "REJECT_LABEL_MISMATCH": TARGET_CHANGED,
    "REJECT_ROLE": TARGET_CHANGED,
}
_STALE_DETAIL = {"gone": TARGET_GONE, "changed": TARGET_CHANGED, "moved": TARGET_CHANGED}


def _gate_refusal(validation) -> str:
    if validation.verdict == "REJECT_STALE_FRAME":
        return _STALE_DETAIL.get(validation.detail, SCREEN_CHANGED)
    return _GATE_REFUSALS.get(validation.verdict, TARGET_UNCERTAIN)


def click(target: Any, frame: Optional[_screen.ScreenFrame] = None,
          task: str = "", confirm: Optional[Callable[[str], bool]] = None,
          expect: str = "") -> ActionResult:
    """
    Press a control grounding_gate validated, having earned the right to, and
    then check it worked.

    There is no (x, y, label) form. It used to be one, and the label it was
    given decided whether the click counted as destructive: a point on
    "Delete All" passed in as "Save" got the ordinary permission, not the
    second yes. A coordinate now reaches the mouse only inside a
    ValidatedTarget, whose label is the one the application publishes for the
    control under it.

    Whatever goes wrong inside, a one-time yes does not outlive the attempt:
    an exception used to leave it unspent, ready for the next click anywhere.
    """
    try:
        return _click(target, frame, task, confirm, expect)
    except BaseException:
        _drop_unspent_once()
        raise


def _click(target: Any, frame: Optional[_screen.ScreenFrame],
           task: str, confirm: Optional[Callable[[str], bool]],
           expect: str) -> ActionResult:
    """
    click(), without the clean-up around it.

    `frame` is the capture the target was validated against, and a click
    without one is refused: there is no way to know whether the screen still
    looks like the screen the target describes, so gate 3 cannot run.
    """
    from skills import grounding_gate as _gate

    if not ENABLED:
        return _refused(SWITCHED_OFF, "I am not allowed to control the computer, Sir. "
                                      "That is switched off.")

    # Gate: a point nobody checked. Whatever made it, model or caller, it says
    # nothing about what is under it.
    if not _gate.issued(target):
        return _refused(NOT_VALIDATED, "I only press a control I have checked is really "
                                       "there, and that was a bare point.")
    x, y = target.centre
    label = target.label

    # Gate: is this even a place on a screen?
    if not _display.on_any_display(x, y):
        return _refused(OFF_DISPLAY, "That point is not on any display I can see, "
                                     "so I will not click it.")

    # Gate: a coordinate with no readable capture behind it describes nothing.
    # Checked before anyone is asked, so nobody is asked to approve a click
    # that was never going to happen.
    if frame is None:
        return _refused(FRAME_MISSING, "I have not looked at the screen for that, "
                                       "so I will not click it.")
    if not frame.ok:
        return _refused(FRAME_INVALID, "I could not read the screen that point came "
                                       "from, so I will not click it.")
    # The target was checked against one capture, and only that capture can
    # vouch for it.
    if getattr(frame, "frame_id", None) != target.frame_id:
        return _refused(FRAME_MISMATCH, "That control was checked against a different "
                                        "look at the screen, so I will not click it.")
    # A capture vouches for one click, at a point inside it. Anything else is
    # a coordinate with nothing behind it.
    if frame.width > 0 and frame.height > 0 and not (
            frame.x <= x < frame.x + frame.width and frame.y <= y < frame.y + frame.height):
        return _refused(FRAME_MISMATCH, "That point is not in the part of the screen "
                                        "I looked at, so I will not click it.")
    if getattr(frame, "spent", False):
        return _refused(FRAME_USED, "I have already clicked from that look at the screen. "
                                    "Let me look again first.")

    # Gate: the window we are about to click into, by the id the gate checked.
    from skills import screen_context as _sc
    window = _sc.window_by_id(target.window_id)
    if window is None:
        return _refused(TARGET_GONE, "The window that control was in has gone.")
    if window.get("sensitive"):
        return _refused(SENSITIVE_WINDOW, f"That is inside {window['app']}. I will not "
                                          f"click inside a password manager.")

    # Gate: harm needs its own yes, standing grant or not — judged on the
    # control's own label. A control with no name gets the same treatment,
    # because nothing says what it does.
    if not target.named:
        if confirm is None:
            return _refused(NO_CONFIRMATION, "That control has no name I can check. I need "
                                             "you to tell me it is the right one.")
        if not confirm("press the control I am pointing at? It has no name I can check"):
            revoke()
            return _refused(DECLINED, "Left it alone.")
    elif is_destructive(label):
        if confirm is None:
            return _refused(NO_CONFIRMATION, f"'{label}' looks like it cannot be undone. "
                                             f"I need you to confirm that one out loud.")
        if not confirm(f"click '{label[:60]}'? That one may not be undoable"):
            # A no ends any standing permission, as it does in ask_for.
            revoke()
            return _refused(DECLINED, "Left it alone.")
    else:
        granted, why, code = _permission(f"click '{label[:60]}'" if label else "click that",
                                         task=task, confirm=confirm)
        if not granted:
            return _refused(code, why)

    def capture():
        return _screen.capture_this_window(window)

    # Gate: is the screen still the screen the coordinate came from? A fresh
    # capture that cannot be read or compared is not a pass: it used to be
    # skipped, and the click went ahead on the old coordinate.
    if frame.is_stale(CLICK_MAX_AGE):
        fresh = capture()
        try:
            if not (fresh.ok and frame.fingerprint and fresh.fingerprint):
                return _refused(FRAME_INVALID, "I cannot tell whether the screen has "
                                               "changed since I looked, so I have not "
                                               "clicked anything.")
            drift = sum(abs(a - b) for a, b in zip(frame.fingerprint, fresh.fingerprint))
            if drift > 6 * len(frame.fingerprint):
                return _refused(SCREEN_CHANGED, "The screen has changed since I looked, "
                                                "Sir. Let me look again before I click "
                                                "anything.")
        finally:
            fresh.release()

    # Without a readable "before" there is nothing to verify against, and a
    # click that cannot be checked is not one to send.
    before = capture()
    if not before.ok:
        before.release()
        return _refused(FRAME_INVALID, "I could not read the screen just before "
                                       "clicking, so I have not clicked anything.")

    # Gate: the control itself, asked again now. The permission question took
    # seconds to speak and answer; the clock on the frame says nothing about
    # what happened in them. This asks the screen.
    still = _gate.revalidate(target)
    if not still.ok:
        before.release()
        return _refused(_gate_refusal(still), f"I have not clicked anything: {still.reason}.")

    _GRANT.uses += 1
    frame.spent = True
    who = label or "it"
    posted = _post_click(x, y)
    if posted == 0:
        before.release()
        return _refused(INPUT_UNAVAILABLE, "I could not send the click. Diablo needs "
                                           "Accessibility permission in System Settings.")
    if posted < CLICK_EVENTS:
        before.release()
        return _sent(PARTIAL, _report(PARTIAL, who, expect, None))

    # Verify, rather than report success because no exception was raised.
    time.sleep(0.6)
    after = capture()
    try:
        change = _compared(before, after, window)
        outcome = _judge(change, expect)
        return _sent(outcome, _report(outcome, who, expect, change), change)
    finally:
        before.release()
        after.release()


def _compared(before, after, window):
    """
    The change a click made, or None if it cannot be read.

    The click has already been sent by the time this runs, so a comparison
    that raises is not a reason to lose the outcome: it is an unverifiable
    click, and saying so is the whole point.
    """
    if not after.ok:
        return None
    try:
        return _screen.compare(before, after, window)
    except Exception:
        return None


def _report(outcome: str, who: str, expect: str, change) -> str:
    """What to say about a click that was sent, from its outcome alone."""
    if outcome == CONFIRMED:
        return f"Clicked {who}. {change.describe()}"
    if outcome == PARTIAL:
        return (f"I started to click {who} but the click did not finish sending, so I "
                f"have stopped. Please check it before I do anything else.")
    if outcome == SUSPECTED_NOOP:
        return (f"I clicked {who}, but nothing on screen changed. I have not clicked "
                f"it again.")
    if change is None:
        return (f"I clicked {who}, but I could not look at the window afterwards, so I "
                f"cannot say what happened.")
    if expect:
        return (f"I clicked {who} and the screen changed, but I cannot see {expect}, "
                f"so I cannot say it worked.")
    return f"I clicked {who}. Something changed, but I cannot confirm it did what you wanted."


def type_text(text: str, task: str = "",
              confirm: Optional[Callable[[str], bool]] = None) -> ActionResult:
    """
    Type into whatever has focus, having asked first.

    Deliberately says what it is about to type in the confirmation. "May I
    type?" is not a question anyone can answer responsibly.
    """
    if not ENABLED:
        return _refused(SWITCHED_OFF, "I am not allowed to control the computer, Sir.")
    if not text:
        return _refused(NOTHING_TO_TYPE, "There is nothing to type.")

    from skills import screen_context as _sc
    front = _sc.frontmost_window()
    if front and front.get("sensitive"):
        return _refused(SENSITIVE_WINDOW, f"{front['app']} is in front. I will not type "
                                          f"into a password manager.")

    # Keystrokes go to whatever has focus. With no text field focused they
    # are lost, and a focused button takes a space as a press. Measured: the
    # planner typed "3" with nothing focused, then saved an empty quantity.
    from skills import ax
    focus = ax.focused_element(ax._pid_for_window(front)) if front else None
    if focus is None or focus.role not in ax.EDITABLE_ROLES:
        return _refused(NO_FIELD, "Nothing that takes text has the keyboard focus, so I "
                                  "have not typed anything. The field needs clicking first.")

    granted, why, code = _permission(
        f"type \"{text[:80]}\" into {front['app'] if front else 'the front window'}",
        task=task, confirm=confirm)
    if not granted:
        return _refused(code, why)

    try:
        import Quartz
    except ImportError:
        return _refused(INPUT_UNAVAILABLE, "I cannot send keystrokes on this machine.")
    where = front['app'] if front else 'the front window'
    posted = 0
    _GRANT.uses += 1
    try:
        for chunk in [text[i:i + 20] for i in range(0, len(text), 20)]:
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            posted += 1
            up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            Quartz.CGEventKeyboardSetUnicodeString(up, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
            posted += 1
            time.sleep(0.03)
    except Exception:
        if posted == 0:
            return _refused(INPUT_UNAVAILABLE, "I could not send those keystrokes. Diablo "
                                               "needs Accessibility permission in System "
                                               "Settings.")
        # Some of it went in. Saying nothing was sent would be false, and
        # saying it was typed would be worse.
        return _sent(PARTIAL, f"I started typing into {where} but stopped partway, so "
                              f"only some of it went in. Please check it.")
    # Sent is not landed: read the field back. Only a field that now holds
    # exactly what was typed confirms it. Measured: unconfirmed typing made
    # the planner type "3" again, and "33" was saved. Whatever else the field
    # holds is said, so the next step sees it.
    time.sleep(0.15)
    after = ax.focused_element(ax._pid_for_window(front)) if front else None
    if after is not None and after.role in ax.EDITABLE_ROLES:
        if after.value == text:
            return _sent(CONFIRMED, f"I typed that into {where}, and the field reads "
                                    f"“{text[:40]}”.")
        if after.value:
            return _sent(UNVERIFIABLE, f"I typed that into {where}, but the field now "
                                       f"reads “{after.value[:40]}”, not just what I typed.")
    return _sent(UNVERIFIABLE, f"I typed that into {where}, but I cannot read it "
                               f"back to check it went in.")


# Commands chosen this process, keyed by (turn, app, menu path).
_CHOSEN: set = set()
# How long a menu command is given to show its effect.
MENU_SETTLE = 1.5


def choose_menu(app_reference: str, command: str, task: str = "",
                confirm: Optional[Callable[[str], bool]] = None) -> ActionResult:
    """
    Carry out one of an application's own menu commands: "new tab" in Google
    Chrome is File › New Tab, pressed through accessibility.

    Behind the same gates as a click: switched off unless allowed, never in a
    password manager, the person's yes before anything happens, a destructive
    command asked about by its own name, and the result looked at, never
    assumed. Only a command the app itself publishes can be chosen, so the
    model cannot name something into existence.
    """
    if not ENABLED:
        return _refused(SWITCHED_OFF, "I am not allowed to control the computer. That is "
                                      "off by default; you can turn it on in Diablo's "
                                      "settings.")
    from skills import app_menus, apps
    from skills import screen_context as _sc
    named = apps.resolve(app_reference)
    if named.status == apps.AMBIGUOUS:
        return _refused(TARGET_UNCERTAIN, apps.describe(named))
    if named.status != apps.RESOLVED:
        return _refused(TARGET_NOT_FOUND, apps.describe(named))
    app = named.app
    if app.name.lower() in _sc.SENSITIVE_APPS:
        return _refused(SENSITIVE_WINDOW, f"I won't work inside {app.name}.")
    if not named.running:
        return _refused(NO_WINDOW, f"{app.name} isn't open. I can open it first.")
    found = app_menus.find(app, command)
    if not found:
        return _refused(TARGET_NOT_FOUND, f"I couldn't find a “{command}” command in "
                                          f"{app.name}'s menus.")
    item = found[0]
    where = app_menus.route(item)
    if not item.enabled:
        return _refused(TARGET_DISABLED, f"{where} is there but greyed out right now.")

    if is_destructive(item.title):
        if confirm is None:
            return _refused(NO_CONFIRMATION, f"“{item.title}” looks like it cannot be "
                                             f"undone. I need you to confirm that one.")
        if not confirm(f"choose {item.title} in {app.name}? That one may not be undoable"):
            revoke()
            return _refused(DECLINED, "Left it alone.")
    else:
        granted, why, code = _permission(f"choose {where} in {app.name}", task=task,
                                         confirm=confirm)
        if not granted:
            return _refused(code, why)

    # One choice of a command per turn. Asked to open a tab in Zen, the agent
    # chose File › New Tab, read "nothing changed", and chose it again: two
    # tabs. A person asking again is a new turn and is not stopped.
    from skills import ax, trace
    key = (trace.current_turn(), app.bundle_id, item.path)
    if key in _CHOSEN:
        return _refused(ALREADY_TRIED, f"I already chose {where} in {app.name} just now, "
                                       f"so I won't do it twice. Ask me again if you "
                                       f"want another.")
    pid = app_menus._pid(app)

    def state():
        return ax.window_titles(pid)
    before = state()
    _GRANT.uses += 1
    if not app_menus._press(item):
        return _refused(INPUT_UNAVAILABLE, f"{app.name} didn't accept {where}. Diablo "
                                           f"needs Accessibility permission for that.")
    _CHOSEN.add(key)
    apps.note_in_use(app)
    deadline = time.time() + MENU_SETTLE
    while state() == before and time.time() < deadline:
        time.sleep(0.15)
    if state() == before:
        return _sent(SUSPECTED_NOOP, f"I chose {where} in {app.name}, but I can't see "
                                     f"anything change.")
    # Something changed. Nothing here proves it is what was wanted, so it is
    # reported as sent, and the caller's own check decides.
    return _sent(UNVERIFIABLE, f"I chose {where} in {app.name}.")


if __name__ == "__main__":
    print("computer use:", state())
    for label in ("Continue", "Delete all messages", "Send", "Search box", "Buy now"):
        print(f"  {label:24} destructive={is_destructive(label)}")


# ─── Can I, and should I ─────────────────────────────────────────────────────
#
# Two different questions, and collapsing them is how an assistant ends up
# clicking something it was technically allowed to click. Permission answers
# the first. Everything below answers the second, from evidence: how sure the
# perception layer is, whether anything else looked equally plausible, what
# the action would cost if it is wrong, and whether the screen still looks
# like the screen the coordinate came from.

LOW = "low"           # opens a menu, switches a tab, scrolls. Undone by looking away.
MEDIUM = "medium"     # edits, renames, moves, changes a setting. Undone with effort.
HIGH = "high"         # deletes, sends, buys, submits. Not undone.

ACT = "act"
ASK = "ask"
REFUSE = "refuse"

# How sure the perception layer has to be before Diablo acts unasked, per
# risk class. These are not tuned parameters so much as a statement of
# policy: the more a mistake costs, the more evidence it takes, and HIGH is
# deliberately unreachable — anything irreversible is confirmed out loud
# every time, however certain the machine claims to be.
NEEDED = {LOW: 0.80, MEDIUM: 0.90, HIGH: 1.01}


def risk_of(label: str, action: str = "click") -> str:
    """How much a mistake would cost."""
    if action in ("type", "type_text") or is_destructive(label):
        return HIGH if is_destructive(label) else MEDIUM
    return LOW


@dataclass
class Decision:
    verdict: str
    reason: str
    candidate: Any = None
    risk: str = LOW

    @property
    def acting(self) -> bool:
        return self.verdict == ACT


def decide(reading, risk: str = LOW, ambiguity_question: str = "") -> Decision:
    """
    Whether the evidence supports acting, asking, or declining.

    Uncertainty makes Diablo hesitate, never improvise. Every branch that does
    not act says which observation stopped it, because "I am not confident" is
    not a reason anyone can check.
    """
    if not getattr(reading, "found", False):
        return Decision(REFUSE, "I cannot find that control on screen.", risk=risk)

    best = reading.best
    if best.enabled is False:
        return Decision(REFUSE,
                        f"The {best.describe()} is there but the application "
                        f"reports it as disabled.", best, risk)

    if reading.ambiguous:
        return Decision(ASK, ambiguity_question or reading.question(), best, risk)

    needed = NEEDED.get(risk, 0.9)
    if best.confidence < needed:
        return Decision(
            ASK,
            f"I think it is {best.describe()}, but I am not certain enough to "
            f"{'do that on my own' if risk != LOW else 'click it'}. Shall I?",
            best, risk)

    if risk == HIGH:
        return Decision(ASK,
                        f"That would {('send, delete or submit something' )}. "
                        f"I found {best.describe()}. Do you want me to?",
                        best, risk)
    return Decision(ACT, f"{best.describe()}, {best.why()}", best, risk)


# ─── What happened, and why ──────────────────────────────────────────────────

@dataclass
class ActionRecord:
    """
    One attempt to touch the computer, with everything needed to explain it.

    Kept because "why did you click that" has to have an answer made of
    observations rather than of a model's recollection. It is also what a
    later undo, replay or evaluation run would be built on.
    """

    task: str = ""
    at: float = 0.0
    app: str = ""
    window_title: str = ""
    fingerprint: List[int] = field(default_factory=list)

    intent: str = ""
    target: str = ""
    target_sources: List[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)

    # What the vision layer contributed, when it was asked at all. Kept
    # separate from the fused confidence on purpose: "the model was 0.88 sure
    # it is a gear" and "the target is 0.88 likely correct" are different
    # claims, and collapsing them hides which one was weak.
    vision_label: str = ""
    vision_confidence: Optional[float] = None
    vision_crop: Optional[Tuple[float, float, float, float]] = None

    risk: str = LOW
    verdict: str = ""
    permission: str = DENY

    action: str = ""
    x: float = 0.0
    y: float = 0.0

    expected: str = ""
    observed: str = ""
    verified: Optional[bool] = None
    retried: bool = False
    spoken: str = ""

    # One of OUTCOMES once the attempt is over, and the gate's code when it
    # was refused. Empty on records built by hand, which then fall back to
    # `verified`.
    outcome: str = ""
    refusal: str = ""

    def settle(self, outcome: str, spoken: str, refusal: str = "") -> "ActionRecord":
        """Close the record with one outcome; `verified` follows from it."""
        self.outcome = outcome
        self.refusal = refusal
        self.verified = legacy_verified(outcome)
        self.spoken = spoken
        if outcome == REFUSED:
            self.action = ""          # nothing was sent, so nothing was clicked
        return self

    def explain(self) -> str:
        """Why Diablo did that, in one sentence, from what it observed."""
        if not self.action:
            return f"I did not act: {self.spoken}"
        who = self.target or "that control"
        because = self.evidence[0] if self.evidence else "it matched what you asked for"
        if self.outcome == UNVERIFIABLE:
            outcome = "but I could not confirm it did what you wanted"
        elif self.outcome == PARTIAL:
            outcome = "but it did not finish sending"
        else:
            outcome = ("and the screen changed as expected" if self.verified
                       else "but the screen did not change the way I expected"
                       if self.verified is False else "")
        tail = f", {outcome}" if outcome else ""
        if self.action == "type":
            # Typing has no target and no evidence; "I clicked that control"
            # was the wrong sentence for it.
            into = f" into {self.app}" if self.app else ""
            return f"You asked me to {self.intent}. I typed it{into}{tail}."
        return f"You asked me to {self.intent}. I clicked {who} because {because}{tail}."

    def debug(self) -> str:
        """Everything, for a developer. Never shown to a user."""
        lines = [
            f"  intent        {self.intent!r}",
            f"  application   {self.app} — {self.window_title[:44]}",
            f"  target        {self.target}",
            f"  sources       {'+'.join(self.target_sources) or 'none'}",
            f"  confidence    {self.confidence}",
        ]
        if self.vision_label:
            lines += [
                f"  vision        {self.vision_label!r} at {self.vision_confidence}",
                f"  vision crop   {tuple(round(v) for v in (self.vision_crop or ()))}",
            ]
        lines += [
            f"  risk          {self.risk}",
            f"  verdict       {self.verdict}",
            f"  permission    {self.permission}",
            f"  action        {self.action or 'none'}"
            + (f" at ({self.x:.0f},{self.y:.0f})" if self.action else ""),
            f"  expected      {self.expected or 'any change'}",
            f"  observed      {self.observed or '—'}",
            f"  outcome       {self.outcome or '—'}"
            + (f" ({self.refusal})" if self.refusal else ""),
            f"  verified      {self.verified}",
            f"  retried       {self.retried}",
        ]
        for item in self.evidence:
            lines.append(f"  evidence      {item}")
        return "\n".join(lines)


_history: List[ActionRecord] = []


def history(limit: int = 20) -> List[ActionRecord]:
    return _history[-limit:]


def last_action() -> Optional[ActionRecord]:
    return _history[-1] if _history else None


def _remember(record: ActionRecord) -> ActionRecord:
    _history.append(record)
    if len(_history) > 100:
        del _history[:50]
    return record


# ─── The golden path ─────────────────────────────────────────────────────────

# No automatic retry. There used to be one, fired whenever a click left the
# screen unchanged — and a click that changes nothing visible is exactly what
# a toggle, a checkbox or a "mark as read" does when it works. The second
# click undid the first. Trying again is the user's decision, or a planner's
# made from a fresh look, never a reflex. Kept as a named policy so the
# bound is stated rather than implied.
MAX_RETRIES = 0


def act_on(intent: str, task: str = "", expect: str = "",
           window: Optional[Dict[str, Any]] = None,
           confirm: Optional[Callable[[str], bool]] = None,
           risk: Optional[str] = None,
           avoid: Optional[Iterable[Tuple[str, float, float]]] = None) -> ActionRecord:
    """
    Understand, locate, decide, act, verify — and report rather than repeat.

    An exception still propagates, but never with a one-time yes left behind.
    """
    try:
        return _act_on(intent, task, expect, window, confirm, risk, avoid)
    except BaseException:
        _drop_unspent_once()
        raise


def _act_on(intent: str, task: str, expect: str,
            window: Optional[Dict[str, Any]],
            confirm: Optional[Callable[[str], bool]],
            risk: Optional[str],
            avoid: Optional[Iterable[Tuple[str, float, float]]]) -> ActionRecord:
    """
    act_on(), without the clean-up around it.

    The whole loop in one place so that no caller can skip the middle of it.
    Every exit writes an ActionRecord with exactly one outcome, including the
    exits that do nothing, because "Diablo declined and here is what it saw"
    is as much a result as a click is.

    `avoid` lists controls (description, x, y) already pressed in this task
    without the result wanted. One of them is refused rather than pressed
    again, however the request for it was worded.
    """
    from skills import perception
    from skills import screen as _screen
    from skills import screen_context as _sc

    record = ActionRecord(task=task or intent, at=time.time(), intent=intent)

    def refuse(code: str, spoken: str) -> ActionRecord:
        if record.verdict not in (ASK, REFUSE):
            record.verdict = REFUSE
        _drop_unspent_once()
        return _remember(record.settle(REFUSED, spoken, code))

    if not ENABLED:
        return refuse(SWITCHED_OFF, "I am not allowed to control the computer, Sir. "
                                    "That is switched off.")

    target_window = window or _sc.frontmost_window()
    if not target_window:
        return refuse(NO_WINDOW, "I cannot see a window to work in.")
    if target_window.get("sensitive"):
        return refuse(SENSITIVE_WINDOW, f"{target_window['app']} is a password manager. "
                                        f"I will not touch it.")

    record.app = target_window.get("app", "")
    record.window_title = target_window.get("title", "")

    # A frame, and a validity gate before anything reads it. A window on
    # another Space captures as well-formed solid black, and perceiving
    # nothing in it is not the same as there being nothing there.
    frame = _screen.capture_this_window(target_window)
    try:
        if not frame.ok:
            return refuse(FRAME_INVALID, frame.problem.capitalize() + ".")
        record.fingerprint = list(frame.fingerprint)
    finally:
        frame.release()

    reading = perception.read(intent, target_window)
    if reading.best:
        record.target = reading.best.describe()
        record.target_sources = sorted(set(reading.best.sources))
        record.confidence = round(reading.best.confidence, 3)
        record.evidence = list(reading.best.evidence)
        seen = getattr(reading.best, "vision", None)
        if seen is not None:
            record.vision_label = seen.label
            record.vision_confidence = round(seen.confidence, 3)
            record.vision_crop = seen.crop

    chosen_risk = risk or risk_of(reading.best.label if reading.best else "", "click")
    record.risk = chosen_risk
    decision = decide(reading, chosen_risk)
    record.verdict = decision.verdict

    if decision.verdict == REFUSE:
        found = getattr(reading, "found", False)
        return refuse(TARGET_DISABLED if found else TARGET_NOT_FOUND, decision.reason)

    # Before anyone is asked: there is no point asking to press a control
    # that will be refused as already tried. Checked again after the fresh
    # look, because the control can move between the two.
    if reading.best is not None and _same_control(
            reading.best.describe(), *reading.best.centre, avoid):
        return refuse(ALREADY_TRIED, f"I have already pressed {reading.best.describe()} "
                                     f"without seeing the result you wanted, so I will "
                                     f"not press it again unless you tell me to.")

    if decision.verdict == ASK:
        # An ambiguous or low-confidence target is a question, not a coin
        # toss. If there is nobody to ask, that is a refusal.
        if confirm is None:
            return refuse(TARGET_UNCERTAIN, decision.reason)
        if not confirm(decision.reason):
            revoke()                    # a no ends any standing permission
            return refuse(DECLINED, "Left it alone.")
        # That question was the permission. Asking "May I click it?" straight
        # after "Shall I?" asked the same thing twice. And from here on the
        # decision is to act: a refusal after this yes is about the screen,
        # not a question still waiting for the user.
        _grant_once(task or intent)
        record.verdict = ACT

    granted, why, code = _permission(f"click {record.target or 'that'}",
                                     task=task or intent, confirm=confirm)
    record.permission = _GRANT.level
    if not granted:
        record.verdict = REFUSE
        return refuse(code, why)

    # Freshness. The coordinate was chosen from a frame that is now a second
    # or two old and a confirmation has been spoken since. Locate again and
    # act on what is there NOW — if the control has moved, the new position is
    # the right one; if it has gone, there is nothing to click.
    fresh = perception.read(intent, _sc.window_by_id(target_window.get("id", 0))
                            or target_window)
    if not fresh.found:
        record.verdict = REFUSE
        return refuse(TARGET_GONE, "That control was there a moment ago and is not "
                                   "now. I have not clicked anything.")
    # The approval was for one control. Only `found` used to be checked here,
    # so a different control in its place — or the same one now ambiguous or
    # disabled — was pressed on the strength of a yes given to something else.
    if fresh.ambiguous:
        return refuse(TARGET_UNCERTAIN, (getattr(fresh, "question", lambda: "")() or
                                         "There is more than one thing that could be "
                                         "it now, so I have not clicked anything."))
    target = fresh.best
    approved = reading.best.describe() if reading.best else ""
    if approved and target.describe() != approved:
        return refuse(TARGET_CHANGED, f"What is there now is {target.describe()}, not "
                                      f"the {approved} you agreed to. I have not "
                                      f"clicked anything.")
    if target.enabled is False:
        return refuse(TARGET_DISABLED, f"The {target.describe()} is there but disabled "
                                       f"now, so clicking it would do nothing.")
    x, y = _press_point(target)
    record.x, record.y = x, y
    if _same_control(target.describe(), x, y, avoid):
        return refuse(ALREADY_TRIED, f"I have already pressed {target.describe()} "
                                     f"without seeing the result you wanted, so I will "
                                     f"not press it again unless you tell me to.")

    from skills import display as _display
    if not _display.on_any_display(x, y):
        record.verdict = REFUSE
        return refuse(OFF_DISPLAY, "That control is not on any display I can see.")

    # Without a readable "before" there is nothing to verify against.
    before = _screen.capture_this_window(target_window)
    if not before.ok:
        before.release()
        record.verdict = REFUSE
        return refuse(FRAME_INVALID, "I could not read the window just before "
                                     "clicking, so I have not clicked anything.")

    # The last look. Perception found this control in the window's own tree
    # and in its pixels, and neither can see another window lying over it, so
    # ask the system what is frontmost at the point: this control, enabled,
    # in this window, or nothing is sent.
    from skills import grounding_gate as _gate
    on_top = _gate.check_point(x, y, target.label, target.role or "", target_window)
    if not on_top.ok:
        before.release()
        record.verdict = REFUSE
        return refuse(_gate_refusal(on_top), f"I have not clicked anything: {on_top.reason}.")

    record.action = "click"
    record.expected = expect
    who = record.target or "it"
    _GRANT.uses += 1
    posted = _post_click(x, y)
    if posted == 0:
        before.release()
        record.verdict = REFUSE
        return refuse(INPUT_UNAVAILABLE, "I could not send the click. Diablo needs "
                                         "Accessibility permission in System Settings.")
    if posted < CLICK_EVENTS:
        before.release()
        return _remember(record.settle(PARTIAL, _report(PARTIAL, who, expect, None)))

    # Verify against the screen, not against the fact that an event was sent.
    time.sleep(0.7)
    after = _screen.capture_this_window(
        _sc.window_by_id(target_window.get("id", 0)) or target_window)
    try:
        change = _compared(before, after, target_window)
        record.observed = change.describe() if change is not None else UNREAD
    finally:
        before.release()
        after.release()

    # Reported, never retried: a second click on a toggle undoes the first.
    outcome = _judge(change, expect)
    if outcome != CONFIRMED:
        # Focusing a field or ticking a box changes no words, so the text
        # comparison calls it a no-op, and the no-op went on the avoid list:
        # the next order's Quantity field was then refused. The control's own
        # state says what the click did.
        state = _state_change(getattr(on_top, "found", None), x, y)
        if state:
            record.observed = state
            return _remember(record.settle(CONFIRMED, f"Clicked {who}. {state}."))
    return _remember(record.settle(outcome, _report(outcome, who, expect, change)))


def _press_point(target: Any) -> Tuple[float, float]:
    """
    Where to press a control: its centre, except for a checkbox or radio
    button wider than it is tall. Those publish a frame that runs well past
    the box and its title, and AppKit ignores a click on the empty part.
    Measured: the centre of a 160-point Priority checkbox did nothing. The
    box is at the leading edge, a square the height of the frame. The point
    stays inside the control, so check_point still vouches for it, and a
    mistake here can only be a no-op, never a different control.
    """
    x, y = target.centre
    role = getattr(target, "role", "") or ""
    width, height = getattr(target, "width", 0.0) or 0.0, getattr(target, "height", 0.0) or 0.0
    if role in ("AXCheckBox", "AXRadioButton") and height > 0 and width > 1.5 * height:
        return (target.x + height / 2.0, y)
    return (x, y)


def _state_change(before: Optional[Any], x: float, y: float) -> str:
    """
    What accessibility says a click did to the control under it, or "".

    Two things only, both read from the application: a text field that now
    has the keyboard focus, and a checkbox or radio button whose value is not
    what it was just before the click.
    """
    from skills import ax
    hit = ax.hit_test(x, y)
    now = hit.element if hit else None
    if now is None:
        return ""
    if now.role in ax.EDITABLE_ROLES:
        focus = ax.focused_element(hit.pid)
        if focus is not None and focus.role == now.role and _same_place(focus, now):
            return f"{now.describe()} has the keyboard focus"
        return ""
    if (now.role in ax.TOGGLE_ROLES and before is not None
            and getattr(before, "role", "") == now.role and _same_place(before, now)
            and before.state is not None and now.state is not None
            and before.state != now.state):
        return f"{now.describe()} is now {'on' if now.state else 'off'}"
    return ""


def _same_place(a: Any, b: Any, slack: float = 2.0) -> bool:
    return all(abs(getattr(a, k) - getattr(b, k)) <= slack
               for k in ("x", "y", "width", "height"))
