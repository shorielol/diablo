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

    # ── Asking for a permission ─────────────────────────────────────────
    r.head("Permissions are asked for, not filed under Settings")
    js = open(os.path.join(DIABLO_HOME, "app", "app.js")).read()
    css = open(os.path.join(DIABLO_HOME, "app", "styles.css")).read()

    r.check("there is a compact request in the conversation",
            "const Needs = memo" in js and "<${Needs}" in js,
            "a settings dashboard is where you check a permission, not where "
            "you are asked for one")
    r.check("it speaks rather than naming a toggle",
            "I need screen access to see what you are seeing." in js,
            "not 'SCREEN RECORDING: DENIED'")
    r.check("one request at a time, most blocking first",
            "const PERM_ORDER = ['screen', 'mic', 'accessibility'];" in js,
            "three prompts on first run is the onboarding clutter to avoid")
    r.check("it uses the permission names the backend actually reports",
            "mic:" in js and "microphone:" not in js.split("PERM_ASK")[1][:300],
            "permissions.snapshot() returns accessibility, screen and mic")
    r.check("an undetermined permission does not trigger a request",
            "perms[name] === false" in js,
            "snapshot reports null for what macOS will not disclose, and 'I "
            "cannot tell' is not a reason to interrupt someone")
    r.check("the grant path is shared with the diagnostic page",
            "function useGrant(" in js and js.count("action: `grant:${name}`") == 1,
            "two copies would drift the moment macOS changes when it shows a dialog")
    r.check("the button says what pressing it does",
            "'Allow screen access'" in js and "Allow ${PERM_LABELS" not in js,
            "concatenation produced 'Allow Screen recording'")
    r.check("the request is styled without a card around it",
            ".needs {" in css and "grantbtn" in css)
    r.check("its button has a visible focus ring",
            ".grantbtn:focus-visible {" in css,
            "keyboard users need to see where they are")

    # ── One accent, his own ─────────────────────────────────────────────
    r.head("One palette, and it is his")
    shell = open(os.path.join(DIABLO_HOME, "app_shell.py")).read()
    # GUI-3.5 took colour out of the state entirely: the orb's mark is the
    # label colour and its shape is the state, so a state colour in here
    # would be a second, contradicting language. Red means a fault, in both
    # surfaces, and nothing else does.
    r.check("the surface keeps no colour of its own for a state",
            not any(token in css for token in
                    ("--listening:", "--thinking:", "--speaking:", "--idle:")),
            "state is the mark's shape and a word, in both surfaces")
    r.check("red is still reserved for a fault", "--error:" in css)
    r.check("and the one primary action is the text colour, not a hue",
            "background: var(--text); color: var(--bg);" in css)
    r.check("blue is no longer used for any state",
            "(BLUE," not in shell, "BLUE stays defined but unused by STATE_LOOK")

    return r.report("rail")


if __name__ == "__main__":
    sys.exit(main())
