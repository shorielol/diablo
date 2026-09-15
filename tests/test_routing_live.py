"""
Does the routing generalise, or does it only know the phrases it was shown?

The suite that matters for this question, and the only one that runs the
classifier. Everything here is a natural request written out rather than a
template with the nouns swapped, because a corpus of "research X", "research Y",
"research Z" measures nothing except that the word "research" is in a list.

It reports accuracy rather than asserting perfection. The gate is set at what
was actually measured, so a regression fails the suite and an improvement does
not need the gate moved. Slow — every case is a model call — so it is skipped
unless Ollama is up.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import agent_core, delegation_tool, local_brain  # noqa: E402

# §39. The same intent, said the way people actually say it.
DIRECT = [
    "turn it down", "make it quieter", "drop the volume a bit", "bit loud, that",
    "can you lower the volume", "mute that", "silence", "put the volume back up",
    "brightness down a notch", "screen's too dim",
    "pause", "pause the music", "skip this one", "play something",
    "take a screenshot", "grab a shot of this", "open my downloads",
    "what time is it", "what's the time", "how long until three",
    "what is the capital of Peru", "who wrote Dubliners", "explain recursion",
    "what does idempotent mean", "calculate 17 times 82", "what's 340 divided by 8",
    "tidy my downloads", "clean up that folder", "look at my screen",
    "what am I looking at", "what's on the screen right now",
    "send that to mum", "how much disk space have I got",
]

# §39. Research, phrased six ways that share no keyword.
RESEARCH = [
    "can you figure out what is going on with NVIDIA",
    "I want a proper assessment of this company before I apply",
    "what does the evidence actually say about intermittent fasting",
    "dig into the history of this organisation",
    "find out whether that claim about the tax rules is true",
    "give me a thorough picture of the apprenticeship market in Delhi",
    "I need to know what people are saying about this course",
    "get to the bottom of why this keeps being recommended",
    "put together what is known about this drug's side effects",
    "check across a few sources whether this is still accurate",
    "look into this properly, not just the first result",
    "build me a picture of the competitive landscape here",
]

# §26 of the earlier brief, restated: explicit wins whatever the task.
EXPLICIT = [
    "create an agent for this", "put an agent on this",
    "spin up an agent to watch this", "run this in the background",
    "make an agent for the Delhi search", "delegate this to someone",
]


def route_of(text):
    """
    Exactly what handle() does, including `said`.

    The first version of this dropped `said`, so it never exercised the path
    that keeps an explicit request alive through understand()'s rewrite — and
    reported a failure that only existed in the test.
    """
    verdict = agent_core.understand(text)
    return delegation_tool.decide(verdict.get("task") or text,
                                  operations=verdict.get("operations"),
                                  said=text)


def score(r, label, cases, want, gate):
    r.head(label)
    right, elapsed = 0, 0.0
    for text in cases:
        start = time.time()
        try:
            decision = route_of(text)
        except Exception as e:                                  # noqa: BLE001
            r.bad(f"{text[:44]!r} raised", f"{type(e).__name__}: {e}")
            continue
        elapsed += time.time() - start
        ok = decision.route == want
        right += ok
        if not ok:
            # Recorded, not failed. The gate below is the assertion: a model
            # classifier is not expected to be perfect, and failing each miss
            # as well as the gate counts the same shortfall twice and makes the
            # suite red at a level of accuracy that was deliberately accepted.
            r.skip(f"{text[:46]!r}", f"{decision.route}: {decision.reason[:50]}")
    accuracy = right / len(cases)
    r.check(f"{label}: {right}/{len(cases)} ({accuracy:.0%}), "
            f"{elapsed / len(cases):.1f}s each",
            accuracy >= gate,
            f"measured {accuracy:.0%}, gate {gate:.0%} — below the gate is a "
            f"regression, above it means the gate can be raised")
    return right, len(cases)


def main():
    r = Results()
    if not local_brain.available():
        r.skip("routing corpus", "Ollama is not running")
        return r.report("routing (live)")

    os.environ["DIABLO_ROUTE_CLASSIFY"] = "1"
    delegation_tool.CLASSIFY = True

    # Gates are the measured figures with room for the model's variance, not
    # targets. Direct and explicit are held at 100% because that is what was
    # measured and because a false delegation is the failure that matters.
    #
    # Research recall is 67% and the gate is 0.6. That is the 4B's ceiling, not
    # an acceptable destination: two of the four it misses are phrases written
    # into the classifier prompt as true examples, and it still answers false.
    # Raising it needs a better classifier — the 14B via DIABLO_ESCALATE_MODEL
    # would do it and costs about 8s on a cold load, which is why it has not
    # simply been switched on.
    #
    # The failure is survivable: a missed research request runs inline, using
    # the same research tool, and produces the same answer without a task row.
    # A false delegation announces an agent for a volume change. Those are not
    # equally bad and the thresholds say so.
    score(r, "Direct requests, said naturally", DIRECT, delegation_tool.DIRECT, 1.0)
    score(r, "Research, phrased without the obvious words", RESEARCH,
          delegation_tool.DELEGATE, 0.6)
    score(r, "Explicit requests for an agent", EXPLICIT,
          delegation_tool.DELEGATE, 1.0)

    # §37, as a property rather than a list: whatever the classifier decides,
    # an atomic control is never a task.
    r.head("The policy floor holds whatever the model says")
    for text in ["lower my volume", "mute", "take a screenshot", "what time is it"]:
        forced = [{"text": text, "intent": "research"}]
        decision = delegation_tool.decide(text, operations=forced)
        r.check(f"{text!r} stays direct against a research verdict",
                decision.route == delegation_tool.DIRECT, decision.reason)

    return r.report("routing (live)")


if __name__ == "__main__":
    sys.exit(main())
