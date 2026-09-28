#!/usr/bin/env bash
# After the Kaggle run: verify, measure, gate. It never deploys or enables anything.
#
#   research/laya_diablo/eval/after_training.sh <checkpoint dir> [device, default mps]
#
# LAYA_PYTHON must be a Python with laya==0.3.20 (torch, transformers) that can also import
# Diablo's own packages; DIABLO_HOME defaults to ~/.diablo. One way to make it:
#   python3 -m venv ~/.laya-venv && ~/.laya-venv/bin/pip install "laya==0.3.20"
#   ls -d ~/.diablo/venv/lib/python3.*/site-packages > \
#      "$(ls -d ~/.laya-venv/lib/python3.*/site-packages)/diablo.pth"
#   export LAYA_PYTHON=~/.laya-venv/bin/python
set -euo pipefail

CK="$(cd "${1:?usage: after_training.sh <checkpoint dir> [device]}" && pwd)"
DEVICE="${2:-mps}"
PY="${LAYA_PYTHON:-python3}"
EVAL="$(cd "$(dirname "$0")" && pwd)"
R="$(cd "$EVAL/../reports" && pwd)"
E="$EVAL/eval_laya_diablo.py"

"$PY" -c 'import laya, sys; sys.exit(laya.__version__ != "0.3.20")' \
    || { echo "LAYA_PYTHON needs laya==0.3.20" >&2; exit 1; }
run() { echo "== $*" >&2; "$PY" "$@"; }

# 1. Provenance before any measurement: contract, questions, base snapshot, Laya version,
#    dataset split hashes, calibration split, fitted temperatures, weights hash, trained steps.
run "$E" verify --checkpoint "$CK" --out "$R/verify_ft.json"

# 2. The untouched v1 test: rules, the checkpoint, hybrid. Same examples, same scorer.
run "$E" score --engine rules --out "$R/rules_test.json"
run "$E" score --engine laya --checkpoint "$CK" --device "$DEVICE" \
    --queue "$R/disagreements_laya_ft.jsonl" --out "$R/laya_ft_test_$DEVICE.json"
run "$E" score --engine hybrid --checkpoint "$CK" --device "$DEVICE" \
    --queue "$R/disagreements_hybrid_ft.jsonl" --out "$R/hybrid_ft_test_$DEVICE.json"
run "$E" score --engine laya --checkpoint "$CK" --device cpu --out "$R/laya_ft_test_cpu.json"

# 3. What calibration is left on the calibration split (the fit itself happened in training).
run "$E" calibrate --checkpoint "$CK" --device "$DEVICE" --out "$R/calibration_ft.json"

# 4. Option order, the long run, and shadow mode over real requests (no words written).
run "$E" robustness --checkpoint "$CK" --device "$DEVICE" --out "$R/robustness_ft.json"
run "$E" longrun --checkpoint "$CK" --device "$DEVICE" --out "$R/longrun_ft.json"
run "$EVAL/shadow_replay.py" --checkpoint "$CK" --device "$DEVICE" --out "$R/shadow_ft.json"

# 5. The gate, unchanged. verify passed, so the checkpoint took optimiser steps on Diablo data
#    and holds temperatures fitted on the calibration split: both flags are established facts.
run "$EVAL/promotion.py" --rules "$R/rules_test.json" --laya "$R/laya_ft_test_$DEVICE.json" \
    --hybrid "$R/hybrid_ft_test_$DEVICE.json" --longrun "$R/longrun_ft.json" \
    --fine-tuned true --calibrated true --out "$R/promotion_ft.json"

# 6. Only now, once, as a secondary check: the hand-written private test.
run "$E" score --engine rules --split test_private --out "$R/rules_test_private.json"
run "$E" score --engine laya --checkpoint "$CK" --device "$DEVICE" --split test_private \
    --out "$R/laya_ft_test_private.json"
run "$E" score --engine hybrid --checkpoint "$CK" --device "$DEVICE" --split test_private \
    --out "$R/hybrid_ft_test_private.json"

echo "decision: $("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["decision"])' \
    "$R/promotion_ft.json")  (rules stay authoritative; nothing was deployed)"
