"""Label situations with the rules (or teacher.py) + planner and write training data."""
from __future__ import annotations

import importlib.util
import itertools
import os
import random
from collections import Counter

from . import planner
from .schema import Schema, SchemaError

MAX_ENUMERATE = 200_000


class RulesTeacher:
    """The schema's own rules, behind the same think()/want() interface as a teacher.py."""

    def __init__(self, schema: Schema):
        self.s = schema

    def think(self, s):
        return self.s.think_of(s)

    def want(self, s, th, facts):
        return self.s.want_of(s, th)


def load_teacher(schema: Schema):
    if schema.has_rules:
        return RulesTeacher(schema)
    path = os.path.join(schema.dir, schema.teacher)
    if not os.path.exists(path):
        raise SchemaError(f"teacher file not found: {path}")
    spec = importlib.util.spec_from_file_location(f"{schema.name}_teacher", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for fn in ("think", "want"):
        if not callable(getattr(mod, fn, None)):
            raise SchemaError(f"{path} must define {fn}()")
    return mod


def situations(schema: Schema, limit: int = MAX_ENUMERATE, seed: int = 0):
    """Every combination of input values, or a random sample if there are too many."""
    names = [s.name for s in schema.inputs]
    if schema.situation_count() <= limit:
        for combo in itertools.product(*(s.values for s in schema.inputs)):
            yield dict(zip(names, combo))
    else:
        rng = random.Random(seed)
        for _ in range(limit):
            yield {s.name: rng.choice(s.values) for s in schema.inputs}


def label(schema: Schema, teacher, dom: planner.Domain, s: dict[str, str]) -> dict:
    th = teacher.think(dict(s))
    for slot in schema.think:
        if th.get(slot.name) not in slot.values:
            raise SchemaError(f"teacher.think() returned {th.get(slot.name)!r} for {slot.name!r}; "
                              f"expected one of {slot.values}")
    facts = schema.facts_of(s, th)
    want_names = list(teacher.want(dict(s), dict(th), dict(facts)))
    want = schema.outcome_mask(want_names)
    actions, achieved, cost, sc = planner.plan(dom, planner.fact_mask(dom, facts), want)
    tokens = (["<bos>"] + [slot.token(s[slot.name]) for slot in schema.inputs] + ["<think>"]
              + [slot.token(th[slot.name]) for slot in schema.think] + ["<plan>"]
              + [dom.names[a] for a in actions] + ["<eos>"])
    return {"inputs": s, "think": th, "facts": facts, "want": want, "plan": actions,
            "achieved": achieved, "cost": cost, "tokens": tokens}


def build(schema: Schema, out_dir: str, val_fraction: float = 0.1, seed: int = 0, max_copies: int = 10):
    teacher = load_teacher(schema)
    dom = planner.domain_of(schema)
    rows = [label(schema, teacher, dom, s) for s in situations(schema, seed=seed)]
    return write(schema, dom, rows, out_dir, val_fraction, seed, max_copies)


def write(schema: Schema, dom, rows: list[dict], out_dir: str, val_fraction: float = 0.1, seed: int = 0,
          max_copies: int = 10):
    """Split labeled rows 90/10 and write train.txt / val.txt (rare plans repeated)."""
    rows = list(rows)
    rng = random.Random(seed)
    rng.shuffle(rows)
    n_val = max(1, int(len(rows) * val_fraction))
    val, train = rows[:n_val], rows[n_val:]

    # rare plans get repeated so the model sees them often enough
    plan_counts = Counter(tuple(r["plan"]) for r in train)
    most = max(plan_counts.values())

    os.makedirs(out_dir, exist_ok=True)
    n_train_lines = 0
    with open(os.path.join(out_dir, "train.txt"), "w") as f:
        for r in train:
            copies = min(max_copies, max(1, round((most / plan_counts[tuple(r["plan"])]) ** 0.5)))
            line = " ".join(r["tokens"]) + "\n"
            f.write(line * copies)
            n_train_lines += copies
    with open(os.path.join(out_dir, "val.txt"), "w") as f:
        for r in val:
            f.write(" ".join(r["tokens"]) + "\n")

    all_plans = Counter(" ".join(dom.names[a] for a in r["plan"]) or "(nothing)" for r in rows)
    think_hist = {slot.name: Counter(r["think"][slot.name] for r in rows) for slot in schema.think}
    return {
        "situations": len(rows), "train_lines": n_train_lines, "val": len(val),
        "longest": max(len(r["tokens"]) for r in rows), "distinct_plans": len(all_plans),
        "plans": all_plans, "think": think_hist,
    }
