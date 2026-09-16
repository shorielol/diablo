"""
D.I.A.B.L.O. — a record of why it did what it did.

Diablo's only output today is print(), which is fine for watching it work and
useless for answering "why did it ask me that?" or "where did those four
seconds go?". Those questions come up constantly and the log cannot answer
either, because a line of prose has no fields.

So: one JSON object per line, per decision. Not a debug log — a decision log.
Every gate writes what it chose and what made it choose, and `diablo trace`
reads it back as a chain.

    {"ts": 1789.., "turn": "a3f1", "stage": "understand", "route": "clarify",
     "confidence": 0.31, "reason": "dangling pronoun", "ms": 412}

Design constraints that shaped this:

  It must never slow a turn down. Writes are appended and flushed, never
  buffered across turns, but the whole thing is wrapped so a full disk or a
  permissions problem costs nothing and breaks nothing.

  It must never be the reason something crashes. Every public function
  swallows its own exceptions. A tracing layer that can take down the
  assistant is worse than no tracing layer.

  It must not grow forever. Rotates at 5 MB, keeps one previous file.
"""

import contextlib
import contextvars
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
PATH = os.getenv("DIABLO_TRACE", os.path.join(HOME, "trace.jsonl"))
MAX_BYTES = int(os.getenv("DIABLO_TRACE_MAX_BYTES", str(5 * 1024 * 1024)))
ENABLED = os.getenv("DIABLO_TRACE_ENABLED", "1") not in ("0", "", "false", "False")

_lock = threading.Lock()

# A ContextVar, not threading.local(), and the difference is the whole point.
#
# This was thread-local until the audit measured what that cost: 1,800 of 3,072
# real records carried no turn id at all, including 944 of 961 tool calls,
# because roster agents run on their own threads and a thread-local starts
# every thread empty. The correlation has to be inherited; the callbacks in
# agent_tools.CONTEXT must NOT be, which is why that one stays thread-local.
# Same mechanism, opposite requirement.
#
# Measured caveat, so nobody assumes this swap was sufficient on its own:
#
#     threading.Thread(target=work)                 -> context NOT inherited
#     threading.Thread(target=lambda: ctx.run(work)) -> inherited
#     ThreadPoolExecutor.submit(work)               -> NOT inherited
#
# So a spawn site still has to carry copy_context() across. `carry()` below is
# that, and the roster's worker at roster.py:620 is not yet using it.
_context: "contextvars.ContextVar[Dict[str, Any]]" = contextvars.ContextVar(
    "diablo_trace_context", default=None)


def _current() -> Dict[str, Any]:
    return _context.get() or {}


def new_turn(source: str = "voice") -> str:
    """Begin a turn and return its id."""
    turn_id = uuid.uuid4().hex[:8]
    _context.set({"turn": turn_id, "started": time.time()})
    event("turn_start", source=source)
    return turn_id


def current_turn() -> Optional[str]:
    return _current().get("turn")


def elapsed_ms() -> Optional[float]:
    started = _current().get("started")
    return round((time.time() - started) * 1000, 1) if started else None


@contextlib.contextmanager
def scope(**fields: Any):
    """
    Bind correlation fields for everything traced inside the block.

    Used by the evaluation harnesses so a record can say which run, which
    scenario and which perception source produced it, without threading those
    three arguments through every function that might want to trace.

        with trace.scope(run="abc123", scenario="gear-01", source="ax"):
            ...

    Restores the previous binding on the way out, including on an exception.
    """
    merged = dict(_current())
    merged.update({k: v for k, v in fields.items() if v is not None})
    token = _context.set(merged)
    try:
        yield merged.get("turn")
    finally:
        _context.reset(token)


def bound() -> Dict[str, Any]:
    """The correlation fields currently in scope, minus the bookkeeping."""
    return {k: v for k, v in _current().items() if k not in ("turn", "started")}


