#!/usr/bin/env python3
"""Focused Phase 4B conversation-ownership checks.

These checks exercise the production Session and delegation source contracts
without starting Ollama, the microphone, or the worker queue.
"""

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402
from skills import conversation, delegation_tool, voice  # noqa: E402


PRODUCTION = Path(os.getenv("DIABLO_HOME", Path.home() / ".diablo"))


def _new_session():
    voice._current_generation = 0
    voice._generation_context.set(None)
    return conversation.Session()


def run():
    results = Results()

    results.head("A. stale interaction cannot commit conversation state")
    with guard(results, "stale commit guard is enforced"):
        session = _new_session()
        old = voice.begin_interaction()
        assert session.commit_turn(old, "old user", "old answer")
        current = voice.begin_interaction()
        before = session.snapshot()
        results.check("old pair is present before supersession",
                      len(before["turns"]) == 2)
        results.check("stale pair is rejected",
                      not session.commit_turn(old, "late user", "late answer"))
        results.check("stale single-turn write is rejected",
                      not session.remember("user", "late memory", generation=old))
        results.check("stale reset is rejected",
                      not session.reset(generation=old))
        results.check("stale focus is rejected",
                      not session.set_focus("late-agent", generation=old))
        results.check("stale screen write is rejected",
                      not session.set_last_screen_app("late-window", generation=old))
        stale_state = session.snapshot()
        results.check("stale writes did not refresh last_spoke_at",
                      stale_state["last_spoke_at"] == before["last_spoke_at"])
        results.check("new generation can commit",
                      session.commit_turn(current, "new user", "new answer"))
        state = session.snapshot()
        results.check("stale commit did not append a turn",
                      [t["content"] for t in state["turns"]] ==
                      ["old user", "old answer", "new user", "new answer"],
                      repr(state["turns"]))
        results.check("stale focus did not change focus", state["focus"] is None)
        results.check("stale screen write did not change screen",
                      state["last_screen_app"] is None)

    results.head("B. pair commits are atomic and ordered")
    with guard(results, "concurrent user/assistant pairs remain adjacent"):
        session = _new_session()
        generation = voice.begin_interaction()
        threads = [threading.Thread(
            target=session.commit_turn,
            args=(generation, f"user-{i}", f"answer-{i}"))
                   for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        turns = session.snapshot()["turns"]
        adjacent = all(
            turns[i]["role"] == "user"
            and turns[i + 1]["role"] == "assistant"
            and turns[i + 1]["content"] == turns[i]["content"].replace("user-", "answer-")
            for i in range(0, len(turns), 2))
        results.check("all pair commits completed", len(turns) == 24,
                      f"turns={len(turns)}")
        results.check("user and assistant messages are not interleaved", adjacent,
                      repr(turns))

    results.head("C. stale background work retains task identity only")
    with guard(results, "stale task callback is isolated from session state"):
        session = _new_session()
        old = voice.begin_interaction()
        session.commit_turn(old, "current request", "current answer")
        current = voice.begin_interaction()
        before = session.snapshot()
        task = session.record_task(
            "seere", "late research", [], "late result",
            task_id="job:late", generation=old)
        after = session.snapshot()
        results.check("stale task record is retained", "job:late" in after["tasks"])
        results.check("stale task does not refresh expiry",
                      after["last_spoke_at"] == before["last_spoke_at"])
        results.check("stale task does not append assistant turn",
                      after["turns"] == before["turns"])
        results.check("stale task does not change focus", after["focus"] is None)
        results.check("task record is complete", task["status"] == "done")
        results.check("current generation remains current", voice.is_current_generation(current))

    results.head("D. task identity is authoritative")
    with guard(results, "repeated completion is idempotent"):
        session = _new_session()
        generation = voice.begin_interaction()
        first = session.record_task(
            "marbas", "same task", [], "first result",
            task_id="job:same", generation=generation)
        second = session.record_task(
            "marbas", "same task", [], "late replacement",
            task_id="job:same", generation=generation)
        session.record_task(
            "marbas", "different task", [], "other result",
            task_id="job:other", generation=generation)
        state = session.snapshot()
        results.check("same task has one record", list(state["tasks"]) ==
                      ["job:same", "job:other"], repr(state["tasks"]))
        results.check("first completion remains authoritative",
                      first["result"] == second["result"] == "first result")

    results.head("E. detached snapshots protect model input")
    with guard(results, "snapshot is not a live mutable view"):
        session = _new_session()
        generation = voice.begin_interaction()
        session.commit_turn(generation, "before model", "answer")
        snapshot = session.snapshot()
        snapshot["turns"][0]["content"] = "mutated outside session"
        snapshot["tasks"]["new"] = {"status": "fake"}
        current = session.snapshot()
        results.check("turn snapshot is detached",
                      current["turns"][0]["content"] == "before model")
        results.check("task snapshot is detached", "new" not in current["tasks"])

    results.head("F. propagation and alternate paths")
    with guard(results, "production paths carry the generation"):
        conversation_source = (PRODUCTION / "skills" / "conversation.py").read_text()
        delegation_source = (PRODUCTION / "skills" / "delegation_tool.py").read_text()
        bridge_source = (PRODUCTION / "voice_bridge.py").read_text()
        manager_source = (PRODUCTION / "companion_manager.py").read_text()
        server_source = (PRODUCTION / "skills" / "server.py").read_text()
        results.check("reply snapshots before model generation",
                      'state = self.snapshot()' in conversation_source
                      and 'self.commit_turn(generation, text, answer)' in conversation_source)
        results.check("voice bridge passes generation to delegation",
                      'generation=generation)' in bridge_source
                      and 'delegation_tool.handle(' in bridge_source)
        results.check("queued jobs store generation",
                      '"generation": generation' in delegation_source
                      and 'generation=job.get("generation")' in delegation_source)
        results.check("manager alternate path starts generation",
                      'generation = voice.begin_interaction()' in manager_source
                      and 'generation=generation' in manager_source)
        results.check("server reset uses a fresh generation",
                      'generation = voice.begin_interaction()' in server_source
                      and 'session.reset(generation=generation)' in server_source)

    return results.report("phase4b conversation ownership")


if __name__ == "__main__":
    raise SystemExit(run())
