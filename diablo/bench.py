"""
D.I.A.B.L.O. — measuring the things that decide which model to use.

Written before anything was swapped, on purpose. Every previous decision about
models in this project was made from a number, and two of those numbers turned
out to be measuring the wrong thing: a 6.54s "warm" turn that included a cold
load, and a 9.97s 4B result that was really a thinking model burning tokens in
a <think> block. Both sent the project the wrong way for a while.

So this measures what actually matters for a voice assistant, separately:

    time to first token     when Diablo could START speaking, if it streamed
    total generation        when the whole answer exists
    tokens per second       how fast the words arrive after the first
    tool call correctness   whether the model reaches for the right tool at all
    resident memory         what the model costs while it sits there

Time to first token is the one that gets ignored and matters most. A model that
finishes in 3.0s but emits its first token at 0.4s feels faster than one that
finishes in 2.0s with nothing until 1.9s, because speech can start early.

    python3 bench.py                      the default comparison
    python3 bench.py --models a,b         specific models
    python3 bench.py --stt base.en,small  transcription only
    python3 bench.py --json out.json      machine-readable
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import local_brain  # noqa: E402

OLLAMA = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Real requests, not synthetic prompts. Two need a tool, two are conversation,
# because a model can be good at one and useless at the other.
PROMPTS = [
    ("chat", "What's a research question, in one sentence?"),
    ("chat", "My extended essay deadline moved to next Friday."),
    ("tool", "Read the file notes.txt on my Desktop and tell me the first line."),
    ("tool", "Make a folder called reports on my Desktop."),
]

TOOL_SCHEMA = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file and return its contents.",
        "parameters": {"type": "object", "required": ["path"],
                       "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "make_folder", "description": "Create a folder.",
        "parameters": {"type": "object", "required": ["path"],
                       "properties": {"path": {"type": "string"}}}}},
]

EXPECTED = {
    "Read the file notes.txt on my Desktop and tell me the first line.": "read_file",
    "Make a folder called reports on my Desktop.": "make_folder",
}


def resident_models() -> Dict[str, float]:
    """What Ollama is holding in memory right now, in megabytes."""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=5) as response:
            body = json.loads(response.read())
        return {m["name"]: m.get("size", 0) / 1e6 for m in body.get("models", [])}
    except Exception:
        return {}


def unload(model: str) -> None:
    """Drop a model so the next measurement starts cold, or leaves room."""
    try:
        request = urllib.request.Request(
            f"{OLLAMA}/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(request, timeout=30).read()
    except Exception:
        pass


def stream_turn(model: str, prompt: str, tools: Optional[list] = None,
                num_ctx: int = 4096, num_predict: int = 256,
                timeout: int = 180) -> Dict[str, Any]:
    """
    One streamed turn, timed token by token.

    Streaming is used even though Diablo does not stream yet, because it is the
    only way to see time-to-first-token, and that number is what decides whether
    streaming is worth building.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": num_predict, "num_ctx": num_ctx},
        "keep_alive": "5m",
    }
    if tools:
        payload["tools"] = tools

    request = urllib.request.Request(
        f"{OLLAMA}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")

    started = time.time()
    first_token_at = None
    tokens = 0
    text_parts: List[str] = []
    tool_calls: List[str] = []

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                if not raw.strip():
                    continue
                chunk = json.loads(raw)
                message = chunk.get("message") or {}
                piece = message.get("content") or ""
                for call in message.get("tool_calls") or []:
                    name = (call.get("function") or {}).get("name")
                    if name:
                        tool_calls.append(name)
                if piece or message.get("tool_calls"):
                    if first_token_at is None:
                        first_token_at = time.time() - started
                    tokens += 1
                    text_parts.append(piece)
                if chunk.get("done"):
                    break
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "total": time.time() - started}

    total = time.time() - started
    generation_window = max(1e-6, total - (first_token_at or total))
    return {
        "ttft": first_token_at,
        "total": total,
        "tokens": tokens,
        "tps": (tokens - 1) / generation_window if tokens > 1 else 0.0,
        "text": "".join(text_parts).strip(),
        "tool_calls": tool_calls,
    }


