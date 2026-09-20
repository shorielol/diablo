#!/usr/bin/env python3
"""GUI-3: Diablo on the desktop — home, target, guide, return.

The spatial flow is the shell's own code (Shell.approach, approach_region,
clear_target, no_target) run against a stand-in shell whose overlay is a
recorder, stepped on a simulated clock. Placement runs through the real
guidance geometry and display.py with a simulated two-display layout, so the
arithmetic is deterministic. Nothing here clicks, types or opens a window.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

import app_shell  # noqa: E402
from skills import display, guidance, lesson, motion  # noqa: E402
from skills.motion import HOME, Presence, Visual  # noqa: E402

HZ = 120.0


def _displays():
    """A Retina laptop as main, and a 1x external to its LEFT (negative x)."""
    D = display.Display
    return [D(id=1, x=0.0, y=0.0, width=1512.0, height=982.0, scale=2.0,
              is_main=True, index=1),
            D(id=2, x=-1920.0, y=-98.0, width=1920.0, height=1080.0, scale=1.0,
              is_main=False, index=2)]


class RecordingGuide:
    def __init__(self):
        self.calls = []

    def prepare(self, x, y, label="", w=0.0, h=0.0):
        self.calls.append(("prepare", x, y))

    def show(self, x, y, label="", w=0.0, h=0.0):
        self.calls.append(("show", x, y))

    def hide(self):
        self.calls.append(("hide",))

    def close(self):
        self.calls.append(("close",))

    def names(self):
        return [c[0] for c in self.calls]


def stand_in(home=(1461.0, 51.0), reduced=False):
    """The shell's spatial code on a stand-in, with a recorder for the overlay."""
    shell = types.SimpleNamespace()
    shell.presence = Presence(home=home, reduced=reduced)
    shell.guide = RecordingGuide()
    shell.wake = lambda: None
    # The real Shell carries one of these, and clear_target now ends the
    # lesson along with the indication, so the double needs it too.
    shell.lesson = lesson.Lesson()
    for name in ("approach", "approach_region", "clear_target", "no_target",
                 "_under_surface"):
        setattr(shell, name, types.MethodType(getattr(app_shell.Shell, name), shell))
    return shell


def run_for(p, seconds):
    for _ in range(int(round(seconds * HZ))):
        p.step(1.0 / HZ)


def until(p, condition, limit=6.0):
    t = 0.0
    while t < limit:
        if condition():
            return t
        p.step(1.0 / HZ)
        t += 1.0 / HZ
    return None


def orb_box(appkit_point):
    """The orb's footprint around a stand point, in Quartz points."""
    qx, qy = display.appkit_to_quartz(*appkit_point)
    radius = motion.STAND_RADIUS
    return (qx - radius, qy - radius, 2 * radius, 2 * radius)


