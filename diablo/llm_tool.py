"""
D.I.A.B.L.O. llm_tool — local replacement
=========================================
Drop-in replacement for the original `skills/llm_tool.py`, which sent every call
to OpenRouter with an API key. This keeps the same three function names and the
same signatures, and routes all of them through Ollama instead.

Why replace rather than delete: `ppt_tool.create_presentation` calls
`ask_diablo_ai` to write slide content. Deleting the module would break
presentations; leaving it would keep an OpenRouter dependency alive inside a
system that is meant to be local. Replacing it fixes both.

The other two functions, `resolve_music_query` and `decrypt_semantic_intent`,
are no longer called from anywhere — the agent loop replaced the semantic
decryption layer entirely. They are kept working rather than removed, because
this file is on the import path of code that is not ours.
"""

import os
import sys
from typing import Optional

# Package parent, so `from skills import ...` works however this is loaded.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from skills import local_brain
except ImportError:  # pragma: no cover - only if the module is loaded alone
    local_brain = None


PERSONA = ("You are Diablo, a capable and dry assistant. Answer directly and "
           "concisely. No preamble, no markdown headings, no filler.")


def _chat(system: str, user: str, num_predict: int = 800,
          temperature: float = 0.3) -> Optional[str]:
    if local_brain is None or not local_brain.available():
        return None
    try:
        message = local_brain.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            num_predict=num_predict, temperature=temperature,
        )
    except Exception:
        return None
    return (message.get("content") or "").strip() or None


def ask_diablo_ai(prompt: str, context: str = "") -> str:
    """
    General question answering. Kept under its original name because ppt_tool
    imports it by that name, and that file is not ours to edit.
    """
    user = f"Context: {context}\n\n{prompt}" if context else prompt
    answer = _chat(PERSONA, user, num_predict=1200)
    if answer is None:
        return "I'm having trouble reaching my local model, Sir. Is Ollama running?"
    return answer


# Kept as an alias so the name reads correctly in anything written from here on.
# Old name, kept so ppt_tool and anything else written against it keeps working.
ask_jarvis_ai = ask_diablo_ai


def resolve_music_query(text: str) -> str:
    """Clean a mis-heard song or artist name. Unused by the agent loop now."""
    answer = _chat(
        "Extract the proper song and artist name from the user's text. Correct "
        "obvious mis-hearings (arjit -> Arijit Singh). Reply with the corrected "
        "string and nothing else.",
        text, num_predict=60, temperature=0.1)
    if answer is None:
        return text.replace("play ", "").strip()
    return answer.replace('"', "").replace("'", "").strip()


def decrypt_semantic_intent(text: str) -> str:
    """
    Superseded by agent_core.understand, which classifies and routes properly
    instead of flattening everything into one of five command shapes. Retained
    so that nothing importing it breaks; it now just returns the text unchanged.
    """
    return text.lower()


if __name__ == "__main__":
    print("local_brain available:", local_brain is not None and local_brain.available())
    print(ask_diablo_ai("In one sentence, what is an extended essay?"))