def bench_model(model: str, runs: int = 1, num_ctx: int = 4096) -> Dict[str, Any]:
    print(f"\n  {model}")
    unload(model)

    cold = stream_turn(model, "Say the single word: ready.", num_ctx=num_ctx, num_predict=16)
    if "error" in cold:
        print(f"    unavailable — {cold['error']}")
        return {"model": model, "error": cold["error"]}
    print(f"    cold start        {cold['total']:6.2f}s")

    resident = resident_models().get(model, 0.0)
    print(f"    resident memory   {resident / 1000:6.2f} GB")

    results = {"chat": [], "tool": []}
    tool_hits = tool_total = 0

    for kind, prompt in PROMPTS:
        samples = []
        for _ in range(runs):
            outcome = stream_turn(
                model, prompt, tools=TOOL_SCHEMA if kind == "tool" else None,
                num_ctx=num_ctx)
            if "error" in outcome:
                continue
            samples.append(outcome)
            if kind == "tool":
                tool_total += 1
                if EXPECTED.get(prompt) in outcome["tool_calls"]:
                    tool_hits += 1
        if not samples:
            continue
        best = min(samples, key=lambda s: s["total"])
        results[kind].append(best)
        marker = ""
        if kind == "tool":
            got = best["tool_calls"][0] if best["tool_calls"] else "NO TOOL CALL"
            want = EXPECTED.get(prompt)
            marker = f"   -> {got}" + ("" if got == want else f"  (wanted {want})")
        print(f"    {prompt[:44]:46} ttft {best['ttft'] or 0:5.2f}s  "
              f"total {best['total']:5.2f}s  {best['tps']:5.1f} tok/s{marker}")

    def mean(key, kind):
        values = [s[key] for s in results[kind] if s.get(key)]
        return statistics.mean(values) if values else 0.0

    return {
        "model": model,
        "cold_start": cold["total"],
        "resident_gb": resident / 1000,
        "chat_ttft": mean("ttft", "chat"),
        "chat_total": mean("total", "chat"),
        "tool_ttft": mean("ttft", "tool"),
        "tool_total": mean("total", "tool"),
        "tps": mean("tps", "chat") or mean("tps", "tool"),
        "tool_accuracy": (tool_hits / tool_total) if tool_total else 0.0,
    }


def bench_stt(names: List[str]) -> List[Dict[str, Any]]:
    """Transcription cost per model, on identical audio."""
    try:
        import numpy as np
        import whisper
    except ImportError as e:
        print(f"  whisper unavailable: {e}")
        return []

    rng = np.random.default_rng(0)
    t = np.arange(16000 * 3) / 16000.0
    audio = (0.3 * np.sin(2 * np.pi * 150 * t)
             + 0.15 * np.sin(2 * np.pi * 430 * t)
             + 0.03 * rng.standard_normal(t.size)).astype(np.float32)

    rows = []
    for name in names:
        try:
            load_started = time.time()
            model = whisper.load_model(name)
            load = time.time() - load_started
            model.transcribe(audio, fp16=False, beam_size=1)     # warm the graph
            started = time.time()
            for _ in range(3):
                model.transcribe(audio, fp16=False, beam_size=1)
            per_call = (time.time() - started) / 3
            parameters = sum(p.numel() for p in model.parameters()) / 1e6
            print(f"    {name:10} load {load:5.2f}s   {per_call * 1000:6.0f} ms "
                  f"per 3s window   {parameters:5.0f}M params")
            rows.append({"model": name, "load": load, "per_call": per_call,
                         "params_m": parameters})
            del model
        except Exception as e:
            print(f"    {name:10} unavailable — {type(e).__name__}: {e}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default=os.getenv(
        "DIABLO_BENCH_MODELS",
        "qwen3:14b,qwen3:4b-instruct-2507-q4_K_M"))
    parser.add_argument("--stt", default="")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--num-ctx", type=int, default=int(os.getenv("DIABLO_NUM_CTX", "4096")))
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    print("\n  Diablo benchmark")
    print(f"  ollama {OLLAMA}   context {args.num_ctx}")

    report: Dict[str, Any] = {"num_ctx": args.num_ctx, "models": [], "stt": []}

    if args.stt:
        print("\n  Transcription")
        report["stt"] = bench_stt([s.strip() for s in args.stt.split(",") if s.strip()])

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if models:
        print("\n  Generation")
        for model in models:
            report["models"].append(bench_model(model, args.runs, args.num_ctx))

    rows = [r for r in report["models"] if "error" not in r]
    if len(rows) >= 1:
        print(f"\n  {'model':38} {'cold':>6} {'ttft':>6} {'total':>6} "
              f"{'tok/s':>6} {'tools':>6} {'GB':>6}")
        print("  " + "-" * 78)
        for r in rows:
            print(f"  {r['model'][:38]:38} {r['cold_start']:6.2f} "
                  f"{r['tool_ttft']:6.2f} {r['tool_total']:6.2f} {r['tps']:6.1f} "
                  f"{r['tool_accuracy'] * 100:5.0f}% {r['resident_gb']:6.2f}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  written to {args.json}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
