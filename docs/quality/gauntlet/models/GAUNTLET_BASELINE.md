# Model gauntlet baseline (control group), 2026-10-05

Measured before anything was changed. The only code change made during this
gauntlet is to three test fixtures (see "Fixture defect" in GAUNTLET_RESULTS.md),
and the incumbent was measured again after it.

## Architecture as it runs

| Role | Model | Where it is chosen | Resident (Ollama, 8K context) | Cold load |
|---|---|---|---|---|
| Brain: intake, conversation, agent loop, DO planner | qwen3:4b-instruct-2507 Q4_K_M | `DIABLO_MODEL`, `DIABLO_FAST_MODEL` | 3.86 GB | 2.1 s |
| Vision: free grounding (`grounding.py`), set-of-marks (`perception._from_vision`), image questions | qwen2.5vl:7b Q4_K_M | `DIABLO_VISION_MODEL` | 7.3 GB | 5.4 s |
| Escalation: uncertain requests that would touch things | qwen3:14b | `DIABLO_ESCALATE_MODEL` | 10.3 GB | 8.1 s |

Ollama keeps one model resident on this 18 GB Mac. Loading the 7B evicted the
4B: a vision question costs a 5.6 s swap in and 1.7 s back. Controls are found
by accessibility first, then OCR, then the set-of-marks vision pick; regions
and figures go through `grounding.py`, mostly a deterministic shape picker
with the vision model behind one seam. Every click passes Diablo's gates
(computer use off by default, task-scoped grants, fresh ScreenFrame, screen-diff
verification), none of which is a model.

## Benchmarks used

| Benchmark | What | Size |
|---|---|---|
| `evals/run_perception_eval.py` corpus, through `scripts/vbench.py` | control grounding on Diablo's AppKit fixtures, ground truth from the fixtures' own layout | 25 cases x 2 repeats, incl. 2 absent targets, 3 ambiguous, dark mode, edges, icons, disabled |
| `scripts/rbench.py` | region and figure grounding on the three figure fixtures | 11 regions |
| `evals/run_planner_eval.py` | next-step planning from a text state, scored against hand-written answer sets | 59 cases: simple, multi-step, state-dependent, single-target, recovery 7, safety 7, ambiguous 4 |
| `evals/run_intake_eval.py` | act, ask or talk, through `route.classify` | 39 cases x 3 |
| live trace | real turns, 2026-09-14 to 2026-10-02 | 206 real turns |

## Incumbent results

| Measure | Result |
|---|---|
| Control grounding, qwen2.5vl free grounding | 48 of 50; 1 wrong target; abstained on 4 of 4 absent; P50 3.0 s, P95 9.8 s |
| Control grounding, production fusion (accessibility + OCR + set-of-marks) | 41 of 50; 0 wrong; P50 0.19 s |
| Region grounding, production path | 9 of 11 (misses: equilibrium point, arrow) |
| Planner, 4B | 73% advancing (43 of 59); 6 regressions; 1 unsafe step; P50 1.6 s |
| Planner, 14B | 75% (44 of 59); 3 regressions; 1 unsafe step; P50 4.9 s |
| Intake, 4B | 107 of 117; 4 of 60 unnecessary questions; 3 of 57 false actions; 12 of 117 escalated |
| Real escalation rate | 9 of 206 real turns (4.4%); median 95 s to finish |

Verification accuracy, recovery after a failed click and stale-frame detection
are enforced by Diablo code and measured by `test_action_outcomes` (147/147),
`test_capture_lifetime`, `test_do_engine` and `test_acting`, all passing in the
round-three regression run. None of them depends on which model is loaded.
