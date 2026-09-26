#!/usr/bin/env python3
"""
B.4 — the model says what a thing is; the pixels say where it is.

Measured before this build, on the subsidy fixture: qwen2.5vl described the
welfare-loss region correctly 9 times in 9 ("the grey triangle just left of
where the two lines cross") and put its box on the triangle 0 times in 9. Two
separate causes, both measured:

  1. A coordinate space nobody knew about. A dot placed at known positions in
     blank images of eight sizes came back as an exact, aspect-preserving,
     zero-offset scaling of the truth, by a factor that depended only on the
     image's size. That is Qwen2.5-VL's smart_resize: the model answers in a
     picture scaled up to at least 1024 x 28 x 28 pixels, and Ollama never
     says so. On an 852-pixel crop that alone moved the box 170 points.
  2. The model itself. With the canvas corrected, its box still sat about 60
     pixels right of the triangle.

The first is a conversion and is corrected here. The second is why geometry
now comes from the pixels: regions.candidates finds filled areas exactly, and
the model is only asked to choose one, by letter, or NONE.

Nothing here draws on screen or asks a real model. Captures are synthetic
images whose every shape is known.
"""

import json
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import (computer_use, display, grounding, regions,  # noqa: E402
                    screen_context, target, teaching)

DEFAULT_NONE_POLICY = grounding.NONE_POLICY

TMP = tempfile.mkdtemp(prefix="diablo_b4_")


def save_png(arr: np.ndarray, name: str) -> str:
    from AppKit import NSBitmapImageRep, NSPNGFileType
    h, w, _ = arr.shape
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, w, h, 8, 3, False, False, "NSDeviceRGBColorSpace", w * 3, 24)
    rep.bitmapData()[: w * h * 3] = np.ascontiguousarray(arr, dtype=np.uint8).tobytes()
    path = os.path.join(TMP, name)
    rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(path, True)
    return path


def triangle(img, a, b, c, colour):
    h, w, _ = img.shape
    yy, xx = np.mgrid[:h, :w]

    def side(p, q):
        return (q[0] - p[0]) * (yy - p[1]) - (q[1] - p[1]) * (xx - p[0])
    s1, s2, s3 = side(a, b), side(b, c), side(c, a)
    inside = ((s1 >= 0) & (s2 >= 0) & (s3 >= 0)) | ((s1 <= 0) & (s2 <= 0) & (s3 <= 0))
    img[inside] = colour


def diagram(grain=0, seed=1):
    """
    A subsidy-like diagram, 852 x 632, with everything the real ones have that
    is not an area: a title bar, a grid, curves, text strokes, legend swatches.
    """
    img = np.full((632, 852, 3), 255, np.uint8)
    img[:28, :] = (35, 36, 36)                                          # title bar
    triangle(img, (45, 138), (300, 324), (45, 324), (183, 206, 234))    # blue: consumer
    triangle(img, (45, 506), (300, 324), (45, 324), (178, 219, 200))    # green: producer
    triangle(img, (313, 281), (372, 281), (372, 362), (169, 169, 171))  # grey: welfare loss
    for x in range(100, 800, 100):                                      # a grid through it
        img[40:600, x] = (225, 225, 225)
    for i in range(700):                                                # two curves, 2px
        x = 45 + i * 0.75
        img[int(138 + i * 0.55): int(138 + i * 0.55) + 2, int(x)] = (200, 50, 50)
        img[int(506 - i * 0.5): int(506 - i * 0.5) + 2, int(x)] = (50, 90, 180)
    for k, colour in enumerate([(183, 206, 234), (178, 219, 200), (169, 169, 171)]):
        img[240 + k * 30: 256 + k * 30, 620:636] = colour               # legend swatches
        img[244 + k * 30: 252 + k * 30, 646:760:3] = (20, 20, 20)       # "text"
    if grain:
        rng = np.random.default_rng(seed)
        noise = rng.integers(-grain, grain + 1, img.shape)
        img = np.clip(img.astype(int) + noise, 0, 255).astype(np.uint8)
    return img


TRUTH = {"consumer": (45, 138, 300, 324), "producer": (45, 324, 300, 506),
         "welfare": (313, 281, 372, 362)}


