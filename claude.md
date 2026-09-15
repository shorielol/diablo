# Diablo — project instructions

Offline voice assistant for macOS. Wake word, local 4B (qwen3:4b-instruct,
14B on escalation), 39 tools, MCP client, per-window screen reading. Parent workstation: `~/Desktop/30 Code/`.

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


## Architecture invariants

These are the rules the system is built to. A change that breaks one of them is
a bug however well it tests, and `tests/test_architecture.py` asserts each one.

- **Diablo is the only permanent assistant, and owns every capability.** Asmo,
  Marbas, Vassago, Seere and Levi are names and faces, not departments. They
  carry a voice and an accent colour; they never carry a tool. `agent_core.run`
  takes no agent id, which is what makes this true rather than intended.
- **An agent is a task, not a department.** It exists for one piece of work and
  is labelled with that work. Diablo has the same capability before, during and
  after it.
- **Normal work never creates an agent.** Questions, conversation, arithmetic,
  short scripts, lookups, OS commands, media, screenshots, messages, one-step
  automation and small combinations of those all run on the main thread.
- **Delegation has exactly two causes:** he asked for an agent, or the task is
  genuinely substantial research. `delegation_tool.decide()` is the only place
  that decides, and it returns the reason with the route.
- **Uncertainty goes to Diablo.** A false delegation costs a spurious task, an
  announcement and a row in the rail. A false direct costs nothing, because
  Diablo can do it anyway. The fallback is DIRECT, in code.
- **Tool availability is not delegation intent.** `research` is on the tool menu
  for "what time is it", because its tags include "the" and "what". `decide()`
  never consults tool selection, and a test asserts it cannot.
- **Direct and delegated work are classified separately.** "Research this
  company and lower my volume" delegates the research and runs the volume here.
- **Inline actions are not tasks.** They write nothing to the job store, push no
  UI event, and never move conversational focus. The rail is for work worth
  reopening.
- **Task state is structured, not a transcript.** Sources, findings,
  calculations and the result — extracted once, when the tool output is still
  structured. Follow-ups never depend on replaying the model's hidden working.
- **One conversation, many tasks.** A single in-memory session plus per-task
  records. No second conversation store, no merge step.
- **Critical behaviour is enforced in code.** The 4B ignores prompt lines
  reproducibly, which is why the ambiguity guard, the unperformed-action guard,
  the no-reads-while-tidying guard and the routing gate are all code.
- **Errors reach him through one boundary.** `BrainOffline.spoken` is the only
  thing that may be said; `str(e)` is for the log. No status codes, bodies,
  paths or tracebacks in speech, ever.
