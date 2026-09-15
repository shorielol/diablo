"""
The sidebar, against real job payloads.

The rail used to render one row per agent from a fixed array, so it showed six
names whether or not any of them had ever done anything — five rows of
furniture, and the one that mattered looked exactly like the other five. It now
lists Diablo, which is permanent, and then the work.

That logic is worth testing rather than looking at, because "looks right" is
how the last UI bug survived. railRows() is exported from app.js as a pure
function for exactly this, and the checks run in node against the payloads the
job store really emits.
"""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "js", "rail_rows.mjs")
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))


def main():
    r = Results()
    r.head("The rail")

    node = shutil.which("node")
    if not node:
        r.skip("rail rows", "node is not installed")
        return r.report("rail")
    if not os.path.exists(os.path.join(DIABLO_HOME, "app", "app.js")):
        r.skip("rail rows", "app.js is not installed")
        return r.report("rail")

    run = subprocess.run([node, SCRIPT], capture_output=True, text=True,
                         env={**os.environ, "DIABLO_HOME": DIABLO_HOME},
                         timeout=60)
    output = (run.stdout + run.stderr).strip()
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("ok   "):
            r.ok(stripped[5:])
        elif stripped.startswith("FAIL "):
            r.bad(stripped[5:])
    r.check("node ran the rail checks cleanly", run.returncode == 0,
            output[-300:] if run.returncode else "")
    # ── The stylesheet parses ───────────────────────────────────────────
    #
    # Not pedantry. An unbalanced brace does not fail loudly in CSS: the
    # parser skips to the next rule it can understand and silently drops
    # everything between. One stray "}" left behind by an edit took the
    # rail's full height away, and the only symptom was a sidebar that
    # stopped two-thirds of the way down a screenshot.
    r.head("The stylesheet")
    css_path = os.path.join(DIABLO_HOME, "app", "styles.css")
    try:
        css = open(css_path).read()
    except OSError as e:
        r.bad("styles.css can be read", str(e))
        css = ""
    if css:
        opens, closes = css.count("{"), css.count("}")
        r.check("every block in styles.css is closed", opens == closes,
                f"{opens} open, {closes} close")
        r.check("there is a narrow-window layout", "max-width: 719px" in css,
                "Diablo opens at 392 points; the rail returns above 720")
        r.check("the rail is hidden in it", ".rail { display: none; }" in css,
                "one surface below 720, two above")
        r.check("the compact head does not reserve traffic-light room it does not need",
                ".head { padding-left: 78px; }" not in css,
                "WKWebView already insets the page below the titlebar")
        r.check("every animation the redesign added respects reduced motion",
                ".runrule i { animation: none;" in css,
                "the working rule stops moving but stays visible; removing it "
                "would take away the only sign that anything is running")
        r.check("no oversized character is left in the empty state",
                "size=${188}" not in open(
                    os.path.join(DIABLO_HOME, "app", "app.js")).read(),
                "the hero mascot was 188px of wallpaper on an empty surface")

    return r.report("rail")


if __name__ == "__main__":
    sys.exit(main())
