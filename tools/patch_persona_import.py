#!/usr/bin/env python3
"""
Fixes the crash that made Diablo look deaf.

`personality` was referenced in three places in voice_bridge.py and imported in
none of them. Python does not care until the line runs, so the file compiled,
started, and answered anything said in one breath ("Hey Diablo, what am I
watching") — the path that never touches it.

Say just "Hey Diablo" and wait, though, and it reaches the greeting:

    NameError: name 'personality' is not defined     voice_bridge.py:436

That runs on a daemon thread, so nothing is spoken, nothing is logged where you
would see it, and the wake listener goes straight back to idle. From the outside
it is indistinguishable from a microphone that is not working — which is exactly
what it was mistaken for.

The dead module is not restored. Its three call sites move to persona.py, which
is the one the humour and warmth dials actually drive, so the greeting is now
time-aware rather than a fixed list indexed by a random number.

The second change matters more than the first: handle_utterance now catches
BaseException and says so out loud. A silent thread death is a bug you can only
find by reading logs, and the whole point of a voice assistant is that you are
not reading logs.

    python3 patch_persona_import.py [--dry-run] [--restore]
"""

import argparse
import glob
import os
import re
import shutil
import sys
import time

BRIDGE = os.getenv("DIABLO_BRIDGE", os.path.join(
    os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")), "voice_bridge.py"))

MARKER = "# ─── Diablo persona wiring ───"


def restore():
    backups = sorted(glob.glob(BRIDGE + ".perbak-*"))
    if not backups:
        print("✗ No persona-patch backups found.")
        return 1
    shutil.copy2(backups[-1], BRIDGE)
    print(f"✓ Restored from {backups[-1]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    args = ap.parse_args()

    if args.restore:
        return restore()
    if not os.path.exists(BRIDGE):
        print(f"✗ Cannot find {BRIDGE}")
        return 1

    src = open(BRIDGE).read()
    if MARKER in src:
        print("= Persona wiring is already installed. Nothing to do.")
        return 0

    steps = []

    # 1. Import it. persona is already on the skills path and already indexed.
    imp = "from skills import delegation_tool, agent_tools, mic"
    if imp not in src:
        print("✗ Could not find the skills import line.")
        return 1
    src = src.replace(imp, imp + ", persona  " + MARKER, 1)
    steps.append("import persona")

    # 2. The two acknowledgements.
    n = src.count("personality.get_random_ack()")
    src = src.replace("personality.get_random_ack()", "persona.ack()")
    if n:
        steps.append(f"{n} acknowledgement{'s' if n > 1 else ''} -> persona.ack()")

    # 3. The greeting. greet_id picked one of three fixed lines; speak() ignores
    #    cached_key now (voice.py caches on the text itself), so the index bought
    #    nothing. persona.greeting() knows what time it is.
    greet = re.compile(
        r"[ \t]*greet_id = random\.randint\(1, 3\)\n"
        r"([ \t]*)text = capture_speech\(\n"
        r"[ \t]*COMMAND_DURATION,\n"
        r"[ \t]*prompt=personality\.RESPONSES_GREETING\[greet_id - 1\],\n"
        r"[ \t]*\)\n")
    src, hit = greet.subn(
        lambda m: f"{m.group(1)}text = capture_speech(COMMAND_DURATION, "
                  f"prompt=persona.greeting())\n", src, count=1)
    if not hit:
        print("✗ Could not find the greeting capture block.")
        return 1
    steps.append("greeting -> persona.greeting() (time-aware)")

    # 4. Never let this class of bug be silent again.
    anchor = "def handle_utterance(command):\n    threading.Thread(target=_warm_the_brain, daemon=True).start()\n"
    if anchor in src:
        src = src.replace(anchor, anchor.rstrip("\n") + """

    try:
        _handle_utterance(command)
    except BaseException as e:
        # This used to die on a daemon thread and take the whole reply with it,
        # which reads as a microphone fault rather than a crash. Say it out loud.
        import traceback
        print(f"  [Utterance error] {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            speak("Something went wrong there, Sir.")
        except BaseException:
            pass


def _handle_utterance(command):
""", 1)
        steps.append("handle_utterance no longer dies silently")
    else:
        print("✗ Could not find handle_utterance's warm-on-wake line.")
        return 1

    if "personality." in src:
        print(f"✗ Still {src.count('personality.')} reference(s) to the dead module.")
        return 1

    for s in steps:
        print(f"  + {s}")

    try:
        compile(src, BRIDGE, "exec")
    except SyntaxError as e:
        print(f"\n✗ The patched file would not compile: {e.msg} (line {e.lineno})")
        return 1

    if args.dry_run:
        preview = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "voice_bridge.persona-preview.py")
        open(preview, "w").write(src)
        print(f"\n✓ Dry run clean, compiles. Preview: {preview}")
        return 0

    backup = f"{BRIDGE}.perbak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(BRIDGE, backup)
    open(BRIDGE, "w").write(src)
    print(f"\n✓ Patched.  Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
