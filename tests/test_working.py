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

from skills import agent_tools, conversation, speech  # noqa: E402


def agent_tools_select(text):
    return agent_tools.select(text)

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
    session.record_task("vassago", "Delhi apprenticeships", RESEARCH)
    r.check("the task is recorded", "vassago" in session.tasks)
    notes = session.evidence_for("vassago")
    r.check("what it found comes back out",
            "Manoj Aggarwal" in notes and "Cushman" in notes, notes[:90])
    r.check("and so does the page it actually opened",
            "in.linkedin.com" in notes,
            "read_page's source is its argument, not its output")
    r.check("as structure, not a transcript of tool calls",
            "search_web(" not in notes and '{"query"' not in notes,
            "the raw calls are dropped; the evidence is kept")
    r.check("an agent that did nothing has nothing", session.evidence_for("levi") == "")
    r.check("nor does an unknown one", session.evidence_for(None) == "")

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
    r.head("Bounded, because the context is finite")
    huge = [{"tool": "read_page", "args": '{"url": "https://example.com/%d"}' % i,
             "result": "x" * 20000} for i in range(60)]
    session.record_task("marbas", "big read", huge)
    record = session.tasks["marbas"]
    r.check("findings are capped",
            len(record["findings"]) <= conversation.TASK_FINDINGS,
            f'{len(record["findings"])} kept')
    r.check("each one is truncated",
            all(len(f) <= conversation.TASK_FINDING_CHARS for f in record["findings"]))
    r.check("sources are capped too",
            len(record["sources"]) <= conversation.TASK_SOURCES,
            f'{len(record["sources"])} sources')
    rendered = session.evidence_for("marbas")
    r.check("and what reaches a prompt is capped again",
            len(rendered) <= conversation.WORKING_BUDGET,
            f"{len(rendered)} chars against {conversation.WORKING_BUDGET}")
    r.check("1.2 MB of pages becomes something a prompt can hold",
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
    r.check("a reset clears the working too", session.tasks == {},
            "sources from a finished conversation must not resurface in the next")

    stale = conversation.Session()
    stale.record_task("vassago", "Delhi apprenticeships", RESEARCH)
    stale.last_spoke_at = 1.0          # long ago
    stale._expire_if_stale()
    r.check("and so does a timeout", stale.tasks == {})

    # ── 6. One list per agent ────────────────────────────────────────────
    # Sub-agents run at once in their own threads. Shared state here was a
    # real bug in this project before: CONTEXT was module-level and the last
    # agent to start owned everyone's callbacks.
    r.head("Agents do not share a scratchpad")
    both = conversation.Session()
    both.record_task("vassago", "Delhi apprenticeships", [dict(RESEARCH[0])])
    both.record_task("marbas", "budget", [{"tool": "research",
                                  "args": "budget", "result": "Budget is 40 lakh."}])
    r.check("each agent keeps its own", "Manoj" in both.evidence_for("vassago")
            and "40 lakh" in both.evidence_for("marbas"))
    r.check("and neither can see the other's",
            "40 lakh" not in both.evidence_for("vassago")
            and "Manoj" not in both.evidence_for("marbas"))

    # ── 7. Who does the work ─────────────────────────────────────────────
    # "Decrease the system volume" is four words. The old gate was a word
    # count at four, so turning the volume down spawned an agent, announced
    # it, and left a row in the sidebar for a single AppleScript call.
    r.head("Trivial things do not get their own agent")
    from skills import delegation_tool  # noqa: E402

    direct = ["decrease the system volume", "turn the volume down", "mute",
              "play some music", "pause the music", "next track",
              "what time is it", "take a screenshot", "set brightness to fifty",
              "open my downloads folder", "what is on my screen",
              "send a message to mum", "what is the capital of peru",
              "turn the volume down and play music"]
    for task in direct:
        r.check(f"{task!r} runs on the main thread",
                not delegation_tool.needs_its_own_agent(task, task))

    r.head("Real research still does")
    deep = ["research apprenticeships in delhi for finance",
            "look into the best noise cancelling headphones",
            "compare the options for studying economics abroad",
            "find out about the new tax rules in india",
            "what are the best universities for finance"]
    for task in deep:
        r.check(f"{task[:44]!r} gets an agent",
                delegation_tool.needs_its_own_agent(task, task))

    r.head("And so does asking for one")
    asked = ["spin up an agent to research this", "get vassago to look into that",
             "put someone on the apprenticeship search",
             "delegate this to someone", "work on it in the background"]
    for task in asked:
        r.check(f"{task[:40]!r} is an explicit request",
                delegation_tool.needs_its_own_agent(task, task),
                "explicit instruction wins however small the task")

    # The trap the first attempt fell into: agent_tools.select() returns a
    # ranked menu, and the research tool's tags include "the", "what", "are",
    # "out" and "about", so it is in nearly every menu.
    r.check("routing does not just ask whether research is on the tool menu",
            "research" in agent_tools_select("what time is it"),
            "it is — which is why membership cannot be the test")

    # ── 8. Emptying the sidebar ──────────────────────────────────────────
    # The store is disk-backed but memory-authoritative: _flush writes
    # self._jobs, so emptying jobs.json by hand is undone by the next add().
    r.head("Clearing the job store")
    import json as _json, tempfile
    from skills.delegation_tool import JobStore

    path = tempfile.mktemp(suffix=".json")
    _json.dump([{"id": "a", "task": "decrease the system volume", "status": "done"},
                {"id": "b", "task": "play 'Ransom' by Lil Tecca", "status": "done"},
                {"id": "c", "task": "research apprenticeships", "status": "running"}],
               open(path, "w"))

    store = JobStore(path)
    r.check("work left running by a dead process is marked interrupted",
            [j["status"] for j in store._jobs].count("interrupted") == 1,
            "its thread died with the process; it is not still going")
    dropped = store.clear(keep_active=True)
    r.check("everything finished is dropped", dropped == 3, f"dropped {dropped}")
    r.check("and the file is emptied with it",
            len(_json.load(open(path))) == 0)
    r.check("so a restart does not bring them back",
            len(JobStore(path)._jobs) == 0,
            "the purge has to survive a reload or it was cosmetic")

    _json.dump([{"id": "d", "task": "research something long", "status": "done"}],
               open(path, "w"))
    live = JobStore(path)
    live._jobs[0]["status"] = "running"          # as if this process started it
    r.check("work still running in THIS process is kept",
            live.clear(keep_active=True) == 0 and len(live._jobs) == 1,
            "its thread carries on, and the row is the only sign of it")
    r.check("unless the caller says otherwise",
            live.clear(keep_active=False) == 1 and len(live._jobs) == 0)
    os.unlink(path)

    return r.report("working notes")


if __name__ == "__main__":
    sys.exit(main())
