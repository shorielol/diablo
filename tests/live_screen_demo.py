"""
The seven scenarios, against your actual screen, one at a time.

This is not part of run_all. It captures your real desktop, sends it to a
local vision model, and in two scenarios waits for you to change something
by hand — none of which belongs in an automated suite. Run it when you want
to know whether the thing works, rather than whether the code is correct.

    ~/.diablo/venv/bin/python tests/live_screen_demo.py
    ~/.diablo/venv/bin/python tests/live_screen_demo.py 3 5     # just those

Every scenario prints what it asked, what came back, and how long it took.
Nothing is asserted. You are the assertion.
"""

import os
import sys
import time

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from companion_manager import load_env
    load_env()
except Exception:
    pass

from skills import agent_tools, display, screen, screen_context  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


def say(label, body, seconds=None):
    took = f"  {DIM}({seconds:.1f}s){RESET}" if seconds is not None else ""
    print(f"\n  {GREEN}{label}{RESET}{took}\n    " + str(body).replace("\n", "\n    "))


def timed(fn, *args, **kwargs):
    start = time.time()
    return fn(*args, **kwargs), time.time() - start


def pause(prompt):
    print(f"\n  {YELLOW}{prompt}{RESET}")
    try:
        input("  press return when you have done it > ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return True


def scenario_1():
    """What am I looking at?"""
    print(f"\n{BOLD}1. What am I looking at?{RESET}")
    print(f"  {DIM}Bring any window to the front first.{RESET}")
    windows = screen_context.list_windows()
    if not windows:
        return say("no windows", "Nothing visible, or no Screen Recording permission.")
    say("frontmost", f"{windows[0]['app']} — {windows[0]['title'][:60]}")
    answer, took = timed(screen_context.explain, "what am I looking at")
    say("Diablo says", answer, took)


def scenario_2():
    """Where do I click? — pointing at something real."""
    print(f"\n{BOLD}2. Where do I click?{RESET}")
    front = screen_context.frontmost_window()
    if not front:
        return say("no window", "nothing in front")
    from skills import pointing
    hits, took = timed(pointing.locate, "", "")
    labels = [h["text"] for h in pointing.locate("the", "")[:0]] or []
    # Ask about something that is genuinely on this screen: the first readable
    # label, found by measurement rather than chosen by a model.
    frame = screen.capture_this_window(front)
    try:
        if not frame.ok:
            return say("could not read it", frame.problem)
        fragments = screen_context._ocr_fragments(frame.path, front)
        words = [f["text"] for f in fragments if isinstance(fragments, list)
                 and len(f["text"].strip()) > 3][:6] if not isinstance(fragments, str) else []
    finally:
        frame.release()
    if not words:
        return say("nothing readable", "no text found in the front window")
    target = words[0]
    say("asking where this is", repr(target))
    answer, took = timed(agent_tools.TOOLS["find_on_screen"]["fn"], target)
    say("Diablo says", answer, took)


def scenario_3():
    """What is under my cursor?"""
    print(f"\n{BOLD}3. What is under my cursor?{RESET}")
    if not pause("Put your mouse pointer over something with a label on it."):
        return
    spot = display.cursor()
    say("pointer", f"{spot} on {(display.cursor_display() or None) and display.cursor_display().describe()}")
    answer, took = timed(agent_tools.TOOLS["whats_under_cursor"]["fn"])
    say("Diablo says", answer, took)


def scenario_4():
    """What changed?"""
    print(f"\n{BOLD}4. What changed?{RESET}")
    screen.forget()
    first, took = timed(screen.what_changed)
    say("before anything changes", first, took)
    if not pause("Now change something in that window — scroll it, type, open a menu."):
        return
    answer, took = timed(screen.what_changed)
    say("Diablo says", answer, took)


def scenario_5():
    """A question the text cannot answer."""
    print(f"\n{BOLD}5. A visual question{RESET}")
    question = "what do the colours and the layout of this look like"
    say("routing", "vision" if screen_context.needs_vision(question, {"regions": []}) else "text")
    if not screen_context.vision_available():
        return say("skipped", f"no vision model. ollama pull {screen_context.VISION_MODEL}")
    answer, took = timed(screen_context.explain, question)
    say("Diablo says", answer, took)


def scenario_6():
    """The displays."""
    print(f"\n{BOLD}6. The displays{RESET}")
    answer, took = timed(agent_tools.TOOLS["screen_layout"]["fn"])
    say("Diablo says", answer, took)
    for one in display.displays():
        frame = screen.capture_display(one.index)
        try:
            say(f"capture of {one.describe()}",
                f"{frame.pixel_width}x{frame.pixel_height} pixels, "
                f"valid={frame.ok} {frame.problem}")
        finally:
            frame.release()


def scenario_7():
    """What it will and will not do."""
    print(f"\n{BOLD}7. Looking is not touching{RESET}")
    from skills import computer_use
    say("privacy", agent_tools.TOOLS["screen_privacy"]["fn"]())
    result = computer_use.click(400, 400, "Continue", confirm=lambda q: True)
    say("asked to click, with a yes already given", result.spoken)
    result = computer_use.click(400, 400, "Delete everything", confirm=lambda q: True)
    say("asked to click something destructive", result.spoken)


SCENARIOS = [scenario_1, scenario_2, scenario_3, scenario_4,
             scenario_5, scenario_6, scenario_7]


def main():
    wanted = [int(a) for a in sys.argv[1:] if a.isdigit()] or list(range(1, 8))
    print(f"{BOLD}Diablo — screen understanding, against your real screen{RESET}")
    print(f"{DIM}{display.describe_arrangement()}{RESET}")
    for number in wanted:
        if 1 <= number <= len(SCENARIOS):
            try:
                SCENARIOS[number - 1]()
            except KeyboardInterrupt:
                print("\n  stopped")
                return 1
            except Exception as e:
                print(f"\n  scenario {number} raised {type(e).__name__}: {e}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
