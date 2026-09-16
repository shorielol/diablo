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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

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

    def explain(self) -> str:
        """Why Diablo did that, in one sentence, from what it observed."""
        if not self.action:
            return f"I did not act: {self.spoken}"
        who = self.target or "that control"
        because = self.evidence[0] if self.evidence else "it matched what you asked for"
        outcome = ("and the screen changed as expected" if self.verified
                   else "but the screen did not change the way I expected"
                   if self.verified is False else "")
        return (f"You asked me to {self.intent}. I clicked {who} because "
                f"{because}{', ' + outcome if outcome else ''}.")

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

# One automatic retry, then stop and ask. A second blind attempt is how a
# wrong coordinate becomes two wrong clicks, and a loop is how it becomes
# forty.
MAX_RETRIES = config.env_int("DIABLO_ACTION_RETRIES", 1, low=0, high=3)


def act_on(intent: str, task: str = "", expect: str = "",
           window: Optional[Dict[str, Any]] = None,
           confirm: Optional[Callable[[str], bool]] = None,
           risk: Optional[str] = None,
           _attempt: int = 0) -> ActionRecord:
    """
    Understand, locate, decide, act, verify — and recover rather than repeat.

    The whole loop in one place so that no caller can skip the middle of it.
    Every exit writes an ActionRecord, including the exits that do nothing,
    because "Diablo declined and here is what it saw" is as much a result as a
    click is.
    """
    from skills import perception
    from skills import screen as _screen
    from skills import screen_context as _sc

    record = ActionRecord(task=task or intent, at=time.time(), intent=intent)

    if not ENABLED:
        record.spoken = ("I am not allowed to control the computer, Sir. "
                         "That is switched off.")
        record.verdict = REFUSE
        return _remember(record)

    target_window = window or _sc.frontmost_window()
    if not target_window:
        record.spoken = "I cannot see a window to work in."
        record.verdict = REFUSE
        return _remember(record)
    if target_window.get("sensitive"):
        record.spoken = (f"{target_window['app']} is a password manager. "
                         f"I will not touch it.")
        record.verdict = REFUSE
        return _remember(record)

    record.app = target_window.get("app", "")
    record.window_title = target_window.get("title", "")

    # A frame, and a validity gate before anything reads it. A window on
    # another Space captures as well-formed solid black, and perceiving
    # nothing in it is not the same as there being nothing there.
    frame = _screen.capture_this_window(target_window)
    try:
        if not frame.ok:
            record.spoken = frame.problem.capitalize() + "."
            record.verdict = REFUSE
            return _remember(record)
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
        record.spoken = decision.reason
        return _remember(record)

    if decision.verdict == ASK:
        # An ambiguous or low-confidence target is a question, not a coin
        # toss. If there is nobody to ask, that is a refusal.
        if confirm is None:
            record.spoken = decision.reason
            return _remember(record)
        if not confirm(decision.reason):
            record.spoken = "Left it alone."
            return _remember(record)

    granted, why = ask_for(f"click {record.target or 'that'}",
                           task=task or intent, confirm=confirm)
    record.permission = _GRANT.level
    if not granted:
        record.spoken = why
        record.verdict = REFUSE
        return _remember(record)

    # Freshness. The coordinate was chosen from a frame that is now a second
    # or two old and a confirmation has been spoken since. Locate again and
    # act on what is there NOW — if the control has moved, the new position is
    # the right one; if it has gone, there is nothing to click.
    fresh = perception.read(intent, _sc.window_by_id(target_window.get("id", 0))
                            or target_window)
    if not fresh.found:
        record.spoken = ("That control was there a moment ago and is not now. "
                         "I have not clicked anything.")
        record.verdict = REFUSE
        return _remember(record)
    target = fresh.best
    x, y = target.centre
    record.x, record.y = x, y

    from skills import display as _display
    if not _display.on_any_display(x, y):
        record.spoken = "That control is not on any display I can see."
        record.verdict = REFUSE
        return _remember(record)

    before = _screen.capture_this_window(target_window)
    record.action = "click"
    record.expected = expect
    _GRANT.uses += 1
    if not _post_click(x, y):
        before.release()
        record.spoken = ("I could not send the click. Diablo needs "
                         "Accessibility permission in System Settings.")
        record.verified = False
        return _remember(record)

    # Verify against the screen, not against the fact that an event was sent.
    time.sleep(0.7)
    after = _screen.capture_this_window(
        _sc.window_by_id(target_window.get("id", 0)) or target_window)
    try:
        change = _screen.compare(before, after, target_window)
        record.observed = change.describe()
        if change.same:
            record.verified = False
        elif expect:
            record.verified = any(expect.lower() in text.lower()
                                  for text in change.appeared)
        else:
            record.verified = True
    finally:
        before.release()
        after.release()

    if record.verified:
        record.spoken = f"Clicked {record.target}. {record.observed}"
        return _remember(record)

    # It did not do what it was supposed to. Look again before deciding
    # anything, and try at most once more.
    if _attempt < MAX_RETRIES:
        record.retried = True
        record.spoken = "That did not do what I expected. Looking again."
        _remember(record)
        return act_on(intent, task=task, expect=expect, window=target_window,
                      confirm=confirm, risk=risk, _attempt=_attempt + 1)

    record.spoken = ("That did not produce the result I expected, and I do not "
                     "want to guess. Should I try again?")
    return _remember(record)
