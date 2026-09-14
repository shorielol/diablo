"""
Delegation: who gets handed what, and what happens when several of them work
at the same time.

The interesting failures here are not "it picked the wrong name". Assignment
decides attribution, not capability, so a miss shows the wrong face on a card.
The failures that matter are the ones where a split loses half the request, or
where two agents running at once tread on each other's permission callbacks.
Those are the checks that are written strictly.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import roster  # noqa: E402
from skills import agent_tools  # noqa: E402


# Assignment is judged on cases where a person would not hesitate. Anything
# genuinely ambiguous — "find the results" could be a file or a web search —
# is left out, because an eval you can only pass by overfitting is worse than
# no eval.
ASSIGNMENT = [
    ("look up who won the F1 race last night", "asmo"),
    ("search wikipedia for the treaty of versailles", "asmo"),
    ("what's the weather in pune", "asmo"),
    ("how much does a macbook cost", "asmo"),
    ("total the numbers in the sales csv on my desktop", "marbas"),
    ("write a summary into notes.txt on my desktop", "marbas"),
    ("append a line to todo.md", "marbas"),
    ("find the biggest file in downloads", "marbas"),
    ("delete the old drafts folder", "marbas"),
    ("what's on my screen right now", "vassago"),
    ("point at the send button", "vassago"),
    ("read the third paragraph off this page", "vassago"),
    ("what window is in front", "vassago"),
    ("take a screenshot", "vassago"),
    ("open spotify", "seere"),
    ("turn the volume down", "seere"),
    ("check my battery", "seere"),
    ("send a whatsapp to mum saying i'll be late", "seere"),
]

# Every one of these has a second half that needs the first half's answer.
# Splitting any of them would have one agent acting on a number nobody has
# computed yet, which is the whole reason the structural gate exists.
SEQUENTIAL = [
    "read the total from sales.csv and then send it to mum on whatsapp",
    "write the summary into notes.txt and then read it back to me",
    "find the biggest file in downloads and move it to the desktop after that",
    "check the weather and then tell me whether to take a coat, using that",
]

# Two or three errands that genuinely do not touch each other.
INDEPENDENT = [
    "look up the weather in pune and tell me what's on my screen",
    "search for noise cancelling headphones and also check how much disk space i have left",
    "find the f1 results, check my battery, and tell me what window is in front",
]


def main() -> int:
    r = Results()

    # ── The roster itself ────────────────────────────────────────────────
    r.head("Roster")
    ids = [a["id"] for a in roster.AGENTS]
    r.check("Diablo leads", ids[0] == "diablo", " ".join(ids))
    r.check("every agent has a name, a role and a colour",
            all(a.get("name") and a.get("role") and a.get("accent")
                for a in roster.AGENTS))
    r.check("agent ids are unique", len(set(ids)) == len(ids))

    # The three internal classes are a drawing of the permission gate. They
    # were never agents and they must not come back as cards.
    public_ids = {a["id"] for a in roster.roster()}
    r.check("the safety classes are not on the roster",
            not ({"scout", "scribe", "warden"} & public_ids), str(sorted(public_ids)))
    r.check("the served roster carries no routing internals",
            all("tools" not in a and "keywords" not in a for a in roster.roster()))

    # ...but the gate they described is still described somewhere, because the
    # trace log wants to know whether a call read, changed or was guarded.
    r.check("delete_file is still classed as guarded",
            roster.SAFETY_CLASS.get("delete_file") == "guarded")
    r.check("run_shell is still classed as guarded",
            roster.SAFETY_CLASS.get("run_shell") == "guarded")
    r.check("read_file is still classed as a read",
            roster.SAFETY_CLASS.get("read_file") == "reads")

    # ── Assignment ───────────────────────────────────────────────────────
    r.head("Assignment")
    misses = []
    for text, want in ASSIGNMENT:
        got = roster.assign(text)
        if got != want:
            misses.append(f"{text!r} -> {got} (wanted {want})")
    r.check(f"{len(ASSIGNMENT) - len(misses)}/{len(ASSIGNMENT)} unambiguous "
            f"requests go to the right agent",
            not misses, "; ".join(misses[:3]))
    r.check("an assignment is always a worker, never Diablo or Levi",
            all(roster.assign(t) in roster.WORKERS for t, _ in ASSIGNMENT))
    r.check("nonsense still gets someone", roster.assign("xyzzy plugh") in roster.WORKERS)

    # ── Splitting ────────────────────────────────────────────────────────
    r.head("Splitting")
    for text in SEQUENTIAL:
        with guard(r, f"refuses to split: {text[:44]}"):
            parts = roster.split(text)
            r.check(f"one agent keeps: {text[:44]}", len(parts) == 1,
                    f"split into {len(parts)}")
            if len(parts) == 1:
                r.check("  and the task is unchanged", parts[0][0] == text)

    for text in INDEPENDENT:
        with guard(r, f"splits: {text[:44]}"):
            parts = roster.split(text)
            ok = r.check(f"fans out: {text[:44]}", len(parts) > 1,
                         f"got {len(parts)}")
            if ok:
                # The check that actually protects the user: nothing asked for
                # may disappear into the gap between two agents.
                r.check("  nothing was dropped",
                        roster._covers(text, [p for p, _a in parts]))
                r.check("  each piece has a different agent",
                        len({a for _p, a in parts}) == len(parts),
                        str([a for _p, a in parts]))
                r.check("  no piece is empty", all(p.strip() for p, _a in parts))

    r.check("a short request is never split", len(roster.split("open spotify")) == 1)
    r.check("the number of pieces is capped",
            all(len(roster.split(t)) <= roster.MAX_PARTS for t in INDEPENDENT))

    # ── Running them at once ─────────────────────────────────────────────
    r.head("Parallel execution")
    with guard(r, "parallel run"):
        order = []
        lock = threading.Lock()

        def runner(subtask, agent_id, quiet, ask, confirm):
            with lock:
                order.append(("start", agent_id, time.time()))
            time.sleep(0.30)
            quiet("working")
            with lock:
                order.append(("end", agent_id, time.time()))
            return f"{agent_id} did {subtask}"

        pieces = [("one", "asmo"), ("two", "marbas"), ("three", "vassago")]
        began = time.time()
        results = roster.run_parallel(pieces, runner, roster.Attention())
        elapsed = time.time() - began

        r.check("every piece came back", len(results) == 3)
        r.check("results keep the order they were handed out, not the order "
                "they finished",
                [a for a, _t, _s in results] == ["asmo", "marbas", "vassago"])
        # Three 0.3s jobs in series is 0.9s. Anything under 0.6 is genuinely
        # concurrent; the slack is for thread start-up on a loaded machine.
        r.check("they really ran at the same time", elapsed < 0.60,
                f"{elapsed:.2f}s for 3 x 0.30s")
        starts = [t for kind, _a, t in order if kind == "start"]
        r.check("all three had started before any had finished",
                max(starts) < min(t for kind, _a, t in order if kind == "end"))

    with guard(r, "a failing agent does not take the others down"):
        def flaky(subtask, agent_id, quiet, ask, confirm):
            if agent_id == "marbas":
                raise RuntimeError("disk on fire")
            return f"{agent_id} fine"

        results = roster.run_parallel(
            [("a", "asmo"), ("b", "marbas"), ("c", "vassago")], flaky,
            roster.Attention())
        r.check("the other two still returned", len(results) == 3)
        failed = [s for _a, _t, s in results if "disk on fire" in s]
        r.check("the failure is reported, not swallowed", len(failed) == 1,
                str(failed))
        r.check("the failure names the agent",
                bool(failed) and "Marbas" in failed[0], str(failed))

    # ── One microphone ───────────────────────────────────────────────────
    r.head("Attention")
    with guard(r, "questions are serialised"):
        overlapping = []
        inside = []
        guard_lock = threading.Lock()

        def ask(question):
            with guard_lock:
                inside.append(question)
                if len(inside) > 1:
                    overlapping.append(list(inside))
            time.sleep(0.12)
            with guard_lock:
                inside.remove(question)
            return "yes"

        attention = roster.Attention(ask=ask)
        threads = [threading.Thread(target=lambda n=n: attention.ask_for(n)("well?"))
                   for n in ("Asmo", "Marbas", "Vassago")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        r.check("only one agent ever holds the microphone", not overlapping,
                str(overlapping))

    with guard(r, "a question says who is asking"):
        heard = []
        attention = roster.Attention(ask=lambda q: heard.append(q) or "ok",
                                     confirm=lambda q: heard.append(q) or True)
        attention.ask_for("Asmo")("which file?")
        attention.confirm_for("Seere")("delete four things")
        r.check("the question is attributed",
                heard and heard[0].startswith("Asmo"), str(heard[:1]))
        r.check("the confirmation is attributed",
                len(heard) > 1 and heard[1].startswith("Seere"), str(heard[1:2]))

    with guard(r, "no callbacks means no callbacks"):
        empty = roster.Attention()
        r.check("ask stays None when there is nobody to ask",
                empty.ask_for("Asmo") is None)
        r.check("confirm stays None", empty.confirm_for("Asmo") is None)

    # ── The permission gate under concurrency ────────────────────────────
    #
    # This is the one that would be a real bug rather than a cosmetic one. The
    # tool context used to be a plain module-level dict, so two agents starting
    # within a few milliseconds of each other shared one `confirm` — and the
    # one that got used was whichever wrote last.
    r.head("Permission callbacks under concurrency")
    with guard(r, "thread isolation"):
        agent_tools.CONTEXT["confirm"] = "main-thread"
        seen = {}
        barrier = threading.Barrier(3)

        def occupy(name):
            agent_tools.CONTEXT["confirm"] = name
            barrier.wait(timeout=3)          # force the overlap
            time.sleep(0.05)
            seen[name] = agent_tools.CONTEXT.get("confirm")

        threads = [threading.Thread(target=occupy, args=(n,))
                   for n in ("asmo", "marbas", "vassago")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        r.check("each agent keeps its own confirm callback",
                all(seen.get(n) == n for n in ("asmo", "marbas", "vassago")),
                str(seen))
        r.check("and none of them overwrote the caller's",
                agent_tools.CONTEXT.get("confirm") == "main-thread")

        fresh = {}
        t = threading.Thread(target=lambda: fresh.update(
            confirm=agent_tools.CONTEXT.get("confirm")))
        t.start(); t.join()
        r.check("a thread that set nothing borrows nothing",
                fresh["confirm"] is None, str(fresh))
    agent_tools.CONTEXT["confirm"] = None

    # ── Merging ──────────────────────────────────────────────────────────
    r.head("Merging")
    merged = roster.merge([("asmo", "a", "It is 31 degrees in Pune"),
                           ("vassago", "b", "A terminal window")])
    r.check("both answers survive",
            "31 degrees" in merged and "terminal window" in merged, merged)
    r.check("each answer is attributed",
            "Asmo" in merged and "Vassago" in merged, merged)
    r.check("one result is passed through unchanged",
            roster.merge([("asmo", "a", "Just this.")]) == "Just this.")
    r.check("nothing at all still says something",
            roster.merge([]).strip() != "")

    r.head("Announcing")
    r.check("one agent", roster.announce([("t", "asmo")]).startswith("Asmo is on it"))
    r.check("two agents", "Asmo and Marbas" in
            roster.announce([("t", "asmo"), ("u", "marbas")]))
    r.check("three agents", "Asmo, Marbas and Vassago" in
            roster.announce([("t", "asmo"), ("u", "marbas"), ("v", "vassago")]))

    return r.report("delegation")


if __name__ == "__main__":
    sys.exit(main())
