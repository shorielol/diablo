#!/usr/bin/env python3
"""
What Diablo keeps on disk after it has spoken, answered or looked.

Three places held content with no limit. voice_cache kept every line ever said
as audio — 704 files and 214 MB on this machine, most of them answers, and an
answer can read the screen back. jobs.json kept every delegated answer in full,
forever, screen-derived or not. And the model's take_screenshot tool could
write a picture of every display to the Desktop on any turn, asked or not.

The policy checked here:
  - audio for a stock phrase is kept; audio for anything else is deleted once
    played, or within ten minutes if playback failed and a retry may use it;
  - a job keeps its status, times, ids and agent; an answer that came from the
    screen is never written; any other answer is cut to 32 KB and dropped after
    24 hours; the answer never reaches the trace or the log;
  - a screenshot is saved only when the request asked for one, and only where
    it asked.

Every check reads the files the real code wrote. The network, the speaker, the
model and the display are doubles; nothing is spoken, captured or sent.
"""

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

BASE = tempfile.mkdtemp(prefix="diablo-retention-")
os.environ["DIABLO_BASE_DIR"] = BASE
TRACE = os.path.join(BASE, "trace.jsonl")
os.environ["DIABLO_TRACE"] = TRACE

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import agent_core, agent_tools, conversation, local_brain  # noqa: E402
from skills import delegation_tool as dt  # noqa: E402
from skills import trace, voice  # noqa: E402

trace.PATH = TRACE
SECRET = "QX7-PRIVATE-SCREEN-TEXT"
ANSWER = "ZK4-ORDINARY-RESEARCH-ANSWER"


# ─── Voice doubles: Voicebox answers, the speaker is a list ──────────────────

class _Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


SYNTHESISED: list = []
PLAYED: list = []          # (command word, path or text, file existed at play time)
OUTCOME = {"afplay": ["played"], "say": ["played"]}


def fake_urlopen(request, timeout=None):
    SYNTHESISED.append(getattr(request, "full_url", ""))
    return _Response(b"RIFF" + b"\0" * 4096)


def fake_playback(command, generation=None):
    word, target = command[0], command[-1]
    PLAYED.append((word, target, word != "afplay" or os.path.exists(target)))
    queue = OUTCOME[word]
    return queue.pop(0) if len(queue) > 1 else queue[0]


def fake_repace(path, text, wpm):
    out = path.replace(".wav", f".{wpm}wpm.wav")
    shutil.copyfile(path, out)
    return out


def voice_reset(afplay=("played",), say=("played",)):
    voice.CACHE_DIR = tempfile.mkdtemp(prefix="voice-", dir=BASE)
    SYNTHESISED.clear()
    PLAYED.clear()
    OUTCOME["afplay"] = list(afplay)
    OUTCOME["say"] = list(say)
    voice.STOP.clear()


def transient():
    folder = voice._transient_dir()
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


def stock():
    folder = voice._stock_dir()
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


voice._voice_fingerprint = "test-voice"
voice.available = lambda: True
voice._get_profile_id = lambda: "profile"
voice.urllib.request.urlopen = fake_urlopen
voice._run_playback = fake_playback
voice.repace = fake_repace


# ─── Job doubles: no model, no voice, no window ──────────────────────────────

dt._push = lambda event, **data: None
conversation.session.record_task = lambda *a, **k: None
conversation.session.remember = lambda *a, **k: None
conversation.session.set_focus = lambda *a, **k: None

SAID: list = []
REAL_RUN = agent_core.run


def scripted_run(task, speak, ask=None, confirm=None, working=None, **_):
    """An agent: reads the screen when the task says so, and answers."""
    if "explode" in task:
        raise RuntimeError(f"tool exploded while reading {SECRET}")
    if "screen" in task:
        if working is not None:
            working.append({"tool": "read_screen", "args": "{}", "result": SECRET})
        return f"The window says {SECRET}."
    if "huge" in task:
        return "é" * 40000                       # 80,000 bytes of UTF-8
    return f"Found it: {ANSWER}."


agent_core.run = scripted_run


def fresh_store():
    path = os.path.join(tempfile.mkdtemp(prefix="jobs-", dir=BASE), "jobs.json")
    dt.store = dt.JobStore(path)
    return path


def settle(seconds=10.0):
    deadline = time.time() + seconds
    while dt._queue.unfinished_tasks and time.time() < deadline:
        time.sleep(0.01)
    return dt._queue.unfinished_tasks == 0


def run_job(task, pieces=None, speak=None):
    job = dt.submit(task, speak or (lambda text, **_: SAID.append(str(text))),
                    pieces=pieces)
    settle()
    return job["id"]


