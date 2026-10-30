"""
# Evaluation tooling only; not part of Diablo. MAI-UI grounding prompt quoted from
# github.com/Tongyi-MAI/MAI-UI (Apache-2.0); Fara prompt rendered by microsoft/fara (MIT).
One challenger model, resident, behind a tiny local HTTP endpoint.

    vlm/bin/python serve.py maiui  PORT
    vlm/bin/python serve.py fara   PORT

POST /ground {"image": path, "instruction": str}
  -> {"point": [u, v] | null, "action": str, "raw": str, "ms": float,
      "peak_gb": float, "load_s": float}

(u, v) is normalised to the image (0..1). The image is only read, never kept;
the caller owns it and deletes it. Prompts are each model's own, from its
official repository: MAI-UI's grounding prompt (coordinates on 0..999) and
Fara-1.5's browser-agent prompt rendered by its own builder (0..1000).
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import mlx.core as mx
from mlx_vlm import generate, load

HF = "/private/tmp/claude-501/-Users-shaurya-Documents-CoWork-OS/1d34736b-d0e2-4e88-9c21-bfbbeedb9b84/scratchpad/g4"
FAMILY = sys.argv[1]
PORT = int(sys.argv[2])
REPO = {"maiui": "mlx-community/MAI-UI-2B-bf16-v2", "fara": "mlx-community/Fara1.5-4B-8bit"}[FAMILY]

MAI_SYSTEM = """You are a GUI grounding agent.
## Task
Given a screenshot and the user's grounding instruction. Your task is to accurately locate a UI element based on the user's instructions.
First, you should carefully examine the screenshot and analyze the user's instructions,  translate the user's instruction into a effective reasoning process, and then provide the final coordinate.
## Output Format
Return a json object with a reasoning process in <grounding_think></grounding_think> tags, a [x,y] format coordinate within <answer></answer> XML tags:
<grounding_think>...</grounding_think>
<answer>
{"coordinate": [x,y]}
</answer>""".strip()

FARA_SYSTEM = open(f"{HF}/ref/fara_system.txt").read()

began = time.time()
model, processor = load(REPO)
LOAD_S = round(time.time() - began, 2)


def prompt_for(instruction: str) -> str:
    # Each model's own order: MAI-UI's grounding agent sends the instruction
    # (with a newline) and then the image; Fara's harness sends the image and
    # then the task.
    if FAMILY == "maiui":
        messages = [{"role": "system", "content": MAI_SYSTEM},
                    {"role": "user", "content": [{"type": "text", "text": instruction + "\n"},
                                                 {"type": "image"}]}]
    else:
        messages = [{"role": "system", "content": FARA_SYSTEM},
                    {"role": "user", "content": [{"type": "image"},
                                                 {"type": "text", "text": instruction}]}]
    return processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)


def parse(text: str):
    """(point or None, action)."""
    if FAMILY == "maiui":
        m = re.search(r"<answer>(.*?)</answer>", text, re.S)
        if m:
            body = m.group(1)
        else:
            m = re.search(r"\{\s*\"coordinate\"\s*:\s*\[[^\]]*\]\s*\}", text)
            body = m.group(0) if m else ""
        try:
            x, y = json.loads(body.strip())["coordinate"][:2]
            return [x / 999.0, y / 999.0], "click"
        except Exception:
            return None, "unparsed"
    m = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.S)
    if not m:
        return None, "unparsed"
    try:
        call = json.loads(m.group(1))
    except Exception:
        return None, "unparsed"
    args = call.get("arguments", {}) or {}
    action = str(args.get("action", ""))
    coord = args.get("coordinate")
    if action in ("left_click", "double_click", "triple_click", "right_click",
                  "mouse_move") and isinstance(coord, list) and len(coord) == 2:
        return [coord[0] / 1000.0, coord[1] / 1000.0], action
    return None, action


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        t0 = time.perf_counter()
        result = generate(model, processor, prompt_for(body["instruction"]),
                          image=[body["image"]], max_tokens=int(body.get("max_tokens", 400)),
                          temperature=0.0, verbose=False)
        text = getattr(result, "text", str(result))
        point, action = parse(text)
        reply = {"point": point, "action": action, "raw": text[-600:],
                 "ms": round((time.perf_counter() - t0) * 1000, 1),
                 "peak_gb": round(mx.get_peak_memory() / 1e9, 2), "load_s": LOAD_S,
                 "prompt_tokens": getattr(result, "prompt_tokens", None),
                 "output_tokens": getattr(result, "generation_tokens", None)}
        out = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


print(f"ready {FAMILY} {REPO} load {LOAD_S}s peak {mx.get_peak_memory()/1e9:.2f} GB", flush=True)
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
