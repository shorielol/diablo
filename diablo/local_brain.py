"""
D.I.A.B.L.O. Local Brain
========================
The only thing in the stack that talks to a language model, and it never leaves
localhost. Ollama's HTTP API, spoken to with `requests` so nothing new has to be
installed into the venv.

One model, qwen3.5:4b, is the brain and the eyes: conversation, intake, the
agent loop, the planner, and every question about a picture. Measured on this
machine (M3 Pro, 2026-10-05): it honours think=false, emits clean tool calls,
follows a JSON schema, and reads button labels from an image. There is no
second, larger model to escalate to and no separate vision model; GUI
grounding is GoClick's job (goclick.py), behind grounding_gate.
"""

import json
import os

from skills import config  # noqa: E402
import threading
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# Deliberately stdlib-only. `requests` is in the Diablo venv but not in the
# system interpreter, and keeping this importable from both means the whole
# stack can be tested without activating anything.

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# The brain a Diablo with no configuration runs, and the one place it is named:
# doctor reads it through CHAT_MODEL and tools/install.sh reads this line.
# It used to be qwen3:14b, so an install without a .env ran a 10 GB model for
# every turn on an 18 GB machine and nothing said so. tests/test_default_model.py
# fails if this ever names a 14B, a Qwen2.5-VL or any other model again.
DEFAULT_BRAIN_MODEL = "qwen3.5:4b"

# A blank value is no value: the settings page writes DIABLO_MODEL= when its
# field is cleared, and that used to make the model name an empty string.
CHAT_MODEL = os.getenv("DIABLO_MODEL", "").strip() or DEFAULT_BRAIN_MODEL
FAST_MODEL = os.getenv("DIABLO_FAST_MODEL", "").strip() or CHAT_MODEL
REQUEST_TIMEOUT = config.env_int("DIABLO_LLM_TIMEOUT", 180)

# Ollama unloads an idle model after five minutes by default, and reloading
# qwen3.5:4b costs about twelve seconds cold (measured 2026-10-05, with its
# vision projector) on the very command you were waiting on. Holding it
# resident trades 4.3 GB of RAM for that delay never happening. 10m keeps it
# resident through a working session and gives the memory back when you walk
# away; the wake word starts the reload, so the cost of that is mostly hidden.
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
# Measured on this machine for qwen3.5:4b, resident size and the same
# 2,736-token agent turn (2026-10-05):
#
#      8192   3.41 GB   P50 1.28 s
#     16384   3.71 GB   P50 1.31 s
#     32768   4.27 GB   P50 0.94 s
#
# Only one layer in four keeps a full KV cache, so a larger window costs far
# less than it did for qwen3. At 8192 a 12,539-token prompt was not refused:
# Ollama silently kept 4,098 tokens of it and answered anyway. 32768 for
# 0.86 GB, and `truncate: false` below so an overflow is an error, not a
# shorter question.
NUM_CTX = config.env_int("DIABLO_NUM_CTX", 32768)

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

    # Plain words, not "local brain". That phrase is Diablo's own jargon and
    # nothing he says explains it: asked afterwards why his local brain had
    # stopped responding, the conversation model had never seen the phrase,
    # denied it existed, and said it had misheard. The sentence was real and
    # came from here; the explanation of it was invented.
    SPOKEN = "I couldn't finish that, Sir. The model on this Mac didn't give me an answer."

    # What the trace's "model" row calls this failure. Set per instance where
    # one class covers several (a refusal, an unreadable body, unusable JSON).
    outcome = "unreachable"

    def __init__(self, detail: str, spoken: Optional[str] = None):
        super().__init__(detail)
        self.spoken = spoken or self.SPOKEN


class ModelTimeout(BrainOffline):
    """
    The model was reachable and did not answer in time.

    Separate because it is a different failure with a different cause. The
    incident that produced this: an escalated request and a delegated research
    job both ran past the 180-second limit while two other turns were using the
    model, and both were reported as "Ollama unreachable" — which it was not.
    It was busy.
    """

    SPOKEN = ("I couldn't finish that, Sir. The model on this Mac took too long "
              "to answer, so I stopped waiting.")
    outcome = "timeout"

    def __init__(self, detail: str, seconds: Optional[float] = None):
        super().__init__(detail, self.SPOKEN)
        self.seconds = seconds


class ContextFull(BrainOffline):
    """The prompt was longer than the window it had to fit in."""

    # "start over" and not "start again" — the first is in RESET_PHRASES and
    # the second is not, and advice that does not work is worse than none.
    SPOKEN = ("My short-term memory is full, Sir. Say \"start over\" and "
              "I'll clear it.")
    outcome = "context_full"

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


