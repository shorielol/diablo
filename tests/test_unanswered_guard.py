#!/usr/bin/env python3
"""Audio Phase 2D: an unanswered guard question ends the turn.

agent_core.run's question_not_asked guard speaks a final answer through ask()
when it looks like a question. If nobody answers, the turn used to carry on:
the same text came back out of run() and the caller said it a second time,
20-30 seconds later, or the loop asked the model again and said a new version.

Both real incidents from 19 September are reproduced here with a scripted
model and a recording speaker. No model, microphone or audio is used, and the
job store is a temporary file.
"""

import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from skills import agent_core, conversation, delegation_tool, local_brain  # noqa: E402

# Incident 1, word for word: a finished answer the guard read as a question
# because of the relative "which".
INCIDENT_1 = ("I did not make any changes because the request was to research "
              "the latest news about OpenAI, which I completed by retrieving and "
              "summarizing current updates. No file or system state was modified. "
              "The research is now complete and available in the search results above.")

# Incident 2's shape: a screen description the guard misreads, carrying a quote
# no tool returned, so an unanswered ask used to fall into quoted_unread and
# run the model a second time.
INCIDENT_2_FIRST = ('The frontmost window is a YouTube video titled "I asked '
                    'Jacksepticeye 100 Questions", which is playing in Zen.')
INCIDENT_2_SECOND = ("A corrupted YouTube page shows garbled text about a video "
                     "with Jacksepticeye and MrBeast.")

READ_SCREEN_CALL = {"content": "", "tool_calls": [
    {"function": {"name": "read_screen", "arguments": {}}}]}
FAKE_SCREEN = "Window: Zen\nTitle: a YouTube video\n\n--- what is on screen ---\nyoutube"

ORDINARY = "It is fourteen oh one, Sir."


