#!/usr/bin/env python3
"""
The intake parse's bounds (G-48): what understand() does when the model writes
more than it was asked for.

  - every string field the model writes has a length limit in the schema;
  - a task that runs into its limit is replaced by the person's own words;
  - a spoken reply or question that runs into its limit is cut back to a whole
    sentence, or to a whole word when it has no sentence break;
  - a question cut that way is still a question: the turn stays a clarify and
    is not turned into acting on the thing it asked about.

The model is a scripted double. Nothing is spoken or touched.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

BASE = tempfile.mkdtemp(prefix="diablo-intake-")
os.environ["DIABLO_TRACE"] = os.path.join(BASE, "trace.jsonl")

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import agent_core, local_brain  # noqa: E402

TEXT = "can you sort out the draft for me before tonight please"


def parsed(answer: dict) -> dict:
    """understand() with the model answering `answer`, past the fast path."""
    saved = (local_brain.chat_json, agent_core.local_brain.chat_json, agent_core.fast_understand)
    seen = {}

    def chat_json(messages, schema=None, **kwargs):
        seen["schema"] = schema
        return dict(answer)
    local_brain.chat_json = agent_core.local_brain.chat_json = chat_json
    agent_core.fast_understand = lambda text: None
    try:
        result = agent_core.understand(TEXT)
    finally:
        (local_brain.chat_json, agent_core.local_brain.chat_json,
         agent_core.fast_understand) = saved
    result["_schema"] = seen.get("schema")
    return result


def run() -> int:
    r = Results()
    limit = agent_core.SPOKEN_MAX_CHARS

    r.head("The schema bounds what the model may write")
    with guard(r, "maxLength on every string"):
        schema = parsed({"action": "act", "task": TEXT})["_schema"] or {}
        props = schema.get("properties", {})
        strings = {k: v for k, v in props.items() if v.get("type") == "string"
                   and "enum" not in v}
        unbounded = [k for k, v in strings.items() if "maxLength" not in v]
        r.check("every free-text field has a limit", bool(strings) and not unbounded,
                f"{sorted(strings)} unbounded {unbounded}")
        r.check("the task's limit follows the request's length",
                props.get("task", {}).get("maxLength") ==
                len(TEXT) + agent_core.TASK_SLACK_CHARS,
                str(props.get("task", {}).get("maxLength")))

    r.head("A task that runs into its bound")
    with guard(r, "is replaced by what was said"):
        long_task = ("Sort out the draft as of 2024 or 2025, checking the reviews "
                     "on several named sites and the rest of it as well, all of it"
                     )[:len(TEXT) + agent_core.TASK_SLACK_CHARS]
        out = parsed({"action": "act", "task": long_task})
        r.check("the person's words are the task", out.get("task") == TEXT, out.get("task"))
    with guard(r, "a task inside its bound"):
        out = parsed({"action": "act", "task": "tidy the draft"})
        r.check("is kept as the model wrote it", out.get("task") == "tidy the draft",
                out.get("task"))

    r.head("Spoken fields cut back to something sayable")
    with guard(r, "a reply with a sentence break"):
        reply = ("That depends on which draft you mean. " + "word " * 200)[:limit]
        out = parsed({"action": "chat", "reply": reply})
        r.check("ends at the last full sentence",
                out.get("reply") == "That depends on which draft you mean.", out.get("reply"))
    with guard(r, "a question with no sentence break"):
        question = ("Which draft do you mean, the one for the " + "launch and the " * 60)[:limit]
        out = parsed({"action": "clarify", "question": question})
        r.check("the turn is still a clarify", out.get("action") == "clarify",
                str(out.get("action")))
        asked = str(out.get("question", ""))
        r.check("and the question ends on a whole word and a question mark",
                asked.endswith("?") and not asked.endswith(" ?") and len(asked) < limit,
                asked[-30:])
    with guard(r, "a short reply"):
        out = parsed({"action": "chat", "reply": "Of course, Sir."})
        r.check("is left alone", out.get("reply") == "Of course, Sir.", out.get("reply"))

    return r.report("intake bounds")


if __name__ == "__main__":
    sys.exit(run())