def carry():
    """
    A snapshot of the current context, for handing to a new thread.

    A plain thread inherits nothing, so the spawn site has to do this:

        ctx = trace.carry()
        threading.Thread(target=lambda: ctx.run(work, arg)).start()
    """
    return contextvars.copy_context()


def event(stage: str, **fields: Any) -> None:
    """
    Record one decision. Never raises, never blocks for long.

    `stage` is what was being decided — understand, clarify, route, tool,
    verify, speak. Everything else is whatever that stage knows.
    """
    if not ENABLED:
        return
    try:
        record = {
            "ts": round(time.time(), 3),
            "turn": current_turn(),
            "stage": stage,
            "ms": elapsed_ms(),
        }
        # Whatever scope() bound, so a benchmark record says which run and
        # which scenario it belongs to. Explicit fields on the call win.
        record.update(bound())
        for key, value in fields.items():
            # Keep it to things json can hold and a human can read. A tool
            # result can be thousands of characters; the decision is what
            # matters here, not the payload.
            if isinstance(value, (str, int, float, bool, type(None))):
                record[key] = value[:300] if isinstance(value, str) else value
            else:
                record[key] = str(value)[:300]

        line = json.dumps(record, ensure_ascii=False)
        with _lock:
            _rotate_if_needed()
            with open(PATH, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        # Deliberately silent. A tracing layer that can break the assistant is
        # worse than no tracing layer.
        pass


def _rotate_if_needed() -> None:
    try:
        if os.path.getsize(PATH) < MAX_BYTES:
            return
    except OSError:
        return
    try:
        previous = PATH + ".1"
        if os.path.exists(previous):
            os.unlink(previous)
        os.rename(PATH, previous)
    except OSError:
        pass


def read(limit: int = 50, turn: Optional[str] = None) -> List[Dict[str, Any]]:
    """The most recent events, oldest first."""
    records: List[Dict[str, Any]] = []
    for path in (PATH,):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if turn and record.get("turn") != turn:
                        continue
                    records.append(record)
        except OSError:
            pass
    return records[-limit:]


def turns(limit: int = 10) -> List[List[Dict[str, Any]]]:
    """The last few turns, each as its own list of events."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for record in read(limit=5000):
        key = record.get("turn") or "-"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(record)
    return [grouped[k] for k in order[-limit:]]


def clear() -> None:
    try:
        os.unlink(PATH)
    except OSError:
        pass


# ─── Reading it back ─────────────────────────────────────────────────────────

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def _render(chain: List[Dict[str, Any]]) -> str:
    if not chain:
        return ""
    head = chain[0]
    when = time.strftime("%H:%M:%S", time.localtime(head["ts"]))
    lines = [f"{BOLD}{when}  turn {head.get('turn')}{RESET}"]
    for record in chain:
        stage = record.get("stage", "?")
        ms = record.get("ms")
        detail = " ".join(
            f"{k}={v}" for k, v in record.items()
            if k not in ("ts", "turn", "stage", "ms") and v not in (None, ""))
        stamp = f"{ms:7.0f}ms" if isinstance(ms, (int, float)) else " " * 9
        lines.append(f"  {DIM}{stamp}{RESET}  {stage:14} {detail}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read Diablo's decision log.")
    parser.add_argument("--last", type=int, default=10, help="how many turns")
    parser.add_argument("--turn", default="", help="one turn id")
    parser.add_argument("--raw", action="store_true", help="one JSON object per line")
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args(argv)

    if args.clear:
        clear()
        print(f"  cleared {PATH}")
        return 0

    if args.raw:
        for record in read(limit=args.last * 20, turn=args.turn or None):
            print(json.dumps(record, ensure_ascii=False))
        return 0

    if args.turn:
        print(_render(read(limit=500, turn=args.turn)))
        return 0

    chains = turns(limit=args.last)
    if not chains:
        print(f"\n  Nothing recorded yet. {PATH}\n")
        return 0
    print()
    for chain in chains:
        print(_render(chain))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
