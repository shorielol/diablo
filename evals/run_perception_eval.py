#!/usr/bin/env python3
"""
Diablo's perception benchmark — DIABLO PERCEPTION BASELINE V1.

The planner benchmark answers "does it choose a sensible next step". It does
that by replacing the screen with a fixture string (`run_planner_eval.py`
patches `_screen_summary`), so its 74.6% advancement figure is measured against
perception that is never wrong. This answers the question that number quietly
assumes:

    how often does perception return the right control?

Four sources, measured separately and never blended into one score:

    ax        Accessibility, through ctypes
    ocr       Apple Vision text recognition, via pointing
    vision    a local vision model naming unlabelled controls
    fusion    what locate()/read() actually hand to a caller

Separately, because `locate()` runs them conditionally — accessibility first,
OCR only when that is not decisive, vision only when the intent is visual and
the cheap sources failed. Calling `locate()` once would measure the cascade and
tell you nothing about the parts. So the runner calls the private source
functions directly and calls `read()` as a fifth thing, and the difference
between them is the most interesting column in the report.

Ground truth never comes from perception. Each fixture prints its own layout on
startup — the rectangles the application told AppKit to use — and the corpus
stores control ids, not coordinates. Perception has to arrive at those
rectangles on its own.

    python3 evals/run_perception_eval.py
    python3 evals/run_perception_eval.py --source ax
    python3 evals/run_perception_eval.py --category icon_unlabelled
    python3 evals/run_perception_eval.py --scenario btn-save --repeat 5
    python3 evals/run_perception_eval.py --json baseline.json
"""

import argparse
import collections
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

CASES = os.path.join(HERE, "perception_cases.json")
FIXTURES = os.path.join(REPO, "tests", "fixtures")

# ─── Verdicts ────────────────────────────────────────────────────────────────
CORRECT = "correct"                   # top-1 is the expected control
IN_TOP3 = "in_top3"                   # expected is ranked 2nd or 3rd
WRONG_TARGET = "wrong_target"         # top-1 is a different real control
OFF_TARGET = "off_target"             # top-1 is not any known control
NOT_FOUND = "not_found"               # nothing returned, but the target exists
ABSTAINED = "abstained"               # nothing returned, and that was right
SHOULD_HAVE_ABSTAINED = "should_have_abstained"
AMBIGUOUS_RIGHT = "ambiguous_right"   # flagged ambiguous, and it was
AMBIGUITY_MISSED = "ambiguity_missed" # decisive when it should have asked
FALSE_AMBIGUITY = "false_ambiguity"   # asked when the answer was clear

GOOD = {CORRECT, ABSTAINED, AMBIGUOUS_RIGHT}

# ─── Failure attribution (§30) ───────────────────────────────────────────────
ACCESSIBILITY = "accessibility"
OCR = "ocr"
VISION = "vision"
FUSION = "fusion"
SCREEN_CAPTURE = "screen_capture"
COORDINATE_MAPPING = "coordinate_mapping"
CONTEXT = "context"
CACHE = "cache"
FIXTURE = "fixture"
UNKNOWN = "unknown"

# ─── Matching (§9) ───────────────────────────────────────────────────────────
#
# A click uses the centre point, so the question a click cares about is whether
# the centre lands inside the control. That is the primary measure and it is
# binary. Two pixels of slack because the Accessibility API reports a button's
# bezel while the fixture reports its bounds, which differs by about one pixel
# a side — measured on the controls fixture, 302x26 against 300x24.
#
# IoU and centre distance are reported as well, but as bounding accuracy, not
# as the pass condition. A source can put the centre in the right place and
# still describe the box badly: `_from_ocr` returns a point and pads it into a
# fixed 60x20 rectangle, so its IoU is meaningless while its centre may be
# exactly right. Scoring those together would hide that.
TOLERANCE = 2.0


def centre_of(box: Dict[str, float]) -> Tuple[float, float]:
    return (box["x"] + box["width"] / 2.0, box["y"] + box["height"] / 2.0)


def inside(point: Tuple[float, float], box: Dict[str, float],
           slack: float = TOLERANCE) -> bool:
    x, y = point
    return (box["x"] - slack <= x <= box["x"] + box["width"] + slack
            and box["y"] - slack <= y <= box["y"] + box["height"] + slack)


