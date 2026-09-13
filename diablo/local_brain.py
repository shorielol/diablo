"""
D.I.A.B.L.O. Local Brain
========================
The only thing in the stack that talks to a language model, and it never leaves
localhost. Ollama's HTTP API, spoken to with `requests` so nothing new has to be
installed into the venv.

Measured on this machine (M3 Pro, 2026-09-10):
  qwen3:14b  ~7s per tool-calling turn, honours think=false, emits clean
             tool_calls. This is the one.
  qwen3:4b   ignores think=false, leaks its reasoning into content and returns
             no tool_calls at all. Do not use it for the agent loop.
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# Deliberately stdlib-only. `requests` is in the Diablo venv but not in the
# system interpreter, and keeping this importable from both means the whole
# stack can be tested without activating anything.

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
CHAT_MODEL = os.getenv("DIABLO_MODEL", "qwen3:14b")
FAST_MODEL = os.getenv("DIABLO_FAST_MODEL", CHAT_MODEL)
REQUEST_TIMEOUT = int(os.getenv("DIABLO_LLM_TIMEOUT", "180"))

# Ollama unloads an idle model after five minutes by default, and reloading
# qwen3:14b costs several seconds on the very command you were waiting on.
# Holding it resident trades ~9 GB of RAM for that delay never happening.
# 30m pinned 9 GB whether Diablo was in use or not. 10m keeps it resident
# through a working session and gives the memory back when you walk away; the
# wake word starts the reload, so the cost of that is mostly hidden.
# LOW_MEMORY releases it after every single command instead.
LOW_MEMORY = os.getenv("DIABLO_LOW_MEMORY", "0") not in ("0", "", "false", "False")
KEEP_ALIVE = os.getenv("DIABLO_KEEP_ALIVE", "0s" if LOW_MEMORY else "10m")


class BrainOffline(Exception):
    """Ollama is not answering."""


def _get_json(path: str, timeout: int) -> Any:
    with urllib.request.urlopen(f"{OLLAMA_HOST}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post_json(path: str, payload: dict, timeout: int) -> Any:
    request = urllib.request.Request(
        f"{OLLAMA_HOST}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as r:
        return json.loads(r.read().decode())


def available(timeout: int = 3) -> bool:
    try:
        _get_json("/api/tags", timeout)
        return True
    except Exception:
        return False


def installed_models() -> List[str]:
    try:
        return [m["name"] for m in _get_json("/api/tags", 5).get("models", [])]
    except Exception:
        return []


def chat(
    messages: List[Dict[str, Any]],
    tools: Optional[List[dict]] = None,
    model: Optional[str] = None,
    fmt: Optional[dict] = None,
    think: bool = False,
    temperature: float = 0.2,
    num_predict: int = 1024,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """
    One turn against Ollama's /api/chat.

    Returns the raw `message` object: {"role", "content", "tool_calls"?}.
    Raises BrainOffline if the daemon cannot be reached.
    """
    payload: Dict[str, Any] = {
        "model": model or CHAT_MODEL,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": {"temperature": temperature, "num_predict": num_predict},
        "keep_alive": KEEP_ALIVE,
    }
    if tools:
        payload["tools"] = tools
    if fmt:
        payload["format"] = fmt

    try:
        body = _post_json("/api/chat", payload, timeout or REQUEST_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise BrainOffline(f"Ollama returned {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        raise BrainOffline(f"Ollama unreachable at {OLLAMA_HOST}: {e}")

    if "error" in body:
        raise BrainOffline(body["error"])
    return body.get("message", {"role": "assistant", "content": ""})


def chat_json(
    messages: List[Dict[str, Any]],
    schema: dict,
    model: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """A turn constrained to a JSON schema. Ollama enforces the shape."""
    message = chat(messages, model=model or FAST_MODEL, fmt=schema, **kwargs)
    content = (message.get("content") or "").strip()
    try:
        return json.loads(content)
    except Exception:
        # Constrained decoding should make this unreachable, but a truncated
        # response is still possible if num_predict was too tight.
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(content[start:end + 1])
            except Exception:
                pass
        raise BrainOffline(f"Model did not return usable JSON: {content[:200]}")


def warm(model: Optional[str] = None) -> float:
    """
    Pull the model into memory so the first real request is not paying load time.
    Returns seconds taken, or -1 if the brain is offline.
    """
    started = time.time()
    try:
        chat([{"role": "user", "content": "ok"}], model=model, num_predict=1, timeout=300)
    except BrainOffline:
        return -1.0
    return time.time() - started


def speech_summary(text: str, limit_words: int = 25) -> str:
    """
    Squeeze a wall of text into one spoken sentence. Falls back to naive
    truncation rather than raising, because this is only ever cosmetic.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if len(text.split()) <= limit_words:
        return text
    try:
        message = chat(
            [
                {"role": "system", "content":
                 "Compress the user's text into ONE spoken sentence under "
                 f"{limit_words} words. Plain speech. No markdown, no file paths, "
                 "no preamble. Reply with the sentence and nothing else."},
                {"role": "user", "content": text[:4000]},
            ],
            num_predict=80,
        )
        out = (message.get("content") or "").strip().strip('"')
        if out:
            return out
    except BrainOffline:
        pass
    words = text.split()
    return " ".join(words[:limit_words]) + "..."


if __name__ == "__main__":
    print(f"Ollama at {OLLAMA_HOST}: {'up' if available() else 'DOWN'}")
    print(f"Models: {', '.join(installed_models()) or 'none'}")
    print(f"Chat model: {CHAT_MODEL}")
    if available():
        print(f"Warm-up: {warm():.1f}s")