def overlaps(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def inside(box, d):
    x, y, w, h = box
    return x >= d.x and y >= d.y and x + w <= d.right and y + h <= d.bottom


def run():
    r = Results()
    real_displays = display.displays
    real_current = app_shell._current_generation
    display.displays = _displays
    try:
        _run(r)
    finally:
        display.displays = real_displays
        app_shell._current_generation = real_current
    return r.report("spatial")


def _run(r):
    # ── Placement ────────────────────────────────────────────────────────
    r.head("Where he stands")
    with guard(r, "the default placement is exactly the old one"):
        same = True
        for x, y in ((700, 400), (60, 60), (1480, 950), (-900, 500)):
            a = guidance.place(x, y, "Export", cursor=(5.0, 5.0))
            b = guidance.place(x, y, "Export", cursor=(5.0, 5.0),
                               extent=(guidance.RING, guidance.RING),
                               marker=guidance.MARKER)
            same &= (a.marker_x, a.marker_y, a.side) == (b.marker_x, b.marker_y, b.side)
        r.check("no extent and no marker gives the same answer as before", same)

    with guard(r, "a grid of targets over both displays, edges and corners included"):
        failures, good, total = [], 0, 0
        for d in _displays():
            xs = [d.x + 20, d.x + 90, d.x + d.width / 2, d.x + d.width - 90, d.x + d.width - 20]
            ys = [d.y + 12, d.y + 80, d.y + d.height / 2, d.y + d.height - 80, d.y + d.height - 12]
            for x in xs:
                for y in ys:
                    total += 1
                    stand = motion.stand_for(x, y, "Settings", cursor=(5.0, 5.0))
                    if stand is None:
                        failures.append(f"none for ({x:.0f},{y:.0f})")
                        continue
                    box = orb_box(stand)
                    on = display.for_point(*display.appkit_to_quartz(*stand))
                    target = (x - guidance.RING, y - guidance.RING,
                              2 * guidance.RING, 2 * guidance.RING)
                    if on is None or on.id != d.id:
                        failures.append(f"({x:.0f},{y:.0f}) stood on the wrong display")
                    elif not inside(box, d):
                        failures.append(f"({x:.0f},{y:.0f}) orb partly off the display")
                    elif overlaps(box, target):
                        failures.append(f"({x:.0f},{y:.0f}) orb covers the target")
                    else:
                        good += 1
        r.check("he always stands on the target's own display, whole, and clear of it",
                not failures, "; ".join(failures[:4]))
        r.ok("targets checked", f"{good}/{total} across a Retina main and a negative-x external")

    with guard(r, "a target right beside home"):
        home_q = (1461.0, 982.0 - 51.0)
        stand = motion.stand_for(home_q[0] - 40, home_q[1] - 10, "Wi-Fi", cursor=(5.0, 5.0))
        target = (home_q[0] - 40 - guidance.RING, home_q[1] - 10 - guidance.RING,
                  2 * guidance.RING, 2 * guidance.RING)
        r.check("even next to home he steps aside rather than covering it",
                not overlaps(orb_box(stand), target))

    with guard(r, "a region of content, not a control"):
        region = (300.0, 250.0, 360.0, 200.0)             # a table: Quartz x, y, w, h
        cx, cy = region[0] + region[2] / 2, region[1] + region[3] / 2
        stand = motion.stand_for(cx, cy, "", cursor=(5.0, 5.0),
                                 extent=(region[2] / 2 + 6, region[3] / 2 + 6))
        box = orb_box(stand)
        r.check("he stands beside the region, not on it", not overlaps(box, region),
                f"orb {tuple(round(v) for v in box)}")
        gap = max(region[0] - (box[0] + box[2]), box[0] - (region[0] + region[2]),
                  region[1] - (box[1] + box[3]), box[1] - (region[1] + region[3]))
        r.check("and close enough to read as referring to it", 0 <= gap < 80, f"gap {gap:.0f} pt")

    with guard(r, "a target in the gap between displays"):
        r.check("has nowhere to stand, and none is invented",
                motion.stand_for(-100.0, -300.0, "") is None)

    # ── Move → settle → indicate ─────────────────────────────────────────
    r.head("MOVE -> SETTLE -> INDICATE, then hold, clear, return")
    with guard(r, "the three beats, in order"):
        s = stand_in()
        p = s.presence
        s.approach(700.0, 400.0, "Settings", seconds=2.5)
        r.check("nothing is indicated while he sets off",
                "show" not in s.guide.names() and s.guide.names()[:2] == ["hide", "prepare"],
                str(s.guide.names()))
        seen, shown_during = [], None
        for _ in range(int(3 * HZ)):
            p.step(1 / HZ)
            if not seen or seen[-1] is not p.visual:
                seen.append(p.visual)
            if shown_during is None and "show" in s.guide.names():
                shown_during = p.visual
        r.check("he travels, then settles, then guides",
                seen[:3] == [Visual.MOVING, Visual.TARGETING, Visual.GUIDING],
                " -> ".join(v.value for v in seen))
        r.check("the indication appears only once he has settled",
                shown_during is Visual.GUIDING, f"{shown_during}")
        r.check("and exactly once", s.guide.names().count("show") == 1)

    with guard(r, "the hold is a real state: long enough to read, and while he talks"):
        s = stand_in()
        p = s.presence
        s.approach(700.0, 400.0, "Settings", seconds=2.5)
        until(p, lambda: "show" in s.guide.names())
        p.set_runtime("responding")                       # he is explaining it
        run_for(p, 4.0)
        r.check("he stays while he is still speaking about it, past the minimum",
                p.visual is Visual.SPEAKING and p.phase == motion.HOLDING
                and "close" not in s.guide.names())
        p.set_runtime("idle")                             # the turn went quiet
        r.check("the moment the turn is quiet the indication fades and he leaves",
                "close" in s.guide.names() and p.phase == motion.RETURNING)
        until(p, lambda: p.phase == HOME)
        r.check("and he is home", p.phase == HOME)

        s = stand_in()
        p = s.presence
        s.approach(700.0, 400.0, "Settings", seconds=2.5)
        until(p, lambda: "show" in s.guide.names())
        run_for(p, 1.0)
        r.check("quiet from the start: still held until the minimum has passed",
                "close" not in s.guide.names())
        run_for(p, 2.0)
        r.check("then cleared", "close" in s.guide.names())

    with guard(r, "the user is asked to act, then it is verified"):
        s = stand_in()
        p = s.presence
        s.approach(700.0, 400.0, "Export", seconds=2.5)
        until(p, lambda: "show" in s.guide.names())
        p.await_user()
        r.check("asked to act, he is WAITING at the target", p.visual is Visual.WAITING)
        run_for(p, 5.0)
        r.check("and waits for as long as it takes", "close" not in s.guide.names())
        p.verified()
        r.check("a real verification shows a short 'done'", p.look().cue == "success")
        r.check("and releases the hold", "close" in s.guide.names())

    with guard(r, "the face leads the trip"):
        s = stand_in(home=(1461.0, 51.0))
        s.approach(200.0, 400.0, "Back")                  # to the left of home
        run_for(s.presence, 0.15)
        left = s.presence.look().face_dx
        s2 = stand_in(home=(60.0, 51.0))
        s2.approach(1300.0, 400.0, "Next")                # to the right
        run_for(s2.presence, 0.15)
        right = s2.presence.look().face_dx
        r.check("travelling left he looks left, right he looks right",
                left < 0 < right, f"left {left:.2f}, right {right:.2f}")

    with guard(r, "an open panel steps back while he is away"):
        s = stand_in()
        p = s.presence
        s.approach(700.0, 400.0, "Settings")
        run_for(p, 0.5)
        r.check("away: the panel dims", p.look().surface < 0.7, f"{p.look().surface:.2f}")
        p.return_home()
        until(p, lambda: p.phase == HOME)
        run_for(p, 0.5)
        r.check("home: it comes back", p.look().surface == 1.0)

    # ── Interrupt, retarget, stale ───────────────────────────────────────
    r.head("OLD TARGET -> NEW TARGET -> OLD TARGET MUST NOT WIN")
    with guard(r, "retarget while moving"):
        s = stand_in()
        p = s.presence
        s.approach(300.0, 300.0, "Export")
        run_for(p, 0.25)
        before = p.position.value
        s.approach(900.0, 600.0, "Share")
        r.check("no jump at the change of mind", p.position.value == before)
        run_for(p, 4.0)
        shows = [c for c in s.guide.calls if c[0] == "show"]
        r.check("only the new target is ever indicated",
                shows == [("show", 900.0, 600.0)], str(shows))
        r.check("any old indication is hidden before each new move",
                s.guide.names().count("hide") == 2)

    with guard(r, "retarget while holding at a target"):
        s = stand_in()
        p = s.presence
        s.approach(300.0, 300.0, "Export")
        until(p, lambda: "show" in s.guide.names())
        p.set_runtime("responding")
        s.approach(900.0, 600.0, "Share")
        r.check("the first indication fades as he leaves it",
                s.guide.names()[-2:] == ["hide", "prepare"], str(s.guide.names()[-3:]))
        until(p, lambda: s.guide.names().count("show") == 2)
        r.check("the new one appears after the new settle",
                s.guide.calls[-1] == ("show", 900.0, 600.0))

    with guard(r, "interrupted mid-flight"):
        s = stand_in()
        p = s.presence
        s.approach(300.0, 300.0, "Export")
        run_for(p, 0.2)
        p.interrupt_motion()
        run_for(p, 3.0)
        r.check("an interrupted approach never indicates", "show" not in s.guide.names())

    with guard(r, "the turn moved on before he arrived"):
        s = stand_in()
        app_shell._current_generation = lambda g: False
        s.approach(700.0, 400.0, "Settings", generation=7)
        run_for(s.presence, 3.0)
        app_shell._current_generation = lambda g: True
        r.check("a stale target is never indicated", "show" not in s.guide.names())
        r.check("he goes home instead", s.presence.phase in (motion.RETURNING, HOME))

    with guard(r, "the same guidance, several times over"):
        s = stand_in()
        p = s.presence
        homes = 0
        for _ in range(4):
            s.approach(700.0, 400.0, "Settings", seconds=2.5)
            until(p, lambda: "show" in s.guide.names())
            until(p, lambda: p.phase == HOME, limit=8.0)
            homes += p.phase == HOME
            s.guide.calls.clear()
        r.check("four identical requests: four clean trips home", homes == 4)

    # ── No target, regions ──────────────────────────────────────────────
    r.head("No target, and regions of content")
    with guard(r, "nothing to point at"):
        s = stand_in()
        s.no_target()
        r.check("at home: a short warning cue and no movement",
                s.presence.look().cue == "missed" and s.presence.phase == HOME
                and s.guide.calls[-1:] in ([], [("close",)]))
        s = stand_in()
        s.approach(-100.0, -300.0, "Nowhere")            # in the gap between displays
        r.check("a target on no display: the same cue, no travel",
                s.presence.look().cue == "missed" and s.presence.phase == HOME)
        s = stand_in()
        s.approach_region(300.0, 250.0, 360.0, 200.0)
        run_for(s.presence, 0.3)
        s.no_target()
        r.check("away when it happens: the cue, then home",
                s.presence.phase == motion.RETURNING and "close" in s.guide.names())

    with guard(r, "reading a region"):
        s = stand_in()
        p = s.presence
        s.approach_region(300.0, 250.0, 360.0, 200.0)
        until(p, lambda: p.phase == motion.HOLDING)
        region = (300.0, 250.0, 360.0, 200.0)
        r.check("he goes and stands beside it",
                p.phase == motion.HOLDING and not overlaps(orb_box(p.position.target), region))
        r.check("with no ring or line: it is content, not a control", s.guide.calls == [])
        p.set_reading(1)
        r.check("reading while he stands there", p.visual is Visual.READING)
        s = stand_in()
        s.approach_region(0.0, 0.0, 1400.0, 900.0)
        r.check("a region that is most of the screen does not move him",
                s.presence.phase == HOME)
        s = stand_in()
        s.approach(700.0, 400.0, "Settings")
        until(s.presence, lambda: "show" in s.guide.names())
        s.approach_region(300.0, 250.0, 360.0, 200.0)
        r.check("a read never pulls him off a target he is pointing at",
                s.presence.visual is Visual.GUIDING)

    # ── Reduced motion ───────────────────────────────────────────────────
    r.head("Reduced motion")
    with guard(r, "the same three beats, without travel"):
        s = stand_in(reduced=True)
        p = s.presence
        s.approach(700.0, 400.0, "Settings")
        xs = []
        for _ in range(int(2 * HZ)):
            p.step(1 / HZ)
            xs.append(round(p.position.value[0], 3))
        r.check("no in-between positions", len(set(xs)) <= 2, str(sorted(set(xs))[:3]))
        r.check("the indication still appears after he has arrived",
                "show" in s.guide.names())
        r.check("and his face does not sway", p.look().face_dx == 0.0)


if __name__ == "__main__":
    sys.exit(run())
