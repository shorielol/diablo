"""
The whole loop, against a real application, with a real mouse event.

    USER INTENT -> APPLICATION -> CAPTURE -> VALIDITY -> PERCEPTION
    -> CANDIDATES -> DECISION -> CONFIDENCE -> PERMISSION -> ACTION
    -> NEW FRAME -> COMPARISON -> VERIFICATION -> SUCCESS or RECOVERY

Nothing in this file is mocked. The target is a genuine NSApplication in its
own process, with genuine NSButtons publishing a genuine accessibility tree,
and it changes only when a real OS mouse event reaches it. A click that
updated Diablo's own state would prove nothing; this one is confirmed twice
over — by the application printing to its own stdout, and by Diablo reading
the change off the screen it did not write.

    python3 tests/golden_path.py              # the controlled target
    python3 tests/golden_path.py --app Zen --intent reload --expect ""

Computer use is switched on for this process only, through the environment,
before the module is imported. Production stays off.
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "fixtures", "target_app.py")
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def stage(name, detail=""):
    print(f"  {GREEN}✓{RESET} {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def failed(name, detail=""):
    print(f"  {RED}✗ {name}{RESET}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def run(intent="Mark Done Later", expect="POSTPONED", app=None, quiet=False):
    """One pass of the golden path. Returns (ok, ActionRecord, timings)."""
    # Switched on for this process only, before computer_use is imported.
    os.environ["DIABLO_COMPUTER_USE"] = "1"
    import importlib
    from skills import computer_use
    importlib.reload(computer_use)
    from skills import ax, perception, screen, screen_context

    timings = {}
    started = time.time()

    child = None
    if app is None:
        child = subprocess.Popen([sys.executable, TARGET],
                                 stdout=subprocess.PIPE, text=True, bufsize=1)
        deadline = time.time() + 12
        while time.time() < deadline:
            if (child.stdout.readline() or "").strip() == "READY":
                break
        time.sleep(1.2)
        app = "Diablo Target"
    timings["launch"] = time.time() - started

    try:
        mark = time.time()
        window = screen_context.find_window(app)
        timings["find_window"] = time.time() - mark
        if not window:
            failed("application identified", f"no window matching {app!r}")
            return False, None, timings
        # Never click outside the fixture unless a real application was asked
        # for by name on the command line.
        #
        # find_window matches generously, which is right for a person saying
        # "my browser" and wrong for a test that is about to post a real mouse
        # event. If the fixture failed to launch, a loose match could put that
        # click into somebody's actual work.
        if child is not None and (window.get("title") or "").strip() != "Diablo Target":
            failed("the window found is the fixture, not a real application",
                   f"found {window['app']} — {window.get('title','')[:40]!r}")
            return False, None, timings

        stage("application identified",
              f"{window['app']} — {window['title'][:40]}  "
              f"({timings['find_window']*1000:.0f}ms)")

        mark = time.time()
        frame = screen.capture_this_window(window)
        timings["capture"] = time.time() - mark
        ok, why = frame.ok, frame.problem
        details = (f"{frame.pixel_width}x{frame.pixel_height}px on "
                   f"{frame.display_name} ({timings['capture']*1000:.0f}ms)")
        frame.release()
        if not ok:
            failed("screen captured and valid", why)
            return False, None, timings
        stage("screen captured and valid", details)

        mark = time.time()
        available = ax.available()
        elements = ax.elements_for_window(window)
        timings["accessibility"] = time.time() - mark
        stage("perception — accessibility",
              f"{len(elements)} controls, trusted={available} "
              f"({timings['accessibility']*1000:.0f}ms)")

        mark = time.time()
        reading = perception.read(intent, window)
        timings["locate"] = time.time() - mark
        if not reading.found:
            failed("target candidates", f"nothing matched {intent!r}")
            return False, None, timings
        stage("target candidates",
              " | ".join(f"{c.confidence:.2f} {c.describe()[:34]}"
                         for c in reading.candidates[:3])
              + f"  ({timings['locate']*1000:.0f}ms)")

        decision = computer_use.decide(reading, computer_use.risk_of(
            reading.best.label if reading.best else ""))
        stage("decision", f"{decision.verdict.upper()} — {decision.reason[:60]}")

        asked = []

        def confirm(question):
            asked.append(question)
            return True

        mark = time.time()
        record = computer_use.act_on(intent, task="golden path", expect=expect,
                                     window=window, confirm=confirm)
        timings["act_and_verify"] = time.time() - mark

        if asked:
            stage("permission asked", asked[0][:64])
        else:
            failed("permission asked", "acted without asking")

        if record.action != "click":
            failed("real mouse event dispatched", record.spoken[:70])
            return False, record, timings
        stage("real mouse event dispatched",
              f"at ({record.x:.0f}, {record.y:.0f})")

        # The application's own account, independent of anything Diablo saw.
        heard = ""
        if child:
            child.stdout.flush()
            os.set_blocking(child.stdout.fileno(), False)
            time.sleep(0.4)
            try:
                heard = child.stdout.read() or ""
            except Exception:
                heard = ""
            if "CLICKED" in heard:
                stage("the application received it", heard.strip().splitlines()[-1])
            else:
                failed("the application received it", "no click reported by the app")

        if record.verified:
            stage("verified from the screen", record.observed[:70])
        else:
            failed("verified from the screen", record.observed[:70] or record.spoken[:70])

        timings["total"] = time.time() - started
        ok = bool(record.verified) and (not child or "CLICKED" in heard)
        return ok, record, timings
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except Exception:
                child.kill()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default=None,
                        help="a real application, instead of the controlled target")
    parser.add_argument("--intent", default="Mark Done Later")
    parser.add_argument("--expect", default="POSTPONED")
    args = parser.parse_args()

    print(f"\nGolden path — {args.intent!r} in "
          f"{args.app or 'the controlled target application'}\n")
    ok, record, timings = run(args.intent, args.expect, args.app)

    print(f"\n  {'latency':22}")
    for name, seconds in timings.items():
        print(f"    {name:18} {seconds*1000:7.0f} ms")
    if record:
        print(f"\n  explanation: {record.explain()}")
    print(f"\n  {GREEN + 'GOLDEN PATH COMPLETE' if ok else RED + 'GOLDEN PATH FAILED'}{RESET}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
