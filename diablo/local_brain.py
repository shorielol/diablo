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

from skills import config  # noqa: E402
import threading
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
REQUEST_TIMEOUT = config.env_int("DIABLO_LLM_TIMEOUT", 180)

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
# real memory in KV cache to keep open.
#
# This said 4096, on the grounds that it held "the system prompt, the tool
# schemas and a dozen turns with room to spare". It did not. A real turn came
# to 4168 tokens and Ollama refused it with a 400, which Diablo then read out
# as JSON. The system prompt and 37 tool schemas are most of that before a
# single word is said.
#
# Measured on this machine, resident size of qwen3:4b-instruct:
#
#      4096   3.2 GB
#      8192   3.9 GB
#     16384   5.1 GB
#
# 8192 for 0.7 GB. 16384 was rejected rather than unsupported: the vision model
# is another 6 GB, and 5.1 + 6 leaves too little of 18 GB for the editor and a
# browser. DIABLO_NUM_CTX raises it on a machine with more to spare.
NUM_CTX = config.env_int("DIABLO_NUM_CTX", 8192)

# How much the model may say in one turn. Everything here is read aloud, so a
# long answer is a long wait with a speaker talking at you.
NUM_PREDICT = config.env_int("DIABLO_NUM_PREDICT", 512)

# Thinking is off by default and has to be asked for. The hybrid qwen3 models
# spend hundreds of tokens inside <think> before answering, which on a voice
# interface is dead air. Measured on qwen3:4b with thinking on: 9.97s and no
# tool call at all.
THINK_DEFAULT = os.getenv("DIABLO_THINK", "0") not in ("0", "", "false", "False")


class BrainOffline(Exception):
    """
    Ollama is not answering, or refused.

    Carries two messages on purpose. str(e) keeps every detail, for the log and
    the trace, where detail is what you want. `spoken` is the only thing that
    may reach him, and it never contains a status code, a body or a path.

    This split exists because the raw one was reaching him. The message used to
    be f"Ollama returned {code}: {body}", and agent_core interpolated it
    straight into its reply, so a context overflow was read out as
    'Ollama returned 400: {"error":{"code":400,"message":"request (4168
    tokens) exceeds the available context size...'. Fixing the callers one at a
    time would leave the next caller to rediscover it, so the safe form lives
    on the exception.
    """

    SPOKEN = "My local brain stopped responding, Sir."

    def __init__(self, detail: str, spoken: Optional[str] = None):
        super().__init__(detail)
        self.spoken = spoken or self.SPOKEN


class ContextFull(BrainOffline):
    """The prompt was longer than the window it had to fit in."""

    # "start over" and not "start again" — the first is in RESET_PHRASES and
    # the second is not, and advice that does not work is worse than none.
    SPOKEN = ("My short-term memory is full, Sir. Say \"start over\" and "
              "I'll clear it.")

    def __init__(self, detail: str, used: int = 0, limit: int = 0):
        super().__init__(detail, self.SPOKEN)
        self.used, self.limit = used, limit


# Ollama says this several ways depending on version and endpoint, so the test
# is on the words rather than on a code — a 400 can also mean a malformed
# request, which is a different sentence and a different fix.
_CONTEXT_SIGNS = ("exceeds the available context", "exceeds context",
                  "context size", "context length", "context window",
                  "too many tokens", "maximum context", "prompt is too long",
                  "input is too long")


def _refusal(code: int, body: str) -> BrainOffline:
    """Turn an HTTP error body into something safe to say."""
    lowered = (body or "").lower()
    if any(sign in lowered for sign in _CONTEXT_SIGNS):
        # Anchored on the word "tokens". Matching bare numbers picked up the
        # 400 out of '"code":400' and reported a 400-token prompt.
        counts = [int(n) for n in re.findall(r"(\d+)\s*tokens", body or "")]
        used = counts[0] if counts else 0
        limit = counts[1] if len(counts) > 1 else 0
        return ContextFull(f"Ollama returned {code}: {body[:200]}", used, limit)
    return BrainOffline(f"Ollama returned {code}: {body[:200]}")


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
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise _refusal(e.code, detail)
    except Exception as e:
        raise BrainOffline(f"Ollama unreachable at {OLLAMA_HOST}: {e}")

    if "error" in body:
        raise _refusal(200, str(body["error"]))
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
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise _refusal(e.code, detail)
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
                raise _refusal(200, str(chunk["error"]))
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



# ─── Escalation ──────────────────────────────────────────────────────────────

def unload(model: Optional[str] = None) -> bool:
    """
    Drop a model from memory now, rather than waiting out keep_alive.

    Necessary rather than tidy: qwen3:4b-instruct is 3.18 GB resident and
    qwen3:14b is 9.64 GB. On an 18 GB machine already running an IDE and a
    browser, holding both is not an option, so escalating means evicting.
    """
    target = model or CHAT_MODEL
    try:
        _post_json("/api/generate", {"model": target, "keep_alive": 0}, 30)
        return True
    except Exception:
        return False


def resident() -> List[str]:
    """Which models Ollama is holding right now."""
    try:
        body = _get_json("/api/ps", 3)
        return [m.get("name", "") for m in (body or {}).get("models", [])]
    except Exception:
        return []


def with_model(model: str, work):
    """
    Run `work()` against a different model, then put things back.

    The swap is a module global rather than a parameter because the call sites
    are three layers down inside the agent loop, and threading a model name
    through all of them to change it twice a day is not worth the churn.

    The small model is evicted first. Without that, Ollama loads the second
    model alongside the first and the machine starts swapping, which is far
    slower than the 8.18s cold start this is trying to manage.
    """
    global CHAT_MODEL
    if not model or model == CHAT_MODEL:
        return work()

    previous = CHAT_MODEL
    unload(previous)
    CHAT_MODEL = model
    try:
        return work()
    finally:
        CHAT_MODEL = previous
        # Evict the big one. Leaving it resident would mean the next ordinary
        # request finds 9.6 GB already spoken for.
        unload(model)
        # ...and bring the small one back in the background, because otherwise
        # the next thing said pays a 2.71s cold start for a swap it had no part
        # in. The user is usually still listening to the escalated answer while
        # this happens.
        try:
            threading.Thread(target=lambda: warm(previous), daemon=True).start()
        except Exception:
            pass

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
