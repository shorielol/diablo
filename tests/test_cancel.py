#!/usr/bin/env python3
"""
Stopping a task: the backend half of the window's Stop button.

  - the real agent loop stops before its next step when asked: the step under
    way finishes, nothing new starts;
  - a running job ends as cancelled, on disk, and its result is not announced;
  - a fanned-out job's parts end as cancelled, not failed;
  - a queued job never starts;
  - a finished job cannot be cancelled, and stays as it finished.

The model and the tools are scripted doubles. Nothing is spoken or touched.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

BASE = tempfile.mkdtemp(prefix="diablo-cancel-")
os.environ["DIABLO_BASE_DIR"] = BASE
os.environ["DIABLO_TRACE"] = os.path.join(BASE, "trace.jsonl")

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import agent_core, agent_tools, conversation, local_brain  # noqa: E402
from skills import delegation_tool as dt  # noqa: E402

dt._push = lambda event, **data: None
conversation.session.record_task = lambda *a, **k: None
conversation.session.remember = lambda *a, **k: None
conversation.session.set_focus = lambda *a, **k: None

REAL_RUN = agent_core.run
SAID: list = []
GATE = threading.Event()        # a job that waits here is "running"
RAN: list = []


def held_run(task, speak, ask=None, confirm=None, working=None, should_stop=None, **_):
    """An agent that works in steps until released, checking for a stop as the real one does."""
    RAN.append(task)
    while not GATE.wait(0.02):
        if should_stop is not None and should_stop():
            raise agent_core.Cancelled()
    return f"finished {task}"


def speaker(text, **_):
    SAID.append(str(text))


def fresh_store():
    path = os.path.join(tempfile.mkdtemp(prefix="jobs-", dir=BASE), "jobs.json")
    dt.store = dt.JobStore(path)
    return path


def status(job_id):
    return next(j for j in dt.store.recent(100) if j["id"] == job_id)["status"]


def wait_for(predicate, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def on_disk(path):
    with open(path) as f:
        return {j["id"]: j for j in json.load(f)}


def run():
    r = Results()

    r.head("The agent loop stops between steps")
    with guard(r, "a stop after two tool calls"):
        executed = []
        script = {"n": 0}

        def chat(messages, tools=None, **k):
            script["n"] += 1
            return {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "system_status", "arguments": {"n": script["n"]}}}]}
        saved = (local_brain.chat, agent_core.local_brain.chat, agent_core.plan,
                 agent_tools.execute)
        local_brain.chat = agent_core.local_brain.chat = chat
        agent_core.plan = lambda task: []
        agent_tools.execute = lambda name, args: executed.append(args) or "ok"
        stopped = False
        try:
            REAL_RUN("keep checking the status", speak=lambda *a, **k: None,
                     should_stop=lambda: len(executed) >= 2)
        except agent_core.Cancelled:
            stopped = True
        finally:
            (local_brain.chat, agent_core.local_brain.chat, agent_core.plan,
             agent_tools.execute) = saved
        r.check("the run raised Cancelled", stopped)
        r.check("after exactly the steps already under way", len(executed) == 2,
                f"{len(executed)} tool calls")
    with guard(r, "no stop asked for"):
        r.check("should_stop defaults to never",
                "should_stop" in REAL_RUN.__code__.co_varnames
                and REAL_RUN.__defaults__[-1] is None)

    agent_core.run = held_run

    r.head("A running job")
    with guard(r, "stopped while it works"):
        path = fresh_store()
        GATE.clear()
        SAID.clear()
        job = dt.submit("compare laptop stands", speaker,
                        pieces=[("compare laptop stands", "asmo")])
        r.check("it is running", wait_for(lambda: status(job["id"]) == "running"))
        r.check("cancel accepts it", dt.cancel(job["id"]).get("ok") is True)
        r.check("it ends as cancelled", wait_for(lambda: status(job["id"]) == "cancelled"))
        r.check("on disk as well", wait_for(
            lambda: on_disk(path)[job["id"]]["status"] == "cancelled"))
        time.sleep(0.2)
        r.check("and nothing was announced", SAID == [], str(SAID))
        r.check("it stays final", not dt.store.update(job["id"], status="running"))

    with guard(r, "a fan-out stopped while its parts work"):
        path = fresh_store()
        GATE.clear()
        job = dt.submit("both", speaker, pieces=[("find flights", "asmo"),
                                                 ("find hotels", "marbas")])
        r.check("it is running", wait_for(lambda: status(job["id"]) == "running"))
        time.sleep(0.1)
        dt.cancel(job["id"])
        r.check("the job ends as cancelled",
                wait_for(lambda: status(job["id"]) == "cancelled"))
        parts = on_disk(path)[job["id"]]["parts"]
        r.check("and every part says cancelled, not failed",
                [p["status"] for p in parts] == ["cancelled", "cancelled"],
                str([p["status"] for p in parts]))

    r.head("A queued job")
    with guard(r, "stopped before it starts"):
        path = fresh_store()
        GATE.clear()
        RAN.clear()
        first = dt.submit("first", speaker, pieces=[("first", "asmo")])
        wait_for(lambda: status(first["id"]) == "running")
        second = dt.submit("second", speaker, pieces=[("second", "asmo")])
        r.check("the second waits in the queue", status(second["id"]) == "queued")
        r.check("cancel accepts it", dt.cancel(second["id"]).get("ok") is True)
        r.check("it is cancelled at once", status(second["id"]) == "cancelled")
        GATE.set()
        wait_for(lambda: status(first["id"]) == "done")
        time.sleep(0.3)
        r.check("the first still finishes", status(first["id"]) == "done")
        r.check("the second never ran", RAN == ["first"], str(RAN))
        r.check("and stays cancelled on disk",
                on_disk(path)[second["id"]]["status"] == "cancelled")

    r.head("A finished job")
    with guard(r, "cannot be cancelled"):
        out = dt.cancel(first["id"])
        r.check("cancel refuses it", out.get("ok") is False, str(out))
        r.check("and it stays done", status(first["id"]) == "done")
        r.check("an unknown id is refused too", dt.cancel("nope0000").get("ok") is False)

    GATE.set()
    return r.report("cancel")


if __name__ == "__main__":
    try:
        sys.exit(run())
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
