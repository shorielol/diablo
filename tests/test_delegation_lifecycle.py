"""
Delegated work: a bounded objective, an honest outcome, and a record that only
moves forward.

Diablo hands work to its agents through delegation_tool: handle() decides,
submit() queues, a worker runs agent_core.run, and the job store is what the
window and "what are you working on" read back. Three things went wrong on
that path and each is checked here against the real code, with a fake agent in
place of the model so nothing on the machine is touched:

  - the direct half of a mixed request ran inline and was then handed to the
    agent again, inside a task that still asked for it;
  - an agent that failed was recorded as done, and so was a job whose every
    agent failed;
  - finished work was rewritten as failed when speaking the result failed.

The rules come from studying how another agent runtime bounds its short-lived
helpers (an explicit task, a terminal status of completed or failed, a final
state that stays final). The code is Diablo's own.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

BASE = tempfile.mkdtemp(prefix="diablo-delegation-")
os.environ["DIABLO_BASE_DIR"] = BASE
os.environ["DIABLO_TRACE"] = os.path.join(BASE, "trace.jsonl")

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import agent_core, agent_tools, computer_use, conversation  # noqa: E402
from skills import delegation_tool as dt  # noqa: E402
from skills import local_brain, roster, tutor  # noqa: E402

# ─── Doubles: no model, no voice, no window, no real agent ───────────────────

EVENTS: list = []
CALLS: list = []
INLINE: list = []
SAID: list = []

dt._push = lambda event, **data: EVENTS.append((event, data))
conversation.session.record_task = lambda *a, **k: None
conversation.session.remember = lambda *a, **k: None
conversation.session.set_focus = lambda *a, **k: None


def fake_run(task, speak, ask=None, confirm=None, working=None, **_):
    CALLS.append({"task": task, "ask": ask, "confirm": confirm})
    if confirm is not None and "delete" in task:
        if not confirm("delete the drafts folder"):
            return "I left it alone."
    if "boom" in task:
        raise RuntimeError("tool exploded")
    return f"did {task}"


agent_core.run = fake_run


def speaker(text, **_):
    SAID.append(str(text))


def fresh_store():
    path = tempfile.mkstemp(prefix="jobs-", suffix=".json", dir=BASE)[1]
    os.unlink(path)
    dt.store = dt.JobStore(path)
    return path


def settle(seconds=10.0):
    """Wait for the queue to drain, but never forever: a dead worker is a result."""
    deadline = time.time() + seconds
    while dt._queue.unfinished_tasks and time.time() < deadline:
        time.sleep(0.01)
    return dt._queue.unfinished_tasks == 0


def run_job(task, pieces, speak=speaker, ask=None, confirm=None):
    job = dt.submit(task, speak, ask=ask, confirm=confirm, pieces=pieces)
    settle()
    return next(j for j in dt.store.recent(100) if j["id"] == job["id"])


def stub_router(split_parts):
    """handle() with the model calls replaced; routing and splitting stay real."""
    local_brain.available = lambda: True
    local_brain.chat_json = lambda *a, **k: {"parts": list(split_parts)}
    dt._by_lane = lambda *a, **k: None
    dt.route = None
    dt._show = lambda *a, **k: None
    tutor.wants_teaching = lambda t: False
    agent_core.understand = lambda text, **k: {"action": "act", "task": text,
                                               "question": "", "reply": ""}
    dt._run_inline = lambda task, *a, **k: INLINE.append(task) or "inline"


def handled(text, split_parts=()):
    stub_router(split_parts)
    INLINE.clear()
    CALLS.clear()
    fresh_store()
    outcome = dt.handle(text, speaker)
    settle()
    jobs = dt.store.recent(100)
    return outcome, list(INLINE), [j["task"] for j in jobs], [c["task"] for c in CALLS]


def run() -> int:
    r = Results()

    # ── The objective an agent is given ──────────────────────────────────
    r.head("A delegated agent is given only the delegated work")
    mixed = "research this company and lower my volume"
    for split_parts in ([], ["research this company", "lower my volume"]):
        with guard(r, f"mixed request, splitter says {split_parts or 'nothing'}"):
            outcome, inline, jobs, agent_tasks = handled(mixed, split_parts)
            r.check("the direct half runs inline, once", inline == ["lower my volume"],
                    str(inline))
            r.check("the job is only the research", jobs == ["research this company"],
                    str(jobs))
            r.check("no agent is asked to touch the volume",
                    agent_tasks and not any("volume" in t for t in agent_tasks),
                    str(agent_tasks))
            r.check("and the turn is reported as delegated work", outcome == "queued",
                    outcome)

    with guard(r, "a request that is all delegable is handed over whole"):
        task = "research the best laptops for students"
        outcome, inline, jobs, agent_tasks = handled(task)
        r.check("nothing ran inline", inline == [], str(inline))
        r.check("the job is the whole request", jobs == [task], str(jobs))
        r.check("the agent was given the whole request", agent_tasks == [task],
                str(agent_tasks))

    with guard(r, "a request with nothing to delegate makes no agent"):
        outcome, inline, jobs, agent_tasks = handled("lower my volume")
        r.check("it ran inline", inline == ["lower my volume"], str(inline))
        r.check("no job and no agent", jobs == [] and agent_tasks == [],
                f"{jobs} {agent_tasks}")

    # ── What a finished job says about itself ────────────────────────────
    r.head("A failed agent is recorded as failed")
    with guard(r, "one of two agents fails"):
        fresh_store()
        EVENTS.clear()
        job = run_job("x", [("boom one", "asmo"), ("tidy two", "marbas")])
        parts = {p["agent"]: p["status"] for p in job["parts"]}
        r.check("the failed part is failed", parts.get("asmo") == "failed", str(parts))
        r.check("the other part is done", parts.get("marbas") == "done", str(parts))
        r.check("the job is done, because some of it was",
                job["status"] == "done", job["status"])
        r.check("the summary says which one failed",
                "Asmo" in job["summary"] and "could not finish" in job["summary"],
                job["summary"])
        finished = [d for e, d in EVENTS if e == "agent_done"]
        r.check("the window hears that the failed agent stopped, and that it failed",
                {(d["agent"], d.get("ok")) for d in finished}
                == {("asmo", False), ("marbas", True)}, str(finished))

    with guard(r, "every agent fails"):
        fresh_store()
        EVENTS.clear()
        SAID.clear()
        job = run_job("x", [("boom a", "asmo"), ("boom b", "marbas")])
        r.check("the job is failed, not done", job["status"] == "failed", job["status"])
        r.check("every part is failed",
                all(p["status"] == "failed" for p in job["parts"]), str(job["parts"]))
        r.check("the window is told it failed, not that it succeeded",
                [e for e, _ in EVENTS if e.startswith("job_")
                 and e not in ("job_queued", "job_started")]
                == ["job_failed"], str([e for e, _ in EVENTS]))
        r.check("he is told what failed, not a generic apology",
                SAID and "could not finish" in SAID[-1], str(SAID[-1:]))

    with guard(r, "a single agent that fails"):
        fresh_store()
        job = run_job("boom alone", [("boom alone", "marbas")])
        r.check("is failed", job["status"] == "failed", job["status"])

    # ── The record only moves forward ────────────────────────────────────
    r.head("Lifecycle")
    with guard(r, "transitions"):
        fresh_store()
        dt.store.add({"id": "j1", "task": "t", "status": "queued", "parts": []})
        r.check("queued -> running", dt.store.update("j1", status="running"))
        r.check("running -> done", dt.store.update("j1", status="done", summary="ok"))
        r.check("done -> failed is refused", not dt.store.update("j1", status="failed"))
        r.check("done -> running is refused", not dt.store.update("j1", status="running"))
        r.check("a second completion is refused",
                not dt.store.update("j1", status="done", summary="again"))
        r.check("a finished job's fields cannot be rewritten",
                not dt.store.update("j1", summary="rewritten"))
        kept = next(j for j in dt.store.recent(10) if j["id"] == "j1")
        r.check("and the record is what it was when it finished",
                kept["status"] == "done" and kept["summary"] == "ok", str(kept))
        r.check("an unknown job is refused, not created",
                not dt.store.update("nope", status="running")
                and not any(j["id"] == "nope" for j in dt.store.recent(10)))
        dt.store.add({"id": "j2", "task": "t", "status": "queued", "parts": []})
        r.check("queued -> done skips running and is refused",
                not dt.store.update("j2", status="done"))
        r.check("queued -> failed is allowed (it never started)",
                dt.store.update("j2", status="failed"))
        r.check("failed -> done is refused", not dt.store.update("j2", status="done"))
        dt.store.add({"id": "j3", "task": "t", "status": "queued", "parts": []})
        r.check("a status that does not exist is refused",
                not dt.store.update("j3", status="paused"))

    with guard(r, "parts follow the same rules"):
        fresh_store()
        dt.store.add({"id": "p1", "task": "t", "status": "running",
                      "parts": [{"agent": "asmo", "task": "a", "status": "queued"}]})
        r.check("part queued -> running", dt.store.update_part("p1", 0, status="running"))
        r.check("part running -> failed", dt.store.update_part("p1", 0, status="failed"))
        r.check("part failed -> done is refused",
                not dt.store.update_part("p1", 0, status="done"))
        r.check("a part that does not exist is refused",
                not dt.store.update_part("p1", 5, status="running"))

    with guard(r, "after a restart"):
        path = fresh_store()
        dt.store.add({"id": "r1", "task": "t", "status": "running", "parts": []})
        dt.store.add({"id": "r2", "task": "t", "status": "queued", "parts": []})
        reopened = dt.JobStore(path)
        statuses = {j["id"]: j["status"] for j in reopened.recent(10)}
        r.check("work that was in flight is interrupted, not resumed or done",
                statuses == {"r1": "interrupted", "r2": "interrupted"}, str(statuses))
        r.check("and an interrupted job cannot be started again",
                not reopened.update("r1", status="running"))

    with guard(r, "speaking the result fails after the work succeeded"):
        fresh_store()
        SAID.clear()

        def broken_voice(text, **k):
            if k.get("speech_kind") == "TASK_RESULT":
                raise OSError("audio device gone")
            SAID.append(text)

        job = run_job("tidy the notes", [("tidy the notes", "marbas")], speak=broken_voice)
        r.check("the job is still done", job["status"] == "done", job["status"])
        r.check("its result is still the result", job["summary"] == "did tidy the notes",
                job["summary"])
        r.check("he is not told the work came apart",
                not any("came apart" in s for s in SAID), str(SAID))

    with guard(r, "a voice that fails every time does not stop the next job"):
        fresh_store()

        def dead_voice(text, **k):
            raise OSError("audio device gone")

        run_job("boom first", [("boom first", "marbas")], speak=dead_voice)
        after = run_job("tidy after", [("tidy after", "marbas")])
        r.check("the next job still runs and finishes", after["status"] == "done",
                after["status"])

    # ── What delegation does not change ──────────────────────────────────
    r.head("Boundaries")
    with guard(r, "an agent cannot delegate"):
        names = set(agent_tools.TOOLS)
        offending = sorted(n for n in names if any(
            w in n for w in ("delegat", "submit", "spawn", "subagent", "agent")))
        r.check("no tool an agent can call hands work to another agent", not offending,
                str(offending))
        source = open(agent_tools.__file__).read()
        r.check("the tool module does not import the delegation machinery",
                "delegation_tool" not in source and "import roster" not in source)

    with guard(r, "delegation grants no permission"):
        fresh_store()
        CALLS.clear()
        asked = []

        def parent_says_no(question):
            asked.append(question)
            return False

        job = run_job("x", [("delete the drafts", "marbas"), ("tidy notes", "asmo")],
                      confirm=parent_says_no)
        r.check("the agent's question reached the parent's confirm, with its name",
                asked == ["Marbas wants to delete the drafts folder"], str(asked))
        r.check("and the parent's no stood",
                any("left it alone" in (p.get("summary") or "") for p in job["parts"]),
                str(job["parts"]))
        fresh_store()
        CALLS.clear()
        run_job("x", [("delete the drafts", "marbas"), ("tidy notes", "asmo")])
        r.check("with no confirm from the parent, no agent gets one",
                CALLS and all(c["confirm"] is None for c in CALLS), str(CALLS))
        r.check("computer use is still off", computer_use.ENABLED is False)
        r.check("parallel agents are still bounded", 1 <= roster.MAX_PARTS <= 4,
                str(roster.MAX_PARTS))

    return r.report("delegation lifecycle")


if __name__ == "__main__":
    sys.exit(run())
