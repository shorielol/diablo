"""
Shared scaffolding for the Diablo stress tests.

Deliberately not pytest. These tests touch the real filesystem, the real
microphone, a real 14B model and a real Trash, so the ability to run one
section at a time, in a known order, with cleanup that runs even after a hard
failure, matters more than fixtures.
"""

import os
import shutil
import sys
import time
import traceback

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

# Read .env before importing anything, exactly as the app does at startup.
#
# Without this the tests ran against local_brain's built-in defaults rather
# than the configuration the app actually uses — CHAT_MODEL came out as
# qwen3:14b while the running app was on qwen3:4b-instruct. Every model-facing
# assertion was therefore being made about a model the user never runs, and a
# task splitter that passed 3 of 3 here returned nothing at all in the app.
# A test suite on the wrong model is worse than no suite: it reports green.
try:
    from companion_manager import load_env as _load_env
    _load_env()
except Exception as _e:                                   # pragma: no cover
    print(f"  (could not load .env: {_e}; tests will use built-in defaults)")

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = []
        self.skipped = []
        self.section = ""

    def head(self, name):
        self.section = name
        print(f"\n{name}")

    def ok(self, label, detail=""):
        self.passed += 1
        print(f"  {GREEN}✓{RESET} {label}" + (f"  {DIM}— {detail}{RESET}" if detail else ""))

    def bad(self, label, detail=""):
        self.failed.append((self.section, label, detail))
        print(f"  {RED}✗ {label}{RESET}" + (f"  {DIM}— {detail}{RESET}" if detail else ""))

    def skip(self, label, why=""):
        self.skipped.append((self.section, label, why))
        print(f"  {YELLOW}–{RESET} {label}  {DIM}(skipped: {why}){RESET}")

    def check(self, label, condition, detail=""):
        (self.ok if condition else self.bad)(label, detail)
        return bool(condition)

    def report(self, title):
        total = self.passed + len(self.failed)
        print(f"\n{'─' * 68}")
        if self.failed:
            print(f"{RED}{len(self.failed)} of {total} failed{RESET} in {title}")
            for section, label, detail in self.failed:
                print(f"  {RED}✗{RESET} [{section}] {label}" + (f" — {detail}" if detail else ""))
        else:
            print(f"{GREEN}{self.passed}/{total} passed{RESET} in {title}")
        if self.skipped:
            print(f"{YELLOW}{len(self.skipped)} skipped{RESET}")
            for section, label, why in self.skipped:
                print(f"  {YELLOW}–{RESET} [{section}] {label} ({why})")
        return 1 if self.failed else 0


def guard(results, label):
    """Turn an unexpected exception into a failed check rather than a dead run."""
    class _Guard:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb):
            if exc_type is not None:
                results.bad(label, f"{exc_type.__name__}: {exc}")
                if os.getenv("DIABLO_TEST_TRACE"):
                    traceback.print_exception(exc_type, exc, tb)
            return True
    return _Guard()


class Sandbox:
    """A scratch folder inside an allowed root, removed however the test ends."""

    def __init__(self, name="diablo-stress"):
        self.path = os.path.join(os.path.expanduser("~/Desktop"), name)

    def __enter__(self):
        shutil.rmtree(self.path, ignore_errors=True)
        os.makedirs(self.path, exist_ok=True)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)

    def file(self, name, content=""):
        p = os.path.join(self.path, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return p

    def rel(self, name=""):
        return os.path.join(os.path.basename(self.path), name) if name \
            else os.path.basename(self.path)


class Timer:
    def __enter__(self):
        self.t = time.time()
        self.elapsed = 0.0   # readable even if the block raised
        return self
    def __exit__(self, *exc):
        self.elapsed = time.time() - self.t
