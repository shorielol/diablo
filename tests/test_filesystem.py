#!/usr/bin/env python3
"""
Every filesystem tool, executed for real, plus a deliberate attempt to break out
of the sandbox.

test_diablo.py proves these tools are registered and callable. It does not prove
they work — a tool that returns "Error: ..." for every input passes that suite.
This one writes real files in a real folder on the Desktop and reads them back.

The escape attempts matter most. Diablo is confined to Desktop, CoWork OS,
Documents and Downloads, and that confinement is one function: _resolve(). If it
can be walked out of with "../.." or a symlink, every file tool becomes a way to
read anything on the machine, so each vector is tried explicitly rather than
assumed.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, Sandbox, guard, DIABLO_HOME  # noqa: E402

from skills import agent_tools as T  # noqa: E402

r = Results()


def run():
    with Sandbox() as box:
        # ── the basics, for real ────────────────────────────────────────────
        r.head("Reading and writing")

        with guard(r, "write_file creates a file"):
            out = T.write_file(box.rel("notes.txt"), "line one")
            r.check("write_file reports success", "Error" not in out, out[:60])
            r.check("the file is actually on disk",
                    os.path.exists(os.path.join(box.path, "notes.txt")))

        with guard(r, "read_file returns the content"):
            got = T.read_file(box.rel("notes.txt"))
            r.check("read_file round-trips", "line one" in got, repr(got[:40]))

        with guard(r, "append_file adds without truncating"):
            T.append_file(box.rel("notes.txt"), "\nline two")
            got = T.read_file(box.rel("notes.txt"))
            r.check("append kept the original", "line one" in got and "line two" in got,
                    repr(got[:40]))

        with guard(r, "write_file overwrites"):
            T.write_file(box.rel("notes.txt"), "replaced")
            got = T.read_file(box.rel("notes.txt"))
            r.check("overwrite really replaces", "line one" not in got and "replaced" in got)

        with guard(r, "list_files sees them"):
            box.file("a.md", "alpha")
            box.file("b.md", "bravo")
            out = T.list_files(box.rel())
            r.check("list_files finds every file",
                    all(n in out for n in ("notes.txt", "a.md", "b.md")), out[:70].replace("\n", " "))

        with guard(r, "make_folder"):
            out = T.make_folder(box.rel("sub/deeper"))
            r.check("make_folder creates nested paths",
                    os.path.isdir(os.path.join(box.path, "sub", "deeper")), out[:60])

        with guard(r, "move_file"):
            T.move_file(box.rel("a.md"), box.rel("sub/a.md"))
            r.check("move_file moved it",
                    os.path.exists(os.path.join(box.path, "sub", "a.md"))
                    and not os.path.exists(os.path.join(box.path, "a.md")))

        with guard(r, "find_files"):
            out = T.find_files("b.md", box.rel())
            r.check("find_files locates by name", "b.md" in out, out[:70].replace("\n", " "))

        with guard(r, "search_text"):
            box.file("hay.txt", "needle in here somewhere")
            out = T.search_text("needle", box.rel())
            r.check("search_text finds content", "hay.txt" in out, out[:70].replace("\n", " "))

        with guard(r, "search_text on a miss"):
            out = T.search_text("zzzznotpresentzzz", box.rel())
            r.check("a miss says so rather than erroring",
                    "Error" not in out, out[:70].replace("\n", " "))

        # ── error handling, not crashes ─────────────────────────────────────
        r.head("Missing things are reported, not raised")

        for label, fn, arg in [
            ("read_file on a missing file", T.read_file, box.rel("nope.txt")),
            ("list_files on a missing folder", T.list_files, box.rel("nofolder")),
            ("move_file with a missing source", None, None),
            ("delete_file on a missing file", T.delete_file, box.rel("ghost.txt")),
        ]:
            if fn is None:
                with guard(r, label):
                    out = T.move_file(box.rel("ghost.txt"), box.rel("x.txt"))
                    r.check(label, isinstance(out, str) and "Error" in out, out[:60])
                continue
            with guard(r, label):
                out = fn(arg)
                r.check(label, isinstance(out, str) and "Error" in out, out[:60])

        with guard(r, "read_file on a directory"):
            out = T.read_file(box.rel("sub"))
            r.check("read_file on a directory is handled",
                    isinstance(out, str) and "Error" in out, out[:60])

        with guard(r, "read_file on binary"):
            p = os.path.join(box.path, "bin.dat")
            with open(p, "wb") as f:
                f.write(bytes(range(256)) * 4)
            out = T.read_file(box.rel("bin.dat"))
            r.check("binary does not raise", isinstance(out, str), type(out).__name__)

        with guard(r, "read_file clips huge files"):
            box.file("big.txt", "x" * 200000)
            out = T.read_file(box.rel("big.txt"))
            r.check("output is clipped to the model budget",
                    len(out) <= T.MAX_TOOL_OUTPUT + 200, f"{len(out)} chars")

        # ── the confinement boundary ────────────────────────────────────────
        r.head("The sandbox boundary holds")

        escapes = [
            ("/etc/passwd",                      "absolute system path"),
            ("/etc/hosts",                       "another system path"),
            ("~/.ssh/id_rsa",                    "ssh private key"),
            ("~/.diablo/.env",                   "Diablo's own secrets"),
            ("../../../etc/passwd",              "relative traversal"),
            ("desktop/../../../etc/passwd",      "traversal via a workspace"),
            ("~/Library/Keychains",              "the keychain"),
            ("/var/db/SystemPolicy",             "system policy database"),
            ("~/Library/Messages/chat.db",       "the iMessage database"),
            ("/System/Library",                  "the system volume"),
        ]
        for path, why in escapes:
            with guard(r, f"blocked: {why}"):
                try:
                    resolved = T._resolve(path)
                    r.bad(f"blocked: {why}", f"ESCAPED to {resolved}")
                except ValueError:
                    r.ok(f"blocked: {why}")

        with guard(r, "symlink escape"):
            link = os.path.join(box.path, "escape")
            if os.path.islink(link):
                os.unlink(link)
            os.symlink("/etc", link)
            try:
                resolved = T._resolve(box.rel("escape/passwd"))
                r.bad("symlink out of the sandbox is blocked", f"ESCAPED to {resolved}")
            except ValueError:
                r.ok("symlink out of the sandbox is blocked")
            os.unlink(link)

        # Through execute(), which is the only path the agent ever uses. The
        # tools themselves raise ValueError; execute() is what turns that into
        # a sentence the model can read. Calling the tool directly and calling
        # it the way Diablo does are different tests, and the second is the one
        # that matters.
        with guard(r, "read_file refuses the escape end-to-end"):
            out = T.execute("read_file", {"path": "/etc/passwd"})
            r.check("read_file refuses rather than resolving",
                    "outside the folders" in out, out[:60])
            r.check("and leaks no content", "root:" not in out)

        with guard(r, "write_file cannot escape"):
            out = T.execute("write_file", {"path": "/tmp/diablo_should_not_exist.txt",
                                           "content": "nope"})
            r.check("write_file refuses an outside path",
                    "Error" in out or "outside" in out, out[:60])
            r.check("and wrote nothing",
                    not os.path.exists("/tmp/diablo_should_not_exist.txt"))

        with guard(r, "every escape is contained by execute()"):
            leaked = []
            for path, why in escapes:
                out = T.execute("read_file", {"path": path})
                if not isinstance(out, str) or "Error" not in out:
                    leaked.append(why)
            r.check("execute() contains all of them", not leaked, ", ".join(leaked) or "none leaked")

        with guard(r, "allowed roots still work"):
            for nickname in ("desktop", "documents", "downloads", "cowork"):
                try:
                    T._resolve(nickname)
                    r.ok(f"'{nickname}' resolves")
                except ValueError as e:
                    r.bad(f"'{nickname}' resolves", str(e)[:60])

        with guard(r, "empty path"):
            try:
                T._resolve("")
                r.bad("an empty path is rejected", "it resolved")
            except ValueError:
                r.ok("an empty path is rejected")

        # ── deletion goes to the Trash ──────────────────────────────────────
        r.head("Deletion is recoverable")

        T.CONTEXT["confirm"] = lambda q: True
        try:
            with guard(r, "delete_file trashes"):
                target = box.file("trash-me.txt", "recoverable")
                out = T.delete_file(box.rel("trash-me.txt"))
                time.sleep(1.2)
                r.check("delete_file reports the Trash", "Trash" in out, out[:60])
                r.check("the file left the sandbox", not os.path.exists(target))
                trashed = os.path.expanduser("~/.Trash/trash-me.txt")
                r.check("and landed in the Trash, not deleted", os.path.exists(trashed),
                        trashed)
                # Emptying it needs Full Disk Access, which the test runner does
                # not have and should not need. Leave it; it is one 11-byte file
                # in the Trash and its presence is the thing being asserted.

            T.CONTEXT["confirm"] = lambda q: False
            with guard(r, "delete_file honours a refusal"):
                target = box.file("keep-me.txt", "safe")
                out = T.delete_file(box.rel("keep-me.txt"))
                r.check("a 'no' cancels the delete", "Cancelled" in out, out[:60])
                r.check("and the file is untouched", os.path.exists(target))
        finally:
            T.CONTEXT["confirm"] = None

    return r.report("filesystem")


if __name__ == "__main__":
    sys.exit(run())
