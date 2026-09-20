#!/usr/bin/env python3
"""The conversation surface: three levels of one object.

What is checked: the surface is anchored to the orb and grows away from him,
so the corner nearest him never moves; each level takes the room its content
needs and no more; it never covers him or leaves the screen; the sizes are
moved by the one motion system, and jump rather than animate under reduced
motion; a report from the page that arrives out of order is ignored; a
surface he opened for a spoken turn folds away by itself, and one you are
using does not; what he points at is never left under it; and the page's
own rules for which surface to show (app/surface.js, in node).
"""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from AppKit import NSApplication  # noqa: E402

NSApplication.sharedApplication()
import app_shell  # noqa: E402
from skills import motion, surface  # noqa: E402
from skills.motion import Presence  # noqa: E402

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
HERE = os.path.dirname(os.path.abspath(__file__))
HZ = 120.0

# A 1512 x 982 display with the menu bar taken off, and the orb at home in
# its bottom-right corner: the geometry every check below is measured against.
VISIBLE = (0.0, 0.0, 1512.0, 957.0)
ORB_HOME = (1464.5, 47.5)
CANVAS = motion.ORB + 2 * 11.0


def run_for(p, seconds):
    for _ in range(int(round(seconds * HZ))):
        p.step(1.0 / HZ)