def iou(a: Dict[str, float], b: Dict[str, float]) -> float:
    ax2, ay2 = a["x"] + a["width"], a["y"] + a["height"]
    bx2, by2 = b["x"] + b["width"], b["y"] + b["height"]
    ix = max(0.0, min(ax2, bx2) - max(a["x"], b["x"]))
    iy = max(0.0, min(ay2, by2) - max(a["y"], b["y"]))
    overlap = ix * iy
    union = a["width"] * a["height"] + b["width"] * b["height"] - overlap
    return overlap / union if union > 0 else 0.0


def distance(point: Tuple[float, float], box: Dict[str, float]) -> float:
    cx, cy = centre_of(box)
    return ((point[0] - cx) ** 2 + (point[1] - cy) ** 2) ** 0.5


def which_control(point: Tuple[float, float],
                  controls: List[Dict[str, Any]]) -> Optional[str]:
    """Which ground-truth control this coordinate actually lands on, if any."""
    for control in controls:
        if inside(point, control):
            return control["id"]
    return None


# ─── The fixture under test ──────────────────────────────────────────────────

class Fixture:
    """A running fixture application, and what it says about its own layout."""

    def __init__(self, name: str, variant: str = "light"):
        self.name = name
        self.variant = variant
        self.path = os.path.join(FIXTURES, f"{name}.py")
        self.child: Optional[subprocess.Popen] = None
        self.layout: Dict[str, Any] = {}
        self.window: Optional[Dict[str, Any]] = None

    @property
    def controls(self) -> List[Dict[str, Any]]:
        return self.layout.get("controls", [])

    def control(self, ident: str) -> Optional[Dict[str, Any]]:
        for one in self.controls:
            if one["id"] == ident:
                return one
        return None

    def start(self, settle: float = 1.3) -> bool:
        args = [sys.executable, self.path]
        if self.variant == "dark":
            args.append("--dark")
        self.child = subprocess.Popen(args, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, bufsize=1)
        deadline = time.time() + 25
        while time.time() < deadline:
            line = self.child.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("LAYOUT "):
                self.layout = json.loads(line[7:])
            if line == "READY":
                break
        if not self.layout:
            return False
        # The window has to be up and composited before anything reads it.
        time.sleep(settle)
        from skills import screen_context
        self.window = screen_context.find_window(self.layout["title"])
        return self.window is not None

    def stop(self) -> None:
        if self.child is None:
            return
        self.child.terminate()
        try:
            self.child.wait(timeout=5)
        except Exception:
            self.child.kill()

    def __enter__(self):
        self.started = self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


# ─── The sources, each asked the same question ───────────────────────────────

def ask(source: str, intent: str, window: Dict[str, Any]):
    """
    One source, one intent. Returns (candidates, milliseconds, ambiguous).

    `ambiguous` is only meaningful for fusion — the raw sources return a list
    and express no opinion about whether it is decisive.
    """
    from skills import perception
    started = time.perf_counter()
    ambiguous = False
    try:
        if source == "ax":
            found = perception._from_accessibility(intent, window)
            found = perception._fuse(found)
        elif source == "ocr":
            found = perception._fuse(perception._from_ocr(intent, window))
        elif source == "vision":
            found = perception._fuse(perception._from_vision(intent, window))
        elif source == "fusion":
            reading = perception.read(intent, window)
            found, ambiguous = reading.candidates, reading.ambiguous
        else:
            raise ValueError(f"unknown source {source!r}")
    except Exception as e:
        return [], (time.perf_counter() - started) * 1000.0, False, f"{type(e).__name__}: {e}"
    return found, (time.perf_counter() - started) * 1000.0, ambiguous, ""


# ─── Judging ─────────────────────────────────────────────────────────────────

