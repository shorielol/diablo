#!/usr/bin/env python3
"""
Fine-tunes Laya typed-decisions on the Diablo decision dataset.

Adapted from upstream's 2xT4 notebook (NandhaKishorM/laya, v0.3.20): the same
RLCD objective (Gaussian exploration over logits, proper-scoring reward,
group baseline) plus soft cross-entropy, with these Diablo changes:

  - per-item weights (class balance, hard negatives, safety) in the loss;
  - validation decoded into Diablo lanes every epoch, and the epoch kept is
    the one with the fewest false-fast, then the best lane accuracy. Never
    the lowest training loss;
  - temperatures fitted on Diablo's own calibration split, never a slice of
    train, and temperature_by_options removed so no inherited bucket can
    override them;
  - diablo_manifest.json, which the runtime's verify_checkpoint requires.

    torchrun --standalone --nproc_per_node=2 train_diablo.py \
        --model-dir <laya>/typed-decisions --items items/ --out out/ \
        --config ../configs/train_v1.json --dataset-manifest ../dataset/v1/manifest.json

    python train_diablo.py ... --dry-run     # CPU, no optimiser step: forward, loss, and
                                             # the save path (verify_checkpoint refuses it)

The test splits are never read here.
"""

import argparse
import hashlib
import json
import math
import os
import random
import time

CONVERSATION = ("casual", "factual", "explanation")


def collate(items, pad_id):
    import torch
    n, L = len(items), max(len(it["ids"]) for it in items)
    kmax = max(len(it["markers"]) for it in items)
    ids = torch.full((n, L), pad_id, dtype=torch.long)
    att = torch.zeros((n, L), dtype=torch.long)
    mpos = torch.zeros((n, kmax), dtype=torch.long)
    mmask = torch.zeros((n, kmax), dtype=torch.bool)
    target = torch.zeros((n, kmax), dtype=torch.float32)
    for i, it in enumerate(items):
        ids[i, : len(it["ids"])] = torch.tensor(it["ids"])
        att[i, : len(it["ids"])] = 1
        k = len(it["markers"])
        mpos[i, :k] = torch.tensor(it["markers"])
        mmask[i, :k] = True
        target[i, : len(it["target"])] = torch.tensor(it["target"], dtype=torch.float32)
    return {"input_ids": ids, "attention_mask": att, "marker_pos": mpos, "marker_mask": mmask,
            "target": target, "qtype": torch.tensor([it["qtype"] for it in items]),
            "weight": torch.tensor([float(it.get("weight", 1.0)) for it in items])}


def loss_for(model, batch, device, sigma, rl, proper_reward, amp=True):
    import torch
    with torch.autocast(device.type, dtype=torch.float16, enabled=amp and device.type == "cuda"):
        logits, act = model(batch["input_ids"].to(device), batch["attention_mask"].to(device),
                            batch["marker_pos"].to(device), batch["marker_mask"].to(device),
                            batch["qtype"].to(device))
    logits = logits.float()
    mask = batch["marker_mask"].to(device)
    k = mask.sum(-1, keepdim=True).float()
    target = batch["target"].to(device)
    weight = batch["weight"].to(device)
    eps = torch.randn((rl["group_size"],) + logits.shape, device=device) * sigma * mask
    eps = (eps - eps.sum(-1, keepdim=True) / k) * mask
    z = logits.detach().unsqueeze(0) + eps
    q = torch.softmax(z.masked_fill(~mask, -1e4), -1)
    with torch.no_grad():
        r = proper_reward(q, target.unsqueeze(0), batch["qtype"].to(device), mask,
                          w_sph=rl["w_sph"], w_rps=rl["w_rps"])
        adv = (r - r.mean(0, keepdim=True)) / (r.std() + 1e-6)
    logp = -(((z - logits.unsqueeze(0)) ** 2) * mask).sum(-1) / (2 * sigma ** 2)
    loss_rl = -(adv * logp * weight.unsqueeze(0)).mean()
    ce = -(target * torch.log_softmax(logits.masked_fill(~mask, -1e4), -1)).sum(-1)
    loss_ce = (ce * weight).sum() / weight.sum()
    return loss_rl + rl["ce_weight"] * loss_ce + 0.0 * act.sum(), logits


