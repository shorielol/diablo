#!/usr/bin/env python3
"""
The grounding gauntlet: real windows, real accessibility, real models.

    python3 evals/run_grounding_gauntlet.py fixtures     every fixture checked before use
    python3 evals/run_grounding_gauntlet.py perception   the 25-case corpus, five ways
    python3 evals/run_grounding_gauntlet.py validation   the gate against live windows
    python3 evals/run_grounding_gauntlet.py native       GoClick in real apps, no clicks
    ... --json out.json

Nothing here clicks. The perception corpus and its judge are the ones in
run_perception_eval.py, unchanged, so a score here means what it meant there.
Ground truth comes from each fixture's own layout or, in a real application,
from the accessibility element itself; no answer and no coordinate is written
into this file. The adversarial proposals in `validation` are deliberately
wrong points — that is what a model getting it wrong looks like — and are
built from the fixture's live layout, never from numbers typed here.

Screen captures are deleted as soon as they are read. Real applications'
control labels are used in memory to ask GoClick and are never written out:
the record keeps roles, verdicts and distances.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)
sys.path.insert(0, HERE)

try:
    from companion_manager import load_env
    load_env()
except Exception:
    pass

import run_perception_eval as E  # noqa: E402
from skills import ax, goclick, perception, screen, screen_context  # noqa: E402
from skills import display  # noqa: E402
from skills import grounding_gate as gate  # noqa: E402

FIXTURE_DIR = os.path.join(REPO, "tests", "fixtures")

# Where fixtures open: "x,y" in AppKit coordinates, or "" for each fixture's
# own place. Set from --display, so a run can use a display whose current
# Space is an ordinary desktop while another shows someone's full-screen app.
FIXTURE_AT = ""


# ─── A fixture that takes commands ───────────────────────────────────────────

class Live:
    """A fixture process: its layout, its window, and a way to change it."""

    def __init__(self, name: str, *args: str):
        self.name, self.args = name, list(args)
        self.child: Optional[subprocess.Popen] = None
        self.layout: Dict[str, Any] = {}
        self.window: Optional[Dict[str, Any]] = None
        self.started = False
        self.covered = False

    @property
    def controls(self) -> List[Dict[str, Any]]:
        return self.layout.get("controls", [])

    def control(self, ident: str) -> Optional[Dict[str, Any]]:
        return next((c for c in self.controls if c["id"] == ident), None)

    def start(self, settle: float = 1.3) -> bool:
        env = dict(os.environ)
        if FIXTURE_AT:
            env["DIABLO_FIXTURE_AT"] = FIXTURE_AT
        self.child = subprocess.Popen(
            [sys.executable, os.path.join(FIXTURE_DIR, f"{self.name}.py"), *self.args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env)
        if not self._read_until("READY"):
            return False
        time.sleep(settle)
        self.window = self._own_window()
        if self.window is None:
            return False
        self.covered = not self.on_top()
        return True

    def _own_window(self, wait: float = 15.0) -> Optional[Dict[str, Any]]:
        """
        This fixture's window, found by the process that owns it.

        Not by title: find_window matches loosely, and an editor with this
        repository open has "Diablo" in its title too. The first version of
        this harness measured the editor's window and reported the fixture as
        covered.
        """
        deadline = time.time() + wait
        while time.time() < deadline:
            for window in screen_context.list_windows():
                if ax._pid_for_window(window) == self.child.pid:
                    return window
            time.sleep(0.3)
        return None

    def on_top(self, wait: float = 10.0) -> bool:
        """Whether a hit test at one of its controls reaches this window."""
        if not self.controls:
            return True
        pid = ax._pid_for_window(self.window)
        deadline = time.time() + wait
        while time.time() < deadline:
            hit = ax.hit_test(*centre(self.controls[0]))
            if hit is not None and (not hit.pid or hit.pid == pid):
                return True
            time.sleep(0.5)
        return False

    def _read_until(self, marker: str, timeout: float = 25.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.child.stdout.readline()
            if not line:
                return False
            line = line.strip()
            if line.startswith("LAYOUT "):
                self.layout = json.loads(line[7:])
            if line.startswith(marker):
                return True
        return False

    def command(self, text: str, settle: float = 0.4) -> bool:
        self.child.stdin.write(text + "\n")
        self.child.stdin.flush()
        ok = self._read_until("DONE")
        time.sleep(settle)
        return ok

    def stop(self) -> None:
        if self.child is not None:
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


def centre(box: Dict[str, float]):
    return (box["x"] + box["width"] / 2.0, box["y"] + box["height"] / 2.0)


def point_candidate(point, label="", source="model"):
    """A raw point as a candidate the corpus judge can score."""
    return SimpleNamespace(x=point[0] - 1, y=point[1] - 1, width=2.0, height=2.0,
                           centre=point, label=label, sources=[source], confidence=1.0)


def rect_candidate(target, source):
    return SimpleNamespace(x=target.x, y=target.y, width=target.width, height=target.height,
                           centre=target.centre, label=target.label, sources=[source],
                           confidence=1.0)


def percentile(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


# ─── Qwen3.5 asked for a point, as a grounder in its own right ───────────────

QWEN_POINT_SCHEMA = {"type": "object",
                     "properties": {"found": {"type": "boolean"},
                                    "point_2d": {"type": "array", "items": {"type": "number"}}},
                     "required": ["found", "point_2d"]}


def qwen_point(frame, intent: str):
    """Qwen3.5's own pointing, on its documented 0..1000 grid, as a proposal."""
    question = (f"This is a screenshot of one application window. Where is {intent}? "
                f"Answer with point_2d, the centre of that control on a 0 to 1000 grid "
                f"across the image (0,0 top-left, 1000,1000 bottom-right). If it is not "
                f"in the picture, set found to false.")
    raw = screen_context.read_image(frame.path, question, num_predict=80,
                                    schema=QWEN_POINT_SCHEMA)
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    point = parsed.get("point_2d") or []
    if not parsed.get("found") or len(point) < 2:
        return None
    try:
        u, v = float(point[0]) / 1000.0, float(point[1]) / 1000.0
    except (TypeError, ValueError):
        return None
    return gate.GroundingProposal(frame_id=frame.frame_id, space=gate.Space.FRAME_NORMALISED,
                                  x=u, y=v, target=intent, source_model="qwen3.5:4b")


