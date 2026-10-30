# Where Diablo depends on the 14B (qwen3:14b)

Searched 2026-10-05 across `diablo/`, `voice_bridge.py`, `companion_manager.py`,
`app_shell.py`, `diablo_app.py`, `tools/`, `bin/`, `tests/`, `evals/`, `.env.example`
and the docs. Every reference falls in one of four kinds.

## 1. Runtime paths that can load it

| Where | What it does | When it fires |
|---|---|---|
| `diablo/route.py:309` | Sets route `escalate` when the intake parse says act, the request needs tools, confidence is below `ESCALATE_BELOW` (0.70), and `DIABLO_ESCALATE_MODEL` is set | uncertain requests that would touch things |
| `diablo/delegation_tool.py:862` | Runs that turn's `agent_core.run` inside `local_brain.with_model(DIABLO_ESCALATE_MODEL, …)` | every `escalate` route |
| `diablo/local_brain.py:511` `with_model` | Swaps `CHAT_MODEL`, runs the work, restores it, and evicts the big model (`keep_alive: 0`) | inside escalation only |
| `diablo/server.py:154,162` | Exposes `DIABLO_ESCALATE_MODEL` as a setting in the window's dials | when the user changes it |

Measured use in real turns (live trace, 2026-09-14 to 2026-10-02): 9 of 206 real
turns were routed `escalate`, about 4.4%. Of the 7 that finished, median time to
`done` was 95 s.

## 2. Hidden defaults that would make it the main brain

| Where | Default | Effect |
|---|---|---|
| `diablo/local_brain.py:33` | `CHAT_MODEL = os.getenv("DIABLO_MODEL", "qwen3:14b")` | an install without `DIABLO_MODEL` runs the 14B for everything |
| `tools/install.sh:16` | `MODEL="${DIABLO_MODEL:-…:-qwen3:14b}"` | the installer sets up the 14B as the brain |
| `.env.example:22` | `DIABLO_ESCALATE_MODEL=qwen3:14b` | escalation on by default for a new `.env` |
| `diablo/bench.py:256` | benchmark default list includes `qwen3:14b` | benchmark only |

The live install sets `DIABLO_MODEL=qwen3:4b-instruct-2507-q4_K_M`, so on this
Mac the 14B is only reached through escalation.

## 3. Historical coupling (text, no behaviour)

Docstrings and comments describe the design as built for a 14B:
`agent_core.py` (lines 4, 8, 11, 468), `conversation.py` (20, 855, 1010),
`roster.py:425`, `pointing.py:16`, `mcp_client.py:422-425`, `agent_tools.py:1948`,
`voice_bridge.py:33`, and the docstrings of `tests/live_check.py`,
`test_agent.py`, `harness.py`, `test_brain.py`, `test_safety.py`,
`test_system.py`, `test_mcp.py`, `run_all.py`. `tests/harness.py:23` already
follows whatever model the environment names. No test requires the 14B.

## 4. Not dependent on it

Vision (`DIABLO_VISION_MODEL`, qwen2.5vl:7b), the 4B brain, speech, the
rules decision engine and every safety gate run without it.

## Conclusion

Diablo is not dependent on the 14B for normal operation. It is reached through
one switch, `DIABLO_ESCALATE_MODEL`, on about 4% of real turns. The real coupling
is the two hidden defaults in section 2, which would put it on the main path
of a fresh install.
