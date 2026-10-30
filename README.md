# Diablo

A voice assistant for macOS that does the work, and never phones home.

Hold Control+Option, say what you want, let go. Diablo works out what you meant and then actually does it: reads the file, runs the numbers, moves the folder, reads the window you are looking at. The models run in Ollama on your own machine (Qwen3 4B Instruct by default, Qwen3 14B when the 4B is out of its depth, and a 7B vision model for questions the text on screen cannot answer). No API keys, no account, no request leaving the laptop apart from the web searches you ask for.

It is built around one idea: an assistant that only talks is a toy. Diablo has 48 tools and an MCP client, so when you ask it to total a spreadsheet it opens the spreadsheet.

```
you    > what youtube video am i watching
diablo > The video is "How a Minecraft SMP Drove Me Insane".

you    > total the revenue in sales.csv and put it in total.txt
         [read_file]       item,units,price / pens,10,2 / books,3,15 / bags,2,25
         [python_snippet]  115
         [write_file]      Wrote total.txt
diablo > That comes to 115. It's in total.txt in the same folder.
```

## Why local

Running models on a laptop is slower than calling a frontier model, and the trade is worth making for this particular job. A voice assistant hears everything said near it. Mine sees my coursework, my business files and whatever is on my screen at the time. That is not data I want to post to a third party in exchange for a better paraphrase.

The practical cost, measured end to end on an 18 GB M3 Pro in October 2026: a direct request such as a calculation or opening an app is decided in under a tenth of a second and answered in about a second, most of which is the voice rendering the reply (the app launch itself was stubbed in the measurement). A three-step file task takes 11 to 18 seconds warm and about 21 cold, and a delegated research job about 20 seconds warm. Ollama releases the model after ten idle minutes, and the next request pays a cold load of 1.2 to 2.5 seconds. A question that needs the vision model takes about 40 seconds, because only one model fits in memory at a time. The timings are in `docs/quality/gauntlet/perf/`. The 4B's memory, about 3.9 GB resident at an 8,192-token context, was measured separately in September and is recorded in the `.env` comments.

## Requirements