def judge(case: Dict[str, Any], candidates, ambiguous: bool,
          fixture: Fixture, source: str = "fusion") -> Tuple[str, str, Dict[str, Any]]:
    """
    (verdict, attribution, measurements). Deterministic, and never asks a model.

    `source` matters for one thing: only fusion can express ambiguity. A raw
    source returns a ranked list and has no opinion about whether the top two
    can be told apart, so judging `_from_accessibility` for "failing to flag" a
    tie punishes it for something it has no way to say. On an ambiguous case a
    raw source is judged on whether its top answer is one of the acceptable
    controls, which is the most it can be asked.
    """
    controls = fixture.controls
    want = case["outcome"]
    expected_id = case.get("expect")
    accept = set([expected_id] + list(case.get("alternatives") or [])) - {None}
    detail: Dict[str, Any] = {"candidates": len(candidates)}

    if not candidates:
        if want == "abstain":
            return ABSTAINED, "", detail
        return NOT_FOUND, _blame_absent(case), detail

    # Which real control does each candidate actually point at?
    landed = [which_control(c.centre, controls) for c in candidates]
    detail["landed"] = landed[:3]
    detail["top_label"] = candidates[0].label[:40]
    detail["top_sources"] = sorted(set(candidates[0].sources))
    detail["top_confidence"] = round(candidates[0].confidence, 3)

    truth = fixture.control(expected_id) if expected_id else None
    if truth:
        point = candidates[0].centre
        detail["centre_error_px"] = round(distance(point, truth), 1)
        detail["iou"] = round(iou({"x": candidates[0].x, "y": candidates[0].y,
                                   "width": candidates[0].width,
                                   "height": candidates[0].height}, truth), 3)

    if want == "abstain":
        return SHOULD_HAVE_ABSTAINED, _blame(candidates[0]), detail

    if want == "ambiguous":
        if source != "fusion":
            # It cannot flag a tie. Did it at least land on one of the pair?
            if landed and landed[0] in accept:
                return CORRECT, "", detail
            if landed and landed[0] is not None:
                return WRONG_TARGET, _blame(candidates[0]), detail
            return OFF_TARGET, _blame(candidates[0]), detail
        # Correct means: it said so, and the things it could not separate are
        # the things that genuinely cannot be separated.
        top_two = set(landed[:2]) - {None}
        if ambiguous and top_two and top_two <= accept:
            return AMBIGUOUS_RIGHT, "", detail
        if ambiguous:
            return AMBIGUOUS_RIGHT if top_two & accept else FALSE_AMBIGUITY, \
                   ("" if top_two & accept else _blame(candidates[0])), detail
        if landed and landed[0] in accept:
            # It picked one of the right pair without flagging the tie. Not a
            # wrong click, but it did not ask when it should have.
            return AMBIGUITY_MISSED, FUSION, detail
        return WRONG_TARGET, _blame(candidates[0]), detail

    # want == "found"
    if ambiguous and landed and landed[0] == expected_id:
        return FALSE_AMBIGUITY, FUSION, detail
    if landed and landed[0] == expected_id:
        return CORRECT, "", detail
    if expected_id in landed[:3]:
        return IN_TOP3, _blame(candidates[0]), detail
    if landed and landed[0] is not None:
        return WRONG_TARGET, _blame(candidates[0]), detail
    return OFF_TARGET, _blame(candidates[0]), detail


def _blame(candidate) -> str:
    """Which source produced the candidate that was wrong."""
    sources = set(candidate.sources)
    if len(sources) > 1:
        return FUSION
    if "ax" in sources or "accessibility" in sources:
        return ACCESSIBILITY
    if "ocr" in sources:
        return OCR
    if "vision" in sources:
        return VISION
    return UNKNOWN


def _blame_absent(case: Dict[str, Any]) -> str:
    """Nothing came back at all. Which source should have had it?"""
    category = case.get("category", "")
    if category in ("icon_unlabelled", "duplicate"):
        return VISION
    if category == "text_field":
        return ACCESSIBILITY
    return ACCESSIBILITY


# ─── Running ─────────────────────────────────────────────────────────────────

def run_case(case: Dict[str, Any], fixture: Fixture, sources: List[str],
             run_id: str, repeat: int = 1) -> List[Dict[str, Any]]:
    from skills import perception, trace
    records = []
    for attempt in range(1, repeat + 1):
        for source in sources:
            # Vision caches by (intent, crop, frame fingerprint). Clearing it
            # on the first attempt and leaving it alone afterwards is how the
            # cache columns get real hit/miss numbers instead of all misses.
            before_cache = len(perception._vision_cache)
            if attempt == 1 and source == "vision":
                perception._vision_cache.clear()
                before_cache = 0

            with trace.scope(run=run_id, scenario=case["id"], source=source):
                candidates, ms, ambiguous, error = ask(
                    source, case["intent"], fixture.window)
                verdict, blame, detail = judge(
                    case, candidates, ambiguous, fixture, source)
                trace.event("perception", verdict=verdict, ms=round(ms, 1),
                            candidates=len(candidates))

            after_cache = len(perception._vision_cache)
            records.append({
                "run": run_id, "attempt": attempt,
                "case": case["id"], "category": case["category"],
                "fixture": case["fixture"], "variant": case["variant"],
                "intent": case["intent"], "expect": case.get("expect"),
                "outcome_wanted": case["outcome"],
                "source": source, "verdict": verdict, "good": verdict in GOOD,
                "attribution": blame, "ms": round(ms, 1),
                "ambiguous": ambiguous, "error": error,
                "cache_grew": after_cache > before_cache,
                "app": fixture.window.get("app", "") if fixture.window else "",
                "window": (fixture.window or {}).get("title", ""),
                "display_scale": (fixture.window or {}).get("scale"),
                **detail,
            })
    return records