def predict_items(model, items, pad_id, device, chunk=16):
    """(item, logits over its options) for every item, no gradients."""
    import torch
    out = []
    model.eval()
    with torch.no_grad():
        for i in range(0, len(items), chunk):
            part = items[i:i + chunk]
            b = collate(part, pad_id)
            logits, _ = model(b["input_ids"].to(device), b["attention_mask"].to(device),
                              b["marker_pos"].to(device), b["marker_mask"].to(device),
                              b["qtype"].to(device))
            logits = logits.float().cpu()
            for j, it in enumerate(part):
                out.append((it, logits[j, :len(it["markers"])].tolist()))
    model.train()
    return out


def lane_metrics(predictions):
    """Decode per-question predictions into Diablo lanes, per example."""
    by_example = {}
    for it, logits in predictions:
        m = max(logits)
        p = [math.exp(v - m) for v in logits]
        s = sum(p)
        p = [v / s for v in p]
        entry = by_example.setdefault(it["example_id"], {})
        if it["question"] == "intent":
            entry["intent"] = it["options"][p.index(max(p))]
            entry["gold_intent"] = it["options"][it["label"]]
        else:
            entry[it["question"]] = p[1] >= 0.5
            entry["gold_" + it["question"]] = it["label"] == 1

    def fast(intent, screen, complete):
        return intent in CONVERSATION and not screen and complete
    ff = acc = n = intent_ok = 0
    per_class = {}
    for e in by_example.values():
        if not {"intent", "needs_screen", "complete"} <= set(e):
            continue
        n += 1
        pred = fast(e["intent"], e["needs_screen"], e["complete"])
        gold = fast(e["gold_intent"], e["gold_needs_screen"], e["gold_complete"])
        ff += int(pred and not gold)
        acc += int(pred == gold)
        intent_ok += int(e["intent"] == e["gold_intent"])
        c = per_class.setdefault(e["gold_intent"], [0, 0])
        c[0] += int(e["intent"] == e["gold_intent"])
        c[1] += 1
    macro = sum(a / b for a, b in per_class.values()) / max(1, len(per_class))
    return {"examples": n, "false_fast": ff, "lane_accuracy": round(acc / max(1, n), 4),
            "intent_accuracy": round(intent_ok / max(1, n), 4), "macro_intent": round(macro, 4)}


def fit_temperature(pairs):
    """
    One temperature for a set of (logits, soft target), by NLL (upstream's LBFGS fit),
    within the range Laya applies at load: a fit outside it would be recorded but clamped.
    """
    import torch
    from laya.common import TEMP_MAX, TEMP_MIN
    if len(pairs) < 10:
        return 1.0
    kmax = max(len(z) for z, _ in pairs)
    Z = torch.full((len(pairs), kmax), -1e4)
    T = torch.zeros((len(pairs), kmax))
    for i, (z, t) in enumerate(pairs):
        Z[i, :len(z)] = torch.tensor(z)
        T[i, :len(t)] = torch.tensor(t, dtype=torch.float32)
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = -(T * torch.log_softmax(Z / log_t.exp(), -1)).sum(-1).mean()
        loss.backward()
        return loss
    opt.step(closure)
    return float(torch.clamp(log_t.exp(), TEMP_MIN, TEMP_MAX).item())


def nll(pairs, t=1.0):
    """Mean cross-entropy of (logits, soft target) pairs at temperature t."""
    total = 0.0
    for z, target in pairs:
        scaled = [v / t for v in z]
        m = max(scaled)
        log_z = m + math.log(sum(math.exp(v - m) for v in scaled))
        total -= sum(p * (v - log_z) for p, v in zip(target, scaled))
    return total / max(1, len(pairs))


def selection_key(metrics):
    """Validation lane safety first, then lane accuracy, then macro intent. Never loss."""
    return (-metrics["false_fast"], metrics["lane_accuracy"], metrics["macro_intent"])


def calibrate(model, items, pad_id, device):
    """One temperature per question type, fitted on the calibration split only."""
    preds = predict_items(model, items, pad_id, device)
    temps, before, after = [], {}, {}
    for qtype, name in ((0, "choice"), (1, "score"), (2, "noul")):
        pairs = [(z, it["target"]) for it, z in preds if it["qtype"] == qtype]
        t = fit_temperature(pairs) if pairs else 1.0
        temps.append(t)
        if pairs:
            before[name], after[name] = round(nll(pairs), 4), round(nll(pairs, t), 4)
    return {"fitted_on_split": "calibration", "temperature": temps,
            "temperature_by_options": {}, "items": len(preds),
            "nll_before": before, "nll_after": after}


