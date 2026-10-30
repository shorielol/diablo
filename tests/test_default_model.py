#!/usr/bin/env python3
"""
A Diablo with no configuration runs qwen3.5:4b, and nothing else.

Until 2026-10-05 it ran the 14B: `local_brain` fell back to qwen3:14b when
DIABLO_MODEL was unset, `diablo doctor` then told a new user to pull it, and
tools/install.sh checked for it. The live install never showed this because
its .env names a 4B, so every test that loaded .env passed.

These checks therefore build their own empty home in a temp folder and run
each piece in a clean environment: no .env, no DIABLO_* variables, HOME
pointing at the sandbox. The 14B, Qwen2.5-VL and Fara are not part of the
architecture at all: nothing resolves to them, and the variables that used to
reach them are no longer read.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "diablo")
INSTALLER = os.path.join(REPO, "tools", "install.sh")

# Changing the default brain is allowed, but it has to be done on purpose:
# here as well as in local_brain.DEFAULT_BRAIN_MODEL.
EXPECTED_DEFAULT = "qwen3.5:4b"
EXPECTED_CONTEXT = 32768

# The variables that pick a model. The two that used to reach a second model
# are listed so that a getenv of either, anywhere, fails the scan below.
BRAIN_KEYS = ("DIABLO_MODEL", "DIABLO_FAST_MODEL")
RETIRED_KEYS = ("DIABLO_ESCALATE_MODEL", "DIABLO_VISION_MODEL")
# What a retired model is called in a value, a tag or a log line.
RETIRED_NAMES = ("qwen3:14b", "qwen2.5vl", "qwen2.5-vl", "fara")


def _is_14b(name: str) -> bool:
    return "14b" in (name or "").lower()


def _is_retired(name: str) -> bool:
    lowered = (name or "").lower()
    return "14b" in lowered or any(r in lowered for r in RETIRED_NAMES)


def _fresh_home(base: str) -> str:
    """An install as it would look on a new Mac: skills/, a venv, no .env."""
    home = os.path.join(base, "home")
    skills = os.path.join(home, "skills")
    os.makedirs(skills)
    open(os.path.join(skills, "__init__.py"), "w").close()
    for module in ("local_brain.py", "config.py", "doctor.py", "goclick.py"):
        shutil.copy(os.path.join(SOURCE, module), skills)
    os.makedirs(os.path.join(home, "venv", "bin"))
    os.symlink(sys.executable, os.path.join(home, "venv", "bin", "python3"))
    return home


def _clean_env(home: str, **extra: str) -> dict:
    """Nothing inherited: a stray DIABLO_MODEL in this shell would hide the bug."""
    env = {"HOME": home, "DIABLO_HOME": home,
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    env.update(extra)
    return env


def _resolve(home: str, cwd: str, **extra: str) -> dict:
    """CHAT_MODEL and friends as a fresh process resolves them."""
    code = ("import json, os, sys; sys.path.insert(0, os.environ['DIABLO_HOME'])\n"
            "from skills import local_brain as b\n"
            "print(json.dumps({'chat': b.CHAT_MODEL, 'fast': b.FAST_MODEL,\n"
            "  'default': b.DEFAULT_BRAIN_MODEL, 'ctx': b.NUM_CTX}))")
    done = subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True,
                          text=True, timeout=30, env=_clean_env(home, **extra))
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[-300:])
    return json.loads(done.stdout.strip().splitlines()[-1])


def _model_getenvs(path: str):
    """Every getenv / environ.get / setdefault of a model variable, with its default."""
    tree = ast.parse(open(path).read(), filename=path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name not in ("getenv", "get", "setdefault"):
            continue
        key = node.args[0]
        if isinstance(key, ast.Constant) and key.value in BRAIN_KEYS + RETIRED_KEYS:
            yield key.value, (node.args[1] if len(node.args) > 1 else None), node.lineno


def _string_values(path: str):
    """String constants that are values, not docstrings."""
    tree = ast.parse(open(path).read(), filename=path)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.value, node.lineno


def _runtime_files():
    for root in (SOURCE, REPO):
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def run():
    r = Results()
    base = tempfile.mkdtemp(prefix="diablo-default-model-")
    try:
        home = _fresh_home(base)
        elsewhere = os.path.join(base, "elsewhere")
        os.makedirs(elsewhere)

        r.head("test_fresh_install_defaults_to_qwen35")
        with guard(r, "the brain resolves without any configuration"):
            got = _resolve(home, elsewhere)
            r.check("there is no .env in the fresh home",
                    not os.path.exists(os.path.join(home, ".env")))
            r.check("DEFAULT_BRAIN_MODEL is qwen3.5:4b", got["default"] == EXPECTED_DEFAULT,
                    got["default"])
            r.check("CHAT_MODEL is qwen3.5:4b", got["chat"] == EXPECTED_DEFAULT, got["chat"])
            r.check("FAST_MODEL is qwen3.5:4b", got["fast"] == EXPECTED_DEFAULT, got["fast"])
            r.check("the context is 32768", got["ctx"] == EXPECTED_CONTEXT, str(got["ctx"]))
        with guard(r, "a blank setting is no setting"):
            got = _resolve(home, elsewhere, DIABLO_MODEL="", DIABLO_FAST_MODEL=" ")
            r.check("DIABLO_MODEL= falls back to qwen3.5:4b, not an empty name",
                    got["chat"] == EXPECTED_DEFAULT, repr(got["chat"]))
            r.check("DIABLO_FAST_MODEL= falls back to the brain", got["fast"] == got["chat"],
                    repr(got["fast"]))
        with guard(r, "an explicit choice is still honoured"):
            got = _resolve(home, elsewhere, DIABLO_MODEL="some-model:7b")
            r.check("DIABLO_MODEL picks the brain", got["chat"] == "some-model:7b", got["chat"])
            r.check("and the fast model follows it", got["fast"] == "some-model:7b", got["fast"])

        r.head("test_fresh_install_never_defaults_to_14b")
        with guard(r, "no slot is a 14B, and the old escalation setting does nothing"):
            got = _resolve(home, elsewhere)
            r.check("no brain slot is a 14B",
                    not any(_is_14b(got[k]) for k in ("chat", "fast", "default")))
            got = _resolve(home, elsewhere, DIABLO_ESCALATE_MODEL="qwen3:14b")
            r.check("DIABLO_ESCALATE_MODEL=qwen3:14b leaves both slots on qwen3.5:4b",
                    got["chat"] == got["fast"] == EXPECTED_DEFAULT, str(got))

        r.head("test_fresh_install_never_defaults_to_qwen25vl")
        with guard(r, "images are read by the brain, and the old vision setting does nothing"):
            got = _resolve(home, elsewhere, DIABLO_VISION_MODEL="qwen2.5vl:7b")
            r.check("DIABLO_VISION_MODEL=qwen2.5vl:7b leaves the brain on qwen3.5:4b",
                    got["chat"] == EXPECTED_DEFAULT, got["chat"])
            sc = open(os.path.join(SOURCE, "screen_context.py")).read()
            r.check("screen_context asks the brain model about images",
                    "model=local_brain.CHAT_MODEL" in sc and "VISION_MODEL" not in sc)
            grounding = open(os.path.join(SOURCE, "grounding.py")).read()
            r.check("grounding has no Qwen2.5-VL box space",
                    "BOX_SPACES" in grounding and '("qwen2.5' not in grounding)

        r.head("no other path can name a model")
        with guard(r, "only local_brain resolves the brain, and nothing reads the retired keys"):
            defaults, retired = [], []
            for path in _runtime_files():
                for key, default, line in _model_getenvs(path):
                    where = f"{os.path.relpath(path, REPO)}:{line} {key}"
                    if key in RETIRED_KEYS:
                        retired.append(where)
                        continue
                    # "" is no default at all: local_brain turns it into qwen3.5:4b.
                    empty = isinstance(default, ast.Constant) and default.value == ""
                    if default is not None and not empty:
                        defaults.append(where)
            r.check("no getenv of a brain variable carries a default", not defaults,
                    "; ".join(defaults))
            r.check("no module reads DIABLO_ESCALATE_MODEL or DIABLO_VISION_MODEL",
                    not retired, "; ".join(retired))
        with guard(r, "no runtime module names a retired model as a value"):
            named = []
            for path in _runtime_files():
                # bench.py compares whichever models it is told to compare.
                if os.path.basename(path) == "bench.py":
                    continue
                for value, line in _string_values(path):
                    lowered = value.lower()
                    if any(lowered.startswith(r) for r in RETIRED_NAMES):
                        named.append(f"{os.path.relpath(path, REPO)}:{line}")
            r.check("none", not named, ", ".join(named))

        r.head("doctor reports the same default")
        with guard(r, "doctor's model and grounding checks, run from outside the home"):
            code = ("import sys; sys.path.insert(0, %r)\n"
                    "import doctor; doctor.check_brain(); doctor.check_grounder()"
                    % os.path.join(home, "skills"))
            done = subprocess.run([sys.executable, "-c", code], cwd=elsewhere,
                                  capture_output=True, text=True, timeout=60,
                                  env=_clean_env(home))
            out = done.stdout + done.stderr
            if "not responding" in out:
                r.skip("doctor names qwen3.5:4b", "Ollama is not running")
            else:
                r.check("doctor could query the brain from another folder",
                        "could not query" not in out and "could not read" not in out,
                        out.strip()[-160:])
                r.check("doctor wants qwen3.5:4b, at context 32768",
                        EXPECTED_DEFAULT in out and str(EXPECTED_CONTEXT) in out,
                        out.strip()[-160:])
                r.check("doctor never asks for a retired model", not _is_retired(out))
            r.check("doctor says what grounding has", "GoClick" in out, out.strip()[-120:])

        r.head("the installer checks the same default")
        with guard(r, "install.sh --check in the fresh home"):
            code_lines = [line for line in open(INSTALLER).read().lower().splitlines()
                          if not line.strip().startswith("#")]
            r.check("install.sh names no model of its own",
                    not any(_is_retired(line) or "qwen3:4b" in line for line in code_lines))
            # An installer without --check ignores the flag and installs. Run
            # against the old one, this went on to its test step inside the
            # sandbox, so it is not run at all unless it says it can stop.
            if 'CHECK_ONLY' not in open(INSTALLER).read():
                r.bad("install.sh supports --check", "not run: it would install")
                return r.report("default model")
            before = sorted(os.listdir(home))
            done = subprocess.run(["bash", INSTALLER, "--check"], capture_output=True,
                                  text=True, timeout=60, env=_clean_env(home))
            out = done.stdout + done.stderr
            if "not responding" in out:
                r.skip("install.sh names qwen3.5:4b", "Ollama is not running")
            else:
                r.check("install.sh resolves qwen3.5:4b", EXPECTED_DEFAULT in out,
                        out.strip().splitlines()[-1] if out.strip() else "")
                r.check("install.sh never asks for a retired model", not _is_retired(out))
            r.check("--check wrote nothing and pulled nothing",
                    sorted(os.listdir(home)) == before)
            done = subprocess.run(["bash", INSTALLER, "--check"], capture_output=True,
                                  text=True, timeout=60,
                                  env=_clean_env(home, DIABLO_MODEL="no-such-model:1b"))
            out = done.stdout + done.stderr
            if "not responding" not in out:
                # The old check compared only the text before the colon.
                r.check("a model that is not installed fails the check",
                        done.returncode != 0 and "not installed" in out, out.strip()[-120:])
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return r.report("default model")


if __name__ == "__main__":
    sys.exit(run())