def run(cases: List[Dict[str, Any]], sources: List[str], repeat: int,
        run_id: str) -> List[Dict[str, Any]]:
    """Group by fixture and variant so each application is launched once."""
    records: List[Dict[str, Any]] = []
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = collections.OrderedDict()
    for case in cases:
        groups.setdefault((case["fixture"], case["variant"]), []).append(case)

    for (name, variant), group in groups.items():
        print(f"\n  {name} ({variant}) — {len(group)} case(s)", flush=True)
        fixture = Fixture(name, variant)
        with fixture:
            if not fixture.started:
                print(f"    could not start or could not find the window", flush=True)
                for case in group:
                    records.append({
                        "run": run_id, "case": case["id"],
                        "category": case["category"], "source": "-",
                        "verdict": NOT_FOUND, "good": False,
                        "attribution": FIXTURE, "ms": 0.0,
                        "error": "fixture did not come up",
                    })
                continue
            for case in group:
                got = run_case(case, fixture, sources, run_id, repeat)
                records.extend(got)
                marks = " ".join(
                    f"{r['source']}:{'✓' if r['good'] else '✗'}" for r in got
                    if r["attempt"] == 1)
                print(f"    {case['id']:16} {marks}", flush=True)
    return records


# ─── Reporting ───────────────────────────────────────────────────────────────

def percent(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:5.1f}%" if whole else "    - "


def summarise(records: List[Dict[str, Any]], source: str) -> Dict[str, Any]:
    mine = [r for r in records if r["source"] == source]
    if not mine:
        return {}
    total = len(mine)
    counts = collections.Counter(r["verdict"] for r in mine)
    latencies = sorted(r["ms"] for r in mine)
    # A source that declines a question it is not for should not be scored as
    # if it got the answer wrong. Vision is asked about every case in this
    # corpus and is only ever meant to answer for unlabelled controls, so its
    # top-1 over everything reads as 8% while its accuracy when it engages is
    # 100%. Both numbers are true and only one of them is about the model.
    answered = [r for r in mine
                if r["verdict"] not in (NOT_FOUND, ABSTAINED)]
    engaged = sorted(r["ms"] for r in answered)
    errors = [r["centre_error_px"] for r in mine if r.get("centre_error_px") is not None]
    ious = [r["iou"] for r in mine if r.get("iou") is not None]
    return {
        "source": source, "n": total,
        "correct": counts[CORRECT],
        "in_top3": counts[IN_TOP3],
        "top3_total": counts[CORRECT] + counts[IN_TOP3],
        "wrong_target": counts[WRONG_TARGET],
        "off_target": counts[OFF_TARGET],
        "not_found": counts[NOT_FOUND],
        "abstained": counts[ABSTAINED],
        "should_have_abstained": counts[SHOULD_HAVE_ABSTAINED],
        "ambiguous_right": counts[AMBIGUOUS_RIGHT],
        "ambiguity_missed": counts[AMBIGUITY_MISSED],
        "false_ambiguity": counts[FALSE_AMBIGUITY],
        "good": sum(1 for r in mine if r["good"]),
        "answered": len(answered),
        "precision": (round(counts[CORRECT] / len(answered), 3)
                      if answered else None),
        "median_ms_engaged": round(statistics.median(engaged), 1) if engaged else None,
        "p95_ms_engaged": (round(engaged[max(0, int(len(engaged) * 0.95) - 1)], 1)
                           if engaged else None),
        "median_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95_ms": round(latencies[max(0, int(len(latencies) * 0.95) - 1)], 1) if latencies else 0.0,
        "median_centre_error_px": round(statistics.median(errors), 1) if errors else None,
        "median_iou": round(statistics.median(ious), 3) if ious else None,
    }