def environment():
    import torch
    import transformers
    n = torch.cuda.device_count() if torch.cuda.is_available() else 0
    return {"torch": torch.__version__, "transformers": transformers.__version__,
            "cuda": torch.version.cuda, "gpus": [torch.cuda.get_device_name(i) for i in range(n)],
            "platform": os.environ.get("KAGGLE_KERNEL_RUN_TYPE", "local")}


def manifest_for(cfg_run, dataset, questions, *, laya_version, history, optimizer_steps,
                 world_size, environment, run_id, calibration, weights_sha256, dry_run=False):
    """The diablo_manifest.json the runtime's verify_checkpoint reads."""
    base = cfg_run["base"]
    return {
        "contract_version": cfg_run["contract_version"],
        "questions": questions,
        "base_checkpoint": f"{base['repo']}:{base['subfolder']}",
        "base_revision": base["revision"],
        "laya_version": laya_version,
        "dataset_version": dataset.get("dataset_version", cfg_run["data"]["dataset_version"]),
        "split_hashes": dataset.get("split_hashes", {}),
        "run_id": run_id,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": environment,
        "training": {"config": cfg_run, "world_size": world_size, "dry_run": dry_run,
                     "optimizer_steps": optimizer_steps,
                     "best_epoch": max(history, key=selection_key)["epoch"] if history else None,
                     "validation_by_epoch": history},
        "calibration": calibration,
        "weights_sha256": weights_sha256,
    }


