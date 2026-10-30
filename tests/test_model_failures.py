#!/usr/bin/env python3
"""
What Diablo says, and records, when the local model fails.

Written after an incident. Asked to set a reminder, Diablo said "My local brain
stopped responding, Sir." twice. The trace and the job store showed what had
actually happened:

  turn f15a0c25   escalated at +9.3s, timed out at +195.5s
  job df49171d    delegated research, ran 180.8s, timed out, marked "done"

Two different requests, both past the 180-second limit while two other turns
were using the model. Not a duplicate. Both were logged as "Ollama unreachable",
which it was not, and both were spoken as a phrase — "local brain" — that the
conversation model, asked about it afterwards, said did not exist.

These tests drive `local_brain.chat` with the transport replaced, so every
failure is deterministic and none of them needs Ollama.
"""

import io
import json
import os
import socket
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))

from harness import Results, guard  # noqa: E402

from skills import local_brain as LB  # noqa: E402

# The words a spoken failure may not contain. Kept identical to
# test_brain_errors.LEAKS, which enforces the same rule on other paths.
LEAKS = ("{", "}", "error", "http", "400", "500", "ollama", "traceback",
         "exception", "token", "json", "127.0.0.1", "api/chat")


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def transport(behaviour, counter):
    def urlopen(request, timeout=None):
        counter["calls"] += 1
        if behaviour == "success":
            return Resp(json.dumps({"message": {"role": "assistant",
                                                "content": "hello"}}).encode())
        if behaviour == "empty":
            return Resp(json.dumps({"message": {"role": "assistant",
                                                "content": ""}}).encode())
        if behaviour == "read_timeout":
            raise TimeoutError("timed out")
        if behaviour == "socket_timeout":
            raise socket.timeout("timed out")
        if behaviour == "connect_timeout":
            raise urllib.error.URLError(socket.timeout("timed out"))
        if behaviour == "refused":
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        if behaviour == "bad_body":
            return Resp(b"<html>not the reply you asked for</html>")
        if behaviour == "runner_crashed":
            raise urllib.error.HTTPError("u", 500, "x", {},
                                         io.BytesIO(b'{"error":"runner crashed"}'))
        if behaviour == "unexpected":
            raise RuntimeError("something odd")
        raise AssertionError(behaviour)
    return urlopen


def attempt(behaviour, stream=False):
    """(returned, raised, calls) for one call through the real adapter."""
    counter = {"calls": 0}
    real = urllib.request.urlopen
    urllib.request.urlopen = transport(behaviour, counter)
    try:
        if stream:
            out = "".join(LB.chat_stream([{"role": "user", "content": "hi"}]))
        else:
            out = LB.chat([{"role": "user", "content": "hi"}])
        return out, None, counter["calls"]
    except LB.BrainOffline as e:
        return None, e, counter["calls"]
    finally:
        urllib.request.urlopen = real


def safe(text):
    return not [w for w in LEAKS if w in (text or "").lower()]


