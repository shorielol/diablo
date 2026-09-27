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
| `eval/eval_laya_diablo.py` | Scores rules, Laya and hybrid on the same examples; calibration, option order, option count, long run |
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
- **Every checkpoint carries `diablo_manifest.json`**: contract, base, Laya version, dataset version,
  split hashes, training parameters, calibration and a weights hash. No manifest, no load.

## Training (external GPU)

1. Upload this folder to Kaggle as a Dataset named `diablo-laya`.
2. Open `notebooks/laya_diablo_finetune_2xT4_kaggle.ipynb`, choose `GPU T4 x2`, Internet on, run all.
3. Download `laya-diablo-decision-v1.zip`, unzip into `checkpoints/laya-diablo-decision-v1/`.

Local check without training: `python train/train_diablo.py ... --dry-run` runs one forward pass and
the loss on CPU.

## Evaluating a checkpoint

    python eval/eval_laya_diablo.py score --engine rules --out reports/rules_test.json
    python eval/eval_laya_diablo.py score --engine laya   --checkpoint checkpoints/<name> --device mps --out reports/laya_ft_test.json
    python eval/eval_laya_diablo.py score --engine hybrid --checkpoint checkpoints/<name> --device mps --out reports/hybrid_ft_test.json
    python eval/eval_laya_diablo.py longrun --checkpoint checkpoints/<name> --device mps --out reports/longrun_ft.json
    python eval/promotion.py --rules reports/rules_test.json --laya reports/laya_ft_test.json \
        --hybrid reports/hybrid_ft_test.json --longrun reports/longrun_ft.json \
        --fine-tuned true --calibrated true

These need `laya==0.3.20`, `transformers`, `torch` and `DIABLO_HOME` pointing at a Diablo install.

## Shadow mode

    DIABLO_DECISION_SHADOW=laya DIABLO_LAYA_CHECKPOINT=/path/to/checkpoint

Rules stay authoritative. The shadow judges the same utterance on one background thread, drops work
when busy, and logs `decide_shadow` trace events with both decisions, probabilities and timing. It
logs no words. Disagreements are reviewed by a person before any of them become training data.
