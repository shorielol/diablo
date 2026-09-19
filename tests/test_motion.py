#!/usr/bin/env python3
"""GUI-2: the one motion system.

Everything here runs without a window: the motion module is pure, and the
AppKit shell only reads values out of it each frame. What is checked:

  time        the same elapsed time gives the same motion at any frame rate
  shape       critically damped: no overshoot, even after a retarget
  states      every visual state comes from a real runtime fact
  ownership   an old completion never wins against a newer motion
  space       home, approach, settle, hold, return; several displays
  reduced     reduced motion keeps the meaning and drops the travel
"""

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from skills import display, motion  # noqa: E402
from skills.motion import HOME, Presence, Visual  # noqa: E402


def run_for(p, seconds, hz=120.0):
    for _ in range(int(round(seconds * hz))):
        p.step(1.0 / hz)


def until(p, condition, limit=5.0, hz=120.0):
    """Step until `condition()` holds. Returns the elapsed time, or None."""
    elapsed = 0.0
    while elapsed < limit:
        if condition():
            return elapsed
        p.step(1.0 / hz)
        elapsed += 1.0 / hz
    return None


def _fake_displays():
    """A Retina laptop as main, and a 1x external to its LEFT (negative x)."""
    D = display.Display
    return [D(id=1, x=0.0, y=0.0, width=1512.0, height=982.0, scale=2.0,
              is_main=True, index=1),
            D(id=2, x=-1920.0, y=-98.0, width=1920.0, height=1080.0, scale=1.0,
              is_main=False, index=2)]


