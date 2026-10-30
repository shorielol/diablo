#!/usr/bin/env python3
"""
End-to-end latency, through the production entry point.

Every request goes in where a transcribed sentence does —
voice_bridge.process_command — and is timed until Diablo has something useful
to say and until it is finished. What is measured, per request:

    first text     the first words Diablo would say, from the request
    first audio    first text, plus synthesising those words with the real
                   voice (played to nobody; the file is deleted at once)
    complete       the last thing said, or the delegated job reaching a final state
    stages         the turn's trace: when each decision was made

Cold means every language model was unloaded first; warm means resident.

What is stubbed, and why. Nothing here may act on the real Mac: launching an
app, opening a browser tab or running AppleScript that changes something is
recorded and answered "done" at the last moment, after every decision has been
made. Read-only AppleScript and screen capture run for real, so the screen
question reads the actual screen (and keeps nothing). The file task runs in a
temporary folder standing in for the Desktop. The microphone and speech-to-text
are not in the loop.

    python3 tests/perf_e2e.py --out perf.json
    python3 tests/perf_e2e.py --only calc,app --warm 10
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

WORK = tempfile.mkdtemp(prefix="diablo-perf-")
TRACE = os.path.join(WORK, "trace.jsonl")
os.environ["DIABLO_TRACE"] = TRACE
os.environ["DIABLO_BASE_DIR"] = WORK

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)
try:
    from companion_manager import load_env
    load_env()
except Exception:
    pass

# ─── The last moment before the Mac is touched ───────────────────────────────

BLOCKED: list = []
_real_run, _real_popen = subprocess.run, subprocess.Popen
_CHANGES = re.compile(r"\b(make new|keystroke|key code|click|activate|open location|"
                      r"set |delete|close|quit|launch|reopen|tell .* to (open|make))\b",
                      re.IGNORECASE)


def _mutating(argv) -> bool:
    if not isinstance(argv, (list, tuple)) or not argv:
        return False
    name = os.path.basename(str(argv[0]))
    if name == "open":
        return True
    if name == "osascript":
        return bool(_CHANGES.search(" ".join(str(a) for a in argv[1:])))
    return False


class _Done:
    returncode, stdout, stderr, pid = 0, "", "", 0

    def wait(self, *a, **k):
        return 0

    def communicate(self, *a, **k):
        return "", ""

    def poll(self):
        return 0


def _guarded_run(argv, *a, **k):
    if _mutating(argv):
        BLOCKED.append(" ".join(str(x) for x in argv)[:80])
        return _Done()
    return _real_run(argv, *a, **k)


def _guarded_popen(argv, *a, **k):
    if _mutating(argv):
        BLOCKED.append(" ".join(str(x) for x in argv)[:80])
        return _Done()
    return _real_popen(argv, *a, **k)


subprocess.run, subprocess.Popen = _guarded_run, _guarded_popen

import voice_bridge  # noqa: E402
from skills import agent_tools, conversation, delegation_tool, local_brain  # noqa: E402
from skills import voice  # noqa: E402

voice_bridge.set_ui_state = lambda state: None

# The file task's Desktop is a temporary folder.
DESK = os.path.join(WORK, "Desktop")
os.makedirs(DESK)
agent_tools.DESKTOP = DESK
agent_tools.WORKSPACES = {"desktop": DESK}
agent_tools.ALLOWED_ROOTS = [DESK]
agent_tools.COWORK = DESK

# ─── A speaker that times, and synthesises without playing ───────────────────

CLOCK = {"t0": 0.0, "first_text": None, "first_audio": None, "last": None,
         "said": 0, "synth": True}
_lock = threading.Lock()


def _heard(text: str) -> None:
    now = time.perf_counter()
    with _lock:
        CLOCK["said"] += 1
        CLOCK["last"] = now - CLOCK["t0"]
        first = CLOCK["first_text"] is None
        if first:
            CLOCK["first_text"] = now - CLOCK["t0"]
    if first and text.strip():
        sentence = text.strip().split(". ")[0]
        if CLOCK["synth"]:
            path = None
            try:
                path = voice.synthesize(sentence)
            finally:
                if path:
                    voice._release([path], "played")
        else:
            # Voicebox down: production speaks with macOS `say`. Rendered to a
            # file nobody plays, so its cost is counted and nothing is heard.
            out = os.path.join(WORK, "first.aiff")
            _real_run(["say", "-o", out, sentence], capture_output=True)
            try:
                os.unlink(out)
            except OSError:
                pass
        CLOCK["first_audio"] = time.perf_counter() - CLOCK["t0"]


def recorder(text, *a, **k):
    if isinstance(text, str):
        _heard(text)
        return "recorded"
    first = True
    for piece in text:                  # a stream: chunks as they are written
        if piece and first:
            _heard(piece)
            first = False
    with _lock:
        CLOCK["last"] = time.perf_counter() - CLOCK["t0"]
    return "recorded"


recorder.streams = True
voice_bridge.speak = recorder
voice_bridge.ask_user_voice = lambda *a, **k: ""
voice_bridge.confirm_voice = lambda *a, **k: False

# ─── The requests ────────────────────────────────────────────────────────────

TASKS = [
    ("app", "open calculator", "direct application launch"),
    ("tab", "open a new browser tab", "open a new browser tab"),
    ("calc", "what is 17 times 23", "deterministic calculation"),
    ("screen", "what app am I looking at right now", "simple screen question"),
    ("multi", "make a folder called bench-notes on my desktop and put a file "
              "called todo.txt in it that says buy milk", "multi-step file task"),
    ("research", "research the best budget mechanical keyboards under 100 dollars",
     "delegated research"),
]


def unload_models() -> None:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=3) as r:
            loaded = [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        loaded = []
    for name in loaded:
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=json.dumps({"model": name, "keep_alive": 0}).encode(),
                headers={"Content-Type": "application/json"}), timeout=30).read()
        except Exception:
            pass
    time.sleep(1.0)


def stages_of(start_ts: float) -> dict:
    """When each stage first appeared in this request's trace, in ms."""
    out = {}
    try:
        for line in open(TRACE, encoding="utf-8"):
            row = json.loads(line)
            if row.get("ts", 0) < start_ts - 0.05:
                continue
            stage = row.get("stage")
            key = stage if stage != "route" else f"route:{row.get('route', '')}"
            if key not in out and isinstance(row.get("ms"), (int, float)):
                out[key] = round(row["ms"], 1)
    except OSError:
        pass
    return out


