# Diablo — project instructions

Offline voice assistant for macOS. Wake word, local 14B, 33 tools, MCP client,
per-window screen reading. Parent workstation: `~/Desktop/30 Code/`.

## Where things live

| Path | What |
|---|---|
| `~/Desktop/30 Code/35 Diablo/` | the repository. Source of truth. Edit here. |
| `~/.diablo/` | the install target. Runtime state, venv, logs, caches. |
| `~/.diablo/.git.openclaw-backup/` | the openclaw history this tree inherited. Not Diablo's. |

Never edit `~/.diablo/skills/*.py` directly and expect it to survive. The
installer copies from `diablo/` in the repo into `~/.diablo/skills/`.

## Rules for this project

- **One module, one instance.** Several modules are currently imported twice,
  once as `skills.x` and once as bare `x`, with separate globals. Do not add
  more bare imports. Use the package path.
- **Never let a failure be silent.** Two shipped bugs were a dependency
  vanishing with no symptom but silence. Every `except` either re-raises or
  says something out loud.
- **Arithmetic goes through `python_snippet`,** never through the model. A 14B
  asked to total three rows returned 350 for a figure that was 115.
- **Humour lives in conversation, never in a task report.**
- **Conversation mode has no tools,** and the prompt must keep saying so. It
  once claimed to have moved a launch date and emailed a team. It had not.
- Destructive actions ask first. Deletes go to the Trash.
- Test against the disk, not against what Diablo said about the disk.

## Running the tests

```bash
python3 tests/run_all.py           # everything safe to run unattended
python3 tests/run_all.py --fast    # skip the suites that call the model
```

`DIABLO_TEST_DEVICE=1` enables the real-microphone tests, and only works from a
terminal that already holds a microphone grant. `DIABLO_TEST_INTRUSIVE=1`
enables the tests that open apps and browser tabs.
