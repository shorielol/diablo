# Model dependency map

Every place in the repository that names, chooses or loads a model, after the
move to one general model (qwen3.5:4b) and one grounding specialist
(GoClick-Large). Built on 2026-10-05 from a search of every tracked and
untracked file for `qwen3`, `qwen3:4b`, `qwen3:14b`, `qwen3.5`, `qwen2.5`,
`Qwen2.5-VL`, `fara`, `goclick`, `DIABLO_MODEL`, `DIABLO_FAST_MODEL`,
`DIABLO_ESCALATE_MODEL`, `DIABLO_VISION_MODEL`, `CHAT_MODEL`, `FAST_MODEL` and
`14b` (386 lines), then by tracing who calls what. Measured result files under
`docs/quality/gauntlet/` are history and are not listed line by line.

## How a model is chosen at runtime

There is one choice and one place it is made:

```
DIABLO_MODEL (env or .env), blank or absent  ─►  local_brain.DEFAULT_BRAIN_MODEL = "qwen3.5:4b"
                                                 │
                         local_brain.CHAT_MODEL ◄┘──► FAST_MODEL (DIABLO_FAST_MODEL, blank → CHAT_MODEL)
```

Every model call goes through `local_brain.chat`, `chat_stream` or `chat_json`
to Ollama's `/api/chat` with `num_ctx` (default 32768) and `truncate: false`.
Image questions use the same model: `screen_context.read_image` and
`describe_visually` send `model=local_brain.CHAT_MODEL`. GoClick is not an
Ollama model; it runs in its own worker process (`goclick.serve`) under
MLX-VLM, from weights in `$DIABLO_HOME/models/goclick-large`.

Nothing else loads a model. No subprocess, HTTP client or OpenAI-compatible
endpoint in `diablo/` names one; the search found no caller of Ollama's API
other than `local_brain`, `bench.py` (a measuring tool) and `doctor.py`
(which only lists installed models).

## Classification

### ACTIVE PRIMARY — qwen3.5:4b

| Where | What |
|---|---|
| `diablo/local_brain.py:40` | `DEFAULT_BRAIN_MODEL = "qwen3.5:4b"`, the only default |
| `diablo/local_brain.py:44-45` | `CHAT_MODEL`, `FAST_MODEL`; blank values fall back to the default |
| `diablo/local_brain.py` `chat`, `chat_stream`, `chat_json` | every model call; `truncate: false`, `num_ctx` from `NUM_CTX` |
| `diablo/screen_context.py:878, 956, 973, 1002` | image questions use `CHAT_MODEL`; there is no separate vision model |
| `diablo/grounding.py:192-198` | `BOX_SPACES`: qwen3.5 boxes are read on a 0..1000 grid and no other way |
| `companion_manager.py:190, 200, 218` | warms, names and checks the resident model, from `CHAT_MODEL` |
| `voice_bridge.py:1111-1116` | startup check that `CHAT_MODEL` is pulled |
| `diablo/doctor.py:213` | `check_brain` reports `CHAT_MODEL` and `NUM_CTX` |
| `diablo/server.py:152, 160` | the settings page's model field writes `DIABLO_MODEL`; blank means the default |

### ACTIVE GROUNDING — GoClick-Large

| Where | What |
|---|---|
| `diablo/goclick.py` | pinned revision `f923d830…`, weights sha256, worker process, `propose()` |
| `diablo/perception.py:520-575` | `_from_grounder`: GoClick only when accessibility and OCR could not settle it, every point through `grounding_gate.validate` |
| `diablo/grounding_gate.py` | the validator every model point must pass; imports no model |
| `diablo/doctor.py:236-260` | `check_grounder`: weights present, runtime importable |

### INSTALLER

| Where | What |
|---|---|
| `tools/install.sh` | brain model resolved as env, then `.env`, then the line in `local_brain.py`; pulled if missing; `--check` pulls nothing |
| `tools/install.sh --grounder` | GoClick revision, hash and size read from `goclick.py`; fetches safetensors and config only, checks the sha256, installs `mlx-vlm==0.7.4` |

### CONFIGURATION

| Where | What |
|---|---|
| `.env.example` | `DIABLO_MODEL=qwen3.5:4b`, `DIABLO_NUM_CTX=32768`, `DIABLO_GROUNDER_KEEP_ALIVE=600` |
| `DIABLO_GOCLICK_PATH`, `DIABLO_GROUNDER_PYTHON`, `DIABLO_GROUNDER_TIMEOUT` | where GoClick's weights and interpreter are; read only by `goclick.py` |

### TEST

