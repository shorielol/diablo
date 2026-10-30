#!/usr/bin/env python3
"""
What a person hears when something inside Diablo fails.

Real use produced spoken lines naming the parts of Diablo rather than what
happened. The rule: say what happened, what could not be done and what comes
next, never the machinery. One translator, at the one place every spoken line
passes (voice.speak and voice.speak_stream), so no path can go round it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import speech  # noqa: E402

INTERNAL = ("voicebox", "voice box", "goclick", "ocr", "ollama", "qwen", "grounding_gate",
            "screenframe", "subprocess", "traceback", "exception", "errno", "reject_",
            "low_confidence", "not_found", ".py", "none.")


def run():
    r = Results()

    r.head("Failures are said as what happened, not as machinery")
    with guard(r, "translated"):
        for internal in ("VoiceBox failed.", "Voicebox timed out: ConnectionError",
                         "grounding_gate returned None.", "OCR timed out.",
                         "AX read failed.", "model subprocess error.", "worker crashed.",
                         "tool point_at failed.",
                         'Traceback (most recent call last): File "x.py", line 3',
                         "Error: [Errno 13] Permission denied: '/Users/x/a.txt'",
                         "RuntimeError: disk on fire",
                         "qwen3.5:4b is not installed. Run: ollama pull qwen3.5:4b",
                         "Perception verdict REJECT_LABEL_MISMATCH",
                         "GoClick worker exited with code 1"):
            said = speech.user_safe(internal)
            heard = [w for w in INTERNAL if w in said.lower()]
            r.check(f"{internal[:40]!r} -> no machinery", not heard and bool(said.strip()),
                    f"{said!r} {heard}")

    r.head("Ordinary sentences are left alone")
    with guard(r, "untouched"):
        for plain in ("Opened Google Chrome.", "Relax, the tax return is filed.",
                      "The worker in the factory model was replaced.",
                      "In Zen, it's Zen › Settings… (⌘,).",
                      "Voicebox is the app in front, and it has a Clear button.",
                      "None of these files changed."):
            r.check(f"{plain!r} unchanged", speech.user_safe(plain) == plain,
                    speech.user_safe(plain))

    r.head("It is applied where speech is made")
    with guard(r, "wired"):
        source = open(os.path.join(DIABLO_HOME, "skills", "voice.py")).read()
        speak_body = source.split("def speak(", 1)[1].split("\ndef ", 1)[0]
        stream_body = source.split("def speak_stream(", 1)[1].split("\ndef ", 1)[0]
        r.check("voice.speak translates", "user_safe" in speak_body)
        r.check("voice.speak_stream translates each piece", "user_safe" in stream_body)

    return r.report("user-safe speech")


if __name__ == "__main__":
    sys.exit(run())