def calibration(records: List[Dict[str, Any]], source: str) -> List[Tuple[str, int, str]]:
    """Confidence against correctness, in bands. Shows whether 0.9 means 0.9."""
    mine = [r for r in records if r["source"] == source
            and r.get("top_confidence") is not None]
    bands = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]
    out = []
    for low, high in bands:
        inside_band = [r for r in mine if low <= r["top_confidence"] < high]
        if inside_band:
            right = sum(1 for r in inside_band if r["verdict"] == CORRECT)
            out.append((f"{low:.1f}–{high:.1f}", len(inside_band),
                        percent(right, len(inside_band))))
    return out


def report(records: List[Dict[str, Any]], sources: List[str], run_id: str) -> str:
    lines = ["", "=" * 74, "  DIABLO PERCEPTION BASELINE V1", "=" * 74]
    cases = len({r["case"] for r in records})
    lines.append(f"  run {run_id} · {cases} cases · {len(records)} measurements")
    lines.append("")

    head = (f"  {'source':9} {'n':>4} {'top-1':>7} {'top-3':>7} {'wrong':>7} "
            f"{'silent':>7} {'ans':>5} {'prec':>7} {'med ms':>8} {'p95 ms':>8}")
    lines += [head, "  " + "-" * 72]
    for source in sources:
        s = summarise(records, source)
        if not s:
            continue
        prec = percent(int(round((s["precision"] or 0) * s["answered"])),
                       s["answered"]) if s["answered"] else "    - "
        lines.append(
            f"  {source:9} {s['n']:>4} {percent(s['correct'], s['n']):>7} "
            f"{percent(s['top3_total'], s['n']):>7} "
            f"{percent(s['wrong_target'] + s['off_target'], s['n']):>7} "
            f"{percent(s['not_found'] + s['abstained'], s['n']):>7} "
            f"{s['answered']:>5} {prec:>7} "
            f"{s['median_ms']:>8.1f} {s['p95_ms']:>8.1f}")
    lines += ["",
              "  top-1 is over every case. precision is over the cases the source",
              "  actually answered — a source that declines what it is not for is",
              "  silent, not wrong. 'silent' counts both.",
              "",
              "  Latency when the source engaged (ignoring instant declines)",
              "  " + "-" * 72,
              f"  {'source':9} {'median ms':>12} {'p95 ms':>12}"]
    for source in sources:
        s = summarise(records, source)
        if not s:
            continue
        med = f"{s['median_ms_engaged']}" if s["median_ms_engaged"] is not None else "-"
        p95 = f"{s['p95_ms_engaged']}" if s["p95_ms_engaged"] is not None else "-"
        lines.append(f"  {source:9} {med:>12} {p95:>12}")

    # ── bounding accuracy ────────────────────────────────────────────────────
    lines += ["", "  Bounding accuracy (top-1, against the fixture's own rectangle)",
              "  " + "-" * 72,
              f"  {'source':9} {'median centre error':>22} {'median IoU':>12}"]
    for source in sources:
        s = summarise(records, source)
        if not s:
            continue
        err = f"{s['median_centre_error_px']} px" if s["median_centre_error_px"] is not None else "-"
        io = f"{s['median_iou']}" if s["median_iou"] is not None else "-"
        lines.append(f"  {source:9} {err:>22} {io:>12}")

    # ── by category ──────────────────────────────────────────────────────────
    lines += ["", "  Top-1 by target category", "  " + "-" * 72]
    categories = sorted({r["category"] for r in records})
    lines.append("  " + f"{'category':18}" + "".join(f"{s:>12}" for s in sources))
    for category in categories:
        row = f"  {category:18}"
        for source in sources:
            mine = [r for r in records
                    if r["category"] == category and r["source"] == source]
            if not mine:
                row += f"{'-':>12}"
            else:
                good = sum(1 for r in mine if r["good"])
                row += f"{percent(good, len(mine)):>12}"
        lines.append(row)

    # ── failures ─────────────────────────────────────────────────────────────
    bad = [r for r in records if not r.get("good")]
    lines += ["", f"  Failure attribution ({len(bad)} of {len(records)} measurements)",
              "  " + "-" * 72]
    by_blame = collections.Counter(r.get("attribution") or UNKNOWN for r in bad)
    for blame, count in by_blame.most_common():
        lines.append(f"  {blame:20} {count:>4}  {percent(count, len(bad))}")

    lines += ["", "  Failure modes", "  " + "-" * 72]
    for verdict, count in collections.Counter(r["verdict"] for r in bad).most_common():
        lines.append(f"  {verdict:24} {count:>4}")

    # ── cache ────────────────────────────────────────────────────────────────
    repeats = {r.get("attempt") for r in records}
    if len(repeats) > 1:
        lines += ["", "  Vision cache — the same question against the same frame",
                  "  " + "-" * 72,
                  f"  {'attempt':>8} {'n':>5} {'median ms':>12} {'p95 ms':>10}"]
        for attempt in sorted(a for a in repeats if a):
            mine = [r["ms"] for r in records
                    if r["source"] == "vision" and r.get("attempt") == attempt
                    and r["verdict"] not in (NOT_FOUND, ABSTAINED)]
            if not mine:
                continue
            mine.sort()
            lines.append(
                f"  {attempt:>8} {len(mine):>5} "
                f"{statistics.median(mine):>12.1f} "
                f"{mine[max(0, int(len(mine) * 0.95) - 1)]:>10.1f}")
        lines.append("  attempt 1 clears the cache; later attempts should hit it.")

    # ── calibration ──────────────────────────────────────────────────────────
    lines += ["", "  Confidence calibration — what a score actually predicts",
              "  " + "-" * 72]
    for source in sources:
        bands = calibration(records, source)
        if not bands:
            continue
        lines.append(f"  {source}")
        for band, n, right in bands:
            lines.append(f"    confidence {band}   n={n:<4} correct {right}")

    lines.append("")
    return "\n".join(lines)


