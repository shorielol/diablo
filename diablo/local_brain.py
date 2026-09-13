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
import re
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

# The context window. Left unset, Ollama uses the model's own default, and for
# qwen3 that is 262,144 tokens — a window Diablo never fills and which costs
# real memory in KV cache to keep open. 4096 holds the system prompt, the tool
# schemas and a dozen turns with room to spare.
NUM_CTX = int(os.getenv("DIABLO_NUM_CTX", "4096"))

# How much the model may say in one turn. Everything here is read aloud, so a
# long answer is a long wait with a speaker talking at you.
NUM_PREDICT = int(os.getenv("DIABLO_NUM_PREDICT", "512"))

# Thinking is off by default and has to be asked for. The hybrid qwen3 models
# spend hundreds of tokens inside <think> before answering, which on a voice
# interface is dead air. Measured on qwen3:4b with thinking on: 9.97s and no
# tool call at all.
THINK_DEFAULT = os.getenv("DIABLO_THINK", "0") not in ("0", "", "false", "False")


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
    think: Optional[bool] = None,
    temperature: float = 0.2,
    num_predict: Optional[int] = None,
    num_ctx: Optional[int] = None,
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
        "think": THINK_DEFAULT if think is None else think,
        "options": {
            "temperature": temperature,
            "num_predict": NUM_PREDICT if num_predict is None else num_predict,
            "num_ctx": NUM_CTX if num_ctx is None else num_ctx,
        },
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



# ─── Streaming ───────────────────────────────────────────────────────────────

# A sentence ends at one of these followed by whitespace. Speech has to be cut
# somewhere, and a sentence is the only boundary where a text-to-speech engine
# produces natural prosody — cutting on a fixed token count gives you a voice
# that stops mid-breath.
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+|(?<=[.!?])$')


def chat_stream(
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    think: Optional[bool] = None,
    temperature: float = 0.2,
    num_predict: Optional[int] = None,
    num_ctx: Optional[int] = None,
    timeout: Optional[int] = None,
):
    """
    Yield the answer in pieces as the model produces it.

    Exists for one reason: time to first token is a fraction of time to last
    token, and on a voice interface that gap is silence the user is sitting
    through. Measured on this machine, a 40-token reply arrives entirely in
    about 1.5s but the first token lands in roughly 0.3s. Speaking from the
    first sentence turns a 1.5s wait into a 0.4s one.

    Tool calls are deliberately not streamed. A partial tool call is not
    useful — you cannot run half of one — so anything with tools goes through
    chat() and waits.
    """
    payload: Dict[str, Any] = {
        "model": model or CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "think": THINK_DEFAULT if think is None else think,
        "options": {
            "temperature": temperature,
            "num_predict": NUM_PREDICT if num_predict is None else num_predict,
            "num_ctx": NUM_CTX if num_ctx is None else num_ctx,
        },
        "keep_alive": KEEP_ALIVE,
    }

    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")

    try:
        response = urllib.request.urlopen(request, timeout=timeout or REQUEST_TIMEOUT)
    except Exception as e:
        raise BrainOffline(f"Ollama unreachable at {OLLAMA_HOST}: {e}")

    with response:
        for raw in response:
            if not raw.strip():
                continue
            try:
                chunk = json.loads(raw)
            except ValueError:
                continue
            if "error" in chunk:
                raise BrainOffline(chunk["error"])
            piece = (chunk.get("message") or {}).get("content") or ""
            if piece:
                yield piece
            if chunk.get("done"):
                return


def stream_sentences(messages: List[Dict[str, Any]], should_stop=None, **kwargs):
    """
    The same stream, regrouped into whole sentences.

    `should_stop` is checked between sentences and is how barge-in works: when
    the user starts talking, the caller flips it and generation stops rather
    than finishing a paragraph nobody is listening to.
    """
    buffer = ""
    for piece in chat_stream(messages, **kwargs):
        if should_stop is not None and should_stop():
            return
        buffer += piece
        while True:
            match = _SENTENCE_END.search(buffer)
            if not match or match.end() == 0:
                break
            sentence, buffer = buffer[:match.end()].strip(), buffer[match.end():]
            if sentence:
                yield sentence
            if should_stop is not None and should_stop():
                return
    tail = buffer.strip()
    if tail and not (should_stop is not None and should_stop()):
        yield tail

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
