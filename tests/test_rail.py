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
    return r.report("rail")


if __name__ == "__main__":
    sys.exit(main())