# The modules this benchmark actually measures. Diablo's tests and evals import
# from the install, not from the repository, and nothing keeps the two in step —
# there is no install.sh in this tree and the launcher has no sync command. A
# benchmark that silently measures last week's perception is worse than no
# benchmark, so this refuses to run rather than produce a plausible number.
MEASURED = ["perception.py", "ax.py", "screen.py", "screen_context.py",
            "display.py", "pointing.py", "trace.py"]


def stale() -> List[str]:
    """Which measured modules differ between the repository and the install."""
    out = []
    for name in MEASURED:
        source = os.path.join(REPO, "diablo", name)
        installed = os.path.join(DIABLO_HOME, "skills", name)
        try:
            if open(source, "rb").read() != open(installed, "rb").read():
                out.append(name)
        except OSError:
            out.append(f"{name} (unreadable)")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Diablo perception baseline")
    parser.add_argument("--source", action="append",
                        choices=["ax", "ocr", "vision", "fusion"],
                        help="measure only this source; repeatable")
    parser.add_argument("--category", help="only cases in this category")
    parser.add_argument("--scenario", help="only this case id")
    parser.add_argument("--fixture", help="only cases using this fixture")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run each case N times, for variability")
    parser.add_argument("--json", help="write the records and summary here")
    parser.add_argument("--allow-stale", action="store_true",
                        help="measure the install even though it differs from the repo")
    parser.add_argument("--no-vision", action="store_true",
                        help="skip the vision source (it needs the model loaded)")
    args = parser.parse_args(argv)

    drifted = stale()
    if drifted and not args.allow_stale:
        print("\n  The install does not match the repository, so this would be")
        print("  measuring code that is not the code in git:")
        for name in drifted:
            print(f"    {name}")
        print("\n  Copy them across, or pass --allow-stale if that is deliberate.\n")
        return 3

    corpus = json.load(open(CASES))
    cases = corpus["cases"]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if args.scenario:
        cases = [c for c in cases if c["id"] == args.scenario]
    if args.fixture:
        cases = [c for c in cases if c["fixture"] == args.fixture]
    if not cases:
        print("  no cases matched")
        return 2

    sources = args.source or ["ax", "ocr", "vision", "fusion"]
    if args.no_vision:
        sources = [s for s in sources if s != "vision"]

    run_id = uuid.uuid4().hex[:8]
    print(f"  DIABLO PERCEPTION BASELINE V1 — run {run_id}")
    print(f"  {len(cases)} case(s), sources: {', '.join(sources)}, "
          f"repeat {args.repeat}")

    began = time.time()
    records = run(cases, sources, args.repeat, run_id)
    took = time.time() - began

    text = report(records, sources, run_id)
    print(text)
    print(f"  {len(records)} measurements in {took:.0f}s\n")

    if args.json:
        payload = {
            "run": run_id, "when": time.time(), "seconds": round(took, 1),
            "sources": sources, "repeat": args.repeat,
            "cases": len(cases),
            "summary": {s: summarise(records, s) for s in sources},
            "records": records,
        }
        with open(args.json, "w") as handle:
            json.dump(payload, handle, indent=2)
        print(f"  written to {args.json}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
