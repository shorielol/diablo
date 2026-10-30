"""
Conversation at 70-110% of the context window, through session.reply.

A fact is planted in the first exchange, filler exchanges (the repo's own docs)
fill the window, and then the fact is asked for. Measured: prompt tokens the
model actually received, latency, and whether the answer has the fact.
"""
import json, os, sys, time
home = os.environ["DIABLO_HOME"]
sys.path.insert(0, home)
from companion_manager import load_env
load_env()
from skills import conversation, local_brain

FILLER = open(sys.argv[1], encoding="utf-8").read()
OUT = sys.argv[2]
CTX = local_brain.NUM_CTX
seen = {}
real = local_brain._post_json


def spy(path, payload, timeout):
    body = real(path, payload, timeout)
    if path == "/api/chat":
        seen["prompt"] = body.get("prompt_eval_count")
        seen["done_reason"] = body.get("done_reason")
    return body


local_brain._post_json = spy
local_brain.warm()
results = []
for fill in (0.70, 0.85, 0.95, 1.10):
    s = conversation.Session()
    s.remember("user", "Remember this for later: the shed padlock code is 4417.")
    s.remember("assistant", "Noted. The shed padlock code is 4417.")
    budget = int(CTX * fill * 3.6)           # characters, about 3.6 per token
    used, i = 0, 0
    chunk = 2400
    while used < budget - 600 and len(s.turns) < 2 * conversation.MAX_TURNS - 4:
        part = FILLER[(i * chunk) % max(1, len(FILLER) - chunk):][:chunk]
        s.remember("user", f"Summarise section {i + 1} of the architecture notes for me.")
        s.remember("assistant", part)
        used += len(part) + 60
        i += 1
    seen.clear()
    began = time.time()
    try:
        answer = s.reply("What was the shed padlock code I told you at the start?")
        error = None
    except Exception as e:
        answer, error = "", type(e).__name__
    results.append({
        "target_fill": fill, "turns": len(s.turns), "summarised": bool(s.earlier),
        "prompt_tokens": seen.get("prompt"), "num_ctx": CTX,
        "actual_fill": round((seen.get("prompt") or 0) / CTX, 3),
        "seconds": round(time.time() - began, 2), "has_fact": "4417" in (answer or ""),
        "answer_chars": len(answer or ""), "error": error,
        "done_reason": seen.get("done_reason")})
    print(results[-1], flush=True)
json.dump(results, open(OUT, "w"), indent=1)