def wait_for_jobs(seconds: float) -> str:
    end = time.time() + seconds
    while time.time() < end:
        if not delegation_tool.store.active():
            jobs = delegation_tool.store.recent(1)
            return jobs[0]["status"] if jobs else "none"
        time.sleep(0.2)
    return "timeout"


MULTI_FILE = os.path.join(DESK, "bench-notes", "todo.txt")


def one(text: str, delegated: bool) -> dict:
    # Each file-task run starts from an empty Desktop. Left in place, the
    # second run met an existing todo.txt, the confirm stub declined the
    # overwrite, and every warm "multi-step" time was a refusal.
    shutil.rmtree(os.path.dirname(MULTI_FILE), ignore_errors=True)
    conversation.session.reset()
    for key in ("first_text", "first_audio", "last"):
        CLOCK[key] = None
    CLOCK["said"] = 0
    BLOCKED.clear()
    jobs_before = len(delegation_tool.store.recent(1000))
    start_ts = time.time()
    CLOCK["t0"] = time.perf_counter()
    error = None
    try:
        voice_bridge.process_command(text)
    except Exception as e:
        error = type(e).__name__
    returned = time.perf_counter() - CLOCK["t0"]
    queued = len(delegation_tool.store.recent(1000)) > jobs_before
    status = wait_for_jobs(300) if queued else "inline"
    complete = (time.perf_counter() - CLOCK["t0"]) if queued else max(
        returned, CLOCK["last"] or 0)
    return {
        "first_text_s": _r(CLOCK["first_text"]),
        "first_audio_s": _r(CLOCK["first_audio"]),
        "complete_s": _r(complete),
        "returned_s": _r(returned),
        "spoken_lines": CLOCK["said"],
        "status": status,
        "blocked": list(BLOCKED),
        "error": error,
        "stages_ms": stages_of(start_ts),
        # Whether this run, not some earlier one, left the file it was asked for.
        "file_written": _wrote_milk(),
    }