# ─── fixtures: is each one fit to score against? ─────────────────────────────

FIXTURE_SET = [("controls_app", []), ("controls_app", ["--dark"]), ("icon_app", []),
               ("target_app", []), ("gate_app", []), ("gate_app", ["--dark"])]


def contrast(image, box, frame) -> Optional[float]:
    """
    How far a control's ink stands out from its own background, measured in
    the capture: the luminance contrast ratio between its darkest and lightest
    1% of pixels. Text covers only a few percent of a button, so a wider
    percentile measures the bezel instead of the ink. Text drawn white on white, the defect this check exists for,
    measures about 1.0. Judged from pixels, not by OCR, because OCR is one of
    the things being measured.
    """
    sx, sy = frame.pixel_width / frame.width, frame.pixel_height / frame.height
    left = int((box["x"] - frame.x) * sx) + 2
    top = int((box["y"] - frame.y) * sy) + 2
    right = int((box["x"] + box["width"] - frame.x) * sx) - 2
    bottom = int((box["y"] + box["height"] - frame.y) * sy) - 2
    if right - left < 4 or bottom - top < 4:
        return None
    crop = image.crop((left, top, right, bottom)).convert("L")
    values = sorted(crop.tobytes())
    if not values:
        return None
    dark = values[int(0.01 * (len(values) - 1))] / 255.0
    light = values[int(0.99 * (len(values) - 1))] / 255.0

    def lum(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return round((lum(light) + 0.05) / (lum(dark) + 0.05), 2)


def check_fixture(name: str, args: List[str]) -> Dict[str, Any]:
    """Controls present, labels legible in the pixels, states right, window the right size."""
    from PIL import Image
    record: Dict[str, Any] = {"fixture": name,
                              "variant": "dark" if "--dark" in args else "light"}
    with Live(name, *args) as live:
        if not live.started:
            record.update(valid=False, problems=["did not come up on screen"])
            return record
        window = live.window
        pid = ax._pid_for_window(window)
        frame = screen.capture_this_window(window)
        try:
            problems, interference, ocr_missed, ratios = [], [], [], {}
            words = set()
            image = None
            if frame.ok:
                image = Image.open(frame.path)
                image.load()
                text = screen_context.ocr_image(frame.path, window) or ""
                words = {w.lower().strip(".,:…") for w in text.split()}
            for control in live.controls:
                label = control.get("label") or ""
                hit = ax.hit_test(*centre(control))
                if hit is not None and hit.pid and pid and hit.pid != pid:
                    interference.append(f"{control['id']}: another application was on top")
                    continue
                element = hit.element if hit else None
                if element is None:
                    problems.append(f"{control['id']}: accessibility finds nothing at its centre")
                    continue
                if label and element.label != label:
                    problems.append(f"{control['id']}: accessibility calls it {element.label!r}")
                if "enabled" in control and element.enabled is not None \
                        and bool(element.enabled) != bool(control["enabled"]):
                    problems.append(f"{control['id']}: enabled state disagrees")
                if image is not None and control.get("kind") != "text_field":
                    ratio = contrast(image, control, frame)
                    ratios[control["id"]] = ratio
                    floor = 1.5 if control.get("enabled") is False else 3.0
                    if ratio is not None and ratio < floor:
                        problems.append(f"{control['id']}: contrast {ratio} below {floor}")
                if label and not all(w.lower() in words for w in label.split()):
                    ocr_missed.append(control["id"])
            size_ok = frame.ok and abs(frame.width - window["width"]) < 2
            record.update(valid=bool(frame.ok and size_ok and not problems and not interference),
                          controls=len(live.controls), capture_ok=frame.ok,
                          problems=problems[:8], interference=interference[:8],
                          min_contrast=min((v for v in ratios.values() if v), default=None),
                          ocr_missed=ocr_missed, display_scale=frame.display_scale,
                          pixel_ratio=(round(frame.pixel_width / frame.width, 3)
                                       if frame.ok and frame.width else None))
        finally:
            frame.release()
    return record


# ─── perception: the corpus, five ways ───────────────────────────────────────

MODES = ("deterministic", "production_goclick", "production_qwen35", "goclick_gated",
         "qwen35_gated")


class Switch:
    """Turn sources off for one call, and put them back."""

    def __init__(self, goclick_on: bool, vision_on: bool):
        self.goclick_on, self.vision_on = goclick_on, vision_on

    def __enter__(self):
        self.saved = (goclick.available, screen_context.vision_available)
        if not self.goclick_on:
            goclick.available = lambda: False
        if not self.vision_on:
            screen_context.vision_available = lambda: False
        return self

    def __exit__(self, *exc):
        goclick.available, screen_context.vision_available = self.saved


def run_perception(repeat: int = 1) -> List[Dict[str, Any]]:
    cases = json.load(open(E.CASES))
    cases = cases["cases"] if isinstance(cases, dict) else cases
    records: List[Dict[str, Any]] = []
    groups: Dict[tuple, list] = {}
    for case in cases:
        groups.setdefault((case["fixture"], case.get("variant", "light")), []).append(case)
    for (name, variant), group in groups.items():
        with Live(name, *(["--dark"] if variant == "dark" else [])) as live:
            if not live.started or live.covered:
                for case in group:
                    records.append({"case": case["id"], "mode": "-",
                                    "verdict": "fixture_failed"})
                continue
            for _ in range(repeat):
                for case in group:
                    for mode in MODES:
                        record = one_case(case, mode, live)
                        records.append(record)
                        print(f"{case['id']:18} {mode:20} {record['verdict']:22} "
                              f"{record.get('ms', 0):7.0f} ms {record.get('gate', '')}",
                              flush=True)
    return records


def one_case(case, mode, live) -> Dict[str, Any]:
    intent, window = case["intent"], live.window
    record: Dict[str, Any] = {"case": case["id"], "category": case.get("category"),
                              "mode": mode, "want": case["outcome"]}
    began = time.time()
    if mode in ("deterministic", "production_goclick", "production_qwen35"):
        switch = {"deterministic": Switch(False, False),
                  "production_goclick": Switch(True, False),
                  "production_qwen35": Switch(False, True)}[mode]
        seen: Dict[str, Any] = {}
        with switch:
            candidates = perception.locate(intent, window, observation=seen)
        runner = candidates[1] if len(candidates) > 1 else None
        ambiguous = False
        if candidates:
            margin = (candidates[0].confidence - runner.confidence) if runner else 1.0
            separation, basis = perception._separation(candidates[0], runner, margin)
            ambiguous = runner is not None and not separation > perception.required_margin(basis)
        verdict, blame, detail = E.judge(case, candidates, ambiguous, live)
        record.update(verdict=verdict, blame=blame,
                      decisive=bool(candidates) and not ambiguous
                      and candidates[0].confidence >= perception.DECISIVE,
                      grounder_called="grounder" in seen,
                      gate=(seen.get("grounder") or {}).get("verdict", ""),
                      centre_error_px=detail.get("centre_error_px"))
    else:
        look = gate.observe(window)
        try:
            proposal = (goclick.propose(look.frame, intent) if mode == "goclick_gated"
                        else qwen_point(look.frame, intent))
            raw_point = None
            if proposal is not None and 0 <= proposal.x <= 1 and 0 <= proposal.y <= 1:
                raw_point = look.frame.to_quartz(proposal.x, proposal.y)
            validation = gate.validate(proposal, look) if proposal is not None else None
        finally:
            look.frame.release()
        raw = [point_candidate(raw_point)] if raw_point else []
        raw_verdict, _, raw_detail = E.judge(case, raw, False, live, source="raw")
        kept = []
        if validation is not None and validation.target is not None:
            kept = [rect_candidate(validation.target, mode)]
        verdict, blame, detail = E.judge(case, kept, False, live, source="raw")
        record.update(verdict=verdict, raw_verdict=raw_verdict,
                      raw_error_px=raw_detail.get("centre_error_px"),
                      gate=validation.verdict if validation else "no_proposal",
                      decisive=bool(validation and validation.ok),
                      centre_error_px=detail.get("centre_error_px"))
    record["ms"] = round((time.time() - began) * 1000, 1)
    return record


def summarise_perception(records) -> Dict[str, Any]:
    out = {}
    for mode in MODES:
        rows = [r for r in records if r["mode"] == mode]
        out[mode] = {
            "n": len(rows),
            "good": sum(r["verdict"] in E.GOOD for r in rows),
            "wrong_target": sum(r["verdict"] == E.WRONG_TARGET for r in rows),
            "would_act_on_wrong": sum(r["verdict"] in (E.WRONG_TARGET, E.SHOULD_HAVE_ABSTAINED)
                                      and bool(r.get("decisive")) for r in rows),
            "absent_rejected": f"{sum(r['verdict'] == E.ABSTAINED for r in rows if r['want'] == 'abstain')}"
                               f"/{sum(r['want'] == 'abstain' for r in rows)}",
            "p50_ms": percentile([r["ms"] for r in rows], 0.5),
            "p95_ms": percentile([r["ms"] for r in rows], 0.95),
            "grounder_calls": sum(bool(r.get("grounder_called")) for r in rows),
        }
        if mode.endswith("_gated"):
            out[mode]["raw_good"] = sum(r.get("raw_verdict") in E.GOOD for r in rows)
            out[mode]["raw_wrong_target"] = sum(
                r.get("raw_verdict") in (E.WRONG_TARGET, E.SHOULD_HAVE_ABSTAINED) for r in rows)
            out[mode]["raw_median_error_px"] = percentile(
                [r.get("raw_error_px") for r in rows if r.get("raw_verdict") in E.GOOD], 0.5)
            out[mode]["gate"] = {v: sum(r.get("gate") == v for r in rows)
                                 for v in sorted({str(r.get("gate")) for r in rows})}
    return out


# ─── validation: the gate against live windows ───────────────────────────────

def placed_on(display_id: int) -> List[str]:
    """The --at arguments that open gate_app near the top-left of one display."""
    if not display_id:
        return []
    screen_ = next((d for d in display.displays() if d.id == display_id), None)
    if screen_ is None:
        return []
    appkit_x = screen_.x + 80
    appkit_y = display.main_height() - (screen_.y + 120 + 300)
    return ["--at", f"{appkit_x},{appkit_y}"]


def run_validation(on_display: int = 0) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    place: List[str] = []

    def case(name, expect, verdict_obj, extra=None, detail_expect=""):
        got = verdict_obj.verdict if verdict_obj is not None else "no_proposal"
        detail = getattr(verdict_obj, "detail", "") or ""
        ok = got == expect and (not detail_expect or detail == detail_expect)
        out.append({"case": name, "expect": f"{expect} {detail_expect}".strip(),
                    "got": f"{got} {detail}".strip(), "pass": ok, **(extra or {})})
        print(f"{'PASS' if ok else 'FAIL'}  {name:60} {got} {detail}", flush=True)

    def propose(look, point, target, **kw):
        return gate.GroundingProposal(frame_id=look.frame_id, space=gate.Space.QUARTZ_POINT,
                                      x=point[0], y=point[1], target=target,
                                      source_model="adversary", **kw)

    def fresh_look(live):
        look = gate.observe(live.window)
        look.frame.release()
        return look

    def pixel_case(label, live_, look_, ident, name):
        f = look_.frame
        c = live_.control(ident)
        u = (centre(c)[0] - f.x) / f.width
        v = (centre(c)[1] - f.y) / f.height
        proposal = gate.GroundingProposal(
            frame_id=look_.frame_id, space=gate.Space.FRAME_PIXEL,
            x=u * f.pixel_width, y=v * f.pixel_height, target=name, source_model="adversary")
        result = gate.validate(proposal, look_)
        error = None
        if result.point:
            error = round(((result.point[0] - centre(c)[0]) ** 2
                           + (result.point[1] - centre(c)[1]) ** 2) ** 0.5, 3)
        case(label, gate.VALIDATED, result,
             {"display_scale": f.display_scale, "pixel_ratio": round(f.pixel_width / f.width, 3),
              "quartz_error_pt": error, "window_x": live_.window["x"]})

    single = "save,delete_all,delete,export,gear"
    with Live("gate_app", "--only", single, *place) as live:
        if not live.started:
            return [{"case": "gate_app", "pass": False, "got": "did not start"}]
        if live.covered:
            return [{"case": "gate_app", "pass": None,
                     "got": "INFRASTRUCTURE: another window covers the fixture; not scored"}]
        at = {c["id"]: centre(c) for c in live.controls}
        look = fresh_look(live)
        case("1 correct target", gate.VALIDATED,
             gate.validate(propose(look, at["save"], "Save"), look))
        empty = (live.window["x"] + live.window["width"] - 30, live.window["y"] + 60)
        case("2 absent target (a point on empty window)", gate.REJECT_NO_TARGET,
             gate.validate(propose(look, empty, "Export all"), look))
        case("3 disabled target", gate.REJECT_DISABLED,
             gate.validate(propose(look, at["export"], "Export"), look))
        case("12 visually similar: Delete is not Delete All", gate.REJECT_LABEL_MISMATCH,
             gate.validate(propose(look, at["delete_all"], "Delete"), look))
        case("13 a point on Delete All, labelled Save", gate.REJECT_LABEL_MISMATCH,
             gate.validate(propose(look, at["delete_all"], "Save", claimed_label="Save",
                                   confidence=0.99), look))
        case("   an unnamed control is asked about, not pressed", gate.CONFIRM_REQUIRED,
             gate.validate(propose(look, at["gear"], "the settings gear"), look))
        frame = look.frame
        case("9 a pixel beyond the image", gate.REJECT_COORDINATE_SPACE,
             gate.validate(gate.GroundingProposal(
                 frame_id=look.frame_id, space=gate.Space.FRAME_PIXEL,
                 x=frame.pixel_width + 10, y=10, target="Save", source_model="adversary"),
                 look))
        pixel_case(f"15 a frame pixel, {frame.display_scale:g}x display, back to Quartz",
                   live, look, "save", "Save")

        for name, command, undo, expect, detail in [
                ("6 the target disappears after the look", "hide save", "show save",
                 gate.REJECT_STALE_FRAME, "gone"),
                ("7 the target moves after the look", "move save 12 0", "move save -12 0",
                 gate.REJECT_STALE_FRAME, "moved"),
                ("8 the target changes identity after the look", "rename save Delete All",
                 "rename save Save", gate.REJECT_STALE_FRAME, "changed"),
                ("   the target becomes disabled after the look", "disable save",
                 "enable save", gate.REJECT_DISABLED, "")]:
            look = fresh_look(live)
            point = centre(live.control("save"))
            live.command(command)
            case(name, expect, gate.validate(propose(look, point, "Save"), look),
                 detail_expect=detail)
            live.command(undo)

        look = fresh_look(live)
        target = gate.validate(propose(look, centre(live.control("save")), "Save"), look).target
        live.command("disable save")
        case("14 changed between validation and click (revalidate)", gate.REJECT_DISABLED,
             gate.revalidate(target) if target else None)
        live.command("enable save")

        save = live.control("save")
        main_h = display.main_height()
        over_x = save["x"] - 300 - 10
        over_y = main_h - (save["y"] + 140)
        with Live("gate_app", "--only", "save", "--title", "Overlay",
                  "--at", f"{over_x},{over_y}") as other:
            if other.started:
                look = fresh_look(live)
                point = centre(live.control("save"))
                case("4 another window lies over the target", gate.REJECT_WRONG_WINDOW,
                     gate.validate(propose(look, point, "Save"), look))
                case("   act_on's last look, under the same overlay", gate.REJECT_WRONG_WINDOW,
                     gate.check_point(point[0], point[1], "Save", "AXButton", live.window))
            else:
                out.append({"case": "4 overlay", "pass": False, "got": "overlay did not start"})

        for ident in ("save", "delete_all", "delete", "export", "gear"):
            control = live.control(ident)
            look = gate.observe(live.window)
            try:
                began = time.time()
                intent = control["label"] or "settings gear icon"
                proposal = goclick.propose(look.frame, intent)
                ms = round((time.time() - began) * 1000)
                raw = (look.frame.to_quartz(proposal.x, proposal.y) if proposal else None)
                result = gate.validate(proposal, look) if proposal else None
            finally:
                look.frame.release()
            inside = bool(raw) and E.inside(raw, control)
            out.append({"case": f"goclick on {ident}",
                        "got": result.verdict if result else "none",
                        "raw_inside_target": inside, "ms": ms,
                        "raw_error_pt": round(E.distance(raw, control), 1) if raw else None,
                        "pass": None})
            print(f"      goclick on {ident:12} raw inside={inside} -> "
                  f"{result.verdict if result else 'none'} ({ms} ms)", flush=True)

    with Live("gate_app", *place) as live:
        if live.started:
            look = fresh_look(live)
            case("5 two controls called Save", gate.REJECT_AMBIGUOUS,
                 gate.validate(propose(look, centre(live.control("save")), "Save",
                                       confidence=0.99), look))

    used = on_display or (display.main_display().id if display.main_display() else 0)
    others = [d for d in display.displays() if d.id != used]
    for other_display in others:
        with Live("gate_app", "--only", single, "--title", "Second display",
                  *placed_on(other_display.id)) as live:
            if live.started and live.covered:
                out.append({"case": f"15 display {other_display.id}", "pass": None,
                            "got": "INFRASTRUCTURE: covered by another window; not scored"})
            elif live.started and live.window.get("display_id") == other_display.id:
                look = fresh_look(live)
                for ident, name in (("save", "Save"), ("delete_all", "Delete All")):
                    pixel_case(f"15 a frame pixel on display {other_display.id} "
                               f"({other_display.scale:g}x, origin x={other_display.x:g}): {name}",
                               live, look, ident, name)
            else:
                out.append({"case": f"15 display {other_display.id}", "pass": False,
                            "got": "the fixture did not open on it"})
    return out


# ─── native: GoClick in real applications, nothing clicked ───────────────────

NATIVE_APPS = ("Finder", "System Settings", "Calculator", "TextEdit", "Safari")
NATIVE_ROLES = ("AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton",
                "AXTextField", "AXSearchField", "AXTab", "AXLink")


def run_native(per_app: int = 8) -> List[Dict[str, Any]]:
    out = []
    windows = screen_context.list_windows()
    for app in NATIVE_APPS:
        window = next((w for w in windows if w["app"] == app and not w.get("sensitive")), None)
        if window is None:
            out.append({"app": app, "status": "not open, and not opened for this run"})
            print(f"{app:16} not open", flush=True)
            continue
        named = [e for e in ax.elements_for_window(window)
                 if e.named and e.enabled is not False and e.width >= 8 and e.height >= 8
                 and e.role in NATIVE_ROLES]
        counts: Dict[str, int] = {}
        for e in named:
            counts[e.label] = counts.get(e.label, 0) + 1
        unique = [e for e in named if counts[e.label] == 1][:per_app]
        rows = []
        for element in unique:
            look = gate.observe(window)
            try:
                began = time.time()
                proposal = goclick.propose(look.frame, element.label)
                ms = round((time.time() - began) * 1000)
                raw = look.frame.to_quartz(proposal.x, proposal.y) if proposal else None
                result = gate.validate(proposal, look) if proposal else None
            finally:
                look.frame.release()
            box = {"x": element.x, "y": element.y, "width": element.width,
                   "height": element.height}
            same = bool(result and result.target and abs(result.target.x - element.x) < 2
                        and abs(result.target.y - element.y) < 2)
            # Whether this app's window is what a click at the control would
            # reach: a covered window is rejected by the gate, correctly.
            hit = ax.hit_test(element.x + element.width / 2, element.y + element.height / 2)
            front = bool(hit and hit.pid == ax._pid_for_window(window))
            rows.append({"role": element.role, "ms": ms, "front_at_control": front,
                         # The fixed detail code only: a reason can quote a
                         # real app's labels, which may be file names.
                         "gate_detail": (result.detail or "") if result else "",
                         "raw_inside": bool(raw) and E.inside(raw, box),
                         "raw_error_pt": round(E.distance(raw, box), 1) if raw else None,
                         "gate": result.verdict if result else "none",
                         "validated_same_element": bool(result and result.ok and same)})
        hits = sum(r["raw_inside"] for r in rows)
        validated = sum(r["gate"] == gate.VALIDATED for r in rows)
        wrong = sum(r["gate"] == gate.VALIDATED and not r["validated_same_element"]
                    for r in rows)
        out.append({"app": app, "targets": len(rows), "raw_inside": hits,
                    "validated": validated, "validated_wrong_element": wrong, "rows": rows})
        print(f"{app:16} {hits}/{len(rows)} points inside the control, {validated} validated, "
              f"{wrong} validated as the wrong control", flush=True)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("section", choices=("fixtures", "perception", "validation", "native"))
    parser.add_argument("--json", default="")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--display", type=int, default=0,
                        help="the display id to open fixtures on (default: their own place)")
    args = parser.parse_args()
    global FIXTURE_AT
    if args.display:
        FIXTURE_AT = placed_on(args.display)[1]
    if args.section == "fixtures":
        result: Any = [check_fixture(n, a) for n, a in FIXTURE_SET]
        for row in result:
            print(json.dumps(row), flush=True)
    elif args.section == "perception":
        records = run_perception(args.repeat)
        summary = summarise_perception(records)
        print(json.dumps(summary, indent=1))
        result = {"summary": summary, "records": records}
    elif args.section == "validation":
        result = run_validation(args.display)
        scored = [r for r in result if r.get("pass") is not None]
        print(f"\n{sum(bool(r['pass']) for r in scored)}/{len(scored)} validation cases passed")
    else:
        result = run_native()
    if args.json:
        json.dump(result, open(args.json, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
