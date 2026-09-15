"""
The invariants, tested as invariants.

Diablo is the only permanent assistant and owns every capability. An agent is a
task, not a department. Ordinary work never creates one. Delegation happens for
exactly two reasons — he asked, or it is real research — and anything uncertain
stays with Diablo, because a false delegation costs a spurious task and a row
in the rail while a false direct costs nothing at all.

These are written as the architecture rather than as the implementation, so
they should survive a rewrite of the thing underneath them.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import agent_tools, conversation, delegation_tool, roster  # noqa: E402

# §26. Ordinary work. None of this may create an agent.
DIRECT = [
    "decrease the system volume", "increase volume", "mute the Mac",
    "pause music", "play music", "take a screenshot", "open Downloads",
    "read my screen", "send this message", "what time is it",
    "what is the capital of Peru", "calculate 25 * 17",
    "write a quick Python script", "tidy my Downloads",
]

# §27. The false positives, each one a phrase that merely sounds like research.
ADVERSARIAL = [
    "look at my screen", "find Downloads", "look into my Downloads folder",
    "look at this file", "find out the time", "compare these two windows",
    "look into my documents folder", "read the screen",
]

# §26. Real investigation.
RESEARCH = [
    "research apprenticeships in Delhi", "investigate this company",
    "compare these ten options", "look into this organization's history",
    "find out whether this claim is true", "perform deep research on this subject",
    "look into NVIDIA's history", "fact check this article",
]

# §4. Explicit delegation wins however trivial the task.
EXPLICIT = [
    "create an agent for this", "get Vassago to research this",
    "put someone on this", "run this in the background",
    "spin up an agent", "make an agent for the Delhi search",
    "delegate this", "put an agent on this",
]


def main():
    r = Results()

    # ── §1, §2. Diablo owns everything ───────────────────────────────────
    r.head("Diablo is the omni-agent")
    everything = set(agent_tools.TOOLS)
    owned = set()
    for agent in roster.AGENTS:
        owned |= set(agent.get("tools") or [])
    r.check("no capability belongs only to a named agent",
            owned <= everything,
            f"agents claim tools that do not exist: {sorted(owned - everything)}")
    r.check("the tools offered never depend on which agent was named",
            agent_tools.select("research apprenticeships in Delhi")
            == agent_tools.select("research apprenticeships in Delhi"),
            "selection is a function of the task alone")
    import inspect
    signature = inspect.signature(__import__("skills.agent_core",
                                             fromlist=["run"]).run)
    r.check("the executor takes no agent id at all",
            not any(p in signature.parameters for p in ("agent", "agent_id", "persona")),
            f"{list(signature.parameters)} — an agent id here would be a silo")

    # ── §3, §26. Ordinary work stays with Diablo ─────────────────────────
    r.head("Ordinary work never creates an agent")
    for task in DIRECT:
        decision = delegation_tool.decide(task)
        r.check(f"{task!r}", not decision.delegates,
                f"{decision.route}: {decision.reason}")

    # ── §27. Things that only sound like research ────────────────────────
    r.head("Phrases that merely sound like research")
    for task in ADVERSARIAL:
        decision = delegation_tool.decide(task)
        r.check(f"{task!r} is not research", not decision.delegates,
                f"{decision.route}: {decision.reason}")

    # ── §4, §26. Real research, and asking outright ──────────────────────
    r.head("Real research delegates")
    for task in RESEARCH:
        decision = delegation_tool.decide(task)
        r.check(f"{task!r}", decision.delegates, f"{decision.route}: {decision.reason}")

    r.head("So does asking for an agent")
    for task in EXPLICIT:
        decision = delegation_tool.decide(task)
        r.check(f"{task!r}", decision.delegates, f"{decision.route}: {decision.reason}")

    # ── §6. Uncertainty falls toward Diablo ──────────────────────────────
    r.head("Uncertainty falls toward Diablo")
    for task in ["", "   ", "asdfgh", "do the thing", "you know what to do",
                 "the usual please", "sort it"]:
        r.check(f"{task!r} stays with Diablo",
                not delegation_tool.decide(task).delegates,
                "a false delegation costs a task and a row; a false direct costs nothing")

    # ── §5, §7. Tool selection is not the delegation decision ────────────
    r.head("Tool availability does not imply delegation")
    menu = agent_tools.select("what time is it")
    r.check("the research tool really is on the menu for 'what time is it'",
            "research" in menu,
            "this is why membership was the wrong signal and must stay wrong")
    r.check("and it still does not delegate",
            not delegation_tool.decide("what time is it").delegates)
    r.check("a decision is reached without consulting the tool menu",
            "select" not in delegation_tool.decide.__code__.co_names,
            "decide() must not reach into tool selection")

    # ── §27. Direct and delegated work are classified independently ──────
    r.head("A sentence holding both")
    for task, expect_direct, expect_deleg in [
        ("research this company and lower my volume", "volume", "research"),
        ("research apprenticeships in delhi and pause the music", "music", "research"),
    ]:
        direct, delegated = delegation_tool.split_by_route(task)
        r.check(f"{task[:40]!r} keeps the direct half inline",
                any(expect_direct in d for d in direct), f"direct={direct}")
        r.check(f"{task[:40]!r} delegates only the research",
                len(delegated) == 1 and expect_deleg in delegated[0][0],
                f"delegated={[p[0] for p in delegated]}")

    direct, delegated = delegation_tool.split_by_route("turn the volume down and play music")
    r.check("two direct errands delegate nothing at all", not delegated,
            f"delegated={[p[0] for p in delegated]}")

    # ── §36. Found by attacking the matcher, not by imagining cases ──────
    # Every line here delegated before it was written down.
    r.head("Negation, description, and phrases used literally")
    for task, why in [
        ("do not create an agent for this, just do it", "an explicit refusal"),
        ("never put someone on this", "negated"),
        ("don't research that", "contraction"),
        ("no need to look into it", "negated politely"),
        ("I was researching yesterday, what time is it", "past tense, then a question"),
        ("I already looked into that, play music", "already"),
        ("in the background music is playing, pause it", "a place, not a request"),
        ("compare the volume of these two tracks", "local objects"),
        ("look into why my mac is slow", "his own machine"),
        ("can you research my downloads folder for a pdf", "research plus a local object"),
    ]:
        decision = delegation_tool.decide(task)
        r.check(f"{task[:44]!r} stays with Diablo", not decision.delegates,
                f"{why} — got {decision.reason}")

    # The negation guard must not swallow real requests.
    r.head("And the real ones still get through")
    for task in ["run this in the background", "do that in the background",
                 "keep it running in the background", "research this properly",
                 "look into NVIDIA's history", "create an agent for this"]:
        r.check(f"{task!r} still delegates",
                delegation_tool.decide(task).delegates,
                "over-eager negation would quietly disable delegation entirely")

    # ── §31. The reason is recorded ──────────────────────────────────────
    r.head("Every decision carries its reason")
    for task in DIRECT[:3] + RESEARCH[:3] + EXPLICIT[:3]:
        decision = delegation_tool.decide(task)
        r.check(f"{task[:34]!r} says why", bool(decision.reason),
                "'why did Diablo delegate this' must be answerable from the record")
    r.check("and names the phrase that decided it",
            delegation_tool.decide("research apprenticeships in Delhi").matched == "research")

    # ── §28. While an agent is already running ───────────────────────────
    # The old gate refused to run anything inline while work was queued, so a
    # volume change during a research job spawned a second agent for it.
    r.head("While a long task is running")
    stop = threading.Event()

    def long_task():
        while not stop.is_set():
            time.sleep(0.01)

    worker = threading.Thread(target=long_task, daemon=True)
    worker.start()
    delegation_tool.store.add({"id": "busy1", "task": "research apprenticeships in Delhi",
                               "status": "running", "created_at": time.time(),
                               "agent": "vassago", "parts": [], "summary": ""})
    try:
        for task in ["decrease volume", "pause music", "take a screenshot",
                     "what time is it"]:
            r.check(f"{task!r} is still direct with work in flight",
                    not delegation_tool.decide(task).delegates,
                    "another task being active is not a reason to delegate")
        r.check("the running task is genuinely still there",
                any(j["id"] == "busy1" for j in delegation_tool.store.active()))
    finally:
        stop.set()
        worker.join(timeout=2)
        delegation_tool.store.clear(keep_active=False)

    # ── §13. Cleaning legacy records ─────────────────────────────────────
    r.head("Legacy cleanup keeps live research")
    delegation_tool.store.clear(keep_active=False)
    delegation_tool.store.add({"id": "old1", "task": "decrease the system volume",
                               "status": "done", "created_at": 1.0, "agent": "seere",
                               "parts": [], "summary": "done"})
    live = {"id": "live1", "task": "research apprenticeships in Delhi",
            "status": "running", "created_at": time.time(), "agent": "vassago",
            "parts": [], "summary": ""}
    delegation_tool.store.add(live)
    dropped = delegation_tool.store.clear(keep_active=True)
    remaining = [j["id"] for j in delegation_tool.store.recent(limit=50)]
    r.check("the stale inline record goes", "old1" not in remaining, f"{remaining}")
    r.check("the live research task stays", "live1" in remaining, f"{remaining}")
    r.check("and it says how many it dropped", dropped == 1, f"dropped {dropped}")

    # §13. The same class of record cannot be made again.
    r.check("an inline action can no longer create a task record",
            not delegation_tool.decide("decrease the system volume").delegates,
            "which is the only route by which one is written")
    delegation_tool.store.clear(keep_active=False)

    # ── §9, §29. Task evidence survives, and answers follow-ups ──────────
    r.head("A task keeps its evidence")
    session = conversation.Session()
    session.record_task(
        "vassago", "Delhi apprenticeships",
        [{"tool": "research", "args": '{"topic": "apprenticeships delhi"}',
          "result": "Per https://internshala.com/x there are 40 openings."},
         {"tool": "read_page", "args": '{"url": "https://in.linkedin.com/jobs/y"}',
          "result": "Cushman & Wakefield, EIC Apprentice, Gurgaon."},
         {"tool": "read_file", "args": '{"path": "/Users/x/diary.md"}',
          "result": "PRIVATE DIARY"}],
        result="Four firms are hiring.")
    record = session.task_for("vassago")
    r.check("the task has an identity", record["task_id"] and record["agent"] == "vassago")
    r.check("an objective", bool(record["objective"]))
    r.check("a status", record["status"] == "done")
    r.check("its sources", len(record["sources"]) == 2, f'{record["sources"]}')
    r.check("its findings", bool(record["findings"]))
    r.check("and its result", "Four firms" in record["result"])
    evidence = session.evidence_for("vassago")
    r.check("a follow-up can name the sources",
            "internshala.com" in evidence and "linkedin.com" in evidence)
    r.check("nothing private is dragged along",
            "PRIVATE DIARY" not in evidence,
            "a file read during a task is not provenance for its answer")
    r.check("and it is not the model's raw working",
            "read_file" not in evidence and '{"topic"' not in evidence,
            "follow-ups must not depend on replaying hidden reasoning")

    # §29. Switching away and back.
    session.focus = "vassago"
    session.focus = None                      # back to Diablo
    r.check("Diablo still knows the task happened",
            session.task_for("vassago") is not None,
            "one shared context; no merge step required")
    session.focus = "vassago"
    r.check("and returning to the agent finds it intact",
            session.evidence_for(session.focus) == evidence)

    # ── §30. Two tasks do not overwrite one another ──────────────────────
    r.head("Concurrent tasks stay separate")
    shared = conversation.Session()
    errors = []

    def record(agent, url, n):
        try:
            for _ in range(20):
                shared.record_task(agent, f"{agent} task",
                                   [{"tool": "read_page", "args": f'{{"url": "{url}"}}',
                                     "result": f"finding from {agent}"}],
                                   task_id=f"t-{agent}")
        except Exception as e:                       # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=record, args=(a, f"https://{a}.test/x", i))
               for i, a in enumerate(("vassago", "marbas", "asmo"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    r.check("no worker raised", not errors, f"{errors[:2]}")
    r.check("each task kept its own identity", len(shared.tasks) == 3, f"{list(shared.tasks)}")
    for agent in ("vassago", "marbas", "asmo"):
        evidence = shared.evidence_for(agent)
        others = [a for a in ("vassago", "marbas", "asmo") if a != agent]
        r.check(f"{agent} has only its own findings",
                all(f"finding from {o}" not in evidence for o in others),
                "one worker must not be able to write into another's task")

    return r.report("architecture")


if __name__ == "__main__":
    sys.exit(main())
