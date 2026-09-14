"""
D.I.A.B.L.O. — reading settings without dying over a typo.

`int(os.getenv("DIABLO_HUMOUR", "2"))` is fine right up until .env contains
something that is not a number. Then persona.py raises at import, conversation
fails to import because it imports persona, voice_bridge fails because it
imports conversation, and the whole assistant is dead before it reaches main()
— over one bad character in a config file, with a ValueError as the only clue.

That happened. So every numeric setting is read through here instead: a bad
value falls back to the default and says so once, and Diablo keeps running.
"""

import os
import sys
from typing import Optional, Set

_warned: Set[str] = set()


def _complain(name: str, raw: str, fallback) -> None:
    if name in _warned:
        return
    _warned.add(name)
    print(f"  [Config] {name}={raw!r} is not valid. Using {fallback!r}.",
          file=sys.stderr)


def env_int(name: str, fallback: int, low: Optional[int] = None,
            high: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return fallback
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        _complain(name, raw, fallback)
        return fallback
    if low is not None and value < low:
        _complain(name, raw, fallback)
        return fallback
    if high is not None and value > high:
        _complain(name, raw, fallback)
        return fallback
    return value


def env_float(name: str, fallback: float, low: Optional[float] = None,
              high: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return fallback
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        _complain(name, raw, fallback)
        return fallback
    if low is not None and value < low:
        _complain(name, raw, fallback)
        return fallback
    if high is not None and value > high:
        _complain(name, raw, fallback)
        return fallback
    return value


def env_bool(name: str, fallback: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return fallback
    return str(raw).strip().lower() not in ("0", "", "false", "no", "off")
