"""
Vision head-to-head on Diablo's own fixtures, identical screenshot per case.

Runs in Diablo's venv. For each perception case, one ScreenFrame of the fixture
window is captured and handed, unchanged, to:

    qwen25vl   Diablo's production free grounding (grounding._sized_for_model,
               _ask_region, _box_in_crop) on qwen2.5vl:7b through Ollama
    maiui      MAI-UI-2B (MLX, bf16) through serve.py
    fara       Fara1.5-4B (MLX, 8-bit) through serve.py

plus Diablo's production perception sources asked the same intent:

    vision     the set-of-marks vision source (qwen2.5vl picks a numbered element)
    fusion     perception.read(): accessibility, OCR, vision, arbitrated

Every point goes image -> (u, v) -> ScreenFrame.to_quartz -> the fixture's own
control rectangles, and is judged by evals/run_perception_eval.judge. The frame
is released after each case; nothing is kept.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

REPO = "/Users/shaurya/Desktop/30 Code/35 Diablo"
sys.path.insert(0, os.path.join(REPO, "evals"))
import run_perception_eval as E  # noqa: E402

PORTS = {"maiui": 17611, "fara": 17612}


class Point:
    def __init__(self, x, y, w=2.0, h=2.0, label="", source="", confidence=1.0):
        self.x, self.y, self.width, self.height = x - w / 2, y - h / 2, w, h
        self.centre = (x, y)
        self.label, self.sources, self.confidence = label, [source], confidence


def ask_mlx(family, path, intent):
    instruction = intent if family == "maiui" else f"Click on {intent}."
    body = json.dumps({"image": path, "instruction": instruction}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORTS[family]}/ground", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


FARA_OLLAMA = "hf.co/bartowski/Fara1.5-4B-GGUF:Q4_K_M"


def ask_fara_ollama(path, intent):
    """Fara through Ollama at Q4_K_M: the single-model configuration's runtime."""
    import base64, re
    system = open("/private/tmp/claude-501/-Users-shaurya-Documents-CoWork-OS/1d34736b-d0e2-4e88-9c21-bfbbeedb9b84/scratchpad/g4/ref/fara_system.txt").read()
    image = base64.b64encode(open(path, "rb").read()).decode()
    payload = {"model": FARA_OLLAMA, "stream": False, "think": False,
               "options": {"temperature": 0.0, "num_predict": 400, "num_ctx": 8192},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": f"Click on {intent}.", "images": [image]}]}
    t0 = time.perf_counter()
    req = urllib.request.Request("http://localhost:11434/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.loads(r.read())
    ms = (time.perf_counter() - t0) * 1000
    text = body.get("message", {}).get("content", "")
    m = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.S)
    try:
        args = json.loads(m.group(1)).get("arguments", {}) if m else {}
    except Exception:
        args = {}
    action = str(args.get("action", "unparsed"))
    coord = args.get("coordinate")
    if action in ("left_click", "double_click", "triple_click", "right_click", "mouse_move")             and isinstance(coord, list) and len(coord) == 2:
        return [coord[0] / 1000.0, coord[1] / 1000.0], ms, action
    return None, ms, action


MAIUI_OLLAMA = "hf.co/mradermacher/MAI-UI-2B-GGUF:Q8_0"


def ask_maiui_ollama(path, intent):
    """MAI-UI through Ollama at Q8_0, with its own prompt and message order."""
    import base64, re
    sys.path.insert(0, "/private/tmp/claude-501/-Users-shaurya-Documents-CoWork-OS/1d34736b-d0e2-4e88-9c21-bfbbeedb9b84/scratchpad/g4")
    system = open("/private/tmp/claude-501/-Users-shaurya-Documents-CoWork-OS/1d34736b-d0e2-4e88-9c21-bfbbeedb9b84/scratchpad/g4/serve.py").read().split('MAI_SYSTEM = """', 1)[1].split('""".strip()', 1)[0].strip()
    image = base64.b64encode(open(path, "rb").read()).decode()
    payload = {"model": MAIUI_OLLAMA, "stream": False,
               "options": {"temperature": 0.0, "num_predict": 300, "num_ctx": 8192},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": intent + "\n", "images": [image]}]}
    t0 = time.perf_counter()
    req = urllib.request.Request("http://localhost:11434/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.loads(r.read())
    ms = (time.perf_counter() - t0) * 1000
    text = body.get("message", {}).get("content", "")
    m = re.search(r"<answer>(.*?)</answer>", text, re.S)
    try:
        x, y = json.loads((m.group(1) if m else "").strip())["coordinate"][:2]
        return [x / 999.0, y / 999.0], ms, "click"
    except Exception:
        return None, ms, "unparsed"


def ask_qwen(path, intent, app):
    from skills import grounding
    sized, w, h, temporary = grounding._sized_for_model(path)
    question = (f"This is a screenshot of {app}.\n\nOutline the position of {intent}.\n\n"
                f"First, in `where`, say in plain words where it is, using what is "
                f"actually in this picture. Then output its coordinates in this image "
                f"as bbox_2d.\n\nIf it is not visible in this picture, set found to "
                f"false — that is a useful answer and a wrong box is not.")
    t0 = time.perf_counter()
    try:
        reply = grounding._ask_region(sized, question, (0, 0, 1, 1)) or {}
    finally:
        if temporary and sized != path:
            try:
                os.unlink(sized)
            except OSError:
                pass
    ms = (time.perf_counter() - t0) * 1000
    if not reply.get("found", True):
        return None, ms, "abstained", reply
    box = grounding._box_in_crop(reply.get("bbox_2d"), (w, h))
    if box is None:
        return None, ms, "unparsed", reply
    u1, v1, u2, v2 = box
    return ((u1 + u2) / 2, (v1 + v2) / 2, (u1, v1, u2, v2)), ms, "click", reply


def run(cases, grounders, sources, repeat):
    from skills import screen
    records = []
    groups = {}
    for case in cases:
        groups.setdefault((case["fixture"], case["variant"]), []).append(case)
    for (name, variant), group in groups.items():
        print(f"\n  {name} ({variant})", flush=True)
        fixture = E.Fixture(name, variant)
        with fixture:
            if not fixture.started:
                print("    fixture did not come up", flush=True)
                continue
            for case in group:
                for attempt in range(1, repeat + 1):
                    marks = []
                    frame = screen.capture_this_window(fixture.window)
                    try:
                        if not frame.ok:
                            print(f"    {case['id']}: capture failed {frame.problem}")
                            continue
                        for g in grounders:
                            note, extra = "", {}
                            try:
                                if g == "maiui_ollama":
                                    point, ms, action = ask_maiui_ollama(frame.path, case["intent"])
                                    cand = [Point(*frame.to_quartz(*point), source=g)] if point else []
                                elif g == "fara_ollama":
                                    point, ms, action = ask_fara_ollama(frame.path, case["intent"])
                                    cand = [Point(*frame.to_quartz(*point), source=g)] if point else []
                                elif g == "sleep3":
                                    t0 = time.perf_counter(); time.sleep(3.0)
                                    cand, ms, action = [], (time.perf_counter() - t0) * 1000, "sleep"
                                elif g == "qwen25vl":
                                    got, ms, action, raw = ask_qwen(frame.path, case["intent"],
                                                                    fixture.window.get("app", "an application"))
                                    if got:
                                        u, v, (u1, v1, u2, v2) = got
                                        qx1, qy1 = frame.to_quartz(u1, v1)
                                        qx2, qy2 = frame.to_quartz(u2, v2)
                                        cx, cy = frame.to_quartz(u, v)
                                        cand = [Point(cx, cy, qx2 - qx1, qy2 - qy1, source=g,
                                                      confidence=float(raw.get("confidence") or 0))]
                                    else:
                                        cand = []
                                    extra = {"conf": raw.get("confidence")}
                                else:
                                    reply = ask_mlx(g, frame.path, case["intent"])
                                    ms, action = reply["ms"], reply["action"]
                                    extra = {"peak_gb": reply["peak_gb"], "load_s": reply["load_s"],
                                             "prompt_tokens": reply.get("prompt_tokens"),
                                             "output_tokens": reply.get("output_tokens")}
                                    if reply["point"]:
                                        cx, cy = frame.to_quartz(*reply["point"])
                                        cand = [Point(cx, cy, source=g)]
                                    else:
                                        cand = []
                            except Exception as e:
                                cand, ms, action, note = [], 0.0, "error", f"{type(e).__name__}: {e}"
                            verdict, blame, detail = E.judge(case, cand, False, fixture, g)
                            records.append({"case": case["id"], "category": case["category"],
                                            "fixture": name, "variant": variant,
                                            "outcome_wanted": case["outcome"], "source": g,
                                            "attempt": attempt, "verdict": verdict,
                                            "good": verdict in E.GOOD, "ms": round(ms, 1),
                                            "action": action, "error": note, **detail, **extra})
                            marks.append(f"{g}:{'✓' if verdict in E.GOOD else '✗'}{verdict[:4]}")
                    finally:
                        frame.release()
                    for s in sources:
                        cand, ms, amb, err = E.ask(s, case["intent"], fixture.window)
                        verdict, blame, detail = E.judge(case, cand, amb, fixture, s)
                        records.append({"case": case["id"], "category": case["category"],
                                        "fixture": name, "variant": variant,
                                        "outcome_wanted": case["outcome"], "source": s,
                                        "attempt": attempt, "verdict": verdict,
                                        "good": verdict in E.GOOD, "ms": round(ms, 1),
                                        "action": "", "error": err, **detail})
                        marks.append(f"{s}:{'✓' if verdict in E.GOOD else '✗'}{verdict[:4]}")
                    print(f"    {case['id']:16} " + " ".join(marks), flush=True)
    return records


def summary(records):
    out = {}
    for s in sorted({r["source"] for r in records}):
        rs = [r for r in records if r["source"] == s]
        ms = sorted(r["ms"] for r in rs)
        errs = sorted(r["centre_error_px"] for r in rs if r.get("centre_error_px") is not None
                      and r["verdict"] == "correct")
        out[s] = {"n": len(rs), "good": sum(r["good"] for r in rs),
                  "verdicts": {v: sum(r["verdict"] == v for r in rs) for v in sorted({r["verdict"] for r in rs})},
                  "wrong_or_off": sum(r["verdict"] in ("wrong_target", "off_target", "should_have_abstained") for r in rs),
                  "p50_ms": ms[len(ms) // 2] if ms else None,
                  "p95_ms": ms[max(0, -(-len(ms) * 95 // 100) - 1)] if ms else None,
                  "median_centre_error_px_when_correct": errs[len(errs) // 2] if errs else None,
                  "peak_gb": max([r.get("peak_gb") or 0 for r in rs] or [0]) or None}
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--grounders", default="qwen25vl,maiui,fara")
    p.add_argument("--sources", default="vision,fusion")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--only", default="")
    p.add_argument("--json", default="")
    a = p.parse_args()
    cases = json.load(open(os.path.join(REPO, "evals", "perception_cases.json")))["cases"]
    if a.only:
        cases = [c for c in cases if c["id"] in a.only.split(",")]
    grounders = [g for g in a.grounders.split(",") if g]
    sources = [s for s in a.sources.split(",") if s]
    records = run(cases, grounders, sources, a.repeat)
    s = summary(records)
    print(json.dumps(s, indent=1))
    if a.json:
        json.dump({"summary": s, "records": records}, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
