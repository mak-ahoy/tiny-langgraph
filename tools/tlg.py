#!/usr/bin/env python3
"""
tiny-langgraph toolchain.

  python tools/tlg.py check  path/to/schema.yaml   validate, show tokens and situation count
  python tools/tlg.py gen    path/to/schema.yaml   C code       -> <dir>/generated/
  python tools/tlg.py data   path/to/schema.yaml   training data -> <dir>/data/
  python tools/tlg.py train  path/to/schema.yaml   model        -> <dir>/model/<name>.bin   (needs PyTorch)
  python tools/tlg.py all    path/to/schema.yaml   gen + data + train
  python tools/tlg.py embed  model.bin out.h        model as a C array for firmware

Needs PyYAML (pip install pyyaml). Only 'train' needs PyTorch.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

try:
    import yaml  # noqa: F401
except ImportError:
    sys.exit("tiny-langgraph needs PyYAML. Install it with:\n    python -m pip install pyyaml")

from tiny_langgraph.toolchain import codegen, dataset, planner, schema as schema_mod  # noqa: E402
from tiny_langgraph.toolchain.schema import SchemaError  # noqa: E402


def cmd_check(s):
    print(f"{s.name}: {s.description}")
    print(f"  inputs   : {', '.join(f'{x.name}({len(x.values)})' for x in s.inputs)}")
    print(f"  think    : {', '.join(f'{x.name}({len(x.values)})' for x in s.think)}")
    if s.scores:
        print(f"  scores   : {', '.join(s.scores)}")
    print(f"  facts    : {', '.join(s.facts) or '-'}")
    print(f"  outcomes : {', '.join(o.name + ('*' if o.only_if_wanted else '') for o in s.outcomes)}"
          "   (* = only if wanted)")
    print(f"  actions  : {', '.join(a.name + ('!' if a.approval else '') for a in s.actions)}"
          "   (! = needs approval)")
    print(f"  vocabulary {len(s.tokens)} tokens, longest sequence {s.seq_len}, "
          f"{s.situation_count():,} possible situations")
    if s.has_rules:
        n_think = sum(len(r) for r in s.think_rules.values())
        print(f"  rules    : {n_think} think rules, {len(s.want_rules)} want rules")
        teacher = dataset.load_teacher(s)
        dom = planner.domain_of(s)
        for sit in dataset.situations(s):           # every situation gets an answer
            dataset.label(s, teacher, dom, sit)
        print("  rules    : ok in every situation")
    else:
        dataset.load_teacher(s)
        print(f"  teacher  : {s.teacher} ok")


def cmd_gen(s):
    for p in codegen.generate(s, os.path.join(s.dir, "generated"), os.path.basename(s.path)):
        print(f"wrote {os.path.relpath(p)}")


def cmd_data(s):
    st = dataset.build(s, os.path.join(s.dir, "data"), seed=s.model["seed"])
    print(f"{st['situations']:,} situations -> train {st['train_lines']:,} lines, val {st['val']:,}")
    print(f"longest sequence {st['longest']} tokens, {st['distinct_plans']} distinct plans")
    for slot, hist in st["think"].items():
        print(f"  {slot:8s} " + "  ".join(f"{k}={v}" for k, v in sorted(hist.items())))
    print("  most common plans:")
    for plan, n in st["plans"].most_common(6):
        print(f"    {n:6d}  {plan}")


def cmd_train(s):
    from tiny_langgraph.toolchain import train
    out = os.path.join(s.dir, "model", f"{s.name}.bin")
    train.train(s, os.path.join(s.dir, "data"), out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="tiny-langgraph toolchain")
    ap.add_argument("command", choices=["check", "gen", "data", "train", "all", "embed"])
    ap.add_argument("path", help="schema.yaml (or model.bin for embed)")
    ap.add_argument("out", nargs="?", help="output header for embed")
    args = ap.parse_args(argv)

    try:
        if args.command == "embed":
            from tiny_langgraph.toolchain import train
            if not args.out:
                ap.error("embed needs an output path: tlg.py embed model.bin out.h")
            sym = os.path.splitext(os.path.basename(args.path))[0] + "_model"
            train.embed(args.path, args.out, sym.replace("-", "_"))
            print(f"wrote {args.out}")
            return 0

        s = schema_mod.load(args.path)
        steps = {"check": [cmd_check], "gen": [cmd_gen], "data": [cmd_data],
                 "train": [cmd_train], "all": [cmd_check, cmd_gen, cmd_data, cmd_train]}[args.command]
        for step in steps:
            step(s)
        return 0
    except SchemaError as e:
        print(f"schema error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
