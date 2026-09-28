# Laya, specialised for Diablo's decisions

Diablo decides how much machinery a turn needs before any model runs (`diablo/decision.py`). The
production engine is deterministic rules. This folder is everything needed to find out whether a
Diablo-trained Laya checkpoint can do that job better, and to prove it before it is trusted.

Laya may classify. It never authorises anything: a Laya verdict of "do" is a lane, and the lane's
own permission gates still decide. Laya is invisible to the user: there is no setting, no UI, no word
of it anywhere a person can see.

## What is here

| Path | What it is |
|---|---|
| `contract.json` | The three questions Laya answers (intent choice, needs_screen, complete), identical to `LAYA_QUESTIONS` in `diablo/decision.py` (a test enforces it) |
| `dataset/build_dataset.py` | Builds the dataset from Diablo's written gold policy (P1–P8, in its docstring). No model labels anything |
| `dataset/v1/` | train / validation / calibration / test / test_private JSONL, `manifest.json` (hashes), `audit.json` (quality checks) |
| `configs/train_v1.json` | Every training parameter, with the reason for it |
| `train/prepare_items.py` | Dataset to Laya training items, with Laya's own sequence builder |
| `train/train_diablo.py` | Upstream's RLCD recipe with Diablo's weighting, lane-based model selection and calibration on Diablo's calibration split |
| `notebooks/laya_diablo_finetune_2xT4_kaggle.ipynb` | Runs the two scripts on Kaggle's free 2×T4 |
| `kaggle/bundle.py` | Builds the Kaggle dataset (training splits only) and the kernel metadata |
| `eval/eval_laya_diablo.py` | Verifies a checkpoint, then scores rules, Laya and hybrid on the same examples; calibration, option order, option count, long run |
| `eval/after_training.sh` | Everything after training, in order: verify, held-out test, calibration, robustness, long run, shadow, gate, then the private test |
| `eval/promotion.py` | The gate: PROMOTE, HYBRID or KEEP_EXPERIMENTAL, from measured reports only |
| `reports/` | Every measurement behind the current decision |
| `checkpoints/` | Where a trained checkpoint goes. Never committed |

## The rules this work follows

- **Splits are by template.** A template (and every noisy rendering of its lines) is in exactly one
  split, so test phrasings are phrasings training never saw. `test_private` is hand-written and scored
  once, at the end.
- **The test splits are never used** for training, calibration, thresholds or model selection.
- **Model selection is by validation lane safety**: fewest false-fast, then lane accuracy. Never loss.
- **Calibration is fitted on the calibration split**, written into the checkpoint's config, and
  `temperature_by_options` is removed so no inherited per-bucket temperature can override the fit.
  `decision.verify_checkpoint` refuses any checkpoint where that is not true.
- **Every checkpoint carries `diablo_manifest.json`**: contract and its exact questions, base and its
  pinned snapshot, Laya version, dataset version and split hashes, run id and time, environment,
  training parameters and optimiser steps, calibration (temperatures, NLL before and after) and a
  weights hash. No manifest, no load. `verify_checkpoint` also refuses a checkpoint that took no
  optimiser step, and a fitted temperature outside [0.5, 5], which Laya would clamp at load.
- **Nothing unverified is measured.** The evaluation checks the base snapshot, Laya version and split
  hashes it was built for before it scores a checkpoint, and refuses the base weights themselves
  (the dry run's output is byte-identical to them), whatever a manifest claims.

## Training (external GPU)

1. `python research/laya_diablo/kaggle/bundle.py --user <kaggle username>` writes `dist/diablo-laya/`
   (the training splits only: test and test_private are never uploaded) and `dist/kernel/`.
2. With the Kaggle CLI signed in (`~/.kaggle/kaggle.json`):

       kaggle datasets create -p research/laya_diablo/dist/diablo-laya
       kaggle kernels push    -p research/laya_diablo/dist/kernel --accelerator NvidiaTeslaT4
       kaggle kernels status  <user>/laya-diablo-finetune
       kaggle kernels output  <user>/laya-diablo-finetune -p ~/Downloads/laya-diablo-run

   Or on kaggle.com: upload `dist/diablo-laya` as a private dataset named `diablo-laya`, import the
   notebook, attach the dataset, Accelerator `GPU T4 x2`, Internet on, Run All.
3. Unzip `laya-diablo-decision-v1.zip` into `checkpoints/laya-diablo-decision-v1/`.

Local check without training: `train/train_diablo.py ... --dry-run` runs a forward pass, the loss and
the whole save path on CPU. Its output has no optimiser steps, so `verify_checkpoint` refuses it.

## Evaluating a checkpoint

    research/laya_diablo/eval/after_training.sh research/laya_diablo/checkpoints/laya-diablo-decision-v1

It verifies the checkpoint, scores rules, the checkpoint and hybrid on the v1 test (MPS and CPU),
checks calibration on the calibration split, option order, a 3,000-decision long run and shadow mode
over real requests, runs the gate, and only then scores the private test. `LAYA_PYTHON` must have
`laya==0.3.20` and import Diablo (`DIABLO_HOME`); the script's header shows one way to make it.

## Shadow mode

    DIABLO_DECISION_SHADOW=laya DIABLO_LAYA_CHECKPOINT=/path/to/checkpoint

Rules stay authoritative. The shadow judges the same utterance on one background thread, drops work
when busy, and logs `decide_shadow` trace events with both decisions, probabilities and timing. It
logs no words. Disagreements are reviewed by a person before any of them become training data.
