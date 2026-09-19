#!/usr/bin/env python3
"""Audio Phase 2G: an answered clarification is not asked again.

Live, 19 September: Diablo asked "Which specific news about OpenAI…", the user
answered "Any product update.", and the 4B returned the identical question
(5 of 5 trials with the real model). The strict loop asked it again, the second
answer was mis-transcribed, and the turn ended "I still don't follow".

The model is scripted here, and everything below routing is recorded rather
than run: no model, microphone, audio or job store is used.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from skills import agent_core, delegation_tool, local_brain  # noqa: E402

LIVE_REQUEST = "Hey Diablo research into news recently about open AI."
LIVE_QUESTION = ("Which specific news about OpenAI are you looking for? For example, "
                 "a recent announcement, a product update, or a development in AI "
                 "research?")
LIVE_ANSWER = "Any product update."
DATE_QUESTION = "What date range should I use?"


def _clarify(question):
    return {"action": "clarify", "question": question}


class _Run:
    """Drive handle() with scripted verdicts and answers; record everything."""

    def __init__(self, verdicts, answers):
        self.verdicts = list(verdicts)
        self.answers = list(answers)
        self.model_calls = 0
        self.asked, self.spoken = [], []
        self.decided, self.submitted, self.inline = [], [], []

    def understand(self, text, in_conversation=False):
        self.model_calls += 1
        return self.verdicts.pop(0)

    def ask(self, question):
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else ""

    def go(self, request):
        real_decide = delegation_tool.decide
        saved = (local_brain.available, delegation_tool.route, agent_core.understand,
                 delegation_tool.decide, delegation_tool.submit,
                 delegation_tool._run_inline, delegation_tool.roster.split,
                 delegation_tool.tutor.wants_teaching)

        def decide(text, *args, **kwargs):
            self.decided.append(text)
            return real_decide(text, *args, **kwargs)

        try:
            local_brain.available = lambda *_a, **_k: True
            delegation_tool.route = None          # the initial verdict comes from understand too
            agent_core.understand = self.understand
            delegation_tool.decide = decide
            delegation_tool.submit = lambda task, *_a, **_k: self.submitted.append(task) or {}
            delegation_tool._run_inline = (lambda task, *_a, **_k:
                                           self.inline.append(task) or "inline")
            delegation_tool.roster.split = lambda task: [(task, "asmo")]
            delegation_tool.tutor.wants_teaching = lambda _text: False
            return delegation_tool.handle(request, self.spoken.append, ask=self.ask)
        finally:
            (local_brain.available, delegation_tool.route, agent_core.understand,
             delegation_tool.decide, delegation_tool.submit,
             delegation_tool._run_inline, delegation_tool.roster.split,
             delegation_tool.tutor.wants_teaching) = saved

    def downstream(self):
        return self.submitted + self.inline


def run():
    results = Results()

    results.head("A. live incident: the answered question comes back unchanged")
    with guard(results, "repeated question"):
        r = _Run([_clarify(LIVE_QUESTION), _clarify(LIVE_QUESTION)], [LIVE_ANSWER])
        outcome = r.go(LIVE_REQUEST)
        results.check("the question was asked once, not again", r.asked == [LIVE_QUESTION],
                      f"asked {len(r.asked)}x")
        results.check("no second clarification wait (one answer consumed)",
                      len(r.asked) == 1)
        results.check("two model calls: the request, then the answer",
                      r.model_calls == 2, f"calls={r.model_calls}")
        results.check("the turn did not end unclear", outcome != "unclear", repr(outcome))
        results.check("nothing was spoken as a refusal",
                      not any("don't follow" in s for s in r.spoken), repr(r.spoken))
        gathered = r.decided[0] if r.decided else ""
        results.check("routing received request, question and answer",
                      LIVE_REQUEST in gathered and LIVE_QUESTION in gathered
                      and LIVE_ANSWER in gathered, repr(gathered[:80]))
        results.check("the gathered request went downstream",
                      any(LIVE_ANSWER in t for t in r.downstream()),
                      f"downstream={len(r.downstream())}")

    results.head("B. a different follow-up question is still asked")
    with guard(results, "different question"):
        r = _Run([_clarify(LIVE_QUESTION), _clarify(DATE_QUESTION),
                  {"action": "act", "task": "research OpenAI product updates this month"}],
                 [LIVE_ANSWER, "this month"])
        outcome = r.go(LIVE_REQUEST)
        results.check("both questions were asked, in order",
                      r.asked == [LIVE_QUESTION, DATE_QUESTION], repr(r.asked))
        results.check("three model calls", r.model_calls == 3, f"calls={r.model_calls}")
        results.check("the turn proceeded", outcome != "unclear" and bool(r.downstream()),
                      repr(outcome))

    return results.report("repeated clarification")


if __name__ == "__main__":
    raise SystemExit(run())
