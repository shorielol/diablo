#!/usr/bin/env python3
"""
The Laya specialisation build: the dataset, the adapter, the shadow and the
promotion gate. Laya may classify. It may never decide what Diablo does.

Nothing here loads a model, needs the Laya package, a GPU or a network: the
checkpoint, the router and the clock are stood in for.
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

TRACE = os.path.join(tempfile.mkdtemp(prefix="diablo-laya-"), "trace.jsonl")
os.environ["DIABLO_TRACE"] = TRACE

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH = os.path.join(REPO, "research", "laya_diablo")
sys.path.insert(0, os.path.join(RESEARCH, "eval"))
sys.path.insert(0, os.path.join(RESEARCH, "dataset"))

from skills import computer_use, decision, trace  # noqa: E402
import promotion  # noqa: E402
import eval_laya_diablo as evaluation  # noqa: E402
import build_dataset as builder  # noqa: E402

VERSION = "v1"
SPLITS = ("train", "validation", "calibration", "test", "test_private")
REQUIRED = {"id": str, "text": str, "history": list, "context": dict, "questions": list,
            "options": dict, "gold_answers": dict, "label_source": str, "difficulty": str,
            "safety_class": str, "split": str, "metadata": dict}


def load(split):
    path = os.path.join(RESEARCH, "dataset", VERSION, f"{split}.jsonl")
    with open(path) as f:
        return [json.loads(line) for line in f]


_UNSET = object()


class Router:
    """A stand-in Laya: answers by name, whatever order the options came in."""

    def __init__(self, intent="factual", screen=0.1, complete=0.9, delay=0.0, fail=False,
                 answers=_UNSET):
        self.intent, self.screen, self.complete = intent, screen, complete
        self.delay, self.fail, self.fixed = delay, fail, answers
        self.calls, self.states, self.orders = 0, [], []

    def predict(self, state, questions):
        self.calls += 1
        self.states.append(state)
        self.orders.append(list(questions["intent"]["criteria"]))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("model fell over")
        if self.fixed is not _UNSET:
            return self.fixed
        probs = {k: (0.9 if k == self.intent else 0.1 / 8) for k in questions["intent"]["criteria"]}
        return {"answers": {"intent": {"choice": self.intent, "probabilities": probs},
                            "needs_screen": {"noul": self.screen},
                            "complete": {"noul": self.complete}}}


def fake_checkpoint(root, temperature=(1.1, 1.2, 1.3), by_options=None, declared=None,
                    weights=b"weights", tamper=False, contract=None):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "model.safetensors"), "wb") as f:
        f.write(weights)
    cfg = {"temperature": list(temperature)}
    if by_options is not None:
        cfg["temperature_by_options"] = by_options
    json.dump(cfg, open(os.path.join(root, "rl_agent_config.json"), "w"))
    manifest = {
        "contract_version": contract or decision.LAYA_CONTRACT_VERSION,
        "base_checkpoint": "convaiinnovations/laya:typed-decisions", "laya_version": "0.3.20",
        "dataset_version": VERSION, "split_hashes": {"train": "x"}, "training": {"epochs": 3},
        "calibration": {"fitted_on_split": "calibration", "temperature": list(temperature),
                        "temperature_by_options": declared or {}},
        "weights_sha256": hashlib.sha256(b"tampered" if tamper else weights).hexdigest(),
    }
    json.dump(manifest, open(os.path.join(root, decision.CHECKPOINT_MANIFEST), "w"))
    return root


def shadow_events():
    return [x for x in trace.read(limit=500) if x.get("stage") == "decide_shadow"]


def run() -> int:
    r = Results()
    data = {s: load(s) for s in SPLITS}
    everything = [e for s in SPLITS for e in data[s]]
    manifest = json.load(open(os.path.join(RESEARCH, "dataset", VERSION, "manifest.json")))

    # ── A, B. Schema and validation ──────────────────────────────────────
    r.head("A-B  dataset schema and validation")
    with guard(r, "schema"):
        bad = [e.get("id") for e in everything
               if any(not isinstance(e.get(k), t) for k, t in REQUIRED.items())]
        r.check("every example has every field, with the right type", not bad, str(bad[:5]))
        contract = json.load(open(os.path.join(RESEARCH, "contract.json")))
        r.check("the research contract is the runtime contract, word for word",
                contract["questions"] == decision.LAYA_QUESTIONS
                and contract["version"] == decision.LAYA_CONTRACT_VERSION)
    with guard(r, "validation"):
        intents = set(decision.LAYA_INTENTS)
        wrong = [e["id"] for e in everything if e["gold_answers"]["intent"] not in intents
                 or not isinstance(e["gold_answers"]["needs_screen"], bool)
                 or not isinstance(e["gold_answers"]["complete"], bool)]
        r.check("every gold answer is inside the contract", not wrong, str(wrong[:5]))
        lanes = [e["id"] for e in everything if e["gold_answers"]["lane"] != decision.lane_for(
            e["gold_answers"]["intent"], e["gold_answers"]["needs_screen"],
            e["gold_answers"]["complete"])]
        r.check("every gold lane follows from its answers by the runtime's rule", not lanes,
                str(lanes[:5]))
        secrets = [e["text"] for e in everything
                   if any(p.search(e["text"]) for p in builder.SECRET_PATTERNS)]
        r.check("no credentials, emails, numbers or keys in any example", not secrets, str(secrets))
        r.check("at least 4,500 examples", len(everything) >= 4500, str(len(everything)))
        cats = {e["gold_answers"]["intent"] for e in data["test"]}
        r.check("the test split covers every intent", cats == intents, str(cats))

    # ── C. Split isolation ───────────────────────────────────────────────
    r.head("C  split isolation")
    with guard(r, "isolation"):
        where, leak = {}, set()
        for e in everything:
            key = (builder.normalise(e["text"]), json.dumps(e["context"], sort_keys=True),
                   json.dumps(e["history"]))
            if key in where and where[key] != e["split"]:
                leak.add(e["text"])
            where[key] = e["split"]
        r.check("no text is in two splits", not leak, str(sorted(leak)[:5]))
        units, spread = {}, set()
        for e in everything:
            u = e["metadata"]["unit"]
            if u in units and units[u] != e["split"]:
                spread.add(u)
            units[u] = e["split"]
        r.check("no template is in two splits", not spread, str(sorted(spread)[:5]))
        for split in SPLITS:
            path = os.path.join(RESEARCH, "dataset", VERSION, f"{split}.jsonl")
            digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
            r.check(f"{split} matches the manifest hash", digest == manifest["split_hashes"][split])
        config = json.load(open(os.path.join(RESEARCH, "configs", "train_v1.json")))
        r.check("training reads train and validation; calibration is separate; test never",
                config["data"]["train"].endswith("train.jsonl")
                and config["data"]["validation"].endswith("validation.jsonl")
                and config["data"]["calibration"].endswith("calibration.jsonl")
                and "test" not in json.dumps(config["data"]))

    # ── D. Provenance ────────────────────────────────────────────────────
    r.head("D  label provenance")
    with guard(r, "provenance"):
        untraced = [e["id"] for e in everything if not re.match(
            r"^(policy:P[1-8]:|policy:final:|adjudicated:)", e["label_source"])]
        r.check("every label names its policy clause or its adjudication", not untraced,
                str(untraced[:5]))
        r.check("no teacher model defined a label",
                manifest["label_sources"]["teacher"] == "none used")

    # ── E. Option order ──────────────────────────────────────────────────
    r.head("E  option-order invariance")
    with guard(r, "order"):
        firsts = {e["options"]["intent"][0] for e in data["train"]}
        r.check("training option orders are shuffled", len(firsts) == len(decision.LAYA_INTENTS))
        engine = evaluation.LayaEngine(predict_raw=Router(intent="guide", screen=0.9).predict)
        ex = data["test"][0]
        verdicts = {engine.predict(ex, order)["intent"] for order in
                    (list(decision.LAYA_INTENTS), list(reversed(decision.LAYA_INTENTS)))}
        r.check("a choice is read by name: reordering options cannot change it",
                verdicts == {"guide"})
        router = Router(intent="guide", screen=0.9)
        engine = evaluation.LayaEngine(predict_raw=router.predict)
        engine.predict(ex, list(reversed(decision.LAYA_INTENTS)))
        r.check("the order asked for is the order sent",
                router.orders[-1] == list(reversed(decision.LAYA_INTENTS)))

    with guard(r, "training sees what inference sees"):
        sys.path.insert(0, os.path.join(RESEARCH, "train"))
        import prepare_items
        mismatched = [e["id"] for e in everything[::7] if prepare_items.diablo_state(e) !=
                      decision.laya_state(e["text"], e["history"], e["context"].get("app", ""),
                                          e["context"].get("window", ""))]
        r.check("the training state is the runtime adapter's state, example for example",
                not mismatched, str(mismatched[:5]))

    # ── F, G, I. Contract and adapter ────────────────────────────────────
    r.head("F-I  the adapter")
    with guard(r, "adapter"):
        v = decision.Laya(router=Router(intent="explanation")).decide("Why is the sky blue?")
        r.check("conversation with no screen, complete: fast",
                v.lane == decision.FAST and v.intent == decision.EXPLANATION)
        v = decision.Laya(router=Router(intent="explanation", screen=0.8)).decide("x")
        r.check("the same, needing the screen: not fast", v.lane != decision.FAST)
        v = decision.Laya(router=Router(intent="factual", complete=0.2)).decide("what is the")
        r.check("cut off: not fast", v.lane != decision.FAST and v.complete is False)
        v = decision.Laya(router=Router(intent="do")).decide("Open Chrome")
        r.check("an action: the agent lane, which only ever means the router's gates",
                v.lane == decision.AGENT)
        router = Router()
        decision.Laya(router=router).judge("open it", {"history": ["a", "b", "c"],
                                                       "app": "Finder", "window": "x" * 500})
        state = router.states[-1]
        r.check("the state is minimal: request, two lines of history, the app, a short title",
                set(state) == {"request", "history", "app", "window"}
                and len(state["history"]) == 2 and len(state["window"]) <= 80)
    with guard(r, "malformed"):
        for answers in (None, {}, {"answers": None}, {"answers": {"intent": {"choice": "do"}}},
                        {"answers": {"intent": {}, "needs_screen": {"noul": "x"},
                                     "complete": {"noul": 1}}}):
            got = decision.Laya(router=Router(answers=answers)).judge("x")
            r.check(f"malformed output {str(answers)[:40]} is no opinion", got is None, str(got))
        r.check("a model that raises is no opinion",
                decision.Laya(router=Router(fail=True)).judge("x") is None)

    # ── H, L, T. Checkpoints ─────────────────────────────────────────────
    r.head("H-L-T  checkpoints: missing, calibration, versioning")
    root = tempfile.mkdtemp(prefix="laya-ckpt-")
    with guard(r, "missing"):
        v = decision.Laya(checkpoint=os.path.join(root, "nope")).decide("What is GDP?")
        r.check("a missing checkpoint is no opinion", v is None)
        ok, why = decision.verify_checkpoint(os.path.join(root, "nope"))
        r.check("  and says why", not ok and "unreadable" in why, why)
    with guard(r, "calibration"):
        good = fake_checkpoint(os.path.join(root, "good"))
        r.check("a checkpoint holding exactly its fitted temperatures is accepted",
                decision.verify_checkpoint(good)[0])
        stale = fake_checkpoint(os.path.join(root, "stale"), by_options={"choice:6-10": 1.0})
        ok, why = decision.verify_checkpoint(stale)
        r.check("an inherited per-bucket temperature is refused", not ok and "inherited" in why, why)
        declared = fake_checkpoint(os.path.join(root, "declared"), by_options={"noul:2": 1.4},
                                   declared={"noul:2": 1.4})
        r.check("a bucket the calibration itself fitted is accepted",
                decision.verify_checkpoint(declared)[0])
        json.dump({"temperature": [1.0, 1.0, 1.0]},
                  open(os.path.join(good, "rl_agent_config.json"), "w"))
        ok, why = decision.verify_checkpoint(good)
        r.check("temperatures that are not the fitted ones are refused", not ok, why)
    with guard(r, "versioning"):
        tampered = fake_checkpoint(os.path.join(root, "tampered"), tamper=True)
        ok, why = decision.verify_checkpoint(tampered)
        r.check("weights that do not match the manifest are refused",
                not ok and "weights" in why, why)
        other = fake_checkpoint(os.path.join(root, "other"), contract="some-other-contract")
        r.check("a checkpoint trained on another contract is refused",
                not decision.verify_checkpoint(other)[0])
        bare = os.path.join(root, "bare")
        os.makedirs(bare)
        json.dump({"contract_version": decision.LAYA_CONTRACT_VERSION},
                  open(os.path.join(bare, decision.CHECKPOINT_MANIFEST), "w"))
        json.dump({}, open(os.path.join(bare, "rl_agent_config.json"), "w"))
        ok, why = decision.verify_checkpoint(bare)
        r.check("a manifest without dataset, splits, training and calibration is refused",
                not ok and "lacks" in why, why)
        r.check("the dataset manifest records its version, seed and every split hash",
                {"dataset_version", "seed", "split_hashes"} <= set(manifest)
                and set(manifest["split_hashes"]) == set(SPLITS))

    # ── J, K, O, R, S. The shadow ────────────────────────────────────────
    r.head("J-S  shadow mode")
    with guard(r, "shadow"):
        open(TRACE, "w").close()
        router = Router(intent="explanation", delay=0.3)
        shadow = decision.Shadow(lambda: decision.Laya(router=router),
                                 context=lambda: {"app": "Terminal"})
        saved = decision._shadow
        decision._shadow = shadow
        try:
            trace.new_turn("test")
            started = time.perf_counter()
            verdict = decision.decide("Explain the diagram in Preview.")
            took = (time.perf_counter() - started) * 1000
            rules_alone = decision.Rules().decide("Explain the diagram in Preview.")
            r.check("the verdict is the rules' verdict, untouched by the shadow",
                    verdict.lane == rules_alone.lane and verdict.intent == rules_alone.intent)
            r.check("a slow shadow does not slow the decision", took < 100, f"{took:.1f} ms")
            time.sleep(0.6)
            logged = shadow_events()
            r.check("the shadow logs both decisions and whether they agree", bool(logged) and
                    {"rules_lane", "shadow_lane", "agree", "p_intent", "ms"} <= set(logged[-1]),
                    str(logged[-1:]))
            r.check("  and a disagreement is recorded as one", bool(logged) and
                    logged[-1]["agree"] is False)
            r.check("  with none of the words", bool(logged) and
                    "diagram" not in json.dumps(logged[-1]).lower())
            r.check("  and it saw the minimal state",
                    bool(router.states) and router.states[-1].get("app") == "Terminal")
            for _ in range(5):
                decision.decide("What is GDP?")
            r.check("a busy shadow drops work instead of queueing it",
                    shadow.stats["dropped"] >= 1, str(shadow.stats))
            errors = []

            def many():
                try:
                    for _ in range(20):
                        v = decision.decide("How are you?")
                        assert v is not None and v.lane == decision.INSTANT
                except Exception as e:
                    errors.append(e)
            threads = [threading.Thread(target=many) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            r.check("concurrent decisions all get the rules' answer, with no error",
                    not errors, str(errors[:2]))
            shadow.stop()
            count = len(shadow_events())
            decision.decide("What is GDP?")
            time.sleep(0.5)
            after = len(shadow_events())
            r.check("a stopped shadow judges nothing more", after == count and
                    not (shadow._thread and shadow._thread.is_alive()), f"{count} -> {after}")
        finally:
            decision._shadow = saved
    with guard(r, "shadow failure"):
        loads = []

        def broken():
            loads.append(1)
            raise ImportError("no laya here")
        shadow = decision.Shadow(broken)
        saved = decision._shadow
        decision._shadow = shadow
        try:
            v = None
            for _ in range(4):
                v = decision.decide("What is GDP?")
                time.sleep(0.05)
            r.check("a shadow that cannot load changes nothing",
                    v is not None and v.lane == decision.FAST)
            r.check("  and is not retried on every turn", len(loads) == 1, str(loads))
        finally:
            decision._shadow = saved
    with guard(r, "off by default"):
        r.check("with no setting, there is no shadow",
                decision.SHADOW == "off" or bool(os.getenv("DIABLO_DECISION_SHADOW")))

    # ── N. Laya has no authority ─────────────────────────────────────────
    r.head("N  safety: Laya has no authority")
    with guard(r, "authority"):
        source = open(decision.__file__).read()
        r.check("the decision module cannot reach tools, the computer or a permission gate",
                not any(name in source for name in (
                    "agent_tools.run", "computer_use", "confirm(", "execute(", "subprocess",
                    "CONTEXT[", "CGEvent", "setMouseLocation")))
        from skills import delegation_tool
        router_source = open(delegation_tool.__file__).read()
        r.check("the router consults only decide(), never a Laya or shadow object",
                "decision.Laya" not in router_source and "Shadow" not in router_source)
        r.check("computer use is off", computer_use.ENABLED is False)
        v = decision.Laya(router=Router(intent="do")).decide("Delete everything")
        r.check("a Laya 'do' is a lane, not a permission: the agent lane, whose gates decide",
                v.lane == decision.AGENT)

    # ── M, P. Evaluation and the gate ────────────────────────────────────
    r.head("M-P  held-out evaluation and the promotion gate")
    with guard(r, "evaluation"):
        rep = evaluation.score(evaluation.RulesEngine(), data["test"])
        rep.pop("_preds")
        r.check("the held-out evaluation reports every required measure",
                {"lane_accuracy", "false_fast", "false_agent", "fast_recall", "per_question",
                 "per_class_recall", "macro_intent_accuracy", "hard_negative_lane_accuracy",
                 "safety", "abstained", "latency_ms", "peak_rss_mb"} <= set(rep))
        r.check("the rules put no held-out safety case on a fast lane",
                rep["safety"]["false_fast"] == 0, str(rep["safety"]))
        t, before, after = evaluation.fit_temperature(
            [({"a": 0.99, "b": 0.01}, "a"), ({"a": 0.99, "b": 0.01}, "b")] * 20, "choice")
        r.check("calibration softens an overconfident model", t > 1 and after < before,
                f"T={t} {before}->{after}")
    with guard(r, "gate"):
        rules = {"lane_accuracy": 0.8, "false_fast": 0, "fast_recall": 0.55}
        perfect = {"lane_accuracy": 0.95, "false_fast": 0, "fast_recall": 0.9,
                   "safety": {"n": 50, "false_fast": 0}, "partials": {"fast_on_partial": 0},
                   "latency_ms": {"p95": 40}, "peak_rss_mb": 900,
                   "calibration": {"intent_ece": 0.03}}
        longrun = {"errors": 0, "rss_growth_mb": 5}
        r.check("a candidate that clears every bar is promoted",
                promotion.decide(rules, perfect, None, longrun, True, True)["decision"] == "PROMOTE")
        unsafe = dict(perfect, safety={"n": 50, "false_fast": 1})
        r.check("one safety false-fast keeps it experimental, however accurate",
                promotion.decide(rules, unsafe, None, longrun, True, True)["decision"]
                == "KEEP_EXPERIMENTAL")
        r.check("a stock checkpoint is never promoted",
                promotion.decide(rules, perfect, None, longrun, False, True)["decision"]
                == "KEEP_EXPERIMENTAL")
        r.check("no long-run evidence, no promotion",
                promotion.decide(rules, perfect, None, None, True, True)["decision"]
                == "KEEP_EXPERIMENTAL")
        slow = dict(perfect, latency_ms={"p95": 400})
        r.check("hybrid is chosen only when laya alone fails and hybrid passes",
                promotion.decide(rules, slow, perfect, longrun, True, True)["decision"] == "HYBRID")

    # ── Q. Partial transcripts ───────────────────────────────────────────
    r.head("Q  partial transcripts")
    with guard(r, "partials"):
        early = [e for e in everything if e["metadata"].get("phenomenon") == "partial"
                 and e["label_source"].startswith("policy:P1")]
        r.check("every early partial is labelled incomplete",
                bool(early) and all(e["gold_answers"]["complete"] is False for e in early))
        rules = decision.Rules()
        risky = []
        for e in early:
            v = rules.decide(e["text"])
            if v is not None and v.lane in (decision.INSTANT, decision.FAST) and \
                    e["text"].split()[0].lower() in ("open", "delete", "send", "explain",
                                                     "where", "research"):
                risky.append(e["text"])
        r.check("rules put no early partial of an action or screen request on a fast lane",
                not risky, str(risky))

    return r.report("laya specialisation")


if __name__ == "__main__":
    sys.exit(run())
