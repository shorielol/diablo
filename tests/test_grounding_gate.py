#!/usr/bin/env python3
"""
A coordinate from a model cannot reach the mouse without the screen agreeing.

These are the security acceptance tests for the grounding boundary: every way
a GroundingProposal can be wrong, and the verdict each one must get. The
screen is stood in for — the accessibility hit test, the window list and the
displays are stubs — so these run anywhere, and the mouse is a recorder that
fails the test if anything is ever posted where it should not be.

The live counterpart, against real fixture windows, is
evals/run_grounding_gauntlet.py.
"""

import ast
import dataclasses
import math
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import ax, computer_use as cu, goclick  # noqa: E402
from skills import grounding_gate as gate, screen, screen_context as sc  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID, OTHER_PID = 501, 999
WINDOW = {"id": 7, "app": "Fixture", "title": "Gate", "x": 100.0, "y": 100.0,
          "width": 600.0, "height": 400.0, "sensitive": False}
DISPLAYS = [SimpleNamespace(id=2, x=0.0, y=0.0, width=1920.0, height=1080.0, scale=1.0),
            SimpleNamespace(id=1, x=-1512.0, y=0.0, width=1512.0, height=982.0, scale=2.0)]


def element(label, x, y, w=80.0, h=30.0, role="AXButton", enabled=True):
    return ax.Element(role=role, title=label, enabled=enabled, x=x, y=y, width=w, height=h,
                      pid=PID, window_title="Gate")


SAVE = element("Save", 580, 450)
DELETE_ALL = element("Delete All", 460, 450, w=100)
EXPORT_OFF = element("Export", 340, 450, enabled=False)
ICON = element("", 120, 120, w=24, h=24)
LAYOUT = [SAVE, DELETE_ALL, EXPORT_OFF, ICON]


def centre(e):
    return (e.x + e.width / 2, e.y + e.height / 2)


class Screen:
    """What is on screen now. Tests change it between the look and the check."""

    def __init__(self, elements=None, pid=PID, frame=(100.0, 100.0, 600.0, 400.0)):
        self.elements = list(LAYOUT if elements is None else elements)
        self.pid = pid
        self.window_frame = frame
        self.window = dict(WINDOW)

    def hit(self, x, y):
        inside = [e for e in self.elements
                  if e.x <= x < e.x + e.width and e.y <= y < e.y + e.height]
        best = min(inside, key=lambda e: e.width * e.height) if inside else None
        return ax.Hit(element=best, deepest_role=best.role if best else "AXGroup",
                      pid=self.pid, window_frame=self.window_frame)

    def window_now(self, window_id):
        return self.window if window_id == WINDOW["id"] and self.window else None

    def elements_now(self, window):
        return list(self.elements)

    def kwargs(self):
        return dict(hit=self.hit, window_now=self.window_now, elements_now=self.elements_now,
                    pid_of=lambda w: PID, displays=lambda: DISPLAYS)


def observation(elements=None, age=0.0, window=None):
    frame = screen.ScreenFrame(path=None, captured_at=time.time() - age, source="window",
                               x=100.0, y=100.0, width=600.0, height=400.0,
                               pixel_width=1200, pixel_height=800, window_id=7,
                               display_id=2, display_scale=1.0)
    return gate.Observation(frame=frame, window=dict(window or WINDOW),
                            elements=list(LAYOUT if elements is None else elements))


def proposal(look, x, y, target, space=gate.Space.QUARTZ_POINT, **extra):
    return gate.GroundingProposal(frame_id=look.frame_id, space=space, x=x, y=y,
                                  target=target, source_model="test", **extra)