- macOS 13 or later, Apple silicon
- Python 3.10+ (3.14 recommended; the MCP client needs 3.10 for its typing)
- [Ollama](https://ollama.com) with `qwen3.5:4b` pulled. It is the only general model: it reasons, plans and reads images. `tools/install.sh` pulls it if it is missing.
- GoClick-Large (0.8B), the GUI grounding specialist, optional: `tools/install.sh --grounder` fetches its pinned weights into `~/.diablo/models/goclick-large`, checks their sha256, and installs `mlx-vlm` to run them. Without it, Diablo grounds controls through accessibility, on-screen text and qwen3.5.
- [Voicebox](https://voicebox.dev) for speech, optional; without it Diablo falls back to the macOS system voice
- Node 18+ if you want the MCP servers

The brain has a history (until 2026-10-05 it was `qwen3:4b-instruct-2507`, with a 14B for escalation and Qwen2.5-VL for vision; all three are gone). The first 4B tried, `qwen3:4b` with thinking on, took 9.97 seconds and emitted no tool call at all. The instruct build of the same size is three times faster than the 14B with the same tool accuracy on this machine, and the things it gets wrong (picking one of two files, reporting a change it never made) are caught by guards in code rather than by prompting; see "Why it does not make things up" below.

## Install

**Not yet packaged for a clean Mac.** `tools/install.sh` predates the current layout: it expects an existing `~/.diablo`, copies modules from the wrong folder and patches a bridge that no longer exists. Until it is rewritten, Diablo runs from an existing install at `~/.diablo`, and `diablo/make_app.py` builds `Diablo.app` around it:

```bash
~/.diablo/venv/bin/python3 diablo/make_app.py    # writes ~/Applications/Diablo.app
```

Building the app matters more than it looks. macOS grants microphone access to the *responsible process*, and for a shell command that is your terminal — which is why access appears to come and go depending on which terminal you used, and vanishes entirely when permissions are reset. Launched as an app, Diablo owns the grant itself.

Start it from Finder the first time so macOS has somewhere to show the prompt:

```bash
open ~/Applications          # then double-click Diablo, and click Allow
```

After that, `diablo` works from anywhere.

## Using it

```
diablo            start listening, and follow the log
diablo stop       shut it down
diablo status     is it alive
diablo doctor     find what is broken, and fix what can be fixed
diablo mic        re-probe the microphone and pick a working device
```

Hold Control+Option while you speak and let go when you are done. The microphone belongs to that hotkey: nothing you say reaches Diablo unless the keys are held, and there is no always-on listening (the old wake-word mode is still in the code as a rollback, off unless `DIABLO_PTT_ONLY=0`). You can also type into the window from the menu bar.

Long work runs in the background and shows in the window with a Stop button; a stopped task stops before its next step and its result is not read out.

## What it can do

**Files.** Read, write, append, move, find, search inside. Deletes go to the Trash, never `unlink`.

**Real work.** Shell commands, Python snippets, spreadsheets through pandas and openpyxl. Arithmetic always goes through Python, never through the model, because a 14B asked to total three rows in its head returned 350 for a figure that was 115.

**Your screen.** Diablo reads one window at a time using Apple's Vision framework, with a 2D occupancy grid that finds the real column boundaries. Reading per-window rather than whole-screen is what keeps the browser's text out of the editor's, which is the difference between screen context being useful and being noise. Password managers and banking windows are refused outright, and so is Diablo's own interface.

Every capture carries where it came from and when: which display, at what scale factor, how many seconds ago. That is what makes the rest possible. It knows which of your monitors a window is on and maps a coordinate correctly onto a Retina panel sitting at a negative x. It checks each capture is a screen at all before reading it, because a window on another desktop captures as a well-formed PNG of solid black and "there is no text in this window" is a confident, wrong answer nobody can see is wrong. It can read just the patch around your pointer rather than the whole desktop, tell you what is under the cursor, and say what has changed since it last looked. Ask it something the words cannot answer — what a chart shows, which button is greyed out — and it routes to a vision model instead of the text path, at a capped resolution that cut a Retina window's answer from 37 seconds to 10 with the same answer coming back.

Looking is not touching. Clicking and typing live behind a separate switch that is off by default, need an explicit grant that is scoped to one task and expires, re-check that the screen has not moved since the coordinate was chosen, ask again for anything that cannot be undone, and verify afterwards that the click actually changed something. No agent can reach them.

**Conversation.** It holds a thread while you work, remembers twelve turns, and knows what is on your screen when you refer to it.

**Anything else.** An MCP client speaks the protocol directly over stdio, so any MCP server works without writing Python. Tool schemas are flattened first, because a 14B does not reliably fill in a nested `$ref`.

## Safety

Diablo acts on speech, and speech is misheard. "Delete the old drafts" and "delete the older drafts" are one phoneme apart. Three defences:

1. **A confined filesystem.** Desktop, Documents, Downloads and one project folder. Traversal, symlinks and absolute paths out are all refused. There are 44 tests on this boundary alone.
2. **A shell denylist.** 19 patterns, including `rm -rf`, `sudo`, `dd`, `mkfs`, `diskutil erase`, curl piped to a shell, fork bombs and `git push`.
3. **Spoken confirmation** before any delete, overwrite, message or shell command.

Deletes go to the Trash so a misheard sentence stays recoverable.

## What it keeps

Being local only helps if the machine itself does not fill up with what Diablo has seen and said. So, by default:

| What | Kept |
|---|---|
| Screen captures | Never. A capture is read and deleted by the code that took it, and anything orphaned by a crash is removed the next time the screen starts. |
| Screenshots | Only when you ask for one ("take a screenshot", "save a screenshot to my Downloads"), and only in the folder you named, or the Desktop. |
| Spoken answers | Deleted once played. A line nobody heard is kept up to ten minutes for a retry. Only Diablo's fixed phrases ("On it, Sir.") are kept as audio. |
| Task results (`jobs.json`) | What ran, when, and how it ended. An answer read off the screen is never written; any other answer is cut to 32 KB and deleted after 24 hours. Readable by your user only. |
| `trace.jsonl`, `diablo.log` | Decisions, sizes, timings and errors (only the kind of error where a message could carry content). Never the text read off the screen, a tool's output or arguments, or an answer; the log shows a spoken line's length, and the words only when Diablo runs in a terminal. The trace does record what you said to Diablo. So does the log if you turn the old wake word back on, and a failed file operation names the file. |

The checks behind each line are in `tests/test_retention.py`, `tests/test_capture_lifetime.py` and `tests/test_trace_privacy.py`, and they read the files Diablo actually writes.

## Who does the work

Diablo does not do the work itself. It listens, decides, and hands the job to
one of five named subagents, chosen from the Ars Goetia for what each demon was
actually said to do rather than for sounding sinister.

| | Does | Tools it owns |
| --- | --- | --- |
| **Asmo** | Finds things out | web search, Wikipedia, opening links |
| **Marbas** | Papers and numbers | reading, writing, moving files; arithmetic |
| **Vassago** | Watches the screen | window capture, OCR, pointing at things |
| **Seere** | Runs the errands | apps, music, volume, messages |
| **Levi** | The deep one | wakes the larger model when the 4B is out of its depth |

Ownership decides whose face goes on a job and who gets handed which half of a
split. It does not decide capability — every agent reaches all 48 tools through
the same permission gate, so a misassignment shows the wrong name on a card, not
a job that cannot be done.

**Two errands in one sentence run at the same time.** "Look up the weather in
Pune and tell me what is on my screen" becomes Asmo and Vassago working in
parallel, and the answer comes back attributed. A request whose second half
needs the first half's answer is never split — "read the total and *then* send
it to her" would have one agent messaging a number nobody had computed yet, so
anything with a dependency marker in it stays with one agent.

The splitter tries the model first and falls back to cutting on the
conjunction, because the 4B will not split a sentence the 14B splits every
time. Both paths go through the same three checks: nothing in the request may
disappear between the halves, the halves must not repeat each other, and
neither may contain a word the request did not.

## Character

Diablo has dials, set in `.env`:

```
DIABLO_HUMOUR=2      # 0 none, 1 dry, 2 wry, 3 cheeky
DIABLO_WARMTH=1
DIABLO_FORMALITY=2
DIABLO_ADDRESS=Sir
```

Humour lives in conversation and never in a task report. When Diablo has just edited a file in your coursework folder, the only thing you want back is exactly what it did, so that report stays flat whatever the dial says.

**Long conversations are summarised, not truncated.** Past about twelve
exchanges the oldest half is folded into a short third-person note and carried
forward as its own system message. It used to be dropped outright, which meant
twenty minutes in, "what did I ask you about first" had no answer and the model
would invent one.

It will also never tell you it has done something it has not done. That sounds obvious; it took three rewrites. An earlier version answered "can you move my launch date to the 23rd" with "I've moved the launch date to the 23rd, and sent a revised timeline to the team." None of it happened. Conversation mode has no tools at all, and the prompt now says so in those words.

## Tests

```bash
diablo test        # 70 suites; --fast skips the ones that use the model
diablo eval        # score it on clear, ambiguous, risky and impossible asks
diablo bench       # latency, throughput and memory, per model
diablo trace       # why it decided what it decided, turn by turn
diablo state       # what every subsystem is doing right now
```

Seventy suites and a little over 4,100 checks. They use the real model, the real filesystem and a real Trash rather than mocks, because the failures worth catching are the ones a small local model produces and a scripted stand-in never will: narrating a plan instead of running it, calling the same tool forever, or reporting success having done nothing. Three suites need something running outside them: `test_voice` needs Voicebox, `test_app` needs Diablo itself on its UI port, and `test_scenarios` reaches Wikipedia. When one of those is down, the suite fails rather than skipping, which is deliberate.

Every agent test checks the disk afterwards, not what Diablo said about the disk.

`diablo eval` is the other half. The suites answer "is it broken"; the eval
answers "is it any good", across seven categories — clear, ambiguous,
impossible, invented-path, tool-failure, risky, and should-escalate — scoring
clarification accuracy, hallucination rate, recovery, unnecessary escalation,
completion and latency. Hallucination is judged from the filesystem and a
forbidden-phrase list, never by asking the model whether it was honest: the
whole failure mode is confidence while wrong, so its own account is worthless.

```
qwen3:4b-instruct-2507-q4_K_M      (September 2026, before qwen3.5)
passed 15/15   hallucinations 0   median 4.09s
```

## Why it does not make things up

A small model asked to edit "the draft" when two exist will pick one. Asked to
read a file, it will sometimes report contents it invented while the real ones
sit in its context. Prompting did not fix either, so six guards do, and each
one holds for any model:

| Guard | What it stops |
|---|---|
| `_ambiguous_target` | editing one of several files you did not name |
| `_claims_unperformed_action` | reporting a change no tool made |
| `_quotes_something_unread` | quoting file contents no tool returned |
| `_parent_must_exist` | inventing a folder from a mis-heard name |
| `_is_really_a_question` | ending the turn on a question it forgot to ask |
| `_invents_a_status` | reporting the state of something it cannot see |

The last one is why "how is Brew and Critics doing" gets "no idea, want me to
check?" instead of a confident paragraph about a backend Diablo has never
touched.

Each guard in code earned the deletion of a prohibition from the prompt. The
conversation prompt went from 924 words containing 40 prohibitions to 515
words containing 16, which is most of the reason Diablo stopped sounding
defensive.

## Architecture

```
companion_manager.py the state machine: owns every subsystem, one health view
voice_bridge.py      the service: push-to-talk capture, reflexes, dispatch
diablo/
  wake_word.py       wake word by transcription (rollback only; off by default)
  mic.py             device selection by evidence, endpointed capture
  agent_core.py      understand, plan, ReAct loop, clarify
  agent_tools.py     the 48 tools, path confinement, denylist
  local_brain.py     Ollama client, stdlib only
  mcp_client.py      JSON-RPC 2.0 over stdio, no SDK
  screen_context.py  per-window OCR, column detection, vision routing
  display.py         the monitors, and the arithmetic between coordinate spaces
  screen.py          captures that know their own provenance, and what changed
  computer_use.py    permission to click, and the four gates before one happens
  conversation.py    rolling memory, anti-fabrication
  persona.py         the character dials
  voice.py           Voicebox, disk cache, system fallback
  pointing.py        finding things on screen by measured coordinates
  overlay.py         the on-screen marker, drawn in its own process
  bench.py           latency, throughput and memory per model
```

`companion_manager.py` is the single place that knows what Diablo is doing.
It owns the lifecycle of the mic, wake word, voice, brain, screen and
conversation, runs an explicit state machine (`cold → init → idle → listening
→ processing → responding`), and emits events anything can subscribe to:

```python
from companion_manager import create_manager

manager = create_manager()
manager.on("state_changed", lambda e: print(e["old_state"], "->", e["new_state"]))
manager.start()
print(manager.health())
```

```bash
python3 companion_manager.py     # full diagnostic report
```

Invalid state transitions are logged and refused rather than raised, because a
voice assistant that throws on an odd transition goes silent, and silence is
the one failure with no symptom.

## Licence

MIT. See [LICENSE](LICENSE).
