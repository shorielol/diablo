# Diablo

A voice assistant for macOS that does the work, and never phones home.

Diablo listens for "Hey Diablo", works out what you meant, and then actually does it — reads the file, runs the numbers, moves the folder, reads the window you are looking at. The model is a 14B running in Ollama on your own machine. No API keys, no account, no request leaving the laptop.

It is built around one idea: an assistant that only talks is a toy. Diablo has 33 tools and an MCP client, so when you ask it to total a spreadsheet it opens the spreadsheet.

```
you    > hey diablo, what youtube video am i watching
diablo > The video is "How a Minecraft SMP Drove Me Insane".

you    > hey diablo, total the revenue in sales.csv and put it in total.txt
         [read_file]       item,units,price / pens,10,2 / books,3,15 / bags,2,25
         [python_snippet]  115
         [write_file]      Wrote total.txt
diablo > That comes to 115. It's in total.txt in the same folder.
```

## Why local

Running a 14B on a laptop is slower than calling a frontier model, and the trade is worth making for this particular job. A voice assistant hears everything said near it. Mine sees my coursework, my business files and whatever is on my screen at the time. That is not data I want to post to a third party in exchange for a better paraphrase.

The practical cost is about 1.9 seconds for a warm tool-calling turn and 9.3 GB of memory while the model is resident. Ollama releases it after ten idle minutes, and Diablo starts reloading the moment the wake word fires, which hides most of the cold start behind the time you spend talking.

## Requirements

- macOS 13 or later, Apple silicon
- Python 3.10+ (3.14 recommended; the MCP client needs 3.10 for its typing)
- [Ollama](https://ollama.com) with `qwen3:14b` pulled
- [Voicebox](https://voicebox.dev) for speech, optional — without it Diablo falls back to the macOS system voice
- Node 18+ if you want the MCP servers

Smaller models were tested and did not work. `qwen3:4b` took 9.97 seconds and emitted no tool call at all, so it was both slower and less capable than the 14B on the only task that matters here.

## Install

```bash
git clone https://github.com/YOUR_USERNAME/diablo.git
cd diablo
./install.sh
```

The installer builds a venv at `~/.diablo/venv`, copies the modules into `~/.diablo`, writes `~/bin/diablo`, and builds `Diablo.app`.

That last part matters more than it looks. macOS grants microphone access to the *responsible process*, and for a shell command that is your terminal — which is why access appears to come and go depending on which terminal you used, and vanishes entirely when permissions are reset. Launched as an app, Diablo owns the grant itself.

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

Say "Hey Diablo" and then your request, or say the whole thing in one breath. One breath is faster: the wake word is detected by transcription rather than by a trained model, so the sentence arrives whole and there is no second round of listening.

## What it can do

**Files.** Read, write, append, move, find, search inside. Deletes go to the Trash, never `unlink`.

**Real work.** Shell commands, Python snippets, spreadsheets through pandas and openpyxl. Arithmetic always goes through Python, never through the model, because a 14B asked to total three rows in its head returned 350 for a figure that was 115.

**Your screen.** Diablo reads one window at a time using Apple's Vision framework, with a 2D occupancy grid that finds the real column boundaries. Reading per-window rather than whole-screen is what keeps the browser's text out of the editor's, which is the difference between screen context being useful and being noise. Password managers and banking windows are refused outright.

**Conversation.** It holds a thread while you work, remembers twelve turns, and knows what is on your screen when you refer to it.

**Anything else.** An MCP client speaks the protocol directly over stdio, so any MCP server works without writing Python. Tool schemas are flattened first, because a 14B does not reliably fill in a nested `$ref`.

## Safety

Diablo acts on speech, and speech is misheard. "Delete the old drafts" and "delete the older drafts" are one phoneme apart. Three defences:

1. **A confined filesystem.** Desktop, Documents, Downloads and one project folder. Traversal, symlinks and absolute paths out are all refused. There are 44 tests on this boundary alone.
2. **A shell denylist.** 19 patterns, including `rm -rf`, `sudo`, `dd`, `mkfs`, `diskutil erase`, curl piped to a shell, fork bombs and `git push`.
3. **Spoken confirmation** before any delete, overwrite, message or shell command.

Deletes go to the Trash so a misheard sentence stays recoverable.

## Character

Diablo has dials, set in `.env`:

```
DIABLO_HUMOUR=2      # 0 none, 1 dry, 2 wry, 3 cheeky
DIABLO_WARMTH=1
DIABLO_FORMALITY=2
DIABLO_ADDRESS=Sir
```

Humour lives in conversation and never in a task report. When Diablo has just edited a file in your coursework folder, the only thing you want back is exactly what it did, so that report stays flat whatever the dial says.

It will also never tell you it has done something it has not done. That sounds obvious; it took three rewrites. An earlier version answered "can you move my launch date to the 23rd" with "I've moved the launch date to the 23rd, and sent a revised timeline to the team." None of it happened. Conversation mode has no tools at all, and the prompt now says so in those words.

## Tests

```bash
diablo test        # 14 suites
diablo eval        # score it on clear, ambiguous, risky and impossible asks
diablo bench       # latency, throughput and memory, per model
diablo trace       # why it decided what it decided, turn by turn
diablo state       # what every subsystem is doing right now
```

Fourteen suites, 470 checks. They use the real model, the real filesystem and a real Trash rather than mocks, because the failures worth catching are the ones a 14B produces and a scripted stand-in never will: narrating a plan instead of running it, calling the same tool forever, or reporting success having done nothing.

Every agent test checks the disk afterwards, not what Diablo said about the disk.

`diablo eval` is the other half. The suites answer "is it broken"; the eval
answers "is it any good", across seven categories — clear, ambiguous,
impossible, invented-path, tool-failure, risky, and should-escalate — scoring
clarification accuracy, hallucination rate, recovery, unnecessary escalation,
completion and latency. Hallucination is judged from the filesystem and a
forbidden-phrase list, never by asking the model whether it was honest: the
whole failure mode is confidence while wrong, so its own account is worthless.

```
qwen3:4b-instruct-2507-q4_K_M
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
voice_bridge.py      the service: audio in, wake detection, dispatch
diablo/
  wake_word.py       rolling-window detection by transcription
  mic.py             device selection by evidence, endpointed capture
  agent_core.py      understand, plan, ReAct loop, clarify
  agent_tools.py     the 33 tools, path confinement, denylist
  local_brain.py     Ollama client, stdlib only
  mcp_client.py      JSON-RPC 2.0 over stdio, no SDK
  screen_context.py  per-window OCR, column detection
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
