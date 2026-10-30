#!/usr/bin/env python3
"""
Restart recovery, with a real process and a real kill.

A child process submits two jobs to a JobStore in a temporary folder: the first
starts and blocks, the second waits behind it. Once the first is running the
child is killed with SIGKILL, so nothing gets to clean up. A second process then
imports delegation_tool the way startup does and reports what it finds, in
memory and on disk, and once more after a further restart.

    DIABLO_HOME=<a scratch home> python3 restart_probe.py

Never point DIABLO_HOME at a live install: the store is kept in a temp folder
(DIABLO_BASE_DIR), but the home's code is what runs.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

CHILD = r'''
import os, sys, time
sys.path.insert(0, os.environ["DIABLO_HOME"])
from skills import agent_core, conversation, delegation_tool as dt
dt._push = lambda *a, **k: None
conversation.session.record_task = lambda *a, **k: None
def held(task, speak, **k):
    open(os.environ["DIABLO_BASE_DIR"] + "/running.flag", "w").close()
    time.sleep(600)
agent_core.run = held
dt.submit("first job", lambda *a, **k: None, pieces=[("first job", "asmo")])
dt.submit("second, still queued", lambda *a, **k: None, pieces=[("second, still queued", "asmo")])
time.sleep(600)
'''

AFTER = r'''
import json, os, sys
sys.path.insert(0, os.environ["DIABLO_HOME"])
from skills import delegation_tool as dt
print("store in the temp folder:", dt.JOBS_PATH.startswith(os.environ["DIABLO_BASE_DIR"]))
print("after restart, in memory:", [(j["task"], j["status"]) for j in dt.store.recent(10)])
print("after restart, on disk:  ", [(j["task"], j["status"]) for j in json.load(open(dt.JOBS_PATH))])
print("after a second restart:  ", [(j["task"], j["status"]) for j in dt.JobStore(dt.JOBS_PATH).recent(10)])
'''


def main() -> int:
    home = os.environ.get("DIABLO_HOME", "")
    if not home or os.path.realpath(home) == os.path.realpath(os.path.expanduser("~/.diablo")):
        print("Set DIABLO_HOME to a scratch home, not the live install.")
        return 2
    base = tempfile.mkdtemp(prefix="diablo-restart-")
    env = dict(os.environ, DIABLO_BASE_DIR=base,
               DIABLO_TRACE=os.path.join(base, "trace.jsonl"))
    child = subprocess.Popen([sys.executable, "-c", CHILD], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    flag = os.path.join(base, "running.flag")
    for _ in range(600):
        if os.path.exists(flag):
            break
        time.sleep(0.1)
    else:
        child.kill()
        print("the first job never started")
        return 1
    time.sleep(0.5)
    jobs = os.path.join(base, "jobs.json")
    print("before the kill, on disk:", [(j["task"], j["status"]) for j in json.load(open(jobs))])
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    print("child killed with SIGKILL, exit", child.returncode)
    out = subprocess.run([sys.executable, "-c", AFTER], env=env, capture_output=True, text=True)
    print(out.stdout.strip() or out.stderr.strip()[-400:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
