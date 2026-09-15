"""
Asking an agent how it got there.

The complaint this exists for: you cannot drill into what a sub-agent found.
That was never a threading problem. run_parallel collected every sub-agent's
narration and then discarded it whenever the final answer came back non-empty
— which is every time it worked. The sources were deleted the moment they
stopped being needed for the answer.

What is tested here is the part that makes keeping them affordable. The
context is 4096 tokens and read_page returns whole articles, so the working
cannot live in the conversation history: it sits to one side and is pulled in
only on a question about provenance. Three things therefore have to hold —
it is recorded, it is retrievable by the agent that did it, and it costs
nothing on every other turn.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import conversation, speech  # noqa: E402

RESEARCH = [
    {"tool": "search_web",
     "args": '{"query": "apprenticeships delhi finance accounts UG"}',
     "result": "1. Manoj Aggarwal & Associates - Articled Assistant, South Delhi\n"
               "2. VCAN & CO LLP - Article Trainee, CA Finalist"},
    {"tool": "read_page",
     "args": '{"url": "https://in.linkedin.com/jobs/apprentice-delhi"}',
     "result": "Cushman & Wakefield are hiring an EIC Apprentice in Gurgaon."},
]


def main():
    r = Results()
    session = conversation.Session()

    # ── 1. It is kept at all ─────────────────────────────────────────────
    r.head("The working survives the answer")
    session.remember_working("vassago", RESEARCH)
    r.check("what the agent did is recorded", len(session.working["vassago"]) == 2)
    notes = session.working_for("vassago")
    r.check("the real sources come back out", "Manoj Aggarwal" in notes
            and "Cushman" in notes, notes[:80])
    r.check("so do the calls that produced them", "search_web" in notes
            and "read_page" in notes)
    r.check("an agent that did nothing has nothing", session.working_for("levi") == "")
    r.check("nor does an unknown one", session.working_for(None) == "")

    # ── 2. It is asked for, not carried ──────────────────────────────────
    # The whole reason this is affordable. If these fire on ordinary turns,
    # a research run evicts the conversation from a 4096-token context to
    # answer a question he never asked.
    r.head("Only pulled in when he asks for it")
    drilling = ["where did you get that", "what are your sources",
                "how do you know that", "walk me through your working",
                "show me your working", "what did you read",
                "says who", "can you back that up", "how did you work that out",
                "What makes you say that?", "Where did you find it?"]
    for question in drilling:
        r.check(f"{question!r} asks about the working",
                conversation.asks_how_you_know(question))

    ordinary = ["what about apprenticeships in mumbai", "how do I open a new tab",
                "play some music", "what's the weather", "how are you",
                "tell me more about the second one", "how long does that take",
                "what time is it", "open my downloads folder",
                "how many did you find"]
    for question in ordinary:
        r.check(f"{question!r} does not",
                not conversation.asks_how_you_know(question))

    # ── 3. It cannot blow the context window ─────────────────────────────
    r.head("Bounded, because the context is 4096 tokens")
    huge = [{"tool": "read_page", "args": '{"url": "https://example.com/%d"}' % i,
             "result": "x" * 20000} for i in range(60)]
    session.remember_working("marbas", huge)
    kept = session.working["marbas"]
    r.check("the number of steps is capped",
            len(kept) <= conversation.WORKING_STEPS, f"{len(kept)} kept")
    r.check("each result is truncated",
            all(len(s["result"]) <= conversation.WORKING_RESULT for s in kept))
    rendered = session.working_for("marbas")
    r.check("and what reaches a prompt is capped again",
            len(rendered) <= conversation.WORKING_BUDGET + 400,
            f"{len(rendered)} chars against a {conversation.WORKING_BUDGET} budget")
    r.check("1.2 MB of pages becomes something a 4096 context can hold",
            len(rendered) < 3000, f"{len(rendered)} chars")

    # ── 4. It is never spoken ────────────────────────────────────────────
    # The reason it is a side store rather than XML in the answer. speech.py
    # verbalises angle brackets, so <internal_working> would be read aloud as
    # "less than internal working greater than" before every research answer.
    r.head("It never reaches the speaker by accident")
    spoken = speech.spoken("<internal_working>notes</internal_working>")
    r.check("angle brackets really are spoken out loud",
            "less than" in spoken, spoken)
    r.check("so the working is not in the answer at all",
            "search_web" not in speech.spoken("Found four firms in Delhi, Sir."))

    # ── 5. It is forgotten with everything else ──────────────────────────
    r.head("It expires with the conversation")
    session.reset()
    r.check("a reset clears the working too", session.working == {},
            "sources from a finished conversation must not resurface in the next")

    stale = conversation.Session()
    stale.remember_working("vassago", RESEARCH)
    stale.last_spoke_at = 1.0          # long ago
    stale._expire_if_stale()
    r.check("and so does a timeout", stale.working == {})

    # ── 6. One list per agent ────────────────────────────────────────────
    # Sub-agents run at once in their own threads. Shared state here was a
    # real bug in this project before: CONTEXT was module-level and the last
    # agent to start owned everyone's callbacks.
    r.head("Agents do not share a scratchpad")
    both = conversation.Session()
    both.remember_working("vassago", [dict(RESEARCH[0])])
    both.remember_working("marbas", [{"tool": "read_file",
                                      "args": '{"path": "notes.md"}',
                                      "result": "Budget is 40 lakh."}])
    r.check("each agent keeps its own", "Manoj" in both.working_for("vassago")
            and "40 lakh" in both.working_for("marbas"))
    r.check("and neither can see the other's",
            "40 lakh" not in both.working_for("vassago")
            and "Manoj" not in both.working_for("marbas"))

    return r.report("working notes")


if __name__ == "__main__":
    sys.exit(main())