def run():
    r = Results()

    # ── Time ─────────────────────────────────────────────────────────────
    r.head("Elapsed time, not frames")
    with guard(r, "frame rate independence"):
        ends = {}
        for label, hz in (("60Hz", 60.0), ("120Hz", 120.0)):
            p = Presence(home=(0.0, 0.0))
            p.move_to((800.0, 300.0))
            run_for(p, 0.25, hz)
            ends[label] = p.position.value
        jitter = [1 / 120, 1 / 30, 1 / 120, 1 / 60, 0.0, 1 / 90] * 8
        p = Presence(home=(0.0, 0.0))
        p.move_to((800.0, 300.0))
        for dt in jitter:
            p.step(dt)
        q = Presence(home=(0.0, 0.0))
        q.move_to((800.0, 300.0))
        for _ in range(12):                      # the same 0.6 s in even steps
            q.step(sum(jitter) / 12)
        r.check("60 Hz and 120 Hz land in the same place",
                math.dist(ends["60Hz"], ends["120Hz"]) < 1e-6,
                f"{ends['60Hz']} vs {ends['120Hz']}")
        r.check("dropped and uneven frames land where even frames land",
                math.dist(p.position.value, q.position.value) < 1e-6,
                f"{p.position.value} vs {q.position.value}")

    with guard(r, "a stall is not a fling"):
        p = Presence(home=(0.0, 0.0))
        p.move_to((1000.0, 0.0))
        p.step(5.0)                              # the lid was shut for 5 s
        r.check("one enormous frame advances at most MAX_STEP",
                p.position.value[0] < 1000.0 and p._clock <= motion.MAX_STEP + 1e-9,
                f"x={p.position.value[0]:.1f}")

    # ── Shape ────────────────────────────────────────────────────────────
    r.head("Critically damped: arrives, never overshoots")
    with guard(r, "a plain move"):
        p = Presence(home=(0.0, 0.0))
        arrived = []
        p.move_to((600.0, 0.0), on_arrive=lambda: arrived.append(p._clock))
        took = until(p, lambda: bool(arrived), limit=3.0)
        run_for(p, 1.0)
        xs = []
        p2 = Presence(home=(0.0, 0.0))
        p2.move_to((600.0, 0.0))
        for _ in range(360):
            p2.step(1 / 120)
            xs.append(p2.position.value[0])
        r.check("it arrives", took is not None, f"{took}")
        r.check("arrival is reported exactly once", len(arrived) == 1, f"{arrived}")
        r.check("x never passes the target", max(xs) <= 600.0 + 1e-9, f"max {max(xs):.4f}")
        r.check("and only ever moves toward it",
                all(b >= a - 1e-9 for a, b in zip(xs, xs[1:])))
        r.check("it decelerates into the target",
                (xs[20] - xs[19]) > 10 * (xs[80] - xs[79]) >= 0,
                f"frame 20: {xs[20]-xs[19]:.3f} pt, frame 80: {xs[80]-xs[79]:.4f} pt")

    with guard(r, "distance-aware timing"):
        short, long_ = motion.move_duration(80), motion.move_duration(1400)
        r.check("a long trip is given longer than a short one", long_ > short,
                f"{short:.2f}s vs {long_:.2f}s")
        r.check("but never more than the ceiling",
                motion.move_duration(10000) == motion.Timing.MOVE_MAX)
        durations = {}
        for d in (80.0, 1400.0):
            p = Presence(home=(0.0, 0.0))
            done = []
            p.move_to((d, 0.0), on_arrive=lambda: done.append(1))
            durations[d] = until(p, lambda: bool(done), limit=4.0)
        r.check("arrival is declared when he visibly arrives, not a tail later",
                durations[1400.0] <= motion.move_duration(1400) + 0.05,
                f"1400pt reported at {durations[1400.0]:.2f}s, "
                f"duration {motion.move_duration(1400):.2f}s")
        r.check("measured: the long trip does take longer",
                durations[1400.0] > durations[80.0],
                f"80pt {durations[80.0]:.2f}s, 1400pt {durations[1400.0]:.2f}s")

    with guard(r, "retarget keeps momentum"):
        p = Presence(home=(0.0, 0.0))
        p.move_to((1000.0, 0.0))
        run_for(p, 0.15)
        before_pos, before_vel = p.position.value, p.position.x.velocity
        p.retarget((300.0, 400.0))
        r.check("position is continuous across the change of mind",
                p.position.value == before_pos)
        r.check("x velocity is carried, not zeroed",
                p.position.x.velocity == before_vel and before_vel > 0,
                f"{before_vel:.1f} -> {p.position.x.velocity:.1f} pt/s")
        ys = []
        for _ in range(480):
            p.step(1 / 120)
            ys.append(p.position.value[1])
        r.check("it ends exactly at the new target", p.position.value == (300.0, 400.0))
        r.check("y never passes the new target", max(ys) <= 400.0 + 1e-9, f"{max(ys):.3f}")

    with guard(r, "never faster than a person can follow"):
        p = Presence(home=(1461.0, 51.0))
        p.approach_target((350.0, 700.0))
        run_for(p, 0.35)
        p.approach_target((740.0, 480.0))       # the walkthrough's A -> B
        peak, last = 0.0, p.position.value
        for _ in range(400):
            p.step(1 / 120)
            here = p.position.value
            peak = max(peak, math.dist(here, last) * 120)
            last = here
        r.check("a retarget stays under the speed limit",
                peak <= motion.MAX_SPEED * 1.01, f"peak {peak:.0f} pt/s")
        r.check("and still ends exactly on B", p.position.value == (740.0, 480.0))

    with guard(r, "a retarget to somewhere nearer, already moving fast"):
        s = motion.Spring(0.0, 0.5)
        s.to(100.0)
        s.step(0.1)
        s.to(s.value + 1.0, 0.5)                 # target now barely ahead
        vals = []
        for _ in range(200):
            s.step(1 / 120)
            vals.append(s.value)
        r.check("the approach speed is capped, so it does not shoot past",
                max(vals) <= s.target + 1e-9, f"max {max(vals):.4f} target {s.target:.4f}")

    # ── States from real facts ───────────────────────────────────────────
    r.head("Every visual state maps a real runtime fact")
    with guard(r, "the manager's states"):
        p = Presence()
        mapping = {"idle": Visual.IDLE, "listening": Visual.LISTENING,
                   "processing": Visual.THINKING, "preparing": Visual.THINKING,
                   "responding": Visual.SPEAKING, "error": Visual.ERROR,
                   "cold": Visual.IDLE, "init": Visual.IDLE}
        wrong = []
        for state, want in mapping.items():
            p.set_runtime(state)
            if p.visual is not want:
                wrong.append(f"{state}->{p.visual.value}")
        r.check("each maps to exactly one visual", not wrong, ", ".join(wrong))
        p.set_runtime("cold")
        r.check("cold and waking are drawn dim", p.look().dim)
        p.set_runtime("preparing")
        r.check("preparing is thinking that holds still", not p.looping() and p.look().still)
        p.set_runtime("processing")
        r.check("processing loops", p.looping())

    with guard(r, "the key"):
        p = Presence()
        p.key_pressed()
        r.check("key down is ACTIVATING at once", p.visual is Visual.ACTIVATING)
        r.check("and he grows toward the press scale", p.scale.target == motion.Scale.PRESS)
        run_for(p, 0.03)
        p.set_runtime("listening")
        r.check("the microphone opening makes it LISTENING",
                p.visual is Visual.LISTENING and not p.key_down)
        q = Presence()
        q.key_pressed()
        q.key_released()
        r.check("released before the microphone opened: back to IDLE",
                q.visual is Visual.IDLE)

    with guard(r, "the rest of the model"):
        p = Presence()
        p.set_runtime("processing")
        p.set_reading(1)
        r.check("reading the screen shows over thinking", p.visual is Visual.READING)
        p.set_reading(0)
        r.check("and gives it back when the reading ends", p.visual is Visual.THINKING)
        p.set_runtime("idle")
        p.set_waiting(True)
        r.check("a question waiting for a press is WAITING", p.visual is Visual.WAITING)
        p.set_waiting(False)
        p.set_jobs(2)
        r.check("background work with nothing else on is WORKING", p.visual is Visual.WORKING)
        p.set_runtime("responding")
        r.check("speaking outranks background work", p.visual is Visual.SPEAKING)
        p.set_runtime("idle")
        p.set_jobs(0)
        p.flash("success")
        r.check("a finished job is SUCCESS", p.visual is Visual.SUCCESS)
        took = until(p, lambda: p.visual is not Visual.SUCCESS)
        r.check("which ends when its cue completes, not on a guess",
                took is not None and abs(took - motion.Timing.SUCCESS) < 0.02, f"{took}")
        p.flash("failure")
        r.check("a failed job is ERROR", p.visual is Visual.ERROR)
        until(p, lambda: p.cue is None)
        p.set_runtime("responding")
        p.flash("interrupted")
        r.check("cutting him off is INTERRUPTED", p.visual is Visual.INTERRUPTED)
        until(p, lambda: p.cue is None)
        r.check("and then whatever is true now", p.visual is Visual.SPEAKING)
        p.flash("interrupted")
        p.key_pressed()
        r.check("barging in with the key is acknowledged at once, over the cue",
                p.visual is Visual.ACTIVATING and p.look().cue == "interrupted")
        p.key_released()
        p.set_runtime("error")
        r.check("a real fault outranks everything", p.visual is Visual.ERROR)

    with guard(r, "rings hand over rather than switch"):
        p = Presence()
        p.set_runtime("processing")
        run_for(p, 0.5)
        p.set_runtime("responding")
        p.step(1 / 120)
        look = p.look()
        r.check("for a moment both rings are partly visible",
                0 < look.rings.get("think", 0) < 1 and 0 < look.rings.get("speak", 0) < 1,
                str({k: round(v, 2) for k, v in look.rings.items()}))
        run_for(p, 0.5)
        r.check("then only the new one", set(p.look().rings) == {"speak"})

    with guard(r, "the clock only runs when something moves"):
        p = Presence()
        run_for(p, 1.0)
        r.check("idle and settled needs no frames", not p.busy())
        p.set_runtime("listening")
        run_for(p, 1.0)
        r.check("a looping state keeps the clock", p.busy())
        p.set_runtime("preparing")
        run_for(p, 1.0)
        r.check("preparing, once settled, does not", not p.busy())

    # ── Ownership ────────────────────────────────────────────────────────
    r.head("OLD EVENT -> NEW STATE -> OLD EVENT MUST NOT WIN")
    with guard(r, "approach A, then B"):
        p = Presence(home=(0.0, 0.0))
        fired = []
        p.approach_target((500.0, 500.0), on_ready=lambda: fired.append("A"))
        run_for(p, 0.1)
        p.approach_target((100.0, 700.0), on_ready=lambda: fired.append("B"))
        until(p, lambda: bool(fired))
        run_for(p, 2.0)
        r.check("only B reports ready", fired == ["B"], str(fired))
        r.check("and he is standing at B", p.position.value == (100.0, 700.0))
        r.check("guiding there", p.visual is Visual.GUIDING)
        p.set_runtime("processing")
        r.check("still working while he stands there: GUIDING, not THINKING",
                p.visual is Visual.GUIDING)
        p.set_runtime("responding")
        r.check("but speaking about it shows as SPEAKING", p.visual is Visual.SPEAKING)
        p.set_runtime("idle")

    with guard(r, "an arrival after an interruption"):
        p = Presence(home=(0.0, 0.0))
        fired = []
        p.approach_target((900.0, 0.0), on_ready=lambda: fired.append("ready"))
        run_for(p, 0.2)
        moving_at = p.position.value
        p.interrupt_motion()
        run_for(p, 2.0)
        r.check("the interrupted approach never reports ready", fired == [], str(fired))
        r.check("he coasts to a stop rather than freezing",
                p.position.value[0] > moving_at[0], f"{moving_at} -> {p.position.value}")
        r.check("and does not claim to be guiding anything", p.visual is Visual.IDLE)

    with guard(r, "a hold that was overtaken"):
        p = Presence(home=(0.0, 0.0))
        p.move_to((300.0, 300.0))
        run_for(p, 2.0)
        ended = []
        p.hold_position(0.5, then=lambda: ended.append("old hold"))
        run_for(p, 0.2)
        p.return_home()
        run_for(p, 2.0)
        r.check("the old hold's ending never runs", ended == [], str(ended))
        r.check("and he is home", p.phase == HOME)

    with guard(r, "return home, then a new target mid-way"):
        p = Presence(home=(0.0, 0.0))
        p.approach_target((800.0, 400.0))
        until(p, lambda: p.visual is Visual.GUIDING)
        homes = []
        p.move_to(p.home, on_arrive=lambda: homes.append("home"), phase=motion.RETURNING)
        run_for(p, 0.15)
        p.approach_target((200.0, 600.0))
        run_for(p, 3.0)
        r.check("the return's arrival does not fire", homes == [], str(homes))
        r.check("he is at the new target, not home", p.position.value == (200.0, 600.0))

    with guard(r, "panel: open, close, open again"):
        p = Presence()
        closed = []
        p.open_panel()
        run_for(p, 0.4)
        p.close_panel(on_done=lambda is_open: closed.append(is_open))
        run_for(p, 0.05)
        p.open_panel()
        run_for(p, 1.0)
        r.check("the superseded close never orders the window out", closed == [], str(closed))
        r.check("the panel ends open", p.panel.value == 1.0)
        done = []
        p.close_panel(on_done=lambda is_open: done.append(is_open))
        run_for(p, 1.0)
        r.check("an uncontested close completes, reporting closed", done == [False], str(done))
        r.check("leaving is quicker than arriving",
                motion.Timing.PANEL_CLOSE < motion.Timing.PANEL_OPEN)

    # ── Space ────────────────────────────────────────────────────────────
    r.head("Home, approach, settle, hold, return")
    with guard(r, "the full trip"):
        p = Presence(home=(1400.0, 60.0))
        seen, ready = [], []
        p.approach_target((600.0, 700.0), on_ready=lambda: ready.append(p._clock))
        for _ in range(600):
            p.step(1 / 120)
            if not seen or seen[-1] != p.visual:
                seen.append(p.visual)
            if ready:
                break
        r.check("it travels, settles, then guides",
                seen[:3] == [Visual.MOVING, Visual.TARGETING, Visual.GUIDING],
                " -> ".join(v.value for v in seen))
        r.check("the settle brings him back to rest from slightly small",
                p.scale.value == motion.Scale.REST)
        p.hold_position(1.0, then=p.return_home)
        run_for(p, 0.9)
        r.check("the deliberate hold keeps him there", p.position.value == (600.0, 700.0))
        until(p, lambda: p.phase == HOME, limit=4.0)
        run_for(p, 1.0)
        r.check("then he goes home by himself",
                p.position.value == (1400.0, 60.0) and p.visual is Visual.IDLE)
        r.check("and the return is drawn as RETURNING",
                any(change.endswith("->returning") for _, change in p.log))

    with guard(r, "home is stable"):
        p = Presence(home=(1400.0, 60.0))
        p.set_home((1300.0, 60.0))
        r.check("moving home while resting moves him with it",
                p.position.value == (1300.0, 60.0) and p.phase == HOME)
        p.approach_target((200.0, 200.0))
        run_for(p, 0.1)
        p.set_home((1200.0, 60.0))
        r.check("but does not yank him back mid-task", p.phase != HOME)

    with guard(r, "several displays, through display.py"):
        real = display.displays
        display.displays = _fake_displays
        try:
            stand = motion.stand_for(-900.0, 500.0, "Export", cursor=(10.0, 10.0))
            r.check("a target on the left display has a stand point", stand is not None)
            quartz = display.appkit_to_quartz(*stand)
            on = display.for_point(*quartz)
            r.check("the stand point is on the same (left, negative-x) display",
                    on is not None and on.id == 2, f"{quartz}")
            r.check("and keeps clear of the target",
                    math.dist(quartz, (-900.0, 500.0)) >= 60.0,
                    f"{math.dist(quartz, (-900.0, 500.0)):.0f} pt away")
            r.check("the one flip is the main display's height",
                    display.quartz_to_appkit(0.0, 0.0) == (0.0, 982.0))
            home = display.quartz_to_appkit(1440.0, 920.0)   # on the Retina main
            p = Presence(home=home)
            done = []
            p.approach_target(stand, on_ready=lambda: done.append(1))
            until(p, lambda: bool(done), limit=4.0)
            run_for(p, 1.0)
            r.check("the trip crosses displays and ends exactly on the stand point",
                    p.position.value == stand, f"{p.position.value} vs {stand}")
            r.check("a target in the gap between displays has nowhere to stand",
                    motion.stand_for(-100.0, -300.0, "") is None)
            stand_main = motion.stand_for(700.0, 400.0, "", cursor=(10.0, 10.0))
            on_main = display.for_point(*display.appkit_to_quartz(*stand_main))
            r.check("a target on the Retina main stays on the main",
                    on_main is not None and on_main.id == 1)
        finally:
            display.displays = real

    # ── Reduced motion ───────────────────────────────────────────────────
    r.head("Reduced motion keeps the meaning, drops the travel")
    with guard(r, "a move is a fade, not a flight"):
        p = Presence(home=(0.0, 0.0), reduced=True)
        xs, alphas, ready = [], [], []
        p.approach_target((900.0, 300.0), on_ready=lambda: ready.append(1))
        for _ in range(240):
            p.step(1 / 120)
            xs.append(p.position.value[0])
            alphas.append(p.alpha.value)
        r.check("no in-between positions: only start and end",
                set(round(x, 6) for x in xs) <= {0.0, 900.0}, str(sorted(set(xs))[:4]))
        r.check("it fades out and back in instead",
                min(alphas) < 0.05 and alphas[-1] == 1.0, f"min alpha {min(alphas):.3f}")
        r.check("and still reports ready at the target", ready == [1], str(ready))
        r.check("then guides from there", p.visual is Visual.GUIDING)
    with guard(r, "scale and sway are held back"):
        p = Presence(reduced=True)
        p.key_pressed()
        r.check("the key press grows him by at most 2%",
                p.scale.target <= 1.0 + motion.Scale.REDUCED_LIMIT + 1e-9,
                f"{p.scale.target}")
        p.key_released()
        p.set_runtime("processing")
        run_for(p, 0.7)
        r.check("thinking does not sway the face", p.look().face_dx == 0.0)
        r.check("but it is still visibly THINKING",
                p.visual is Visual.THINKING and p.look().rings.get("think", 0) > 0.9)

    # ── Cost ─────────────────────────────────────────────────────────────
    r.head("Cost per frame")
    with guard(r, "the engine is cheap enough for 120 Hz"):
        p = Presence(home=(0.0, 0.0))
        p.set_runtime("listening")
        p.approach_target((800.0, 500.0))
        n = 5000
        began = time.perf_counter()
        for _ in range(n):
            p.step(1 / 120)
            p.look()
        per = (time.perf_counter() - began) / n * 1e6
        r.check("step + look well under a millisecond", per < 300, f"{per:.1f} µs per frame")

    return r.report("motion")


if __name__ == "__main__":
    sys.exit(run())
