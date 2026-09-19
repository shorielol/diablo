#!/usr/bin/env python3
"""Audio Phase 2E: a relative "which" is not a question.

agent_core._is_really_a_question decides whether a final answer is really a
question the model forgot to ask. On 19 September it read a finished answer as
a question because of ", which I completed", and Diablo asked it aloud and
waited twenty seconds for a reply nobody was going to give.

Pure function checks, plus the incident run end to end with a scripted model.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from skills import agent_core, local_brain  # noqa: E402

# Word for word from the incident.
INCIDENT = ("I did not make any changes because the request was to research the "
            "latest news about OpenAI, which I completed by retrieving and "
            "summarizing current updates. No file or system state was modified. "
            "The research is now complete and available in the search results above.")

STATEMENTS = [
    INCIDENT,
    ("I did not make any changes because the request was to research the latest "
     "news about OpenAI, which I completed by retrieving and summarizing current "
     "updates."),
    "The report, which I finished yesterday, is ready.",
    "The settings, which I checked earlier, were already correct.",
    "It is 14:01.",
]

QUESTIONS = [
    "Which one should I use?",
    "Which file should I open?",
    "I found two drafts. Which one should I send?",
    # The shapes the rule was written for: a choice offered without a "?".
    "Which one should I send",
    "Tell me which one you want and I will open it.",
    "I found two draft files: draft-a.md and draft-b.md.",
    "Would you like me to open it.",
]


def run():
    results = Results()

    results.head("declarative statements are not questions")
    for text in STATEMENTS:
        with guard(results, text[:40]):
            results.check(f"not a question: {text[:60]}",
                          agent_core._is_really_a_question(text, set()) is False)

    results.head("genuine choices are still questions")
    for text in QUESTIONS:
        with guard(results, text[:40]):
            results.check(f"a question: {text[:60]}",
                          agent_core._is_really_a_question(text, set()) is True)

    results.head("the incident, end to end")
    with guard(results, "no guard, no wait, no extra model call"):
        calls, asked, said = [0], [], []

        def chat(messages, tools=None, **_kwargs):
            calls[0] += 1
            return {"content": INCIDENT}

        saved = local_brain.chat, local_brain.chat_json
        try:
            local_brain.chat = chat
            local_brain.chat_json = lambda *_a, **_k: {"steps": []}
            summary = agent_core.run(
                "Research into the latest news about OpenAI",
                lambda text, **_k: said.append(text),
                ask=lambda q: asked.append(q) or "")
        finally:
            local_brain.chat, local_brain.chat_json = saved
        results.check("no clarification asked", asked == [], repr(asked))
        results.check("one model call", calls[0] == 1, f"calls={calls[0]}")
        results.check("the answer comes back to be spoken normally",
                      str(summary).strip() == INCIDENT.strip()
                      and not isinstance(summary, agent_core.AlreadySpoken))

    return results.report("question detector")


if __name__ == "__main__":
    raise SystemExit(run())