def on_disk(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    return raw, {j["id"]: j for j in json.loads(raw)}


def written_trace():
    try:
        return open(TRACE, encoding="utf-8").read()
    except OSError:
        return ""


def voice_checks(r):
    r.head("Spoken audio: played, then gone")
    with guard(r, "an answer that plays"):
        voice_reset()
        route = voice.speak(f"The window says {SECRET}.")
        r.check("it was synthesised and played from a real file",
                route == "voicebox" and PLAYED and PLAYED[0][2], f"{route} {PLAYED[:1]}")
        r.check("and nothing of it is left on disk", transient() == [] and stock() == [],
                f"transient={transient()} stock={stock()}")

    with guard(r, "a stock phrase"):
        voice_reset()
        line = voice.COMMON_LINES[0]
        voice.speak(line)
        kept = stock()
        r.check("a fixed phrase is kept, since it holds nothing of the user's",
                len(kept) == 1 and transient() == [], f"stock={kept}")
        SYNTHESISED.clear()
        voice.speak(line)
        r.check("and is played from disk the second time", SYNTHESISED == [],
                f"{len(SYNTHESISED)} syntheses")

    with guard(r, "a paced answer"):
        voice_reset()
        voice.speak("Step one is to open the settings and then the second panel.", pace=120)
        r.check("the slowed copy goes with the original", transient() == [], str(transient()))

    with guard(r, "playback interrupted"):
        voice_reset(afplay=("interrupted",))
        route = voice.speak("An answer the user talked over.")
        r.check("an interrupted line is not kept",
                route == "interrupted" and transient() == [], f"{route} {transient()}")

    with guard(r, "the speaker fails, the system voice says it"):
        voice_reset(afplay=("failed",), say=("played",))
        route = voice.speak("An answer the fallback voice delivered.")
        r.check("heard is heard: the file goes", route == "local" and transient() == [],
                f"{route} {transient()}")

    with guard(r, "nothing could say it"):
        voice_reset(afplay=("failed",), say=("failed",))
        text = "An answer nobody heard."
        route = voice.speak(text)
        held = transient()
        r.check("a line nobody heard is kept for a retry",
                route == "failed" and len(held) == 1, f"{route} {held}")
        r.check("for no longer than ten minutes", voice.RETRY_SECONDS == 600)
        OUTCOME["afplay"] = ["played"]
        SYNTHESISED.clear()
        route = voice.speak(text)
        r.check("the retry uses it rather than synthesising again",
                route == "cache" and SYNTHESISED == [], f"{route} {len(SYNTHESISED)}")
        r.check("and deletes it once heard", transient() == [], str(transient()))

    with guard(r, "a failed line past its window"):
        voice_reset(afplay=("failed",), say=("failed",))
        text = "Another answer nobody heard."
        voice.speak(text)
        path = os.path.join(voice._transient_dir(), transient()[0])
        r.check("is still there inside ten minutes",
                voice.sweep_transient() == 0 and os.path.exists(path))
        old = time.time() - 601
        os.utime(path, (old, old))
        r.check("cached_path stops offering it", voice.cached_path(text) is None)
        r.check("and the sweep removes it",
                voice.sweep_transient() == 1 and not os.path.exists(path))

    with guard(r, "a streamed answer"):
        voice_reset()
        voice.speak_stream(["First sentence of the answer.", f"Second, with {SECRET}."])
        r.check("both sentences played",
                len([p for p in PLAYED if p[0] == "afplay" and p[2]]) == 2, str(PLAYED))
        r.check("and neither is left", transient() == [], str(transient()))

    with guard(r, "a streamed answer cut off after its first sentence"):
        voice_reset(afplay=("interrupted",))
        voice.speak_stream(["One.", "Two, never heard.", "Three, never heard."])
        time.sleep(0.3)
        r.check("nothing rendered ahead survives", transient() == [], str(transient()))

    r.head("Restart: the old cache and anything orphaned")
    with guard(r, "the startup sweep"):
        voice_reset()
        os.makedirs(voice._stock_dir(), exist_ok=True)
        os.makedirs(voice._transient_dir(), exist_ok=True)
        old = time.time() - 3600
        legacy = os.path.join(voice.CACHE_DIR, "0123456789abcdef0123456789abcdef.wav")
        legacy_paced = legacy.replace(".wav", ".120wpm.wav")
        fresh = os.path.join(voice.CACHE_DIR, "fedcba9876543210fedcba9876543210.wav")
        orphan = os.path.join(voice._transient_dir(), "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.wav")
        kept_stock = os.path.join(voice._stock_dir(), "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.wav")
        not_ours = os.path.join(voice.CACHE_DIR, "notes.txt")
        for path in (legacy, legacy_paced, fresh, orphan, kept_stock, not_ours):
            with open(path, "wb") as f:
                f.write(b"\0" * 1024)
        for path in (legacy, legacy_paced, orphan, kept_stock, not_ours):
            os.utime(path, (old, old))
        removed = voice.sweep_transient()
        r.check("the old unbounded cache and orphans go", removed == 3
                and not any(os.path.exists(p) for p in (legacy, legacy_paced, orphan)),
                f"{removed} removed")
        r.check("stock lines stay", os.path.exists(kept_stock))
        r.check("a file Diablo did not name is never touched", os.path.exists(not_ours))
        r.check("nor one written inside the last ten minutes", os.path.exists(fresh))

    with guard(r, "the sweep runs when the voice starts"):
        import companion_manager
        calls = []
        saved_sweep = voice.sweep_transient
        voice.sweep_transient = lambda *a, **k: calls.append(1) or 0
        try:
            companion_manager.VoiceSubsystem(None).start()
        finally:
            voice.sweep_transient = saved_sweep
        r.check("start() sweeps once", calls == [1], str(calls))


def job_checks(r):
    r.head("Job store: what it keeps of an answer")
    with guard(r, "an ordinary delegated answer"):
        path = fresh_store()
        open(TRACE, "w").close()
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            job = run_job("research solar panel prices")
        raw, jobs = on_disk(path)
        record = jobs[job]
        r.check("the job is done on disk", record["status"] == "done", record["status"])
        r.check("with its times and agent", all(k in record for k in
                ("created_at", "started_at", "finished_at", "agent")))
        r.check("its answer is kept for continuity", ANSWER in record.get("summary", ""))
        r.check("with a 24-hour expiry",
                abs(record["summary_expires_at"] - record["finished_at"] - 86400) < 5,
                str(record.get("summary_expires_at")))
        mode = stat.S_IMODE(os.stat(path).st_mode)
        r.check("the file is readable by this user only", mode == 0o600, oct(mode))
        r.check("the answer is not in the trace", ANSWER not in written_trace())
        r.check("nor in the log", ANSWER not in printed.getvalue())

    with guard(r, "an answer read off the screen"):
        path = fresh_store()
        job = run_job("summarise my screen")
        raw, jobs = on_disk(path)
        record = jobs[job]
        r.check("the job is still recorded as done", record["status"] == "done")
        r.check("the screen's words are nowhere in jobs.json", SECRET not in raw)
        r.check("and the record says why there is no answer",
                record.get("summary_withheld") == "screen", str(record))
        r.check("nothing memory-only was written", "screen" not in record)

    with guard(r, "Vassago's answer, even without a screen tool"):
        path = fresh_store()
        job = run_job("look at this", pieces=[("look at this", "vassago")])
        raw, jobs = on_disk(path)
        r.check("is treated as screen-derived",
                jobs[job].get("summary_withheld") == "screen", str(jobs[job]))

    with guard(r, "a fan-out where one part read the screen"):
        path = fresh_store()
        job = run_job("both", pieces=[("research flights", "asmo"),
                                      ("summarise my screen", "marbas")])
        raw, jobs = on_disk(path)
        record = jobs[job]
        parts = {p["agent"]: p for p in record["parts"]}
        r.check("the screen's words are nowhere in jobs.json", SECRET not in raw)
        r.check("the merged answer is withheld, since it carries every part",
                record.get("summary_withheld") == "screen",
                str(record.get("summary_withheld")))
        r.check("the part that read the screen is withheld",
                parts["marbas"].get("summary_withheld") == "screen")
        r.check("the part that did not keeps its answer",
                ANSWER in parts["asmo"].get("summary", ""), str(parts["asmo"]))

    with guard(r, "a very long answer"):
        path = fresh_store()
        job = run_job("huge report")
        raw, jobs = on_disk(path)
        kept = jobs[job].get("summary", "")
        size = len(kept.encode("utf-8"))
        r.check("is cut to 32 KB", 0 < size <= 32 * 1024, f"{size} bytes")
        r.check("without breaking a character", kept == "é" * (size // 2))

    with guard(r, "a job that failed"):
        path = fresh_store()
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            job = run_job("explode please")
        raw, jobs = on_disk(path)
        record = jobs[job]
        r.check("is failed on disk, and final", record["status"] == "failed")
        r.check("with the kind of failure", record.get("error") == "RuntimeError")
        r.check("and not the message, which held screen text", SECRET not in raw)
        r.check("nor is the message in the log", SECRET not in printed.getvalue())

    with guard(r, "speaking the result fails"):
        path = fresh_store()

        def broken(text, **_):
            raise OSError("speaker gone")
        job = run_job("research tide tables", speak=broken)
        raw, jobs = on_disk(path)
        r.check("the job stays done", jobs[job]["status"] == "done")

    r.head("Job store: expiry and restart")
    with guard(r, "an answer past 24 hours"):
        path = fresh_store()
        job = run_job("research train times")
        with dt.store._lock:
            record = next(j for j in dt.store._jobs if j["id"] == job)
            record["summary_expires_at"] = time.time() - 1
        shown = next(j for j in dt.store.recent(10) if j["id"] == job)
        r.check("is no longer shown", not shown.get("summary"), str(shown.get("summary")))
        with dt.store._lock:
            dt.store._flush()
        raw, jobs = on_disk(path)
        r.check("and is gone from disk", ANSWER not in raw)
        r.check("the job is still done", jobs[job]["status"] == "done")
        r.check("and says the answer expired",
                jobs[job].get("summary_withheld") == "expired")

    with guard(r, "a file written before this policy"):
        path = os.path.join(tempfile.mkdtemp(prefix="jobs-", dir=BASE), "jobs.json")
        legacy = [
            {"id": "old00001", "task": "t", "status": "done", "created_at": 1.0,
             "finished_at": 2.0, "agent": "asmo", "summary": f"old {SECRET}"},
            {"id": "old00002", "task": "t", "status": "running", "created_at": 3.0,
             "agent": "asmo", "parts": [{"agent": "asmo", "task": "t",
                                         "status": "running", "summary": ""}]},
            {"id": "old00003", "task": "t", "status": "done", "created_at": 4.0,
             "finished_at": time.time(), "agent": "asmo", "summary": f"new {ANSWER}",
             "summary_expires_at": time.time() + 3600},
        ]
        with open(path, "w") as f:
            json.dump(legacy, f)
        dt.JobStore(path)                       # what starting Diablo does
        raw, jobs = on_disk(path)
        r.check("an answer with no expiry is removed at startup", SECRET not in raw
                and jobs["old00001"].get("summary_withheld") == "legacy")
        r.check("work cut off by the restart is interrupted, on disk at once",
                jobs["old00002"]["status"] == "interrupted"
                and jobs["old00002"]["parts"][0]["status"] == "interrupted")
        r.check("an answer still inside its 24 hours survives the restart",
                ANSWER in jobs["old00003"].get("summary", ""))
        reopened = dt.JobStore(path)
        r.check("and interrupted stays final",
                not reopened.update("old00002", status="running"))

    with guard(r, "a corrupt or foreign file"):
        path = os.path.join(tempfile.mkdtemp(prefix="jobs-", dir=BASE), "jobs.json")
        with open(path, "w") as f:
            f.write('{"not": "a list"}')
        r.check("loads as empty rather than crashing", dt.JobStore(path).recent(5) == [])


def screenshot_checks(r):
    r.head("Screenshots: only when asked, only where asked")
    landing = tempfile.mkdtemp(prefix="home-", dir=BASE)
    for folder in ("Desktop", "Downloads"):
        os.makedirs(os.path.join(landing, folder))
    saved = (agent_tools.HOME, agent_tools.DESKTOP, agent_tools._skill)
    captures: list = []

    class Display:
        def describe(self):
            return "main display"

    class FakeDisplays:
        def displays(self):
            return [Display()]

        def capture_display(self, one):
            path = tempfile.mkstemp(prefix="diablo_cap_", suffix=".png", dir=BASE)[1]
            with open(path, "wb") as f:
                f.write(b"\x89PNG" + b"\0" * 2048)
            captures.append(path)
            return path

    agent_tools.HOME = landing
    agent_tools.DESKTOP = os.path.join(landing, "Desktop")
    agent_tools._skill = (lambda name: FakeDisplays() if name == "display"
                          else saved[2](name))

    def files():
        return sorted(f for d in ("Desktop", "Downloads")
                      for f in os.listdir(os.path.join(landing, d)))

    try:
        with guard(r, "an ordinary question, through the agent loop"):
            script = [
                {"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": "take_screenshot",
                                              "arguments": {}}}]},
                {"role": "assistant", "content": "Here is what is on your screen."},
            ]
            kept = (local_brain.chat, agent_core.local_brain.chat, agent_core.plan)
            local_brain.chat = agent_core.local_brain.chat = (
                lambda messages, tools=None, **k: script.pop(0) if script
                else {"role": "assistant", "content": "Done, Sir."})
            agent_core.plan = lambda task: []
            working: list = []
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    REAL_RUN("what's on my screen", speak=lambda *a, **k: None,
                             ask=None, confirm=None, working=working)
            finally:
                local_brain.chat, agent_core.local_brain.chat, agent_core.plan = kept
            results = [s["result"] for s in working if s["tool"] == "take_screenshot"]
            r.check("the model asked for a screenshot", len(results) == 1, str(results))
            r.check("and was refused", bool(results) and results[0].startswith("Not saved"))
            r.check("nothing was captured or written",
                    captures == [] and files() == [], str(files()))

        with guard(r, "the person's words decide, not the model's rewrite of them"):
            script = [
                {"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": "take_screenshot",
                                              "arguments": {}}}]},
                {"role": "assistant", "content": "Done, Sir."},
            ]
            kept = (local_brain.chat, agent_core.local_brain.chat, agent_core.plan)
            local_brain.chat = agent_core.local_brain.chat = (
                lambda messages, tools=None, **k: script.pop(0) if script
                else {"role": "assistant", "content": "Done, Sir."})
            agent_core.plan = lambda task: []
            working = []
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    REAL_RUN("take a screenshot of the screen and describe it",
                             speak=lambda *a, **k: None, ask=None, confirm=None,
                             working=working, request="what's on my screen")
            finally:
                local_brain.chat, agent_core.local_brain.chat, agent_core.plan = kept
            results = [s["result"] for s in working if s["tool"] == "take_screenshot"]
            r.check("a rewrite that mentions a screenshot does not save one",
                    bool(results) and results[0].startswith("Not saved")
                    and captures == [] and files() == [], str(results)[:80])

        with guard(r, "the tool called with no request at all"):
            agent_tools.CONTEXT["request"] = None
            out = agent_tools.execute("take_screenshot", {})
            r.check("is refused", out.startswith("Not saved") and files() == [], out)

        with guard(r, "asked for, plainly"):
            agent_tools.CONTEXT["request"] = "take a screenshot"
            out = agent_tools.execute("take_screenshot", {})
            r.check("one file on the Desktop",
                    len(os.listdir(agent_tools.DESKTOP)) == 1, out)
            r.check("and the private capture it came from is gone",
                    not any(os.path.exists(p) for p in captures))

        with guard(r, "asked for, somewhere else"):
            agent_tools.CONTEXT["request"] = "save a screenshot to my downloads"
            out = agent_tools.execute("take_screenshot", {})
            r.check("it goes where it was asked",
                    len(os.listdir(os.path.join(landing, "Downloads"))) == 1, out)
            r.check("and says so", "Downloads" in out, out)

        with guard(r, "talking about a screenshot is not asking for one"):
            # Each of these reached the reflex as a request, and the reflex
            # would have put a capture of every display on the Desktop.
            about = ("take a look at the screenshot on my desktop",
                     "can you get the text from this screenshot",
                     "make sense of the screenshot I just took",
                     "grab the screenshots folder and tidy it",
                     "screenshots folder is a mess, sort it",
                     "what's in the screenshot I took yesterday")
            wrong = [t for t in about if agent_tools.wants_saved_screenshot(t)]
            r.check("none of them is read as a request", not wrong, "; ".join(wrong))
            asks = ("take a screenshot", "screenshot this", "screenshot please",
                    "take a quick screenshot of my screen", "can you take me a screenshot",
                    "grab a screenshot", "save a screenshot to my downloads",
                    "capture a screenshot of this window", "take screenshots",
                    "save a copy of my screen")
            missed = [t for t in asks if not agent_tools.wants_saved_screenshot(t)]
            r.check("while real requests still are", not missed, "; ".join(missed))

        with guard(r, "asked not to"):
            agent_tools.CONTEXT["request"] = "don't take a screenshot, just read it"
            before = files()
            out = agent_tools.execute("take_screenshot", {})
            r.check("is refused", out.startswith("Not saved") and files() == before, out)

        with guard(r, "the voice reflex uses the same check"):
            src = open(os.path.join(DIABLO_HOME, "voice_bridge.py"), encoding="utf-8").read()
            r.check("save_screenshot is only reached through wants_saved_screenshot",
                    "wants_saved_screenshot(lower)" in src
                    and src.count("save_screenshot(") == 1
                    and "screencapture" not in src)
    finally:
        agent_tools.CONTEXT["request"] = None
        agent_tools.HOME, agent_tools.DESKTOP, agent_tools._skill = saved


def run():
    r = Results()
    voice_checks(r)
    job_checks(r)
    screenshot_checks(r)
    return r.report("retention")


if __name__ == "__main__":
    try:
        sys.exit(run())
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