class _Model:
    """Returns scripted final answers in order and counts the calls."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def chat(self, messages, tools=None, **_kwargs):
        self.calls += 1
        answer = self.answers[min(self.calls, len(self.answers)) - 1]
        return answer if isinstance(answer, dict) else {"content": answer}


class _Voice:
    """Records every utterance: asked questions and ordinary speech alike."""

    def __init__(self, reply=""):
        self.reply = reply
        self.said = []
        self.asked = []

    def speak(self, text, **_kwargs):
        self.said.append(str(text))
        return "test"

    def ask(self, question):
        self.asked.append(question)
        self.said.append(str(question))
        return self.reply

    def times(self, text):
        return sum(1 for s in self.said if s.strip() == text.strip())


class _Patched:
    """Swap the model, the session and the job store for the duration."""

    def __init__(self, model):
        self.model = model

    def __enter__(self):
        self.saved_chat = local_brain.chat
        self.saved_chat_json = local_brain.chat_json
        self.saved_session = conversation.session
        self.saved_store = delegation_tool.store
        local_brain.chat = self.model.chat
        # plan() asks the model for steps on anything with an "and" in it. The
        # planner is not under test, and its call must not eat a scripted answer.
        local_brain.chat_json = lambda *_a, **_k: {"steps": []}
        conversation.session = conversation.Session()
        self.tmp = tempfile.TemporaryDirectory()
        delegation_tool.store = delegation_tool.JobStore(
            os.path.join(self.tmp.name, "jobs.json"))
        return self

    def __exit__(self, *_exc):
        local_brain.chat = self.saved_chat
        local_brain.chat_json = self.saved_chat_json
        conversation.session = self.saved_session
        delegation_tool.store = self.saved_store
        self.tmp.cleanup()
        return False


def _is_marked_spoken(value):
    marker = getattr(agent_core, "AlreadySpoken", None)
    return marker is not None and isinstance(value, marker)


def _job(job_id):
    return next((j for j in delegation_tool.store.recent(100) if j["id"] == job_id), {})


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def run():
    results = Results()

    results.head("R2. guard misfire, nobody answers (run level)")
    with guard(results, "unanswered guard ends the turn"):
        model = _Model(INCIDENT_1, "a second answer that must never be asked for")
        voice = _Voice(reply="")
        with _Patched(model):
            summary = agent_core.run("Research into the latest news about OpenAI",
                                     voice.speak, ask=voice.ask)
        results.check("the guard asked once", len(voice.asked) == 1, repr(voice.asked))
        results.check("no second model call", model.calls == 1, f"calls={model.calls}")
        results.check("the asked text is what run() returns",
                      bool(voice.asked) and str(summary).strip() == voice.asked[0].strip())
        results.check("the result is marked as already spoken",
                      _is_marked_spoken(summary), type(summary).__name__)
        results.check("run() itself spoke it only once", voice.times(INCIDENT_1) == 1,
                      f"times={voice.times(INCIDENT_1)}")

    results.head("R3. guard fires, the user answers")
    with guard(results, "answered guard continues"):
        model = _Model(INCIDENT_1, "Done. The latest OpenAI news is the analytics launch.")
        voice = _Voice(reply="yes, the enterprise news")
        with _Patched(model):
            summary = agent_core.run("Research into the latest news about OpenAI",
                                     voice.speak, ask=voice.ask)
        results.check("the guard asked once", len(voice.asked) == 1)
        results.check("the turn continued to a second model call", model.calls == 2,
                      f"calls={model.calls}")
        results.check("the final answer is the new one",
                      str(summary).startswith("Done."), repr(str(summary)[:40]))
        results.check("an answered turn is spoken normally", not _is_marked_spoken(summary))

    results.head("A. ordinary response")
    with guard(results, "normal inline turn"):
        model = _Model(ORDINARY)
        voice = _Voice()
        with _Patched(model):
            delegation_tool._run_inline("what time is it", "what time is it",
                                        voice.speak, voice.ask, None)
        results.check("one model call", model.calls == 1, f"calls={model.calls}")
        results.check("no question asked", not voice.asked)
        results.check("the answer is spoken exactly once", voice.times(ORDINARY) == 1,
                      f"times={voice.times(ORDINARY)}")

    results.head("E. incident 1 shape, inline")
    with guard(results, "inline caller"):
        model = _Model(INCIDENT_1, "must not be generated")
        voice = _Voice(reply="")
        with _Patched(model):
            delegation_tool._run_inline("research OpenAI news", "research OpenAI news",
                                        voice.speak, voice.ask, None)
            turns = conversation.session.snapshot()["turns"]
        results.check("spoken once, never repeated", voice.times(INCIDENT_1) == 1,
                      f"times={voice.times(INCIDENT_1)}")
        results.check("no second model call", model.calls == 1, f"calls={model.calls}")
        results.check("the answer is still recorded in the conversation",
                      bool(turns) and turns[-1]["content"].strip() == INCIDENT_1.strip())

    results.head("D. incident 1 shape, queued job")
    with guard(results, "queue worker"):
        model = _Model(INCIDENT_1, "must not be generated")
        voice = _Voice(reply="")
        with _Patched(model):
            task = "Research into the latest news about OpenAI"
            job = delegation_tool.submit(task, voice.speak, ask=voice.ask,
                                         pieces=[(task, "asmo")])
            done = _wait_for(lambda: _job(job["id"]).get("status") == "done")
            stored = _job(job["id"])
            # The worker speaks after marking the job done; let it get there.
            _wait_for(lambda: voice.times(INCIDENT_1) >= 2, timeout=0.5)
        results.check("the job finished", done, repr(stored.get("status")))
        results.check("the job result did not repeat the asked text",
                      voice.times(INCIDENT_1) == 1, f"times={voice.times(INCIDENT_1)}")
        results.check("no second model call", model.calls == 1, f"calls={model.calls}")
        results.check("the job still records its summary",
                      (stored.get("summary") or "").strip() == INCIDENT_1.strip())

    results.head("F. incident 1 shape, escalated")
    with guard(results, "escalation caller"):
        model = _Model(INCIDENT_1, "must not be generated")
        voice = _Voice(reply="")
        saved = (local_brain.available, local_brain.with_model,
                 delegation_tool.route, delegation_tool.tutor.wants_teaching)
        decision = SimpleNamespace(route="escalate", goal="research OpenAI news",
                                   question="", confidence=0.6, risk="none",
                                   kind="action", reason="test escalation")
        try:
            local_brain.available = lambda: True
            local_brain.with_model = lambda _model, work: work()
            delegation_tool.route = SimpleNamespace(classify=lambda *_a, **_k: decision)
            delegation_tool.tutor.wants_teaching = lambda _text: False
            with _Patched(model):
                outcome = delegation_tool.handle("research OpenAI news", voice.speak,
                                                 ask=voice.ask)
        finally:
            (local_brain.available, local_brain.with_model,
             delegation_tool.route, delegation_tool.tutor.wants_teaching) = saved
        results.check("the escalated route ran", outcome == "escalated", repr(outcome))
        results.check("spoken once, never repeated", voice.times(INCIDENT_1) == 1,
                      f"times={voice.times(INCIDENT_1)}")
        results.check("no second model call", model.calls == 1, f"calls={model.calls}")

    results.head("I2. incident 2 shape: no second model call, no second answer")
    with guard(results, "screen answer"):
        # As on the day: read_screen first, then an answer quoting a title the
        # tool never returned, which used to send an unanswered ask on into
        # quoted_unread and a fresh model call. The screen read is faked.
        model = _Model(READ_SCREEN_CALL, INCIDENT_2_FIRST, INCIDENT_2_SECOND)
        voice = _Voice(reply="")
        saved_execute = agent_core.agent_tools.execute
        try:
            agent_core.agent_tools.execute = lambda _name, _args: FAKE_SCREEN
            with _Patched(model):
                delegation_tool._run_inline(
                    "read the frontmost window and say what is on it",
                    "what am I looking at", voice.speak, voice.ask, None)
        finally:
            agent_core.agent_tools.execute = saved_execute
        results.check("the guard asked once", len(voice.asked) == 1)
        results.check("no model call after the unanswered ask", model.calls == 2,
                      f"calls={model.calls} (1 tool step + 1 answer expected)")
        results.check("the rewritten second answer is never spoken",
                      voice.times(INCIDENT_2_SECOND) == 0)
        results.check("the first answer is heard once", voice.times(INCIDENT_2_FIRST) == 1,
                      f"times={voice.times(INCIDENT_2_FIRST)}")

    return results.report("unanswered guard question")


if __name__ == "__main__":
    raise SystemExit(run())
