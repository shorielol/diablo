"""
What Diablo remembers, and what it must admit it does not.

Written after a measurement that failed completely. Asked "what was the
original objective?" at five session lengths, it got the answer wrong five
times out of five — and three of those were inventions rather than blanks:

    "The original objective was to discuss the weather and some unrelated
     errands."
    "The original objective was to generate a list of names."

The summariser prompt forbids that in those words. The 4B did it anyway, which
is the argument for keeping the opening turn in code instead of trusting a
model to compress it faithfully.

The live parts need Ollama and are skipped without it. The structural parts do
not, and they are the ones that guarantee the behaviour.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import conversation, local_brain  # noqa: E402

OBJECTIVE = ("I'm looking into finance apprenticeships. Focus on Delhi. "
             "Prioritise beginner-friendly programmes. I want actual training "
             "rather than unpaid work.")


def long_session(objective=OBJECTIVE, filler_turns=80):
    session = conversation.Session()
    session.remember("user", objective)
    session.remember("assistant", "Noted, Sir.")
    for i in range(filler_turns):
        session.remember("user", f"Unrelated errand number {i}, about the weather.")
        session.remember("assistant", "Right.")
    return session


def main():
    r = Results()

    # ── The opening survives whatever happens to the rest ────────────────
    r.head("The objective is kept, not summarised")
    session = long_session()
    r.check("the opening turn is held word for word",
            "finance apprenticeships" in session.opening
            and "Delhi" in session.opening, session.opening[:60])
    r.check("compaction has happened", bool(session.earlier),
            "if it has not, this test is not testing anything")
    r.check("and the opening is not what was compacted",
            "finance apprenticeships" in session.opening,
            "the summary may lose it; the opening may not")
    r.check("it survives being asked for after 80 unrelated exchanges",
            "apprenticeship" in session.opening.lower())

    # The detail beyond the first sentence matters too — the criteria are the
    # part a follow-up asks about.
    for detail in ["Delhi", "beginner-friendly", "training"]:
        r.check(f"{detail!r} survives in the opening", detail in session.opening)

    r.head("Clearing means clearing")
    session.reset()
    r.check("a reset forgets the objective", session.opening == "")
    r.check("and the summary with it", session.earlier == "")
    r.check("and the tasks", session.tasks == {})

    stale = long_session()
    stale.last_spoke_at = 1.0
    stale._expire_if_stale()
    r.check("a timeout forgets it too", stale.opening == "" and stale.turns == [],
            "the session is in-memory and self-erasing; that is the contract")

    # ── Compaction is not where the objective lives ──────────────────────
    r.head("Compaction cannot lose what it never held")
    session = long_session()
    r.check("the turns are bounded", len(session.turns) <= conversation.MAX_TURNS * 2,
            f"{len(session.turns)} turns")
    r.check("but far below the context budget",
            sum(len(t["content"]) for t in session.turns) < local_brain.NUM_CTX * 4 * 0.5,
            "compaction that fires while the window is nearly empty is just "
            "forgetting")

    # ── Honesty ──────────────────────────────────────────────────────────
    if not local_brain.available():
        r.skip("spoken memory answers", "Ollama is not running")
        return r.report("memory")

    r.head("Answering from what is actually retained")
    session = long_session()
    answer = session.reply("What was the original objective?")
    r.check("the objective comes back",
            "apprentice" in answer.lower() or "delhi" in answer.lower(),
            answer[:90])
    r.check("and it is not the filler",
            "weather" not in answer.lower(),
            f"invented from the filler: {answer[:90]}")

    r.head("And admitting what is not")
    fresh = conversation.Session()
    answer = fresh.reply("What did I tell you last Tuesday?")
    admits = any(phrase in answer.lower() for phrase in
                 ("don't have", "do not have", "no record", "nothing", "can't",
                  "cannot", "not sure", "don't know", "do not know", "no memory",
                  "remind me", "first thing"))
    r.check("no record means saying so", admits, answer[:110])
    r.check("and not inventing a Tuesday",
            "tuesday" not in answer.lower() or admits, answer[:110])

    return r.report("memory")


if __name__ == "__main__":
    sys.exit(main())