def run():
    r = Results()
    posted = []
    saved_post, saved_enabled = cu._post_click, cu.ENABLED
    cu._post_click = lambda x, y: posted.append((x, y)) or 2

    try:
        r.head("the validation matrix")
        with guard(r, "1. the correct target validates, as itself"):
            look, now = observation(), Screen()
            v = gate.validate(proposal(look, *centre(SAVE), "Save"), look, **now.kwargs())
            r.check("VALIDATED", v.verdict == gate.VALIDATED, v.verdict)
            r.check("the label is the control's own", v.target and v.target.label == "Save")
            r.check("the point is the control's centre, not the model's",
                    v.target and v.target.centre == centre(SAVE))
            r.check("and it was issued by the gate", gate.issued(v.target))

        with guard(r, "2. an absent target"):
            look, now = observation(), Screen()
            v = gate.validate(proposal(look, 300, 300, "Export all"), look, **now.kwargs())
            r.check("REJECT_NO_TARGET", v.verdict == gate.REJECT_NO_TARGET, v.verdict)

        with guard(r, "3. a disabled target"):
            look, now = observation(), Screen()
            v = gate.validate(proposal(look, *centre(EXPORT_OFF), "Export"), look,
                              **now.kwargs())
            r.check("REJECT_DISABLED", v.verdict == gate.REJECT_DISABLED, v.verdict)

        with guard(r, "4. the wrong window"):
            look = observation()
            v = gate.validate(proposal(look, *centre(SAVE), "Save"), look,
                              **Screen(pid=OTHER_PID).kwargs())
            r.check("another application on top: REJECT_WRONG_WINDOW",
                    v.verdict == gate.REJECT_WRONG_WINDOW, v.verdict)
            v = gate.validate(proposal(look, *centre(SAVE), "Save"), look,
                              **Screen(frame=(400.0, 300.0, 500.0, 400.0)).kwargs())
            r.check("another window of the same app on top: REJECT_WRONG_WINDOW",
                    v.verdict == gate.REJECT_WRONG_WINDOW, v.verdict)
            wide = gate.Observation(
                frame=screen.ScreenFrame(path=None, captured_at=time.time(), source="display",
                                         x=0.0, y=0.0, width=1920.0, height=1080.0,
                                         pixel_width=1920, pixel_height=1080),
                window=dict(WINDOW), elements=list(LAYOUT))
            v = gate.validate(proposal(wide, 900, 700, "Save"), wide, **Screen().kwargs())
            r.check("a point in the capture but outside the window: REJECT_WRONG_WINDOW",
                    v.verdict == gate.REJECT_WRONG_WINDOW, v.verdict)

        with guard(r, "5. two controls with the same name"):
            twin = element("Save", 200, 450)
            look, now = observation(LAYOUT + [twin]), Screen(LAYOUT + [twin])
            v = gate.validate(proposal(look, *centre(SAVE), "Save", confidence=0.99), look,
                              **now.kwargs())
            r.check("REJECT_AMBIGUOUS, whatever the model's confidence",
                    v.verdict == gate.REJECT_AMBIGUOUS, v.verdict)

        with guard(r, "6. the target disappears between the look and the check"):
            look = observation()
            now = Screen([DELETE_ALL, EXPORT_OFF, ICON])
            v = gate.validate(proposal(look, *centre(SAVE), "Save"), look, **now.kwargs())
            r.check("REJECT_STALE_FRAME (gone)",
                    v.verdict == gate.REJECT_STALE_FRAME and v.detail == "gone",
                    f"{v.verdict} {v.detail}")

        with guard(r, "7. the target moves"):
            look = observation()
            moved = element("Save", 590, 450)
            now = Screen([moved, DELETE_ALL, EXPORT_OFF, ICON])
            v = gate.validate(proposal(look, *centre(SAVE), "Save"), look, **now.kwargs())
            r.check("REJECT_STALE_FRAME (moved)",
                    v.verdict == gate.REJECT_STALE_FRAME and v.detail == "moved",
                    f"{v.verdict} {v.detail}")

        with guard(r, "8. the target changes identity"):
            look = observation()
            swapped = element("Delete All", 580, 450)
            now = Screen([swapped, DELETE_ALL, EXPORT_OFF, ICON])
            v = gate.validate(proposal(look, *centre(SAVE), "Save"), look, **now.kwargs())
            r.check("REJECT_STALE_FRAME (changed)",
                    v.verdict == gate.REJECT_STALE_FRAME and v.detail == "changed",
                    f"{v.verdict} {v.detail}")

        with guard(r, "9. invalid coordinate spaces"):
            look, now = observation(), Screen()
            cases = [
                ("an unknown space", proposal(look, 600, 465, "Save", space="pixels")),
                ("not a number", proposal(look, math.nan, 465, "Save")),
                ("normalised outside 0..1", proposal(look, 1.4, 0.5, "Save",
                                                     space=gate.Space.FRAME_NORMALISED)),
                ("a pixel beyond the image", proposal(look, 1300, 100, "Save",
                                                      space=gate.Space.FRAME_PIXEL)),
                ("a display pixel with no display", proposal(look, 10, 10, "Save",
                                                             space=gate.Space.DISPLAY_PIXEL)),
                ("a quartz point outside the capture", proposal(look, 50, 50, "Save")),
            ]
            for name, bad in cases:
                v = gate.validate(bad, look, **now.kwargs())
                r.check(f"{name}: REJECT_COORDINATE_SPACE",
                        v.verdict == gate.REJECT_COORDINATE_SPACE, v.verdict)
            other = dataclasses.replace(proposal(look, *centre(SAVE), "Save"), frame_id="xyz")
            v = gate.validate(other, look, **now.kwargs())
            r.check("a point read from a different capture: REJECT_STALE_FRAME",
                    v.verdict == gate.REJECT_STALE_FRAME, v.verdict)

        with guard(r, "each space converts the one way it should"):
            look, now = observation(), Screen()
            u, v_ = (centre(SAVE)[0] - 100) / 600, (centre(SAVE)[1] - 100) / 400
            for name, p in [
                ("FRAME_NORMALISED", proposal(look, u, v_, "Save",
                                              space=gate.Space.FRAME_NORMALISED)),
                ("FRAME_PIXEL (2x)", proposal(look, u * 1200, v_ * 800, "Save",
                                              space=gate.Space.FRAME_PIXEL)),
                ("DISPLAY_PIXEL", proposal(look, centre(SAVE)[0], centre(SAVE)[1], "Save",
                                           space=gate.Space.DISPLAY_PIXEL, display_id=2)),
            ]:
                v = gate.validate(p, look, **now.kwargs())
                r.check(f"{name} validates Save", v.verdict == gate.VALIDATED, v.verdict)
            retina = SimpleNamespace(id=1, x=-1512.0, y=0.0, width=1512.0, height=982.0,
                                     scale=2.0)
            q, why = gate.to_quartz(proposal(look, 100.0, 50.0, "x",
                                             space=gate.Space.DISPLAY_PIXEL, display_id=1),
                                    look.frame, lambda: [retina])
            r.check("a Retina display pixel at negative x maps to (-1462, 25)",
                    q == (-1462.0, 25.0), str(q))

        with guard(r, "10. a coordinate off every display"):
            low = dict(WINDOW, y=900.0)
            look = gate.Observation(
                frame=screen.ScreenFrame(path=None, captured_at=time.time(), source="window",
                                         x=100.0, y=900.0, width=600.0, height=400.0,
                                         pixel_width=1200, pixel_height=800, window_id=7),
                window=low, elements=[])
            now = Screen(frame=(100.0, 900.0, 600.0, 400.0))
            now.window = low
            v = gate.validate(proposal(look, 300, 1200, "Save"), look, **now.kwargs())
            r.check("REJECT_OFF_DISPLAY", v.verdict == gate.REJECT_OFF_DISPLAY, v.verdict)

        with guard(r, "11. a stale observation"):
            now = Screen()
            old = observation(age=gate.MAX_OBSERVATION_AGE + 5)
            v = gate.validate(proposal(old, *centre(SAVE), "Save"), old, **now.kwargs())
            r.check("too old: REJECT_STALE_FRAME", v.verdict == gate.REJECT_STALE_FRAME
                    and v.detail == "too_old", f"{v.verdict} {v.detail}")
            look = observation()
            now.window = dict(WINDOW, x=140.0)
            v = gate.validate(proposal(look, *centre(SAVE), "Save"), look, **now.kwargs())
            r.check("window moved: REJECT_STALE_FRAME", v.verdict == gate.REJECT_STALE_FRAME
                    and v.detail == "window_moved", f"{v.verdict} {v.detail}")
            now.window = None
            v = gate.validate(proposal(look, *centre(SAVE), "Save"), look, **now.kwargs())
            r.check("window gone: REJECT_STALE_FRAME", v.verdict == gate.REJECT_STALE_FRAME
                    and v.detail == "window_gone", f"{v.verdict} {v.detail}")

        with guard(r, "12. visually similar controls"):
            look, now = observation(), Screen()
            v = gate.validate(proposal(look, *centre(DELETE_ALL), "Delete"), look,
                              **now.kwargs())
            r.check("'Delete' is not 'Delete All': REJECT_LABEL_MISMATCH",
                    v.verdict == gate.REJECT_LABEL_MISMATCH, v.verdict)
            v = gate.validate(proposal(look, *centre(SAVE), "the Save checkbox"), look,
                              **now.kwargs())
            r.check("a button asked for as a checkbox: REJECT_ROLE",
                    v.verdict == gate.REJECT_ROLE, v.verdict)

        r.head("13. the model's label is never authoritative")
        with guard(r, "point on Delete All, labelled Save"):
            look, now = observation(), Screen()
            forged = proposal(look, *centre(DELETE_ALL), "Save", claimed_label="Save",
                              confidence=0.99, reasoning="this is the Save button")
            v = gate.validate(forged, look, **now.kwargs())
            r.check("REJECT_LABEL_MISMATCH", v.verdict == gate.REJECT_LABEL_MISMATCH, v.verdict)
            r.check("and it names what is really there",
                    v.found is not None and v.found.label == "Delete All")
            r.check("no target is issued", v.target is None)
        with guard(r, "asked for Delete All, it is Delete All — and it is destructive"):
            look, now = observation(), Screen()
            v = gate.validate(proposal(look, *centre(DELETE_ALL), "Delete All",
                                       claimed_label="Save"), look, **now.kwargs())
            r.check("VALIDATED with the real label", v.ok and v.target.label == "Delete All")
            r.check("the real label is destructive", cu.is_destructive(v.target.label))
        with guard(r, "an edited copy of a validated target is not issued"):
            look, now = observation(), Screen()
            v = gate.validate(proposal(look, *centre(DELETE_ALL), "Delete All"), look,
                              **now.kwargs())
            relabelled = dataclasses.replace(v.target, label="Save")
            moved = dataclasses.replace(v.target, x=v.target.x + 100)
            r.check("relabelled: not issued", not gate.issued(relabelled))
            r.check("moved: not issued", not gate.issued(moved))
            hand_made = gate.ValidatedTarget(
                role="AXButton", label="Save", x=580, y=450, width=80, height=30, pid=PID,
                window_id=7, window_bounds=(100, 100, 600, 400), frame_id=look.frame_id,
                validated_at=time.time(), source_model="forged")
            r.check("built by hand: not issued", not gate.issued(hand_made))
        with guard(r, "an unnamed control needs a person's yes"):
            look, now = observation(), Screen()
            v = gate.validate(proposal(look, *centre(ICON), "gear icon"), look, **now.kwargs())
            r.check("CONFIRM_REQUIRED, not VALIDATED", v.verdict == gate.CONFIRM_REQUIRED,
                    v.verdict)
            r.check("its target says it has no name", v.target and not v.target.named)

        r.head("revalidation immediately before the click")
        look, now = observation(), Screen()
        good = gate.validate(proposal(look, *centre(SAVE), "Save"), look, **now.kwargs()).target
        with guard(r, "unchanged screen"):
            v = gate.revalidate(good, hit=now.hit, window_now=now.window_now)
            r.check("VALIDATED", v.ok, v.verdict)
        with guard(r, "became disabled"):
            later = Screen([element("Save", 580, 450, enabled=False), DELETE_ALL])
            v = gate.revalidate(good, hit=later.hit, window_now=later.window_now)
            r.check("REJECT_DISABLED", v.verdict == gate.REJECT_DISABLED, v.verdict)
        with guard(r, "replaced by another control"):
            later = Screen([element("Delete All", 580, 450), DELETE_ALL])
            v = gate.revalidate(good, hit=later.hit, window_now=later.window_now)
            r.check("REJECT_STALE_FRAME", v.verdict == gate.REJECT_STALE_FRAME, v.verdict)
        with guard(r, "covered by another application"):
            later = Screen(pid=OTHER_PID)
            v = gate.revalidate(good, hit=later.hit, window_now=later.window_now)
            r.check("REJECT_WRONG_WINDOW", v.verdict == gate.REJECT_WRONG_WINDOW, v.verdict)
        with guard(r, "a target the gate never issued"):
            v = gate.revalidate(dataclasses.replace(good, label="Other"),
                                hit=now.hit, window_now=now.window_now)
            r.check("REJECT_UNVERIFIABLE", v.verdict == gate.REJECT_UNVERIFIABLE, v.verdict)

        r.head("the click accepts nothing else")
        cu.ENABLED = True
        saved = (sc.window_by_id, screen.capture_this_window, ax.hit_test)

        def frame_for(_window):
            return screen.ScreenFrame(path=None, captured_at=time.time(), source="window",
                                      x=100.0, y=100.0, width=600.0, height=400.0,
                                      fingerprint=[1, 2, 3])
        try:
            sc.window_by_id = now.window_now
            screen.capture_this_window = frame_for
            ax.hit_test = now.hit
            with guard(r, "a bare coordinate"):
                posted.clear()
                result = cu.click((600, 465), frame=look.frame, confirm=lambda q: True)
                r.check("refused as not validated", result.refusal == cu.NOT_VALIDATED,
                        result.refusal)
                r.check("nothing posted", not posted)
            with guard(r, "a relabelled target"):
                posted.clear()
                result = cu.click(dataclasses.replace(good, label="Delete All"),
                                  frame=look.frame, confirm=lambda q: True)
                r.check("refused as not validated", result.refusal == cu.NOT_VALIDATED,
                        result.refusal)
                r.check("nothing posted", not posted)
            with guard(r, "a destructive control needs its own yes, by its own label"):
                posted.clear()
                look2 = observation()
                delete = gate.validate(proposal(look2, *centre(DELETE_ALL), "Delete All"),
                                       look2, **now.kwargs()).target
                asked = []
                result = cu.click(delete, frame=look2.frame,
                                  confirm=lambda q: asked.append(q) or False)
                r.check("the question names it as possibly not undoable",
                        any("undoable" in q for q in asked), str(asked))
                r.check("declined, nothing posted", result.refusal == cu.DECLINED and not posted)
            with guard(r, "a target that went stale between permission and click"):
                posted.clear()
                look3 = observation()
                fresh = gate.validate(proposal(look3, *centre(SAVE), "Save"), look3,
                                      **now.kwargs()).target

                def disable_then_answer(question):
                    now.elements = [element("Save", 580, 450, enabled=False), DELETE_ALL]
                    return True
                result = cu.click(fresh, frame=look3.frame, confirm=disable_then_answer)
                r.check("refused as disabled", result.refusal == cu.TARGET_DISABLED,
                        result.refusal)
                r.check("nothing posted", not posted)
                now.elements = list(LAYOUT)
            with guard(r, "the same frame cannot authorise two clicks"):
                posted.clear()
                cu.revoke()
                look4 = observation()
                once = gate.validate(proposal(look4, *centre(SAVE), "Save"), look4,
                                     **now.kwargs()).target
                first = cu.click(once, frame=look4.frame, confirm=lambda q: True)
                second = cu.click(once, frame=look4.frame, confirm=lambda q: True)
                r.check("the first is sent", first.ok and len(posted) == 1, first.refusal)
                r.check("the second is refused", second.refusal == cu.FRAME_USED,
                        second.refusal)
                r.check("at the control's centre", posted[:1] == [centre(SAVE)], str(posted))
        finally:
            sc.window_by_id, screen.capture_this_window, ax.hit_test = saved
            cu.ENABLED = saved_enabled
            cu.revoke()

        r.head("act_on's last look")
        with guard(r, "a control covered by another window is not pressed"):
            v = gate.check_point(*centre(SAVE), "Save", "AXButton", WINDOW,
                                 hit=Screen(pid=OTHER_PID).hit, pid_of=lambda w: PID)
            r.check("REJECT_WRONG_WINDOW", v.verdict == gate.REJECT_WRONG_WINDOW, v.verdict)
            v = gate.check_point(*centre(DELETE_ALL), "Save", "AXButton", WINDOW,
                                 hit=Screen().hit, pid_of=lambda w: PID)
            r.check("a perception label that is not what is there: REJECT_LABEL_MISMATCH",
                    v.verdict == gate.REJECT_LABEL_MISMATCH, v.verdict)

        r.head("GoClick only ever proposes")
        with guard(r, "its answer is read as a normalised point, or nothing"):
            r.check("'(<loc_817>,<loc_913>)' is (0.8175, 0.9135)",
                    goclick.parse("<s>(<loc_817>,<loc_913>)</s>") == (0.8175, 0.9135))
            r.check("one location is no answer", goclick.parse("<loc_5>") is None)
            r.check("a run of <s> is no answer", goclick.parse("<s>" * 12) is None)
        with guard(r, "a proposal carries no label of its own"):
            saved_ask, saved_installed = goclick._WORKER.ask, goclick.installed
            goclick._WORKER.ask = lambda image, target: {"raw": "<s>(<loc_500>,<loc_500>)</s>"}
            goclick.installed = lambda: True
            try:
                frame = SimpleNamespace(ok=True, path="/nonexistent.png", frame_id="F9")
                p = goclick.propose(frame, "Save")
            finally:
                goclick._WORKER.ask, goclick.installed = saved_ask, saved_installed
            r.check("FRAME_NORMALISED against the frame it was given",
                    p is not None and p.space == gate.Space.FRAME_NORMALISED
                    and p.frame_id == "F9")
            r.check("no claimed label", p is not None and p.claimed_label == "")

        r.head("nothing but computer_use can press anything")
        with guard(r, "the architecture"):
            source = os.path.join(REPO, "diablo")
            posting = []
            for name in sorted(os.listdir(source)):
                if not name.endswith(".py"):
                    continue
                text = open(os.path.join(source, name)).read()
                if any(api in text for api in ("CGEventPost", "CGEventCreateMouseEvent",
                                               "CGEventCreateKeyboardEvent")):
                    posting.append(name)
            r.check("only computer_use.py posts input events", posting == ["computer_use.py"],
                    str(posting))
            for name in ("grounding_gate.py", "goclick.py"):
                tree = ast.parse(open(os.path.join(source, name)).read())
                names = {getattr(n, "attr", None) or getattr(n, "id", None)
                         for n in ast.walk(tree)}
                imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                           for a in n.names}
                r.check(f"{name} imports no action layer",
                        "computer_use" not in imports and "do_engine" not in imports,
                        str(imports))
                r.check(f"{name} names no click or event API",
                        not names & {"click", "act_on", "_post_click", "CGEventPost",
                                     "type_text"}, str(names & {"click", "act_on"}))
            # Calls, not mentions: a docstring may say what click() accepts.
            callers = []
            for name in sorted(os.listdir(source)):
                if not name.endswith(".py") or name == "computer_use.py":
                    continue
                tree = ast.parse(open(os.path.join(source, name)).read())
                for node in ast.walk(tree):
                    func = getattr(node, "func", None)
                    if (isinstance(node, ast.Call) and isinstance(func, ast.Attribute)
                            and func.attr == "click" and isinstance(func.value, ast.Name)
                            and func.value.id in ("computer_use", "_cu", "cu", "use")):
                        callers.append(f"{name}:{node.lineno}")
            r.check("no module calls click() but the action layer itself", not callers,
                    str(callers))
    finally:
        cu._post_click, cu.ENABLED = saved_post, saved_enabled
    return r.report("grounding gate")


if __name__ == "__main__":
    sys.exit(run())
