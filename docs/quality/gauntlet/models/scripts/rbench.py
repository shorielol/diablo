"""
Region and figure grounding on Diablo's own figure fixtures.

    prod      grounding.ground("where is the <region>?") — the production path,
              using whatever DIABLO_VISION_MODEL this process was started with
    fara      Fara through Ollama with its own prompt ("Click on the <region>.")
    maiui     MAI-UI through Ollama with its own prompt

A hit is the answer's centre inside the fixture's own region rectangle. Every
frame is released after use.
"""

import json
import os
import sys
import time

REPO = "/Users/shaurya/Desktop/30 Code/35 Diablo"
sys.path.insert(0, os.path.join(REPO, "evals"))
sys.path.insert(0, "/private/tmp/claude-501/-Users-shaurya-Documents-CoWork-OS/1d34736b-d0e2-4e88-9c21-bfbbeedb9b84/scratchpad/g4")
import run_perception_eval as E  # noqa: E402
import vbench  # noqa: E402

FIXTURES = ("diagram_app", "market_app", "chart_app")


def inside(point, box):
    x, y = point
    return box["x"] <= x <= box["x"] + box["width"] and box["y"] <= y <= box["y"] + box["height"]


def main():
    modes = sys.argv[1].split(",")
    out_path = sys.argv[2]
    from skills import grounding, screen
    records = []
    for name in FIXTURES:
        fixture = E.Fixture(name, "light")
        with fixture:
            if not fixture.started:
                print(f"{name}: did not come up", flush=True)
                continue
            for region in fixture.layout.get("regions", []):
                label = region.get("label") or region.get("name")
                for mode in modes:
                    t0 = time.perf_counter()
                    centre, box, note = None, None, ""
                    try:
                        if mode == "prod":
                            g = grounding.ground(f"where is the {label}?", fixture.window)
                            if g.candidate is not None:
                                c = g.candidate
                                centre = c.centre
                                box = {"x": c.x, "y": c.y, "width": c.width, "height": c.height}
                            else:
                                note = (g.note or "abstained")[:80]
                        else:
                            frame = screen.capture_this_window(fixture.window)
                            try:
                                ask = vbench.ask_fara_ollama if mode == "fara" else vbench.ask_maiui_ollama
                                point, _, action = ask(frame.path, f"the {label}")
                                if point:
                                    centre = frame.to_quartz(*point)
                                else:
                                    note = action
                            finally:
                                frame.release()
                    except Exception as e:
                        note = f"{type(e).__name__}: {e}"[:120]
                    ms = (time.perf_counter() - t0) * 1000
                    hit = bool(centre) and inside(centre, region)
                    iou = round(E.iou(box, region), 3) if box else None
                    records.append({"fixture": name, "region": label, "mode": mode, "hit": hit,
                                    "iou": iou, "ms": round(ms, 1), "note": note,
                                    "centre": [round(v, 1) for v in centre] if centre else None})
                    print(f"{name:12} {label:18} {mode:6} {'HIT ' if hit else 'miss'} "
                          f"iou={iou} {ms/1000:5.1f}s {note}", flush=True)
    summary = {}
    for mode in modes:
        rs = [r for r in records if r["mode"] == mode]
        ms = sorted(r["ms"] for r in rs)
        summary[mode] = {"n": len(rs), "hits": sum(r["hit"] for r in rs),
                         "p50_ms": ms[len(ms) // 2] if ms else None,
                         "answered": sum(r["centre"] is not None for r in rs)}
    print(json.dumps(summary))
    json.dump({"vision_model": os.getenv("DIABLO_VISION_MODEL", ""), "summary": summary,
               "records": records}, open(out_path, "w"), indent=1)


if __name__ == "__main__":
    main()
