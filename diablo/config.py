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


# ─── PATH ────────────────────────────────────────────────────────────────────

def repair_path() -> str:
    """
    Put Homebrew back on PATH, because a .app does not inherit your shell's.

    An application launched from Finder gets PATH=/usr/bin:/bin:/usr/sbin:
    /sbin:/usr/local/bin and nothing else. Homebrew on Apple Silicon installs
    to /opt/homebrew/bin, so every tool that shells out by name works in a
    terminal and fails in the app — silently, because the failures come back
    as exceptions inside libraries that swallow them.

    This cost a day of "Diablo can't hear me". Parakeet decodes audio by
    invoking ffmpeg, so every single utterance died with "FFmpeg is not
    installed or not in your PATH" while ffmpeg sat in /opt/homebrew/bin. The
    same gap had already turned the slower explaining pace into a no-op, since
    that runs audio through ffmpeg too and falls back to the original file
    when it fails.

    Returns the PATH it set, so a caller can log it.
    """
    import os as _os
    extra = [p for p in ("/opt/homebrew/bin", "/opt/homebrew/sbin",
                         "/usr/local/bin", _os.path.expanduser("~/.local/bin"))
             if _os.path.isdir(p)]
    current = _os.environ.get("PATH", "").split(":")
    missing = [p for p in extra if p not in current]
    if missing:
        _os.environ["PATH"] = ":".join(missing + current)
    return _os.environ["PATH"]


def require(tool: str) -> str:
    """
    Where a command-line tool actually is, or "".

    Used instead of trusting PATH, so a caller can say plainly that something
    is missing rather than discovering it three layers down inside a library.
    """
    import shutil as _shutil
    repair_path()
    return _shutil.which(tool) or ""


repair_path()
