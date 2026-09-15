"""
What Diablo says when the model refuses.

It said this, out loud, to a real person:

    Ollama returned 400: {"error":{"code":400,"message":"request (4168
    tokens) exceeds the available context size of 4096 tokens"}}

Two separate failures in one sentence. The window was too small for a real
turn — 4096 was chosen on the assumption it held the prompt, the tool schemas
and a dozen exchanges, and it did not. And the refusal was interpolated
straight into the reply, so the status code and the response body were read
aloud.

The second is the one that must never come back, so it is tested at the
source: BrainOffline carries the detail for the log and a separate `spoken`
that is the only thing a caller may say. Nothing here needs Ollama running.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import local_brain  # noqa: E402

# The exact body from the failure, and the shapes Ollama uses elsewhere.
REAL = ('{"error":{"code":400,"message":"request (4168 tokens) exceeds the '
        'available context size of 4096 tokens"}}')
OTHER_CONTEXT = [
    "input length exceeds the maximum context length",
    '{"error":"prompt too many tokens for context size"}',
    "this model's maximum context length is 4096 tokens, however you requested 5000 tokens",
]
NOT_CONTEXT = [
    '{"error":"model \'qwen9\' not found, try pulling it first"}',
    '{"error":"invalid options: unknown field"}',
    "internal server error",
    '{"error":"unexpected EOF"}',
]

LEAKS = ("{", "}", "error", "http", "400", "500", "ollama", "traceback",
         "exception", "token", "json", "127.0.0.1", "api/chat")


def says_nothing_technical(r, label, spoken):
    lowered = spoken.lower()
    found = [w for w in LEAKS if w in lowered]
    r.check(f"{label} is safe to say out loud", not found,
            f"leaked {found} in {spoken!r}")


def main():
    r = Results()

    # ── 1. The window ────────────────────────────────────────────────────
    r.head("The context window")
    r.check("is bigger than the turn that overflowed it",
            local_brain.NUM_CTX > 4168, f"NUM_CTX={local_brain.NUM_CTX}")
    r.check("and is still a size this machine can hold",
            local_brain.NUM_CTX <= 16384, f"NUM_CTX={local_brain.NUM_CTX}")

    # ── 2. The refusal that started this ─────────────────────────────────
    r.head("The refusal it read out loud")
    e = local_brain._refusal(400, REAL)
    r.check("is recognised as the context being full",
            isinstance(e, local_brain.ContextFull), type(e).__name__)
    r.check("the prompt length is read off it", e.used == 4168, f"used={e.used}")
    r.check("and so is the limit it broke", e.limit == 4096, f"limit={e.limit}")
    r.check("the detail survives for the log", "4168" in str(e))
    says_nothing_technical(r, "what he hears", e.spoken)
    # It advises him to say something. That something has to work — advice
    # that does nothing is worse than no advice, and the first draft of this
    # message said "start again", which is not one of the reset phrases.
    from skills import conversation  # noqa: E402
    advised = re.findall(r'"([^"]+)"', e.spoken)
    r.check("it tells him what to do about it", bool(advised),
            "a dead end is not an answer; clearing the session is")
    for phrase in advised:
        r.check(f"and saying {phrase!r} really does clear it",
                conversation.wants_reset(phrase.lower()),
                "it is not in RESET_PHRASES, so nothing would happen")

    # ── 3. Every other way it is worded ──────────────────────────────────
    r.head("The same problem, worded differently")
    for body in OTHER_CONTEXT:
        got = local_brain._refusal(400, body)
        r.check(f"{body[:44]!r} is a full context",
                isinstance(got, local_brain.ContextFull), type(got).__name__)

    r.head("And problems that are not that")
    for body in NOT_CONTEXT:
        got = local_brain._refusal(400, body)
        r.check(f"{body[:40]!r} is not mistaken for one",
                not isinstance(got, local_brain.ContextFull),
                "telling him to clear the session would send him the wrong way")
        says_nothing_technical(r, f"{body[:26]!r}", got.spoken)

    # ── 4. Nothing can leak by accident ──────────────────────────────────
    # The bug was not that one caller was careless. It was that the unsafe
    # string was the easiest thing to reach for.
    r.head("The safe form is always there")
    plain = local_brain.BrainOffline("Ollama unreachable at http://127.0.0.1:11434")
    says_nothing_technical(r, "a bare BrainOffline", plain.spoken)
    r.check("even when nobody passed a spoken form", bool(plain.spoken))
    r.check("while the detail is kept intact",
            "127.0.0.1" in str(plain),
            "the log is where the address belongs")

    # ── 5. No caller reaches for the unsafe one ──────────────────────────
    r.head("No caller interpolates the exception")
    offenders = []
    for name in ("agent_core.py", "conversation.py", "screen_context.py",
                 "delegation_tool.py", "tutor.py"):
        path = os.path.join(DIABLO_HOME, "skills", name)
        if not os.path.exists(path):
            continue
        for number, line in enumerate(open(path), 1):
            if re.search(r'return f".*\{e\}', line) and "spoken" not in line:
                offenders.append(f"{name}:{number}")
    r.check("no module returns a formatted exception to the user",
            not offenders, f"{offenders}")

    return r.report("brain errors")


if __name__ == "__main__":
    sys.exit(main())