def near(region, box, tol=4):
    return all(abs(a - b) <= tol for a, b in
               zip((region.left, region.top, region.right, region.bottom), box))


def run() -> int:
    r = Results()

    # ── A. The coordinate experiment, as law ────────────────────────────────
    r.head("A  the model answers in its own canvas")
    with guard(r, "model_canvas matches every measured size"):
        # (image, canvas the model's answers implied, within one 28-pixel patch)
        measured = [((426, 316), (1082, 782)), ((852, 632), (1059, 783)),
                    ((600, 600), (893, 891)), ((1200, 400), (1566, 524)),
                    # 1278x948: the line fit said 1253, dragged by one point; the
                    # per-point ratios have a median of 1.005, i.e. about 1284.
                    ((1278, 948), (1284, 953)), ((1280, 1280), (1286, 1287)),
                    ((1280, 1000), (1285, 1008)), ((1100, 1100), (1091, 1095))]
        for (w, h), (mw, mh) in measured:
            cw, ch = grounding.model_canvas(w, h)
            r.check(f"{w}x{h} -> {cw}x{ch} (measured ~{mw}x{mh})",
                    abs(cw - mw) <= 28 and abs(ch - mh) <= 28)
        cw, ch = grounding.model_canvas(1200, 400)
        r.check("aspect is kept", abs(cw / ch - 3.0) < 0.1, f"{cw}x{ch}")
        r.check("patch multiples", all(v % 28 == 0 for v in grounding.model_canvas(852, 632)))

    with guard(r, "the box is read in that canvas"):
        saved = os.environ.get("DIABLO_VISION_COORDS")
        os.environ["DIABLO_VISION_COORDS"] = "canvas"
        try:
            # The raw box the model gave for the triangle on the 852 crop.
            got = grounding._box_in_crop([477, 337, 529, 406], (852, 632))
            cw, ch = grounding.model_canvas(852, 632)
            r.check("read as fractions of the 1064x784 canvas, not of the image",
                    got is not None and abs(got[0] - 477 / cw) < 1e-6
                    and abs(got[1] - 337 / ch) < 1e-6, str(got))
            r.check("  which is 1.25x nearer the origin than the old reading",
                    got is not None and abs(got[0] * 852 - 477 / 1.249) < 3)
            r.check("fractions still read as fractions",
                    grounding._box_in_crop([0.1, 0.2, 0.3, 0.4], (852, 632))
                    == (0.1, 0.2, 0.3, 0.4))
        finally:
            if saved is None:
                os.environ.pop("DIABLO_VISION_COORDS", None)
            else:
                os.environ["DIABLO_VISION_COORDS"] = saved

    with guard(r, "only for the model it was measured on"):
        saved = {k: os.environ.get(k) for k in ("DIABLO_VISION_COORDS", "DIABLO_VISION_MODEL")}
        try:
            os.environ.pop("DIABLO_VISION_COORDS", None)
            os.environ["DIABLO_VISION_MODEL"] = "qwen2.5vl:7b"
            r.check("qwen2.5vl: canvas", grounding._canvas_applies())
            os.environ["DIABLO_VISION_MODEL"] = "qwen3-vl:8b"
            r.check("another model: not assumed", not grounding._canvas_applies())
            os.environ["DIABLO_VISION_COORDS"] = "pixels"
            os.environ["DIABLO_VISION_MODEL"] = "qwen2.5vl:7b"
            r.check("an explicit setting wins", not grounding._canvas_applies())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    # ── B. The mapper, proven with known coordinates ────────────────────────
    r.head("B  crop -> screen, independently of any model")
    with guard(r, "image_to_quartz"):
        for label, frame in (
                ("main display", {"x": 128.3, "y": 288.0, "width": 851.7, "height": 632.0}),
                ("negative-x Retina display", {"x": -1400.0, "y": 120.0, "width": 700.0,
                                               "height": 500.0}),
                ("non-square", {"x": 10.0, "y": 20.0, "width": 1200.0, "height": 300.0})):
            for u, v in ((0.0, 0.0), (1.0, 1.0), (0.3712, 0.8125)):
                x, y = display.image_to_quartz(u, v, frame)
                r.check(f"{label}: ({u},{v}) -> exact",
                        abs(x - (frame["x"] + u * frame["width"])) < 1e-9
                        and abs(y - (frame["y"] + v * frame["height"])) < 1e-9)
        frame = {"x": -1512.0, "y": 0.0, "width": 756.0, "height": 491.0}
        at_1x = regions.Region(100, 50, 200, 150, 5000, (0, 0, 0)).fractions(756, 491)
        at_2x = regions.Region(200, 100, 400, 300, 20000, (0, 0, 0)).fractions(1512, 982)
        r.check("a Retina capture (2x pixels) lands on the same points as 1x",
                all(abs(a - b) < 1e-9 for a, b in zip(at_1x, at_2x)))
        edge = regions.Region(0, 0, 756, 491, 1, (0, 0, 0)).fractions(756, 491)
        x0, y0 = display.image_to_quartz(edge[0], edge[1], frame)
        x1, y1 = display.image_to_quartz(edge[2], edge[3], frame)
        r.check("a target at the crop's edges maps to the crop's edges",
                (x0, y0, x1, y1) == (-1512.0, 0.0, -756.0, 491.0))

    # ── C, D, E, F. Candidates from the pixels ──────────────────────────────
    r.head("C-F  areas found in the picture itself")
    with guard(r, "a clean diagram"):
        found = regions.candidates(diagram(), min_edge=24)
        r.check("the three shaded areas are found",
                any(near(f, TRUTH["welfare"]) for f in found)
                and any(near(f, TRUTH["consumer"]) for f in found)
                and any(near(f, TRUTH["producer"]) for f in found),
                str([(f.left, f.top, f.right, f.bottom) for f in found]))
        r.check("the two look-alike triangles are two candidates, not one",
                sum(1 for f in found if near(f, TRUTH["consumer"]) or
                    near(f, TRUTH["producer"])) == 2)
        r.check("no title bar, legend swatch, grid line, curve or text",
                len(found) == 3, str([(f.left, f.top, f.width, f.height) for f in found]))
        welfare = next((f for f in found if near(f, TRUTH["welfare"])), None)
        r.check("a candidate carries its geometry and colour", welfare is not None
                and abs(welfare.width - 59) <= 2 and 155 <= welfare.colour[0] <= 185,
                str(welfare))
        r.check("  and a triangle fills about half its box",
                welfare is not None and 0.4 <= welfare.fill <= 0.6, str(welfare))
    with guard(r, "the same diagram under grain, as the real page has"):
        found = regions.candidates(diagram(grain=10), min_edge=24)
        r.check("still the same three areas",
                sum(1 for key in TRUTH if any(near(f, TRUTH[key], tol=6) for f in found)) == 3
                and len(found) == 3, str([(f.left, f.top, f.right, f.bottom) for f in found]))
    with guard(r, "a highlighted bar among plain ones"):
        img = np.full((500, 700, 3), 255, np.uint8)
        for i in range(5):
            colour = (230, 120, 30) if i == 3 else (90, 120, 170)
            img[440 - 60 * (i + 1):440, 60 + i * 120: 140 + i * 120] = colour
        found = regions.candidates(img, min_edge=24)
        r.check("every bar is its own candidate", len(found) == 5, str(len(found)))
        r.check("  the highlighted one has its own colour",
                sum(1 for f in found if f.colour[0] > 200) == 1)
    with guard(r, "the page is not an area of the figure"):
        img = np.full((500, 700, 3), 255, np.uint8)
        img[20:480, 20:680] = (235, 234, 229)                       # an off-white card
        img[40:120, 40:660] = (219, 218, 213)                       # a light header band
        triangle(img, (200, 200), (400, 400), (200, 400), (195, 172, 230))
        found = regions.candidates(img, min_edge=24)
        r.check("only the violet triangle, not the card or the band",
                len(found) == 1 and found[0].colour[2] > 200 and found[0].colour[1] < 190,
                str([(f.left, f.top, f.right, f.bottom, f.colour) for f in found]))

    with guard(r, "a piece of an area is not a second area"):
        img = np.full((400, 600, 3), 255, np.uint8)
        img[60:340, 60:420] = (190, 170, 230)                       # the whole region
        img[200:340, 250:420] = (176, 156, 216)                     # a darker part of it
        found = regions.candidates(img, min_edge=24)
        r.check("one candidate: the whole region",
                len(found) == 1 and near(found[0], (60, 60, 420, 340), tol=6),
                str([(f.left, f.top, f.right, f.bottom) for f in found]))

    with guard(r, "does the picked area agree with the words? (triangulation)"):
        grey_triangle = regions.Region(313, 281, 372, 362, 2390, (169, 169, 171))
        violet_area = regions.Region(60, 60, 420, 340, 60000, (190, 170, 230))
        bar = regions.Region(420, 200, 500, 440, 19200, (230, 120, 30))
        for words, region, expected in (
                ("purple circle", grey_triangle, False),
                ("violet shaded area", violet_area, True),
                ("blue shaded area", violet_area, False),
                ("grey triangle", grey_triangle, True),
                ("orange bar", bar, True),
                ("circle", bar, False),
                ("welfare loss region", grey_triangle, True)):
            r.check(f"{words!r} vs {region.colour} fill {region.fill:.2f} -> {expected}",
                    grounding._agrees(words, region) is expected)

    with guard(r, "a line, an arrow or a point is not an area"):
        for words in ("the demand curve", "the arrow", "the x axis",
                      "the point where they cross", "the intersection"):
            wanted = target.read_question(words, stated="region")
            r.check(f"{words!r}: the area step is not used",
                    grounding._pick_area(save_png(diagram(), "thin.png"), (852, 632),
                                         wanted, None, (0, 0, 852, 632)) is None)
        prompt = grounding._judge_prompt(target.read_question("welfare loss", stated="region"),
                                         "ABC", None)
        r.check("the judge is told a line, arrow or point is NONE",
                "none" in prompt.lower() and "line" in prompt.lower()
                and "point" in prompt.lower())

    with guard(r, "nothing to find"):
        r.check("a blank picture has no areas",
                regions.candidates(np.full((300, 400, 3), 255, np.uint8)) == [])
        r.check("a degenerate picture is not an error",
                regions.candidates(np.zeros((4, 4, 3), np.uint8)) == []
                and regions.candidates(None) == [])

    # ── G, H. The judge ─────────────────────────────────────────────────────
    r.head("G-H  choosing is a letter, not a coordinate")
    saved = (display.capture_region, screen_context._for_vision, screen_context.read_image,
             grounding._crop_for, grounding._anchor_for, target.scope_for)
    crop = (100.0, 200.0, 852.0, 632.0)          # one pixel per point, for clarity
    asked = []
    counter = [0]

    def install(choice, bbox=None, anchor=None):
        source = save_png(diagram(grain=8), "diagram.png")

        def capture(*a, **k):
            counter[0] += 1
            fresh = os.path.join(TMP, f"capture_{counter[0]}.png")
            shutil.copy(source, fresh)
            return fresh

        def read_image(path, prompt, num_predict=0, schema=None):
            asked.append({"prompt": prompt, "schema": schema, "path": path})
            if schema and "choice" in json.dumps(schema):
                return json.dumps({"choice": choice, "reason": "stub"})
            return json.dumps({"where": "stub", "found": bbox is not None,
                               "bbox_2d": bbox or [0, 0, 0, 0], "confidence": 0.9,
                               "what": "stub"})
        display.capture_region = capture
        screen_context._for_vision = lambda p: (p, False)
        screen_context.read_image = read_image
        grounding._crop_for = lambda scope, window, anchor_, cursor: crop
        grounding._anchor_for = lambda wanted, window: anchor or {
            "text": "Welfare loss", "x": 800, "y": 460, "width": 90, "height": 16}
        target.scope_for = lambda wanted: (target.WINDOW,)

    window = {"id": 9, "app": "Preview", "title": "Subsidy", "x": 100.0, "y": 172.0,
              "width": 852.0, "height": 660.0, "sensitive": False}
    try:
        with guard(r, "the judge picks the welfare loss"):
            del asked[:]
            install("C")
            found = grounding._by_looking(target.read_question("welfare loss region",
                                                               stated="region"), window)
            judge = [a for a in asked if a["schema"] and "choice" in json.dumps(a["schema"])]
            r.check("one bounded question was asked", len(judge) == 1, str(len(asked)))
            schema = judge[0]["schema"] if judge else {}
            r.check("  its answer can only be a letter or NONE",
                    schema.get("properties", {}).get("choice", {}).get("enum", [])[-1:] == ["NONE"]
                    and "bbox_2d" not in json.dumps(schema) and "point" not in json.dumps(schema))
            prompt = judge[0]["prompt"] if judge else ""
            r.check("  it names the thing, and says the caption is only a label",
                    "welfare loss" in prompt.lower() and "label" in prompt.lower()
                    and "coordinate" not in prompt.lower(), prompt)
            r.check("  and it looks at a lettered sheet, not the raw capture",
                    bool(judge) and judge[0]["path"].endswith(".sheet.png"))
            c = found.candidate
            areas = regions.candidates(regions.load_rgb(save_png(diagram(grain=8), "x.png")),
                                       min_edge=24)
            want = areas[regions.LETTERS.index("C")] if len(areas) > 2 else None
            r.check("the geometry is the area's, mapped by the existing mapper",
                    c is not None and want is not None
                    and abs(c.x - (crop[0] + want.left)) < 1.5
                    and abs(c.y - (crop[1] + want.top)) < 1.5
                    and abs(c.width - want.width) < 1.5, f"{c} vs {want}")
            r.check("  it is a region, from the region source, with its extent",
                    c is not None and c.role == "region" and c.sources == [target.REGION_SOURCE]
                    and target.satisfies(target.REGION, c))
            r.check("  and it says where it came from",
                    c is not None and any("filled areas found in the picture" in e
                                          for e in c.evidence))
            r.check("no box was asked for once an area was chosen",
                    not any(a["schema"] and "bbox_2d" in json.dumps(a["schema"]) for a in asked))
            r.check("GUI-X accepts it as a place to show",
                    c is not None and teaching.waypoint(c, target.REGION, say="x") is not None)

        with guard(r, "the default is to abstain"):
            # Measured: with NONE falling back to the model's own box, a picture
            # with no purple circle got a confident ring on a grey triangle, 2
            # trials in 2. Abstaining was right 2 in 2 and cost no accuracy.
            r.check("NONE abstains unless told otherwise",
                    os.getenv("DIABLO_REGION_NONE") is not None or
                    DEFAULT_NONE_POLICY == "abstain", DEFAULT_NONE_POLICY)

        with guard(r, "NONE"):
            del asked[:]
            install("NONE", bbox=[400, 350, 470, 450])
            grounding.NONE_POLICY = "abstain"
            found = grounding._by_looking(target.read_question("purple circle",
                                                               stated="region"), window)
            r.check("abstain policy: NONE is an honest 'could not'",
                    found.candidate is None and found.abstained)
            r.check("  and no coordinate was asked for",
                    not any(a["schema"] and "bbox_2d" in json.dumps(a["schema"]) for a in asked))
            del asked[:]
            grounding.NONE_POLICY = "bbox"
            grounding._by_looking(target.read_question("purple circle", stated="region"), window)
            r.check("bbox policy: NONE falls back to the corrected box question",
                    any(a["schema"] and "bbox_2d" in json.dumps(a["schema"]) for a in asked))
        with guard(r, "a pick that contradicts the words is refused"):
            del asked[:]
            install("C")
            grounding.NONE_POLICY = "abstain"
            found = grounding._by_looking(target.read_question("purple circle",
                                                               stated="region"), window)
            r.check("the judge chose the grey triangle for 'purple circle': refused",
                    found.candidate is None, str(found.candidate))
        with guard(r, "a malformed judge"):
            del asked[:]
            install("Z")
            grounding.NONE_POLICY = "abstain"
            found = grounding._by_looking(target.read_question("welfare loss region",
                                                               stated="region"), window)
            r.check("a letter that was not offered is not a choice", found.candidate is None)
        with guard(r, "a caption boxed by an area is still refused"):
            del asked[:]
            caption = {"text": "Consumer surplus", "x": 145.0, "y": 338.0,
                       "width": 255.0, "height": 186.0}
            install("A", anchor=caption)
            found = grounding._by_looking(target.read_question("consumer surplus",
                                                               stated="region"), window)
            r.check("an area that is only a box round its caption is not accepted",
                    found.candidate is None
                    or not target.boxed_the_caption(found.candidate, caption))
    finally:
        (display.capture_region, screen_context._for_vision, screen_context.read_image,
         grounding._crop_for, grounding._anchor_for, target.scope_for) = saved
        grounding.NONE_POLICY = "bbox"

    # ── V, W. What the user hears and what guidance gets ────────────────────
    r.head("V-W  the answer, spoken and drawn")
    with guard(r, "point_at on a chosen area"):
        from skills import perception, pointing
        picked = perception.Candidate(
            label="welfare loss region", role="region", x=440.0, y=567.0,
            width=59.0, height=84.0, confidence=0.85, sources=[target.REGION_SOURCE],
            source_confidence={target.REGION_SOURCE: 0.85},
            evidence=[grounding.PICKED + " 3 filled areas found in the picture itself",
                      "it looks like welfare loss region"])
        wanted = target.read_question("welfare loss region", stated="region")
        drawn = []
        saved = (grounding.ground, pointing.show, screen_context.frontmost_window)
        grounding.ground = lambda q, window=None, stated_kind="": grounding.Ground(
            target=wanted, candidate=picked, scope=target.WINDOW)
        pointing.show = lambda x, y, label="", seconds=3.0, marker=True, width=0.0, \
            height=0.0: drawn.append((x, y, width, height)) or ""
        screen_context.frontmost_window = lambda: {"id": 1, "app": "Preview", "x": 0.0,
                                                   "y": 0.0, "width": 1000.0,
                                                   "height": 800.0, "sensitive": False}
        try:
            answer = pointing.point_at("welfare loss region", kind="region")
        finally:
            grounding.ground, pointing.show, screen_context.frontmost_window = saved
        r.check("how it was found is not said to the user",
                "filled areas" not in answer.lower() and "picture itself" not in answer.lower(),
                answer)
        r.check("  but it is still in the record", grounding.PICKED in picked.evidence[0])
        r.check("guidance gets the area's extent, not a point",
                bool(drawn) and drawn[-1][2] == 59.0 and drawn[-1][3] == 84.0, str(drawn))

    # ── B.4.1. Where the first look is taken ───────────────────────────────
    r.head("B.4.1  a named region is looked for in the whole window first")
    with guard(r, "the ladder, by kind"):
        for words in ("where is the welfare loss region", "where is the consumer surplus",
                      "where is the producer surplus", "where is the blue shaded area"):
            wanted = target.read_question(words)
            ladder = target.scope_for(wanted)
            r.check(f"{words!r}: {wanted.kind}, first look {ladder[0]}",
                    wanted.kind == target.REGION and ladder[0] == target.WINDOW
                    and target.PANEL not in ladder, str(ladder))
        figure = target.read_question("what does this graph show")
        r.check("a whole figure too", target.scope_for(figure)[0] == target.WINDOW,
                str(target.scope_for(figure)))
        for words in ("what is this", "what am i pointing at"):
            ladder = target.scope_for(target.read_question(words))
            r.check(f"{words!r}: still the cursor first", ladder[0] == target.CURSOR,
                    str(ladder))
        r.check("a control keeps its own first look",
                target.scope_for(target.read_question("where is the Save button"))[0]
                == target.CONTROL_SCOPE)
        placed = target.read_question("the thing", stated="object")
        r.check("an object not at the pointer keeps the ladder it had",
                not placed.cursor_local
                and target.scope_for(placed) == (target.PANEL, target.WINDOW,
                                                 target.SCREEN_SCOPE),
                str(target.scope_for(placed)))
        r.check("the whole screen is still the last resort",
                target.scope_for(target.read_question("where is the welfare loss region"))[-1]
                == target.SCREEN_SCOPE)

    with guard(r, "text and controls never take a look at all"):
        routed = []
        saved = (grounding._by_perception, grounding._by_looking)
        grounding._by_perception = lambda wanted, window: routed.append(
            ("perception", wanted.kind)) or grounding.Ground(target=wanted, abstained=True)
        grounding._by_looking = lambda wanted, window: routed.append(
            ("looking", wanted.kind)) or grounding.Ground(target=wanted, abstained=True)
        try:
            for words in ("where is the note title", "where is the Save button"):
                grounding.ground(words, {"id": 1, "app": "X", "x": 0.0, "y": 0.0,
                                         "width": 800.0, "height": 600.0,
                                         "sensitive": False})
        finally:
            grounding._by_perception, grounding._by_looking = saved
        r.check("text and a control go to perception, where no crop ladder exists",
                routed == [("perception", target.TEXT), ("perception", target.CONTROL)],
                str(routed))

    with guard(r, "a caption far from its region does not decide the first crop"):
        saved = (display.capture_region, screen_context._for_vision,
                 screen_context.read_image, grounding._anchor_for)
        looks = []
        source = save_png(diagram(grain=8), "wholewindow.png")
        whole = {"id": 9, "app": "Preview", "title": "Subsidy", "x": 100.0, "y": 200.0,
                 "width": 852.0, "height": 632.0, "sensitive": False}

        def capture(x, y, w, h):
            looks.append((x, y, w, h))
            fresh = os.path.join(TMP, f"look_{len(looks)}.png")
            shutil.copy(source, fresh)
            return fresh

        display.capture_region = capture
        screen_context._for_vision = lambda p: (p, False)
        screen_context.read_image = lambda path, prompt, num_predict=0, schema=None: \
            json.dumps({"choice": "C"})
        # The legend entry, at the far right of the window: the shape it names
        # is on the left, as on the real price-floor page.
        grounding._anchor_for = lambda wanted, window: {
            "text": "Welfare loss", "left": 880.0, "top": 470.0, "width": 60.0,
            "height": 14.0, "x": 880.0, "y": 470.0}
        try:
            found = grounding._by_looking(
                target.read_question("where is the welfare loss region"), whole)
        finally:
            (display.capture_region, screen_context._for_vision,
             screen_context.read_image, grounding._anchor_for) = saved
        first = looks[0] if looks else None
        r.check("the first capture is the whole window, not a box round the caption",
                first is not None and abs(first[0] - 100.0) < 1 and abs(first[1] - 200.0) < 1
                and abs(first[2] - 852.0) < 1 and abs(first[3] - 632.0) < 1, str(looks))
        r.check("  one look was enough", len(looks) == 1, str(looks))
        c = found.candidate
        r.check("and the region found is the welfare loss, not its caption",
                c is not None and found.scope == target.WINDOW
                and abs(c.x - (100.0 + TRUTH["welfare"][0])) <= 3
                and abs(c.y - (200.0 + TRUTH["welfare"][1])) <= 3
                and c.x + c.width < 880.0, f"{found.scope} {c}")

    # ── R, S. Actions keep their own path ───────────────────────────────────
    r.head("R-S  a control is not looked for in the pixels")
    with guard(r, "B.3's action targets are untouched"):
        wanted = target.read_question("Show me how to open a new note.")
        r.check("an action is a CONTROL with its operation", wanted.kind == target.CONTROL
                and wanted.operation == target.CREATE)
        r.check("  and the area step declines it, so perception answers as before",
                grounding._pick_area(save_png(diagram(), "d2.png"), (852, 632), wanted,
                                     None, (0, 0, 852, 632)) is None)

    # ── X, Y. Safety ────────────────────────────────────────────────────────
    r.head("X-Y  reading pixels is not touching anything")
    with guard(r, "no input APIs"):
        banned = ("CGEventPost", "CGEventCreateMouseEvent", "CGEventCreateKeyboardEvent",
                  "CGWarpMouseCursorPosition", "setMouseLocation")
        for module in (regions, grounding):
            r.check(f"{module.__name__} posts nothing and moves nothing",
                    not any(b in open(module.__file__).read() for b in banned))
        r.check("computer use is off", computer_use.ENABLED is False)

    shutil.rmtree(TMP, ignore_errors=True)
    return r.report("grounding geometry")


if __name__ == "__main__":
    sys.exit(run())