def _wrote_milk() -> bool:
    try:
        return "buy milk" in open(MULTI_FILE).read().lower()
    except OSError:
        return False


def _r(x):
    return None if x is None else round(x, 3)


def pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    rank = max(1, -(-len(values) * q // 100))        # nearest rank
    return values[int(rank) - 1]


def summary(runs):
    out = {}
    for key in ("first_text_s", "first_audio_s", "complete_s"):
        values = [r[key] for r in runs]
        out[key] = {"p50": pct(values, 50), "p95": pct(values, 95),
                    "n": len([v for v in values if v is not None])}
    return out


def model_load() -> dict:
    unload_models()
    began = time.perf_counter()
    try:
        local_brain.warm()
    except Exception:
        pass
    return {"chat_model": local_brain.CHAT_MODEL,
            "cold_load_s": round(time.perf_counter() - began, 3)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="")
    parser.add_argument("--only", default="")
    parser.add_argument("--warm", type=int, default=5)
    parser.add_argument("--research-warm", type=int, default=2)
    parser.add_argument("--no-cold", action="store_true")
    parser.add_argument("--no-synth", action="store_true")
    parser.add_argument("--keep-trace", default="",
                        help="copy the run's trace (sizes and timings only) here")
    args = parser.parse_args()
    CLOCK["synth"] = not args.no_synth and voice.available()

    wanted = set(filter(None, args.only.split(",")))
    report = {"when": time.strftime("%Y-%m-%d %H:%M:%S"),
              "voicebox": CLOCK["synth"],
              "first_audio_path": "voicebox" if CLOCK["synth"] else "macOS say (fallback)",
              "model": model_load(), "tasks": {}}
    for key, text, label in TASKS:
        if wanted and key not in wanted:
            continue
        delegated = key == "research"
        entry = {"label": label, "request": text}
        if not args.no_cold:
            unload_models()
            entry["cold"] = one(text, delegated)
        warm = args.research_warm if delegated else args.warm
        one(text, delegated)                       # make sure it is resident
        entry["warm_runs"] = [one(text, delegated) for _ in range(warm)]
        entry["warm"] = summary(entry["warm_runs"])
        report["tasks"][key] = entry
        w = entry["warm"]
        print(f"{key:9} warm first-text p50 {w['first_text_s']['p50']}s "
              f"p95 {w['first_text_s']['p95']}s · complete p50 {w['complete_s']['p50']}s "
              f"p95 {w['complete_s']['p95']}s · cold complete "
              f"{entry.get('cold', {}).get('complete_s')}s · "
              f"{entry['warm_runs'][-1]['status']} · blocked "
              f"{len(entry['warm_runs'][-1]['blocked'])}", flush=True)
    multi = report["tasks"].get("multi")
    if multi:
        runs = multi["warm_runs"] + ([multi["cold"]] if "cold" in multi else [])
        report["multi_step_runs_that_wrote_the_file"] = (
            f"{sum(r['file_written'] for r in runs)} of {len(runs)}")
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
    if args.keep_trace and os.path.exists(TRACE):
        shutil.copyfile(TRACE, args.keep_trace)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        subprocess.run, subprocess.Popen = _real_run, _real_popen
        shutil.rmtree(WORK, ignore_errors=True)
    os._exit(code)
