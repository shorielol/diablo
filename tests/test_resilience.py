#!/usr/bin/env python3
"""
What Diablo does when the things it depends on go away.

An assistant that only works when Ollama, Voicebox, four MCP servers and the
microphone are all healthy is not something you can leave running. Each of those
will be missing at some point — the laptop sleeps, a server crashes, the model
gets unloaded to free memory — and the required behaviour is always the same:
say what is wrong, out loud, and stay alive.

The failure this guards against is the silent one. Diablo has already shipped
two bugs where a dependency vanished and the only symptom was that nothing
happened.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, Sandbox, guard, Timer  # noqa: E402

from skills import agent_core, agent_tools, conversation, local_brain, mcp_client, voice  # noqa: E402

r = Results()


class Voice:
    def __init__(self): self.lines = []
    def __call__(self, text, **kw): self.lines.append(text)
    def all(self): return " | ".join(self.lines)


def run():
    # ── the brain disappears ────────────────────────────────────────────────
    r.head("Ollama goes away")
    was = local_brain.OLLAMA_HOST
    local_brain.OLLAMA_HOST = "http://127.0.0.1:9"
    try:
        with guard(r, "handle() says so out loud"):
            voice_out = Voice()
            with Timer() as t:
                result = agent_core.handle("read my econ ee draft", speak=voice_out)
            r.check("it does not raise", isinstance(result, dict), str(result)[:50])
            r.check("it reports the brain is offline",
                    "offline" in voice_out.all().lower() or
                    "ollama" in voice_out.all().lower(), voice_out.all()[:70])
            r.check("and fails fast", t.elapsed < 20, f"{t.elapsed:.1f}s")

        with guard(r, "conversation degrades gracefully"):
            conversation.session.reset()
            reply = conversation.talk("how's it going")
            r.check("it answers rather than raising", isinstance(reply, str) and reply,
                    reply[:60])
            # "model" accepted alongside the older words. This checked for the
            # literal "brain", which pinned the test to the phrase "My local
            # brain stopped responding" — the phrase an incident on 2026-09-16
            # showed he could not interpret, and that the conversation model,
            # asked about, said did not exist. What this asserts is unchanged:
            # the reply admits the model is unavailable instead of pretending.
            r.check("and says the brain is down", "offline" in reply.lower()
                    or "brain" in reply.lower() or "model" in reply.lower(),
                    reply[:60])
    finally:
        local_brain.OLLAMA_HOST = was

    with guard(r, "it recovers once Ollama is back"):
        r.check("available() is true again", local_brain.available())
        conversation.session.reset()
        reply = conversation.talk("say the word recovered")
        r.check("and it answers normally", "recover" in reply.lower(), reply[:60])

    # ── local tools work without the brain ──────────────────────────────────
    r.head("Tools do not need the model")
    local_brain.OLLAMA_HOST = "http://127.0.0.1:9"
    try:
        with Sandbox() as box:
            with guard(r, "filesystem tools still run"):
                out = agent_tools.execute("write_file",
                                          {"path": box.rel("x.txt"), "content": "ok"})
                r.check("writing works with no brain", "Error" not in out, out[:50])
                r.check("and reading it back works",
                        "ok" in agent_tools.execute("read_file", {"path": box.rel("x.txt")}))
    finally:
        local_brain.OLLAMA_HOST = was

    # ── MCP ─────────────────────────────────────────────────────────────────
    r.head("MCP servers")
    with guard(r, "the registry loads"):
        with Timer() as t:
            mcp_client.registry.load()
        status = mcp_client.registry.status()
        r.check("load() returns", isinstance(status, str), f"{t.elapsed:.1f}s")
        print(f"      {status}")

    with guard(r, "a dead server does not take the others down"):
        tools = mcp_client.registry.ready_tools()
        r.check("ready tools are listed even if some servers failed",
                isinstance(tools, list), f"{len(tools)} tools")

    with guard(r, "calling a tool on a dead server is an error string"):
        out = mcp_client.call("mcp__nosuchserver__nosuchtool", {})
        r.check("no exception escapes", isinstance(out, str), out[:60])

    with guard(r, "list_capabilities survives whatever MCP is doing"):
        out = agent_tools.execute("list_capabilities", {})
        r.check("it always returns text", isinstance(out, str) and len(out) > 5,
                out.splitlines()[0] if out else "")

    # ── voice ───────────────────────────────────────────────────────────────
    r.head("Voicebox goes away")
    was_url = voice.VOICEBOX_URL
    voice.VOICEBOX_URL = "http://127.0.0.1:9"
    try:
        with guard(r, "speak falls through to the system voice"):
            with Timer() as t:
                out = voice.speak("fallback check")
            r.check("speak returns", isinstance(out, str), f"{t.elapsed:.1f}s — {out}")
            r.check("and does not hang", t.elapsed < 20, f"{t.elapsed:.1f}s")
    finally:
        voice.VOICEBOX_URL = was_url

    # ── concurrency ─────────────────────────────────────────────────────────
    r.head("Two things at once")
    with Sandbox() as box:
        import threading
        errors = []

        def worker(n):
            try:
                agent_tools.execute("write_file",
                                    {"path": box.rel(f"c{n}.txt"), "content": str(n)})
                agent_tools.execute("read_file", {"path": box.rel(f"c{n}.txt")})
                agent_tools.execute("list_files", {"path": box.rel()})
            except BaseException as e:
                errors.append(f"{type(e).__name__}: {e}")

        with guard(r, "parallel tool calls"):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
            for t_ in threads:
                t_.start()
            for t_ in threads:
                t_.join(timeout=60)
            r.check("no thread raised", not errors, "; ".join(errors[:2]) or "12 threads clean")
            written = [f for f in os.listdir(box.path) if f.startswith("c")]
            r.check("every file was written", len(written) == 12, f"{len(written)}/12")

    # ── the running service ─────────────────────────────────────────────────
    r.head("The service itself")
    with guard(r, "the bridge compiles"):
        bridge = os.path.join(os.path.expanduser("~/.diablo"), "voice_bridge.py")
        proc = subprocess.run([sys.executable, "-m", "py_compile", bridge],
                              capture_output=True, text=True)
        r.check("voice_bridge.py compiles", proc.returncode == 0,
                (proc.stderr or "").strip()[-80:] or "clean")

    with guard(r, "no undefined names anywhere"):
        skills_dir = os.path.join(os.path.expanduser("~/.diablo"), "skills")
        files = [bridge] + [os.path.join(skills_dir, f)
                            for f in os.listdir(skills_dir) if f.endswith(".py")]
        proc = subprocess.run([sys.executable, "-m", "pyflakes"] + files,
                              capture_output=True, text=True)
        undefined = [ln for ln in (proc.stdout or "").splitlines() if "undefined name" in ln]
        r.check("pyflakes finds no undefined names", not undefined,
                "; ".join(undefined[:3]) or f"{len(files)} files clean")

    with guard(r, "every skill imports"):
        skills_dir = os.path.join(os.path.expanduser("~/.diablo"), "skills")
        failed = []
        for f in sorted(os.listdir(skills_dir)):
            if not f.endswith(".py") or f == "__init__.py":
                continue
            name = f[:-3]
            proc = subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, {os.path.expanduser('~/.diablo')!r}); "
                 f"from skills import {name}"],
                capture_output=True, text=True, timeout=180)
            if proc.returncode != 0:
                failed.append(f"{name}: {(proc.stderr or '').strip().splitlines()[-1][:60]}")
        r.check("all skills import cleanly", not failed, "; ".join(failed[:3]) or "all clean")

    return r.report("resilience")


if __name__ == "__main__":
    sys.exit(run())