def main() -> int:
    r = Results()

    r.head("MODEL_SUCCESS")
    with guard(r, "a normal reply"):
        out, raised, calls = attempt("success")
        r.check("returns the message", raised is None and out.get("content") == "hello",
                repr(out))
        r.check("  in one call", calls == 1, str(calls))

    r.head("MODEL_EMPTY")
    with guard(r, "an empty reply"):
        out, raised, _ = attempt("empty")
        # Recorded as it is, not as it ought to be: the adapter does not treat
        # an empty reply as a failure, and callers decide what empty means.
        r.check("is returned rather than raised",
                raised is None and out.get("content") == "", repr(out))

    r.head("MODEL_TIMEOUT")
    for behaviour in ("read_timeout", "socket_timeout", "connect_timeout"):
        with guard(r, behaviour):
            _, raised, _ = attempt(behaviour)
            r.check(f"{behaviour} raises ModelTimeout",
                    isinstance(raised, LB.ModelTimeout), type(raised).__name__)
            r.check("  which is still a BrainOffline, so every caller catches it",
                    isinstance(raised, LB.BrainOffline))
            r.check("  and the detail says it timed out, not that it was unreachable",
                    raised is not None and "did not answer within" in str(raised)
                    and "unreachable" not in str(raised), str(raised)[:70])
    with guard(r, "the timeout records how long it waited"):
        _, raised, _ = attempt("read_timeout")
        r.check("seconds is the limit that expired",
                getattr(raised, "seconds", None) == LB.REQUEST_TIMEOUT,
                f"{getattr(raised, 'seconds', None)} vs {LB.REQUEST_TIMEOUT}")
    with guard(r, "streaming times out the same way"):
        _, raised, _ = attempt("read_timeout", stream=True)
        r.check("chat_stream raises ModelTimeout too",
                isinstance(raised, LB.ModelTimeout), type(raised).__name__)

    r.head("MODEL_UNAVAILABLE")
    with guard(r, "nothing listening"):
        _, raised, _ = attempt("refused")
        r.check("raises BrainOffline", isinstance(raised, LB.BrainOffline))
        r.check("  and is not mistaken for a timeout",
                not isinstance(raised, LB.ModelTimeout))
        r.check("  and here unreachable is the right word",
                raised is not None and "unreachable" in str(raised))

    r.head("MODEL_ERROR")
    with guard(r, "a body that is not a reply"):
        _, raised, _ = attempt("bad_body")
        r.check("raises BrainOffline", isinstance(raised, LB.BrainOffline))
        r.check("  and says it answered badly rather than not at all",
                raised is not None and "unreadable body" in str(raised)
                and "unreachable" not in str(raised), str(raised)[:70])
    with guard(r, "the runner crashed"):
        _, raised, _ = attempt("runner_crashed")
        r.check("raises BrainOffline", isinstance(raised, LB.BrainOffline)
                and not isinstance(raised, LB.ModelTimeout))
    with guard(r, "something nobody anticipated"):
        _, raised, _ = attempt("unexpected")
        r.check("raises BrainOffline and names the type",
                isinstance(raised, LB.BrainOffline)
                and "RuntimeError" in str(raised), str(raised)[:70])

    r.head("Retry")
    for behaviour in ("read_timeout", "refused", "bad_body"):
        with guard(r, behaviour):
            _, _, calls = attempt(behaviour)
            # Recorded, not endorsed. There is no retry in the adapter, so a
            # failure is one attempt, and a message that appears twice cannot
            # have come from a retry here.
            r.check(f"{behaviour} is attempted exactly once", calls == 1, str(calls))

    r.head("What he hears")
    with guard(r, "every failure is safe and plain"):
        spoken = {
            "generic": LB.BrainOffline("x").spoken,
            "timeout": LB.ModelTimeout("x").spoken,
        }
        for name, text in spoken.items():
            r.check(f"{name} contains nothing technical", safe(text), text)
            r.check(f"{name} does not say 'local brain'",
                    "local brain" not in text.lower(), text)
        r.check("a timeout is told apart from other failures",
                spoken["timeout"] != spoken["generic"])
        r.check("the timeout says what happened",
                "too long" in spoken["timeout"], spoken["timeout"])
        r.check("context-full still has its own sentence",
                LB.ContextFull("x").spoken not in spoken.values())

    r.head("Every production catcher still catches it")
    with guard(r, "subclassing is what keeps callers unchanged"):
        r.check("ModelTimeout is a BrainOffline",
                issubclass(LB.ModelTimeout, LB.BrainOffline))
        repo = os.path.dirname(HERE)
        catchers = 0
        for name in ("agent_core.py", "conversation.py", "screen_context.py",
                     "local_brain.py"):
            body = open(os.path.join(repo, "diablo", name)).read()
            catchers += body.count("except BrainOffline") + body.count(
                "except local_brain.BrainOffline")
            r.check(f"{name} does not catch ModelTimeout separately",
                    "except ModelTimeout" not in body
                    and "except local_brain.ModelTimeout" not in body)
        r.check("the existing catchers are all on BrainOffline",
                catchers >= 8, f"{catchers} catchers")

    # Real use had turns where the model "did not run" and the trace could not
    # say why: a timeout, a refusal and unusable JSON were all one class name.
    r.head("Every call leaves one row saying how it ended")
    from skills import trace
    import tempfile
    saved_path = trace.PATH
    trace.PATH = os.path.join(tempfile.mkdtemp(prefix="diablo-model-rows-"), "trace.jsonl")

    def rows():
        try:
            return [json.loads(l) for l in open(trace.PATH) if l.strip()]
        except OSError:
            return []

    def last_outcomes(n):
        return [row.get("outcome") for row in rows() if row.get("stage") == "model"][-n:]

    try:
        for behaviour, expected in (("success", "ok"), ("read_timeout", "timeout"),
                                    ("connect_timeout", "timeout"), ("refused", "unreachable"),
                                    ("runner_crashed", "refused"), ("bad_body", "unreadable"),
                                    ("unexpected", "unreachable")):
            with guard(r, behaviour):
                attempt(behaviour)
                r.check(f"{behaviour}: one row, outcome {expected}",
                        last_outcomes(1) == [expected], str(last_outcomes(1)))
        with guard(r, "a stream"):
            attempt("success", stream=True)
            row = [x for x in rows() if x.get("stage") == "model"][-1]
            r.check("a stream that finished is ok", row.get("outcome") == "ok", str(row))
        with guard(r, "an answer that is not JSON"):
            counter = {"calls": 0}
            real = urllib.request.urlopen
            urllib.request.urlopen = transport("success", counter)   # content "hello"
            try:
                LB.chat_json([{"role": "user", "content": "hi"}], schema={"type": "object"})
                outcome = None
            except LB.BrainOffline as e:
                outcome = e.outcome
            finally:
                urllib.request.urlopen = real
            r.check("the call came back, and what came back was unusable",
                    last_outcomes(2) == ["ok", "invalid_json"] and outcome == "invalid_json",
                    str(last_outcomes(2)))
        with guard(r, "what a row holds"):
            row = [x for x in rows() if x.get("stage") == "model"][0]
            r.check("which call, which model, how long",
                    row.get("purpose") == "attempt" and row.get("model")
                    and isinstance(row.get("call_ms"), float), str(row)[:120])
            r.check("and never the words", "hello" not in open(trace.PATH).read()
                    and "hi" not in [str(v) for v in row.values()])
    finally:
        trace.PATH = saved_path

    return r.report("model failures")


if __name__ == "__main__":
    sys.exit(main())