def _timed_out(e: BaseException) -> bool:
    """
    Whether a transport failure was a timeout.

    urllib reports it three ways: a bare TimeoutError on a slow read, socket.
    timeout on older paths, and URLError wrapping either on a slow connect.
    """
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(e, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


def _transport_failure(e: BaseException, timeout: float) -> BrainOffline:
    """
    Name what actually happened, instead of calling every failure unreachable.

    The detail is for the trace and the log. It used to read "Ollama
    unreachable" for a timeout and for a reply that was not valid JSON, and
    in both of those cases Ollama had been reached.
    """
    if _timed_out(e):
        return ModelTimeout(f"Ollama at {OLLAMA_HOST} did not answer within "
                            f"{timeout}s: {e}", seconds=timeout)
    if isinstance(e, ValueError):
        failure = BrainOffline(f"Ollama at {OLLAMA_HOST} answered with an "
                               f"unreadable body: {type(e).__name__}: {e}")
        failure.outcome = "unreadable"
        return failure
    return BrainOffline(f"Ollama unreachable at {OLLAMA_HOST}: "
                        f"{type(e).__name__}: {e}")


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
    refused = BrainOffline(f"Ollama returned {code}: {body[:200]}")
    refused.outcome = "refused"
    return refused


# ─── One row per model call ──────────────────────────────────────────────────
#
# Real use showed turns where "the model did not run" and the trace could not
# say why: the classifier's failure was recorded as the class name BrainOffline
# and nothing else, so a timeout, a refusal and an answer that was not JSON all
# looked the same. Every call now leaves one "model" row saying which call it
# was, whether it came back, how long it took and how many tokens it cost.
# Never the prompt or the answer.

def _purpose() -> str:
    """The function outside this module that asked for the model."""
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__", "").endswith("local_brain"):
        frame = frame.f_back
    return frame.f_code.co_name if frame is not None else ""


def _report(purpose: str, model: str, began: float, outcome: str,
            body: Optional[dict] = None, first_ms: Optional[float] = None) -> None:
    try:
        from skills import trace
        fields: Dict[str, Any] = {"purpose": purpose, "model": model, "outcome": outcome,
                                  "call_ms": round((time.time() - began) * 1000, 1)}
        if body:
            fields["load_ms"] = round((body.get("load_duration") or 0) / 1e6, 1)
            fields["prompt_tokens"] = body.get("prompt_eval_count")
            fields["output_tokens"] = body.get("eval_count")
        if first_ms is not None:
            fields["first_token_ms"] = first_ms
        trace.event("model", **fields)
    except Exception:
        pass                              # telemetry never breaks a turn


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
        # Refuse rather than truncate. Left to itself Ollama drops the front
        # of a prompt that does not fit and answers what is left.
        "truncate": False,
    }
    if tools:
        payload["tools"] = tools
    if fmt:
        payload["format"] = fmt

    purpose, began = _purpose(), time.time()
    try:
        try:
            body = _post_json("/api/chat", payload, timeout or REQUEST_TIMEOUT)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise _refusal(e.code, detail)
        except Exception as e:
            raise _transport_failure(e, timeout or REQUEST_TIMEOUT)
        if "error" in body:
            raise _refusal(200, str(body["error"]))
    except BrainOffline as failure:
        _report(purpose, payload["model"], began, failure.outcome)
        raise
    _report(purpose, payload["model"], began, "ok", body=body)
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
        # Refuse rather than truncate. Left to itself Ollama drops the front
        # of a prompt that does not fit and answers what is left.
        "truncate": False,
    }

    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")

    purpose, began = _purpose(), time.time()
    first_ms, outcome, last = None, "stopped", None
    try:
        try:
            response = urllib.request.urlopen(request, timeout=timeout or REQUEST_TIMEOUT)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise _refusal(e.code, detail)
        except Exception as e:
            raise _transport_failure(e, timeout or REQUEST_TIMEOUT)

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
                    if first_ms is None:
                        first_ms = round((time.time() - began) * 1000, 1)
                    yield piece
                if chunk.get("done"):
                    outcome, last = "ok", chunk
                    return
        outcome = "ok"
    except BrainOffline as failure:
        outcome = failure.outcome
        raise
    except Exception:
        outcome = "broken"                # the connection failed mid-answer
        raise
    finally:
        # "stopped" when the caller closed the stream early (barge-in).
        _report(purpose, payload["model"], began, outcome, body=last, first_ms=first_ms)


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
        # A second row for the same call: it came back (the "ok" above), and
        # what came back could not be used.
        _report(_purpose(), model or FAST_MODEL, time.time(), "invalid_json")
        unusable = BrainOffline(f"Model did not return usable JSON: {content[:200]}")
        unusable.outcome = "invalid_json"
        raise unusable


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