def run():
    r = Results()
    anchor = surface.anchor_for(ORB_HOME, CANVAS)

    r.head("Anchored to him, growing away from him")
    with guard(r, "the corner nearest the orb"):
        r.check("the surface's bottom-right corner sits just above the orb",
                anchor == (ORB_HOME[0] + CANVAS / 2, ORB_HOME[1] + CANVAS / 2 + surface.GAP),
                str(anchor))
        frames = []
        for level, height in ((surface.INTERACTION, 58), (surface.INTERACTION, 300),
                              (surface.WORK, 620), (surface.WORK, 900)):
            size = surface.size_for(level, height, VISIBLE, anchor)
            frames.append(surface.frame_for(size, anchor, VISIBLE))
        rights = {round(f[0] + f[2], 1) for f in frames}
        bottoms = {round(f[1], 1) for f in frames}
        r.check("every size keeps the same right edge", rights == {anchor[0]}, str(rights))
        r.check("and the same bottom edge", bottoms == {anchor[1]}, str(bottoms))
        r.check("so it grows to the left and upward, never over him",
                all(f[1] >= ORB_HOME[1] + CANVAS / 2 for f in frames))

    with guard(r, "it stays on the screen"):
        wide = surface.frame_for((2000.0, 400.0), anchor, VISIBLE)
        r.check("a surface wider than the display is pulled inside it",
                wide[0] >= VISIBLE[0], f"x {wide[0]}")
        tall = surface.frame_for((560.0, 4000.0), anchor, VISIBLE)
        r.check("one taller than the display sits inside it too",
                tall[1] >= VISIBLE[1] and tall[1] + tall[3] <= VISIBLE[1] + VISIBLE[3] + 1,
                f"y {tall[1]}, h {tall[3]}")
        low, high = surface.work_bounds(VISIBLE, anchor)
        r.check("the work surface is never the whole desktop",
                high[0] <= VISIBLE[2] * 0.7 and high[1] <= VISIBLE[3] * 0.9,
                f"{high}")
        r.check("and never so tall it reaches over the orb",
                anchor[1] + high[1] <= VISIBLE[1] + VISIBLE[3] - surface.INSET + 0.5)

    r.head("The room the content needs, and no more")
    with guard(r, "the compact surface follows its exchange"):
        heights = [surface.size_for(surface.INTERACTION, h, VISIBLE, anchor)[1]
                   for h in (0, 86, 166, 300, 900)]
        r.check("an empty one is the command line alone",
                heights[0] == surface.INTERACTION_MIN_HEIGHT, str(heights[0]))
        r.check("it grows with what is in it", heights[1] < heights[2] < heights[3],
                str(heights))
        r.check("and stops at its ceiling", heights[4] == surface.INTERACTION_MAX_HEIGHT,
                str(heights[4]))
        r.check("its width does not change with content",
                len({surface.size_for(surface.INTERACTION, h, VISIBLE, anchor)[0]
                     for h in (86, 300)}) == 1)

    with guard(r, "the work surface follows its content too"):
        small = surface.size_for(surface.WORK, 300, VISIBLE, anchor)
        medium = surface.size_for(surface.WORK, 700, VISIBLE, anchor)
        huge = surface.size_for(surface.WORK, 3000, VISIBLE, anchor)
        r.check("a short one does not open a tall window", small[1] == surface.WORK_MIN[1],
                str(small))
        r.check("a longer answer gets the room", medium[1] == 700, str(medium))
        r.check("and it is still bounded", huge[1] < 3000 and huge[1] <= VISIBLE[3] * 0.88,
                str(huge))
        dragged = surface.size_for(surface.WORK, 300, VISIBLE, anchor, chosen=(700.0, 640.0))
        r.check("a size you dragged it to wins over the content",
                dragged == (700, 640), str(dragged))
        r.check("but is still kept inside the screen",
                surface.size_for(surface.WORK, 0, VISIBLE, anchor,
                                 chosen=(4000.0, 4000.0))[0] <= VISIBLE[2])

    r.head("Moved by the one motion system")
    with guard(r, "sizes are springs, like everything else"):
        p = Presence(home=ORB_HOME)
        p.open_panel()
        p.size_panel(380.0, 166.0, instant=True)
        run_for(p, 0.5)
        p.size_panel(560.0, 620.0)
        r.check("a new size does not arrive at once",
                p.look().panel_size[1] < 620.0, str(p.look().panel_size))
        r.check("and the clock is asked for frames while it changes", p.busy())
        run_for(p, 0.6)
        r.check("it gets there", all(abs(a - b) < 0.5 for a, b in
                                     zip(p.look().panel_size, (560.0, 620.0))),
                str(p.look().panel_size))
        run_for(p, 1.5)
        r.check("and then stops asking", not p.busy())

    with guard(r, "a size change mid-change retargets, it does not restart"):
        p = Presence(home=ORB_HOME)
        p.open_panel()
        p.size_panel(380.0, 166.0, instant=True)
        p.size_panel(560.0, 620.0)
        run_for(p, 0.1)
        half = p.look().panel_size[1]
        p.size_panel(380.0, 200.0)
        run_for(p, 1.0 / HZ)
        r.check("it carries on from where it had got to",
                abs(p.look().panel_size[1] - half) < 40, f"{half} -> {p.look().panel_size[1]}")
        run_for(p, 1.6)
        r.check("and settles at the newest size", p.look().panel_size == (380.0, 200.0))

    with guard(r, "reduced motion: the surface changes size without moving"):
        p = Presence(home=ORB_HOME, reduced=True)
        p.open_panel()
        p.size_panel(380.0, 166.0, instant=True)
        p.size_panel(560.0, 620.0)
        r.check("it is simply the new size", p.look().panel_size == (560.0, 620.0))
        r.check("with no frames needed for it",
                p.panel_w.at_rest() and p.panel_h.at_rest())

    with guard(r, "a closed surface does not animate to its first size"):
        p = Presence(home=ORB_HOME)
        p.size_panel(380.0, 166.0)
        r.check("it is that size when it opens", p.look().panel_size == (380.0, 166.0))

    r.head("Out of the way of what he points at")
    with guard(r, "the surface steps aside, not just back"):
        p = Presence(home=ORB_HOME)
        p.open_panel()
        run_for(p, 0.4)
        p.covering = True
        p.approach_target((600.0, 500.0))
        run_for(p, 0.5)
        r.check("while he is away over it, the surface is gone", p.look().surface < 0.05,
                f"{p.look().surface:.2f}")
        p.covering = False
        p._apply()
        run_for(p, 0.5)
        r.check("away from anything under it, it only steps back",
                abs(p.look().surface - motion.SURFACE_AWAY) < 0.05, f"{p.look().surface:.2f}")
        p.return_home()
        run_for(p, 2.0)
        r.check("home again, it is fully there", p.look().surface > 0.99)
        r.check("and nothing is still marked as covered", p.covering is False)

    r.head("What the page reports")
    panel = app_shell.Panel("http://127.0.0.1:1/", presence=Presence(home=ORB_HOME))
    with guard(r, "out of order reports are ignored"):
        panel.report({"seq": 10, "level": "work", "height": 0, "kind": "long"})
        r.check("the newest is used", panel.level == surface.WORK, panel.level)
        panel.report({"seq": 9, "level": "interaction", "height": 100, "kind": "short"})
        r.check("an older one cannot undo it", panel.level == surface.WORK, panel.level)
        panel.report({"seq": 11, "level": "interaction", "height": 166, "kind": "short"})
        r.check("a newer one can", panel.level == surface.INTERACTION
                and panel.content_height == 166.0, f"{panel.level} {panel.content_height}")
        panel.report({"seq": 12, "level": "nonsense", "height": 9})
        r.check("a level it does not know is refused", panel.level == surface.INTERACTION)
        r.check("and the ordering rule is the same one the geometry states",
                surface.accept(2, 1) and not surface.accept(1, 1) and not surface.accept(1, 2))

    with guard(r, "using it keeps it, ignoring it lets it go"):
        panel.report({"engaged": True})
        r.check("someone typing or reading is engaged", panel.engaged is True)
        panel.implicit = True
        panel.report({"engaged": True})
        r.check("and a surface he opened becomes yours once you use it",
                panel.implicit is False)
        panel.report({"engaged": False})
        r.check("leaving it lets go again", panel.engaged is False)
        before = panel._dwell
        panel.dwell(0.01)
        r.check("a dwell on a hidden surface does nothing", panel._dwell == before)
        panel._dwell = 5
        panel._cancel_dwell()
        r.check("cancelling makes the pending one void", panel._dwell != 5)

    r.head("The page's own rules")
    with guard(r, "which surface, for what"):
        node = shutil.which("node")
        if not node or not os.path.exists(os.path.join(HOME, "app", "surface.js")):
            r.skip("surface levels", "node or the installed app is missing")
        else:
            out = subprocess.run([node, os.path.join(HERE, "js", "surface_levels.mjs")],
                                 capture_output=True, text=True,
                                 env={**os.environ, "DIABLO_HOME": HOME}, timeout=60)
            for line in (out.stdout + out.stderr).splitlines():
                line = line.strip()
                if line.startswith("ok   "):
                    r.ok(line[5:])
                elif line.startswith("FAIL "):
                    r.bad(line[5:])
            r.check("node ran the surface checks cleanly", out.returncode == 0,
                    (out.stdout + out.stderr)[-300:] if out.returncode else "")

    with guard(r, "the numbers the two sides share"):
        js = open(os.path.join(HOME, "app", "surface.js")).read()
        for name, value in (("INTERACTION_MAX_HEIGHT", surface.INTERACTION_MAX_HEIGHT),):
            r.check(f"{name} is the same in the page and the geometry",
                    f"export const {name} = {int(value)};" in js, name)
        css = open(os.path.join(HOME, "app", "styles.css")).read()
        r.check("and in the stylesheet that scrolls inside it",
                f"--interaction-max: {int(surface.INTERACTION_MAX_HEIGHT)}px;" in css)

    r.head("It can be used without a mouse, and without motion")
    with guard(r, "the controls say what they are"):
        js = open(os.path.join(HOME, "app", "app.js")).read()
        for label in ('aria-label="Open larger"', 'aria-label="Back to one exchange"',
                      'aria-label="More"', 'role="status"'):
            r.check(f"{label} is there", label in js, label)
        r.check("Escape steps back rather than trapping you", "'Escape'" in js)
        r.check("the moment's line is announced, not only drawn", 'aria-live="polite"' in js)
        css = open(os.path.join(HOME, "app", "styles.css")).read()
        r.check("focus is visible and never removed", ":focus-visible" in css
                and "outline: 2px solid var(--focus)" in css)
        r.check("and reduced motion is honoured", "prefers-reduced-motion: reduce" in css)
        r.check("light and dark both have values", "prefers-color-scheme: light" in css
                and "color-scheme: light dark" in css)

    return r.report("surface")


if __name__ == "__main__":
    sys.exit(run())
