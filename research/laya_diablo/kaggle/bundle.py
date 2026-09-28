#!/usr/bin/env python3
"""
Builds what Kaggle needs to train, and nothing it must not see.

    python research/laya_diablo/kaggle/bundle.py --user <kaggle username>

    dist/diablo-laya/   the Kaggle Dataset: contract, config, training code, and the
                        train / validation / calibration splits with the dataset manifest.
                        The test and private splits stay on this Mac: training cannot read
                        what was never uploaded.
    dist/kernel/        the notebook and kernel-metadata.json (GPU T4 x2, internet on, private)

With the Kaggle CLI signed in (~/.kaggle/kaggle.json):

    kaggle datasets create -p research/laya_diablo/dist/diablo-laya        # private by default
    kaggle kernels push    -p research/laya_diablo/dist/kernel --accelerator NvidiaTeslaT4
    kaggle kernels status  <user>/laya-diablo-finetune
    kaggle kernels output  <user>/laya-diablo-finetune -p ~/Downloads/laya-diablo-run
"""

import argparse
import hashlib
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
DATASET_SLUG = "diablo-laya"
KERNEL_SLUG = "laya-diablo-finetune"
KERNEL_TITLE = "Laya Diablo Finetune"          # Kaggle derives the slug from the title
NOTEBOOK = "laya_diablo_finetune_2xT4_kaggle.ipynb"
TRAINING_SPLITS = ("train", "validation", "calibration")
UPLOAD = ["contract.json", "configs/train_v1.json", "train/prepare_items.py",
          "train/train_diablo.py", "dataset/v1/manifest.json"] + \
         [f"dataset/v1/{s}.jsonl" for s in TRAINING_SPLITS]
MARKER = "BUNDLE.json"


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def fresh(path):
    """An empty directory; only ever clears one this script built."""
    if os.path.exists(path):
        if not os.path.exists(os.path.join(path, MARKER)):
            raise SystemExit(f"{path} exists and was not built here; not touching it")
        shutil.rmtree(path)
    os.makedirs(path)
    return path


def build(out, user):
    dataset_dir = fresh(os.path.join(out, DATASET_SLUG))
    kernel_dir = fresh(os.path.join(out, "kernel"))
    with open(os.path.join(RESEARCH, "dataset", "v1", "manifest.json")) as f:
        manifest = json.load(f)
    files = {}
    for rel in UPLOAD:
        target = os.path.join(dataset_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(os.path.join(RESEARCH, rel), target)
        files[rel] = sha256(target)
    for split in TRAINING_SPLITS:
        if files[f"dataset/v1/{split}.jsonl"] != manifest["split_hashes"][split]:
            raise SystemExit(f"{split} does not match the dataset manifest")
    json.dump({"title": "Diablo decision data v1 (training splits)",
               "id": f"{user}/{DATASET_SLUG}", "licenses": [{"name": "other"}]},
              open(os.path.join(dataset_dir, "dataset-metadata.json"), "w"), indent=1)
    json.dump({"files": files, "held_out_not_uploaded": ["test", "test_private"]},
              open(os.path.join(dataset_dir, MARKER), "w"), indent=1)

    shutil.copy2(os.path.join(RESEARCH, "notebooks", NOTEBOOK), os.path.join(kernel_dir, NOTEBOOK))
    json.dump({"id": f"{user}/{KERNEL_SLUG}", "title": KERNEL_TITLE, "code_file": NOTEBOOK,
               "language": "python", "kernel_type": "notebook", "is_private": "true",
               "enable_gpu": "true", "enable_internet": "true", "machine_shape": "NvidiaTeslaT4",
               "dataset_sources": [f"{user}/{DATASET_SLUG}"], "competition_sources": [],
               "kernel_sources": [], "model_sources": []},
              open(os.path.join(kernel_dir, "kernel-metadata.json"), "w"), indent=1)
    json.dump({"notebook": sha256(os.path.join(kernel_dir, NOTEBOOK))},
              open(os.path.join(kernel_dir, MARKER), "w"), indent=1)
    return {"dataset": dataset_dir, "kernel": kernel_dir, "files": files}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=os.getenv("KAGGLE_USERNAME", ""),
                    help="Kaggle username (or KAGGLE_USERNAME)")
    ap.add_argument("--out", default=os.path.join(RESEARCH, "dist"))
    args = ap.parse_args()
    if not args.user:
        raise SystemExit("--user <kaggle username> is needed for the dataset and kernel ids")
    print(json.dumps(build(args.out, args.user), indent=1))


if __name__ == "__main__":
    main()