def save_checkpoint(out, state, model, tok, cfg, calibration, trained):
    """Weights, encoder config, tokenizer and the calibrated config; returns the weights hash."""
    from safetensors.torch import save_file
    os.makedirs(out, exist_ok=True)
    save_file(state, os.path.join(out, "model.safetensors"))
    model.encoder.config.save_pretrained(os.path.join(out, "encoder"))
    tok.save_pretrained(os.path.join(out, "tokenizer"))
    cfg = dict(cfg, fine_tuned=trained, model_name="laya-diablo-decision",
               temperature=calibration["temperature"])
    cfg.pop("temperature_by_options", None)   # the fit is per type; no bucket may override it
    json.dump(cfg, open(os.path.join(out, "rl_agent_config.json"), "w"), indent=2)
    return sha256(os.path.join(out, "model.safetensors"))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--items", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--dataset-manifest", default="")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--dry-run", action="store_true",
                    help="CPU, no optimiser step: forward, loss, decode, and the save path")
    args = ap.parse_args()

    import torch
    from safetensors.torch import load_file
    from transformers import AutoTokenizer
    from laya.common import build_model, proper_reward

    cfg_run = json.load(open(args.config))
    opt_cfg, rl = cfg_run["optimisation"], cfg_run["rlcd"]
    ddp = "LOCAL_RANK" in os.environ and not args.dry_run
    if ddp:
        import torch.distributed as dist
        from torch.nn.parallel import DistributedDataParallel as DDP
        dist.init_process_group("nccl")
        rank, world = dist.get_rank(), dist.get_world_size()
        local = int(os.environ["LOCAL_RANK"])
        torch.cuda.set_device(local)
        device = torch.device("cuda", local)
    else:
        rank, world = 0, 1
        device = torch.device("cuda" if torch.cuda.is_available() and not args.dry_run else "cpu")

    cfg = json.load(open(os.path.join(args.model_dir, "rl_agent_config.json")))
    cfg.update(gradient_checkpointing=True, max_len=cfg_run["sequence"]["max_len"],
               head_max_len=cfg_run["sequence"]["head_max_len"])
    tok = AutoTokenizer.from_pretrained(os.path.join(args.model_dir, "tokenizer"))
    model = build_model(cfg, encoder_dir=os.path.join(args.model_dir, "encoder"))
    model.load_state_dict(load_file(os.path.join(args.model_dir, "model.safetensors")), strict=True)
    model.to(device)
    model.train()

    train = torch.load(os.path.join(args.items, "train.pt"), weights_only=False)
    valid = torch.load(os.path.join(args.items, "validation.pt"), weights_only=False)
    calib = torch.load(os.path.join(args.items, "calibration.pt"), weights_only=False)

    if args.dry_run:
        batch = collate(train[:2], tok.pad_token_id)
        with torch.no_grad():
            loss, logits = loss_for(model, batch, device, rl["sigma_start"], rl, proper_reward,
                                    amp=False)
        metrics = lane_metrics(predict_items(model, valid[:30], tok.pad_token_id, device))
        state = {k: v.detach().half().contiguous().cpu() for k, v in model.state_dict().items()}
        manifest = finish(args, cfg_run, cfg, state, model, tok, calib[:60], device, [], 0, 1,
                          dry_run=True)
        print(json.dumps({"dry_run": True, "loss": round(float(loss), 4),
                          "logits_shape": list(logits.shape), "train_items": len(train),
                          "validation_items": len(valid), "calibration_items": len(calib),
                          "validation_decode_check": metrics, "saved": args.out,
                          "calibration": manifest["calibration"],
                          "optimizer_steps": manifest["training"]["optimizer_steps"]}))
        return

    model.encoder.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False})
    model.head_checkpointing = True
    net = DDP(model, device_ids=[device.index], find_unused_parameters=True) if ddp else model
    mine = train[rank::world]
    enc = [p for n, p in net.named_parameters() if "encoder." in n]
    head = [p for n, p in net.named_parameters() if "encoder." not in n]
    optimizer = torch.optim.AdamW([{"params": enc, "lr": opt_cfg["lr_encoder"]},
                                   {"params": head, "lr": opt_cfg["lr_head"]}],
                                  weight_decay=opt_cfg["weight_decay"])
    mb, accum, epochs = opt_cfg["micro_batch"], opt_cfg["grad_accum"], opt_cfg["epochs"]
    total = max(1, (len(mine) // (mb * accum)) * epochs)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total, eta_min=1e-6)
    scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda")
    history, best, best_key, optimizer_steps = [], None, None, 0
    started = time.time()
    for epoch in range(epochs):
        random.Random(42 + epoch + rank).shuffle(mine)
        sigma = rl["sigma_start"] + (rl["sigma_end"] - rl["sigma_start"]) * epoch / max(1, epochs - 1)
        optimizer.zero_grad(set_to_none=True)
        step = 0
        for i in range(0, len(mine), mb):
            loss, _ = loss_for(net, collate(mine[i:i + mb], tok.pad_token_id), device, sigma, rl,
                               proper_reward)
            scaler.scale(loss / accum).backward()
            step += 1
            if step % accum == 0 or i + mb >= len(mine):
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(net.parameters(), opt_cfg["clip_grad_norm"])
                scaler.step(optimizer)
                scaler.update()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
                optimizer_steps += 1
        if ddp:
            dist.barrier()
        if rank == 0:
            metrics = lane_metrics(predict_items(model, valid, tok.pad_token_id, device))
            metrics.update(epoch=epoch + 1, minutes=round((time.time() - started) / 60, 1))
            history.append(metrics)
            print(json.dumps(metrics), flush=True)
            key = selection_key(metrics)
            if best_key is None or key > best_key:
                best_key = key
                best = {k: v.detach().half().contiguous().cpu()
                        for k, v in model.state_dict().items()}
        if ddp:
            dist.barrier()

    if rank == 0:
        model.load_state_dict({k: v.float() for k, v in best.items()})
        model.to(device)
        manifest = finish(args, cfg_run, cfg, best, model, tok, calib, device, history,
                          optimizer_steps, world)
        print("saved", args.out, "run", manifest["run_id"], "steps", optimizer_steps,
              "temperatures", manifest["calibration"]["temperature"], flush=True)
    if ddp:
        dist.destroy_process_group()


def finish(args, cfg_run, cfg, state, model, tok, calib, device, history, steps, world,
           dry_run=False):
    """Calibrate on the calibration split, save the checkpoint, write its manifest."""
    import laya
    calibration = calibrate(model, calib, tok.pad_token_id, device)
    weights = save_checkpoint(args.out, state, model, tok, cfg, calibration, trained=steps > 0)
    json.dump(history, open(os.path.join(args.out, "validation_by_epoch.json"), "w"), indent=1)
    dataset = json.load(open(args.dataset_manifest)) if args.dataset_manifest else {}
    research = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    questions = json.load(open(os.path.join(research, "contract.json")))["questions"]
    run_id = args.run_id or f"{cfg_run['run_name']}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    manifest = manifest_for(cfg_run, dataset, questions,
                            laya_version=getattr(laya, "__version__", "unknown"),
                            history=history, optimizer_steps=steps, world_size=world,
                            environment=environment(), run_id=run_id, calibration=calibration,
                            weights_sha256=weights, dry_run=dry_run)
    json.dump(manifest, open(os.path.join(args.out, "diablo_manifest.json"), "w"), indent=1)
    return manifest


if __name__ == "__main__":
    main()
