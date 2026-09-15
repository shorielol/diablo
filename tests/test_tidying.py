"""
Tidying a folder without reading what is in it.

Asked to tidy the Downloads folder, Diablo said:

    "I'm reading the ESS IA - FDI and PM2.5 Final Draft.md file to understand
     the project's current state."

It opened a piece of his coursework to decide where to put a disk image. That
is a coding agent's reflex — orient in the repo, read the files, then act — and
it is wrong twice on a personal folder. Useless, because where a file goes is
decided by its extension. And a privacy failure, because nobody tidying a
drawer reads the letters.

The important test here is not that the sorting is correct. It is that no file
is opened, and that is checked by watching open() itself rather than by reading
the code and believing it.
"""

import builtins
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import agent_core, agent_tools  # noqa: E402

SAMPLE = {
    "ESS IA - FDI and PM2.5 Final Draft.md": "SECRET COURSEWORK CONTENTS",
    "Econ EE draft.pdf": "%PDF-1.4 private",
    "Antigravity IDE.dmg": "binary",
    "ProdKeys.zip": "binary",
    "screenshot.png": "binary",
    "budget.xlsx": "binary",
    "notes.txt": "private notes",
    "script.py": "print('hi')",
    "song.mp3": "binary",
    "mystery.qqq": "unknown kind",
}


class WatchOpens:
    """Every path opened for reading while this is active."""

    def __enter__(self):
        self.opened = []
        self._real = builtins.open

        def spy(path, mode="r", *a, **k):
            if "r" in str(mode) and "w" not in str(mode):
                self.opened.append(str(path))
            return self._real(path, mode, *a, **k)

        builtins.open = spy
        return self

    def __exit__(self, *exc):
        builtins.open = self._real


def make_folder(root):
    for name, content in SAMPLE.items():
        with open(os.path.join(root, name), "w") as f:
            f.write(content)
    os.makedirs(os.path.join(root, "an existing folder"), exist_ok=True)
    old = time.time() - 60 * 86400
    for name in ("Antigravity IDE.dmg", "ProdKeys.zip"):
        os.utime(os.path.join(root, name), (old, old))


def main():
    r = Results()
    root = tempfile.mkdtemp(prefix="diablo-tidy-", dir=os.path.expanduser("~/Downloads"))
    try:
        make_folder(root)

        # ── 1. Surveying opens nothing ───────────────────────────────────
        r.head("Looking at a folder")
        with WatchOpens() as watch:
            survey = agent_tools.survey_folder(root)
        sample = [p for p in watch.opened if os.path.dirname(p) == root]
        r.check("not one file in the folder was opened", not sample,
                f"opened {sample}")
        r.check("the coursework in particular stayed shut",
                not any("ESS IA" in p for p in watch.opened),
                "this is the exact file it read last time")
        r.check("it still counted them by kind",
                "Images" in survey and "Installers" in survey, survey[:90])
        r.check("and spotted what is finished with",
                "Antigravity IDE.dmg" in survey,
                "a 60-day-old disk image is the clearest thing to clear out")

        # ── 2. Sorting opens nothing either ──────────────────────────────
        r.head("Sorting it out")
        with WatchOpens() as watch:
            dry = agent_tools.tidy_folder(root, dry_run=True)
        sample = [p for p in watch.opened if os.path.dirname(p) == root]
        r.check("a dry run opens nothing", not sample, f"opened {sample}")
        r.check("it says what it would do", "Ready to move" in dry, dry[:80])
        r.check("and has not touched anything yet",
                os.path.exists(os.path.join(root, "screenshot.png")))

        agent_tools.CONTEXT["confirm"] = lambda q: True
        with WatchOpens() as watch:
            done = agent_tools.tidy_folder(root, dry_run=False)
        sample = [p for p in watch.opened if root in p and os.path.isfile(p)]
        r.check("the real move opens nothing", not sample, f"opened {sample}")
        r.check("files went where they belong",
                os.path.exists(os.path.join(root, "Images", "screenshot.png"))
                and os.path.exists(os.path.join(root, "Installers", "Antigravity IDE.dmg"))
                and os.path.exists(os.path.join(root, "Documents", "notes.txt")),
                done)
        r.check("an unrecognised kind is left exactly where it was",
                os.path.exists(os.path.join(root, "mystery.qqq")),
                "guessing at a file it cannot classify is how things get lost")
        r.check("and so is a folder he made himself",
                os.path.isdir(os.path.join(root, "an existing folder")))

        # ── 3. Nothing is overwritten to tidy up ─────────────────────────
        r.head("A name that is already taken")
        with open(os.path.join(root, "notes.txt"), "w") as f:
            f.write("A DIFFERENT FILE WITH THE SAME NAME")
        agent_tools.tidy_folder(root, dry_run=False)
        with open(os.path.join(root, "Documents", "notes.txt")) as f:
            r.check("the first one survives untouched",
                    f.read() == "private notes",
                    "tidying may never destroy one of his files")
        r.check("and the second is left alone rather than lost",
                os.path.exists(os.path.join(root, "notes.txt")))
    finally:
        agent_tools.CONTEXT["confirm"] = None
        shutil.rmtree(root, ignore_errors=True)

    # ── 4. The request is recognised ─────────────────────────────────────
    r.head("Which requests are a tidy-up")
    for task in ["tidy my downloads folder", "clean up my downloads",
                 "organise my desktop", "sort out the downloads folder",
                 "declutter my desktop", "clear out my downloads",
                 "free up space in downloads"]:
        r.check(f"{task!r} is housekeeping", agent_core.is_housekeeping(task))

    for task in ["tidy up the wording of my essay", "clean up the code in that script",
                 "read my ess draft", "what is in my downloads folder",
                 "summarise my notes", "find my econ ee"]:
        r.check(f"{task!r} is not", not agent_core.is_housekeeping(task),
                "these genuinely need the file open")

    # ── 5. The tools are taken away, and refused if asked for ────────────
    r.head("Reading tools during a tidy-up")
    offered = agent_tools.select("tidy my downloads folder")
    r.check("survey_folder and tidy_folder are offered first",
            offered[:2] == ["tidy_folder", "survey_folder"], f"{offered[:3]}")
    kept = [n for n in offered if n not in agent_core.CONTENT_TOOLS]
    r.check("read_file is removed from what the model can see",
            "read_file" not in kept,
            "it was on the menu, which is most of why it reached for it")
    r.check("but it is still there for ordinary work",
            "read_file" in agent_tools.select("read my econ ee draft"))

    return r.report("tidying")


if __name__ == "__main__":
    sys.exit(main())