| Where | What |
|---|---|
| `tests/test_default_model.py` | `test_fresh_install_defaults_to_qwen35`, `_never_defaults_to_14b`, `_never_defaults_to_qwen25vl`; also fails if any module reads `DIABLO_ESCALATE_MODEL` or `DIABLO_VISION_MODEL` |
| `tests/test_grounding_gate.py` | the security matrix for model-supplied points |
| `tests/test_grounding.py`, `test_grounding_geometry.py` | box space declared per model family; Qwen2.5-VL has none |
| `tests/test_brain.py`, `live_check.py`, `perf_e2e.py`, `test_agent.py` | read `CHAT_MODEL`, whatever it resolves to |

### EVALUATION TOOLING (not runtime)

| Where | What |
|---|---|
| `diablo/bench.py:256` | compares the models it is told to; the default is now `qwen3.5:4b` only |
| `evals/run_intake_eval.py`, `run_eval.py`, `run_planner_eval.py` | `--model` sets `DIABLO_MODEL` for one run |
| `evals/run_grounding_gauntlet.py`, `run_reading_gauntlet.py`, `run_long_horizon.py` | the final gauntlet |
| `docs/quality/gauntlet/models/scripts/*` | the previous round's harnesses for Fara, MAI-UI and Qwen2.5-VL; kept as that round's record, imported by nothing |

### DOCUMENTATION

`README.md` (requirements, and the history paragraph marked with its date),
`claude.md`, `docs/DEMO.md`, the `.env.example` comments, and this directory.

### HISTORICAL comments, kept on purpose

These are measurements that explain why a guard exists, each naming the model
it was measured on. Rewriting them to say qwen3.5 would make them false.

`agent_core.py:468, 876, 898, 984, 996`; `agent_tools.py:278, 1948`;
`conversation.py:855, 1010`; `mcp_client.py:422, 425`; `pointing.py:16`;
`roster.py:425`; `local_brain.py:37, 88`; `grounding.py:80, 180, 345`;
`route.py:47`; `do_engine.py:364`; the docstrings of `test_diablo.py:122`,
`test_mcp.py:208`, `test_safety.py:16`, `test_system.py:12`,
`test_vision_maths.py:215`, `test_routing_live.py:124`, `harness.py:5, 22`
and `run_all.py:6`; `memory.md:37-38`;
`docs/architecture/diablo_overhaul_audit.md`; `tools/patch_memory.py:7`.

### LEGACY

`tools/patch_voice_bridge.py` patches a bridge layout that no longer exists.
It reads `CHAT_MODEL`, so it would follow the default if run, but nothing
runs it.

### DEAD CODE, removed on 2026-10-05

| Was | Why it went |
|---|---|
| `local_brain.with_model`, `unload`, `resident` | existed only to swap the 14B in and out |
| `route.ESCALATE_BELOW` and the `escalate` route | the confidence band that handed requests to the 14B |
| the escalation block in `delegation_tool` and its `escalate → act` mapping | the caller of `with_model` |
| the "Escalates to" setting in `server.py` | wrote `DIABLO_ESCALATE_MODEL` |
| `screen_context.VISION_MODEL` / `DIABLO_VISION_MODEL` | the Qwen2.5-VL default, and the variable that could bring it back |
| `grounding.model_canvas`, `_canvas_applies`, `DIABLO_VISION_COORDS`, `DIABLO_VISION_PATCH`, `DIABLO_VISION_MIN_PIXELS`, `DIABLO_VISION_MAX_PIXELS` | Qwen2.5-VL's resized-canvas reading, and a guess between four box spaces |
| the `local_brain` fallback to `qwen3:14b` | the fresh-install defect this work started from |

## After the change

A search of `diablo/` for a retired model name used as a value (not a
comment) returns nothing except `bench.py`, which compares what it is given.
`tests/test_default_model.py` enforces this. `DIABLO_ESCALATE_MODEL` and
`DIABLO_VISION_MODEL` are read nowhere.

## Outside the repository, untouched

- **Ollama's store** still holds `qwen3:14b`, `qwen2.5vl:7b`,
  `qwen3:4b-instruct-2507-q4_K_M`, the Fara and MAI-UI GGUFs from the last
  round, and `qwen3-large-ctx:latest`, which someone else created on
  2026-10-05. No code refers to any of them. Removing them is the owner's
  call: `ollama rm <name>`.
- **The live install at `~/.diablo`** runs its own copy of the old code, and
  its `.env` still names `qwen3:4b-instruct-2507-q4_K_M`, `qwen3:14b` and
  `qwen2.5vl:7b`. It was not modified. Moving it to this architecture means
  installing these modules, setting `DIABLO_MODEL=qwen3.5:4b` (or deleting
  that line), and running `tools/install.sh --grounder`.
