"""
What Diablo can see, and what it refuses to do about it.

These are deliberately not all mocked. A screen test against a fake screen
proves the mock works — and every real bug this suite was written after was a
bug about the real machine: a second display with a different scale factor, a
capture that came back valid and solid black, Diablo reading its own window
back to the user. So the display, cursor, capture and coordinate tests run
against whatever is actually plugged in, and skip honestly when the hardware to
test a case is not present.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import computer_use  # noqa: E402
from skills import display  # noqa: E402
from skills import screen  # noqa: E402
from skills import screen_context  # noqa: E402
from skills import tutor  # noqa: E402


def _solid(path, r, g, b, w=600, h=400):
    """A PNG of one flat colour, which is what a failed capture looks like."""
    from AppKit import (NSBezierPath, NSBitmapImageRep, NSColor,
                        NSGraphicsContext, NSPNGFileType)
    from Foundation import NSMakeRect
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, w, h, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0).set()
    NSBezierPath.fillRect_(NSMakeRect(0, 0, w, h))
    NSGraphicsContext.restoreGraphicsState()
    rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(path, True)
    return path


def run():
    r = Results()

    # ── The displays, as they actually are ──────────────────────────────────
    r.head("The displays")
    screens = display.displays()
    r.check("at least one display is found", len(screens) >= 1, f"{len(screens)} found")

    with guard(r, "display geometry"):
        if screens:
            r.check("exactly one display is main",
                    sum(1 for d in screens if d.is_main) == 1)
            r.check("the main display is first",
                    screens[0].is_main,
                    "screencapture -D numbers from 1 with main first, so the "
                    "list order and the flag have to agree")
            r.check("every display has a positive size",
                    all(d.width > 0 and d.height > 0 for d in screens))
            r.check("every display reports a sane scale",
                    all(1.0 <= d.scale <= 3.0 for d in screens),
                    ", ".join(f"{d.scale:g}x" for d in screens))
            r.check("indices are 1-based and contiguous",
                    [d.index for d in screens] == list(range(1, len(screens) + 1)))

    with guard(r, "scale factor is read from the right source"):
        # CGDisplayPixelsWide returns the POINT width on a scaled Retina panel,
        # so a scale computed from it came out 1.0 for a 2.0 display. The check
        # is against AppKit, which is where the truth is.
        from AppKit import NSScreen
        truth = {}
        for s in NSScreen.screens():
            number = s.deviceDescription().get("NSScreenNumber")
            if number is not None:
                truth[int(number)] = float(s.backingScaleFactor())
        wrong = [d for d in screens if d.id in truth and abs(d.scale - truth[d.id]) > 0.01]
        r.check("scale matches NSScreen for every display", not wrong,
                "; ".join(f"display {d.id} says {d.scale} not {truth[d.id]}" for d in wrong))

    if len(screens) < 2:
        r.skip("multi-monitor geometry", "only one display attached")
        r.skip("a point on the second display resolves to it", "only one display")
        r.skip("region capture on a non-main display", "only one display")
    else:
        with guard(r, "multi-monitor geometry"):
            r.check("displays do not overlap",
                    all(not (a.contains(b.x + 1, b.y + 1))
                        for i, a in enumerate(screens) for b in screens[i + 1:]),
                    "overlapping bounds would make for_point ambiguous")
            second = screens[1]
            middle = (second.x + second.width / 2, second.y + second.height / 2)
            found = display.for_point(*middle)
            r.check("a point on the second display resolves to it",
                    found is not None and found.id == second.id,
                    f"({middle[0]:.0f},{middle[1]:.0f}) -> "
                    f"{found.describe() if found else 'nothing'}")
            r.check("a negative-origin display is handled",
                    all(display.for_point(d.x + 5, d.y + 5) is not None for d in screens),
                    "; ".join(f"origin ({d.x:.0f},{d.y:.0f})" for d in screens))

    with guard(r, "off-screen points are rejected, not clamped silently"):
        far = (99999.0, 99999.0)
        r.check("a point off every display is known to be off",
                not display.on_any_display(*far))
        left, top, right, bottom = display.desktop_bounds()
        r.check("desktop bounds enclose every display",
                all(left <= d.x and d.right <= right and top <= d.y and d.bottom <= bottom
                    for d in screens),
                f"({left:.0f},{top:.0f}) to ({right:.0f},{bottom:.0f})")

    # ── Coordinates ─────────────────────────────────────────────────────────
    r.head("The coordinate pipeline")
    with guard(r, "quartz and appkit round trip"):
        for point in [(0.0, 0.0), (100.0, 250.0), (-1512.0, 500.0), (1900.0, 1070.0)]:
            there = display.quartz_to_appkit(*point)
            back = display.appkit_to_quartz(*there)
            if abs(back[0] - point[0]) > 0.01 or abs(back[1] - point[1]) > 0.01:
                r.bad("quartz -> appkit -> quartz is lossless", f"{point} became {back}")
                break
        else:
            r.ok("quartz -> appkit -> quartz is lossless", "4 points including a negative x")

    with guard(r, "the flip is against the main display"):
        main = display.main_display()
        if main:
            _, flipped = display.quartz_to_appkit(0.0, 0.0)
            r.check("quartz y=0 is appkit y=main height", abs(flipped - main.height) < 0.01,
                    f"{flipped:.0f} vs {main.height:.0f}")

    with guard(r, "a normalised box becomes a screen point"):
        window = {"x": -1512.0, "y": 100.0, "width": 1512.0, "height": 900.0}
        r.check("centre of a window on a negative-x display maps correctly",
                display.image_to_quartz(0.5, 0.5, window) == (-756.0, 550.0),
                "normalised coordinates cancel the scale factor, which is why "
                "Retina needs no special case")

    # ── Is this image a screen at all? ──────────────────────────────────────
    r.head("Rejecting captures that are not screens")
    import tempfile
    scratch = tempfile.mkdtemp(prefix="diablo_screen_test_")
    try:
        with guard(r, "blank frame detection"):
            cases = [("solid black", (0, 0, 0), "minimised or on another desktop"),
                     ("solid white", (1, 1, 1), "blank white"),
                     ("flat grey", (0.5, 0.5, 0.5), "flat colour")]
            for name, colour, expected in cases:
                path = _solid(os.path.join(scratch, f"{name}.png"), *colour)
                ok, why = screen.validity(path)
                r.check(f"a {name} capture is refused", not ok and expected in why,
                        why or "accepted it")

        with guard(r, "malformed captures"):
            broken = os.path.join(scratch, "broken.png")
            with open(broken, "wb") as f:
                f.write(b"not a png")
            r.check("a truncated file is refused", not screen.validity(broken)[0])
            r.check("a missing file is refused",
                    not screen.validity(os.path.join(scratch, "nope.png"))[0])
            r.check("an empty path is refused", not screen.validity("")[0])
    finally:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)

    with guard(r, "a real capture passes"):
        frame = screen.capture_window()
        try:
            if not frame.ok and "permission" in frame.problem.lower():
                r.skip("a real window capture is accepted", "no screen recording permission")
            else:
                r.check("a real window capture is accepted", frame.ok, frame.problem)
                r.check("it knows which display it came from",
                        frame.display_index >= 1, frame.display_name)
                r.check("it knows its own pixel size",
                        frame.pixel_width > 0 and frame.pixel_height > 0,
                        f"{frame.pixel_width}x{frame.pixel_height}")
                r.check("pixel size matches points times scale",
                        abs(frame.pixel_width - frame.width * frame.display_scale) <= 2,
                        f"{frame.pixel_width}px vs {frame.width:.0f}pt "
                        f"@{frame.display_scale:g}x")
        finally:
            frame.release()

    # ── Provenance and staleness ────────────────────────────────────────────
    r.head("A frame knows when and where it came from")
    with guard(r, "staleness"):
        frame = screen.ScreenFrame(path=None, captured_at=time.time() - 10.0,
                                   source="window")
        r.check("a ten-second-old frame is stale", frame.is_stale())
        fresh = screen.ScreenFrame(path=None, captured_at=time.time(), source="window")
        r.check("a new frame is not stale", not fresh.is_stale())
        r.check("the staleness bound can be tightened per caller",
                fresh.is_stale(max_age=-1.0),
                "clicking uses a far tighter bound than reading")

    with guard(r, "the structured form carries no image"):
        frame = screen.capture_window()
        context = frame.context()
        frame.release()
        r.check("context() never includes the image path",
                "path" not in context and not any(
                    isinstance(v, str) and v.endswith(".png") for v in context.values()),
                str(sorted(context))[:90])
        r.check("release() drops the file", frame.path is None)

    with guard(r, "failures come back as a frame, not an exception or a None"):
        missing = screen.capture_window("a window that is definitely not open")
        r.check("an unknown window gives ok=False with a reason",
                missing.ok is False and bool(missing.problem), missing.problem)
        r.check("the reason is sayable, not a stack trace",
                "Traceback" not in missing.problem and "Error:" not in missing.problem,
                missing.problem)

    # ── Diablo's own window ─────────────────────────────────────────────────
    r.head("Diablo does not read itself")
    with guard(r, "self-exclusion"):
        r.check("a window owned by this process is recognised as Diablo's own",
                screen_context.is_own_window("Python", os.getpid(), "Diablo"))
        r.check("the check works by PID, not only by name",
                screen_context.is_own_window("anything at all", os.getpid(), ""))
        r.check("a bundled Diablo is excluded by name",
                screen_context.is_own_window("Diablo", 999999, "Diablo"))
        r.check("a browser tab that merely mentions Diablo is NOT excluded",
                not screen_context.is_own_window("Zen", 999999, "Improve Diablo GUI"),
                "a page about Diablo is a real window the user may be asking about")
        r.check("no listed window belongs to this process",
                all(w.get("app") != "__self__" for w in screen_context.list_windows()))

    with guard(r, "windows carry their display"):
        windows = screen_context.list_windows()
        if not windows:
            r.skip("every window knows which display it is on", "no windows open")
        else:
            r.check("every window knows which display it is on",
                    all(w.get("display_id") for w in windows),
                    f"{sum(1 for w in windows if w.get('display_id'))}/{len(windows)}")

    # ── The cursor ──────────────────────────────────────────────────────────
    r.head("Cursor awareness")
    with guard(r, "cursor position"):
        spot = display.cursor()
        r.check("the pointer has a position", spot is not None, str(spot))
        if spot:
            r.check("the pointer is on a real display",
                    display.on_any_display(*spot),
                    (display.cursor_display() or type("x", (), {"describe": lambda s: "none"})()).describe()
                    if display.cursor_display() else "off every display")
            window = screen.window_under_cursor()
            r.check("the window under the pointer is found, or honestly not",
                    window is None or "app" in window,
                    window["app"] if window else "pointer is over the desktop")

    with guard(r, "reading what is under the pointer"):
        hit = screen.under_cursor()
        if hit is None:
            r.skip("something readable is found under the pointer",
                   "nothing with text near the pointer right now")
        else:
            r.check("what is under the pointer has text and a distance",
                    bool(hit["text"]) and hit["distance"] >= 0,
                    f"{hit['text'][:36]!r} at {hit['distance']:.0f} points")
            r.check("its coordinate is on a real display",
                    display.on_any_display(hit["x"], hit["y"]))

    # ── Regions ─────────────────────────────────────────────────────────────
    r.head("Reading part of a screen")
    with guard(r, "region capture"):
        main = display.main_display()
        if main:
            frame = screen.capture_region(main.x + 50, main.y + 50, 300, 200)
            try:
                r.check("a region of the main display captures", frame.ok, frame.problem)
                if frame.ok:
                    r.check("the region is captured at the display's scale",
                            frame.pixel_width == int(300 * main.scale),
                            f"{frame.pixel_width}px for 300pt @{main.scale:g}x")
            finally:
                frame.release()

    if len(screens) >= 2:
        with guard(r, "region capture on a non-main display"):
            other = screens[1]
            frame = screen.capture_region(other.x + 40, other.y + 40, 200, 150)
            try:
                r.check("a region on the second display captures at its own scale",
                        frame.ok and frame.pixel_width == int(200 * other.scale),
                        f"{frame.pixel_width}px for 200pt @{other.scale:g}x "
                        f"at origin {other.x:.0f}")
            finally:
                frame.release()

    with guard(r, "the region around the pointer"):
        frame = screen.capture_around_cursor()
        try:
            r.check("a region around the pointer captures", frame.ok, frame.problem)
            if frame.ok:
                spot = display.cursor()
                r.check("that region actually contains the pointer",
                        spot is None or (frame.x <= spot[0] <= frame.x + frame.width
                                         and frame.y <= spot[1] <= frame.y + frame.height),
                        f"region ({frame.x:.0f},{frame.y:.0f}) {frame.width:.0f}x"
                        f"{frame.height:.0f}, pointer {spot}")
                r.check("it is smaller than the whole display",
                        frame.width * frame.height < screens[0].width * screens[0].height)
        finally:
            frame.release()

    # ── What changed ────────────────────────────────────────────────────────
    r.head("Telling what changed")
    with guard(r, "step verification logic"):
        before = [{"text": "File"}, {"text": "Edit"}]
        r.check("an unchanged screen is not a success",
                tutor.step_landed(before, before)[0] is False)
        opened = before + [{"text": "Export as PDF"}]
        r.check("a new label counts as a change",
                tutor.step_landed(before, opened)[0] is True)
        r.check("the RIGHT new label is required when one was named",
                tutor.step_landed(before, opened, expect="Export")[0] is True)
        wrong = before + [{"text": "Preferences"}]
        r.check("the wrong thing opening is not success",
                tutor.step_landed(before, wrong, expect="Export")[0] is False,
                "clicking the wrong control also changes the screen")
        noisy = before + [{"text": "12"}]
        r.check("a churning short fragment is not reported as a change",
                tutor.step_landed(before, noisy)[0] is False,
                "clocks and counters change constantly")

    with guard(r, "the walkthrough has explicit states"):
        for state in ("STARTED", "PRESENTED", "WAITING", "SCREEN_CHANGED",
                      "VERIFIED", "NOT_VERIFIED", "NEXT", "COMPLETED", "CANCELLED"):
            if not hasattr(tutor, state):
                r.bad("every guidance state is named", f"missing {state}")
                break
        else:
            r.ok("every guidance state is named", "9 states")

    with guard(r, "comparing two real captures"):
        main = display.main_display()
        if main:
            here = screen.capture_region(main.x + 20, main.y + 20, 500, 300)
            there = screen.capture_region(main.x + 20, main.y + main.height - 340, 500, 300)
            try:
                if here.ok and there.ok:
                    same = screen.compare(here, here)
                    r.check("a capture compared with itself reports no change",
                            same.same or not (same.appeared or same.vanished),
                            same.describe())
                    different = screen.compare(here, there)
                    r.check("two different parts of the screen compare as different",
                            not different.same, different.describe()[:70])
                else:
                    r.skip("comparing two real captures", "could not capture regions")
            finally:
                here.release()
                there.release()

    with guard(r, "what_changed refuses to invent a comparison"):
        screen.forget()
        first = screen.what_changed()
        r.check("the first ask admits there is nothing to compare against",
                "nothing to compare" in first.lower() or "had not looked" in first.lower(),
                first[:80])
        second = screen.what_changed()
        r.check("the second ask makes a real comparison",
                "nothing to compare" not in second.lower(), second[:80])

    with guard(r, "the remembered moment holds no image"):
        moment = screen.last_moment()
        if moment is None:
            r.skip("the remembered moment holds no image", "nothing remembered")
        else:
            r.check("what is remembered is text, not a screenshot",
                    not hasattr(moment, "path") and isinstance(moment.lines, list),
                    f"{len(moment.lines)} lines from {moment.app or 'a window'}")
            screen.forget()
            r.check("it can be forgotten on request", screen.last_moment() is None)

    # ── Which model answers ─────────────────────────────────────────────────
    r.head("Routing a screen question to a model that can answer it")
    with guard(r, "vision routing"):
        wordy = {"regions": [{"lines": ["x" * 400]}]}
        for question in ("what does this graph show", "which button is greyed out",
                         "what colour is the header", "what does this chart mean"):
            if not screen_context.needs_vision(question, wordy):
                r.bad("visual questions go to the vision model", question)
                break
        else:
            r.ok("visual questions go to the vision model", "4 of 4")
        for question in ("what is the error message", "summarise this article",
                         "what does this line of code do", "what is the page title"):
            if screen_context.needs_vision(question, wordy):
                r.bad("text questions stay on the text path", question)
                break
        else:
            r.ok("text questions stay on the text path", "4 of 4")
        r.check("a window with almost no text goes to vision whatever is asked",
                screen_context.needs_vision("what is this", {"regions": [{"lines": ["ok"]}]}),
                "a photo, a video or a chart OCRs to nothing, and 'there is no "
                "text here' answers no question anyone asked")

    with guard(r, "captures are sized for the vision model"):
        frame = screen.capture_window()
        try:
            if not frame.ok:
                r.skip("a large capture is shrunk before the vision model", frame.problem)
            else:
                sized, temporary = screen_context._for_vision(frame.path)
                try:
                    from AppKit import NSBitmapImageRep
                    from Foundation import NSData
                    rep = NSBitmapImageRep.imageRepWithData_(
                        NSData.dataWithContentsOfFile_(sized))
                    longest = max(int(rep.pixelsWide()), int(rep.pixelsHigh()))
                    r.check("nothing larger than the cap is sent to the vision model",
                            longest <= screen_context.VISION_MAX_EDGE,
                            f"{longest}px against a {screen_context.VISION_MAX_EDGE}px cap "
                            f"(native was {frame.pixel_width}x{frame.pixel_height})")
                    r.check("a resize is reported so the caller can clean it up",
                            temporary == (max(frame.pixel_width, frame.pixel_height)
                                          > screen_context.VISION_MAX_EDGE))
                finally:
                    if temporary and os.path.exists(sized):
                        os.unlink(sized)
        finally:
            frame.release()

    with guard(r, "a small capture is not resized at all"):
        main = display.main_display()
        if main:
            small = screen.capture_region(main.x + 10, main.y + 10, 200, 150)
            try:
                if small.ok:
                    sized, temporary = screen_context._for_vision(small.path)
                    r.check("an already-small capture is passed through untouched",
                            sized == small.path and temporary is False,
                            "the common case costs nothing")
                else:
                    r.skip("an already-small capture is passed through untouched",
                           small.problem)
            finally:
                small.release()

    # ── Does this sentence even want the screen? ────────────────────────────
    r.head("Knowing when the screen is not the subject")
    wants = ["what am I looking at", "what changed?", "whats under my cursor",
             "which button do I press", "where do I click", "what does this error mean",
             "can you point it out", "walk me through this", "did that work?",
             "what am I doing wrong", "look at my screen", "read this page",
             "what is on my second screen", "why isnt this working",
             "what's wrong here", "show me where the settings are"]
    leaves_alone = ["what is the capital of France", "remind me to call mum",
                    "play some music", "how much screen time did I have this week",
                    "write me a screenplay", "what SPF sunscreen should I use",
                    "summarise my notes", "what time is it", "email Priya",
                    "what is 15 percent of 240", "who won the match",
                    "add milk to the shopping list"]
    with guard(r, "screen relevance"):
        missed = [t for t in wants if not screen_context.wants_screen(t)]
        wrong = [t for t in leaves_alone if screen_context.wants_screen(t)]
        r.check(f"screen questions are recognised ({len(wants)} cases)",
                not missed, "; ".join(missed))
        r.check(f"non-screen questions do not trigger a capture ({len(leaves_alone)} cases)",
                not wrong, "; ".join(wrong))

    # ── The screen is not the document ──────────────────────────────────────
    r.head("Not mistaking a screenful for a document")
    with guard(r, "what is behind the window"):
        found = screen.document_behind()
        r.check("the answer is one of the three known kinds",
                found.get("kind") in ("page", "file", "none"), str(found)[:80])
        r.check("a 'none' answer says why",
                found.get("kind") != "none" or bool(found.get("why")),
                found.get("why", ""))
        said = screen.visible_or_whole()
        r.check("it is stated in a sentence, not a dict",
                isinstance(said, str) and said.endswith("."), said[:70])
        if found.get("kind") == "none":
            r.check("it admits the screen is all there is",
                    "all I can read" in said or "on screen" in said, said[:70])
        else:
            r.check("it offers the whole thing rather than the visible part",
                    "whole" in said or "all of it" in said, said[:70])

    with guard(r, "a document app is asked, a terminal is not"):
        r.check("document apps have a way to name their file",
                all(app in screen._DOCUMENT_APPS for app in ("Preview", "Pages")),
                "these answer 'path of front document'")
        r.check("the list stays short on purpose",
                len(screen._DOCUMENT_APPS) <= 10,
                "guessing which file a window shows is worse than admitting "
                "the window is all there is")

    with guard(r, "researching what is on screen needs a subject"):
        from skills import agent_tools
        fake = {"id": 0, "app": "Nothing", "title": "", "x": 0, "y": 0,
                "width": 400, "height": 300, "sensitive": True}
        blank = screen.capture_this_window(fake)
        r.check("a sensitive window is refused before research too",
                blank.ok is False, blank.problem[:60])
        r.check("the screen-research tool is registered and reaches the network",
                agent_tools.TOOLS["research_whats_on_screen"]["network"] is True,
                "it goes to the web, so it is gated with everything else that does")

    # ── Permission to touch the machine ─────────────────────────────────────
    r.head("Looking is not touching")
    with guard(r, "computer use is off by default"):
        r.check("computer use is disabled unless explicitly turned on",
                computer_use.ENABLED is False or os.getenv("DIABLO_COMPUTER_USE") == "1",
                "off" if not computer_use.ENABLED else "ON via DIABLO_COMPUTER_USE")
        state = computer_use.state()
        r.check("no permission stands by default", state["level"] == computer_use.DENY)

    with guard(r, "no path grants permission without being asked"):
        computer_use.revoke()
        granted, why = computer_use.ask_for("click something", confirm=None)
        r.check("an absent confirmation is a refusal, not a default yes",
                granted is False, why[:70])
        granted, _ = computer_use.ask_for("click something", confirm=lambda q: False)
        r.check("saying no refuses", granted is False)

    with guard(r, "destructive targets are recognised"):
        dangerous = ["Delete all", "Send", "Buy now", "Submit order", "Empty Trash",
                     "Transfer funds", "Post to feed", "Uninstall"]
        safe = ["Continue", "Search box", "Next", "Cancel", "Preferences", "Zoom in"]
        r.check("destructive labels are caught",
                all(computer_use.is_destructive(d) for d in dangerous),
                "; ".join(d for d in dangerous if not computer_use.is_destructive(d)))
        r.check("ordinary controls are not treated as destructive",
                not any(computer_use.is_destructive(s) for s in safe),
                "; ".join(s for s in safe if computer_use.is_destructive(s)))

    with guard(r, "clicking refuses every way it should"):
        result = computer_use.click(100, 100, "Continue", confirm=lambda q: True)
        r.check("a click is refused while computer use is off",
                result.ok is False and "not allowed" in result.spoken.lower(),
                result.spoken[:70])
        r.check("even with a confirmation that says yes", result.ok is False,
                "seeing the screen never implies permission to use it")

    with guard(r, "a standing grant is scoped and expires"):
        computer_use.revoke()
        grant = computer_use.Grant(level=computer_use.ALLOW_FOR_TASK, task="tidy",
                                   granted_at=time.time())
        r.check("a task grant covers its own task", grant.covers("tidy"))
        r.check("a task grant does not cover another task", not grant.covers("email"))
        old = computer_use.Grant(level=computer_use.ALLOW_FOR_TASK, task="tidy",
                                 granted_at=time.time() - 3600)
        r.check("a task grant expires", not old.covers("tidy"), "one hour old")
        once = computer_use.Grant(level=computer_use.ALLOW_ONCE, granted_at=time.time())
        r.check("a one-off grant covers one use", once.covers(""))
        once.uses = 1
        r.check("a one-off grant does not cover a second", not once.covers(""))
        computer_use.revoke()

    with guard(r, "no agent can reach a click"):
        from skills import agent_tools, roster
        reachable = {t for agent in roster.AGENTS for t in agent.get("tools", [])}
        clicky = {n for n in agent_tools.TOOLS
                  if any(w in n for w in ("click", "type_text", "press_key"))}
        r.check("no clicking tool is registered for any agent",
                not (reachable & clicky), str(reachable & clicky))
        r.check("the retired click path refuses",
                "Error" in __import__("skills.ocr_tool", fromlist=["x"]).find_and_click_text("Delete"))

    # ── Failing cleanly ─────────────────────────────────────────────────────
    r.head("Failing without inventing anything")
    with guard(r, "sensitive windows"):
        fake = {"id": 0, "app": "1Password", "title": "Vault", "x": 0, "y": 0,
                "width": 800, "height": 600, "sensitive": True}
        frame = screen.capture_this_window(fake)
        r.check("a password manager is refused before any capture",
                frame.ok is False and "password manager" in frame.problem,
                frame.problem[:70])
        r.check("nothing was written to disk for it", frame.path is None)

    with guard(r, "bad coordinates"):
        r.check("a coordinate off every display is rejected",
                not display.on_any_display(-99999, -99999))
        result = computer_use.click(-99999, -99999, "anything", confirm=lambda q: True)
        r.check("clicking off-screen is refused", result.ok is False, result.spoken[:60])

    with guard(r, "no raw errors reach the spoken path"):
        problems = []
        for text in [screen.capture_window("nonexistent").problem,
                     screen.what_changed("nonexistent"),
                     computer_use.click(1, 1, "x").spoken]:
            if any(sign in text for sign in ("Traceback", "Exception", "errno",
                                             "0x", "self.", "__")):
                problems.append(text[:50])
        r.check("failure messages are sentences, not diagnostics",
                not problems, "; ".join(problems))

    # ── Privacy ─────────────────────────────────────────────────────────────
    r.head("Privacy")
    with guard(r, "captures do not accumulate"):
        import glob
        import tempfile
        pattern = os.path.join(tempfile.gettempdir(), "diablo_*")
        before = set(glob.glob(pattern))
        for _ in range(3):
            frame = screen.capture_window()
            frame.release()
        screen.what_changed()
        leaked = set(glob.glob(pattern)) - before
        r.check("reading the screen repeatedly leaves no files behind",
                not leaked, f"{len(leaked)} left: {sorted(leaked)[:2]}")

    with guard(r, "nothing captures in the background"):
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "diablo_*")))
        time.sleep(1.5)
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "diablo_*")))
        r.check("no capture happens when nobody asked", before == after,
                "screen capture is event-driven; there is no watcher")

    with guard(r, "the privacy position can be stated"):
        from skills import agent_tools
        said = agent_tools.TOOLS["screen_privacy"]["fn"]()
        r.check("Diablo can say what it does with the screen",
                "only when you ask" in said and "deleted" in said, said[:70])

    return r.report("screen")


if __name__ == "__main__":
    sys.exit(run())
