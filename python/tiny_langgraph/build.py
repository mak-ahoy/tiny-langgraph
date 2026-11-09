"""
app.build(target): everything between your Python file and a board.

  1. convert your functions to C              -> <agent dir>/generated/
  2. label every situation with your @rules   -> <agent dir>/data/              (only with a reason node)
  3. train the model when the rules changed   -> <agent dir>/model/<name>.bin   (needs PyTorch; ditto)
  4. check the C decides exactly like your Python in every situation (needs a C compiler)
  5. check it fits the board, make its project -> <agent dir>/build/<name>/

target: a board from boards.py ("esp32s3", "nodemcu", ...) or "c" (just the C files).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile

from . import boards, cagent
from .drivers import Camera, Driver
from .toolchain import dataset, planner

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class BuildError(Exception):
    pass


def _cc():
    for name in (os.environ.get("CC"), "cc", "gcc", "clang"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def label_all(app, limit=dataset.MAX_ENUMERATE, seed=0) -> list[dict]:
    """Every situation (or a sample if there are too many), labeled by running your @rules."""
    sc, dom, st = app.schema, app.domain, app.state_schema
    rows = []
    for sit in dataset.situations(sc, limit=limit, seed=seed):
        s = st(**sit)
        app.run_rules(s)
        th = {f.name: getattr(s, f.name) for f in st.thinks()}
        acts, achieved, cost, _ = planner.plan(dom, s._agent.facts, s._agent.want, states=app.plan_states)
        tokens = (["<bos>"] + [f"{k}_{v}" for k, v in sit.items()] + ["<think>"]
                  + [f"{k}_{v}" for k, v in th.items()] + ["<plan>"] + [dom.names[a] for a in acts] + ["<eos>"])
        rows.append({"inputs": sit, "think": th, "facts": s._agent.facts, "want": s._agent.want,
                     "plan": acts, "achieved": achieved, "cost": cost, "tokens": tokens})
    return rows


HARNESS = """#include "{p}_schema.h"
#include <stdio.h>
static uint32_t workspace[{ws_words}];                  /* the planner's, as on the board */
/* every situation: index | think values | want | plan */
int main(void)
{{
    tlg_plan_workspace(workspace, sizeof workspace);
    {p}_inputs_t in;
    for (uint32_t i = 0; {p}_situation(i, &in); i++) {{
        {p}_think_t th = {{0}};
        uint32_t want = 0;
        {p}_rules(&in, &th, &want);
        uint32_t facts = {p}_facts(&in, &th);
        tlg_plan_t p;
        tlg_plan(&{p}_domain, facts, want, 0, &p);
        printf("%u|{fmt}|%u|", (unsigned)i{args}, (unsigned)want);
        for (int k = 0; k < p.len; k++) printf("%s%d", k ? " " : "", p.actions[k]);
        printf("\\n");
    }}
    return 0;
}}
"""


def crosscheck(app, gen_dir, rows, cc) -> int:
    """Compile the converted rules with the C planner and compare with Python on every situation."""
    p, st = app.name, app.state_schema
    work = tempfile.mkdtemp(prefix="tlg_check_")
    harness = os.path.join(work, "check.c")
    ws_words = (planner.STATE_BYTES * app.plan_states + 3) // 4 if len(app.goals) > planner.SMALL_OUTCOMES else 1
    _write(harness, HARNESS.format(p=p, ws_words=ws_words, fmt=" ".join(["%d"] * len(st.thinks())),
                                   args="".join(f", th.{f.name}" for f in st.thinks())))
    exe = os.path.join(work, "check")
    cmd = [cc, "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror", "-I", gen_dir, "-I", os.path.join(ROOT, "core"),
           "-o", exe, harness, os.path.join(gen_dir, f"{p}_schema.c"), os.path.join(gen_dir, f"{p}_rules.c"),
           os.path.join(ROOT, "core", "tlg_plan.c")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise BuildError("the converted rules don't compile (please report this):\n" + r.stderr[-2000:])
    out = subprocess.run([exe], check=True, capture_output=True, text=True).stdout.splitlines()
    by_index = {}
    for line in out:
        idx, th, want, plan = line.split("|")
        by_index[int(idx)] = ([int(x) for x in th.split()], int(want), [int(x) for x in plan.split()] if plan else [])
    names = [f.name for f in st.inputs()]
    counts = [len(f.values) for f in st.inputs()]
    bad = []
    for r_ in rows:
        index = 0
        for name, n in zip(names, counts):
            index = index * n + st._fields[name].values.index(r_["inputs"][name])
        th = [st._fields[k].values.index(v) for k, v in r_["think"].items()]
        if by_index.get(index) != (th, r_["want"], r_["plan"]):
            bad.append((r_["inputs"], by_index.get(index), (th, r_["want"], r_["plan"])))
    if bad:
        sit, c_said, py_said = bad[0]
        raise BuildError(f"C and Python disagree in {len(bad)} situations (please report this), e.g. {sit}: "
                         f"C {c_said}, Python {py_said}")
    return len(rows)


def compile_agent(app, gen_dir, cc, custom_c):
    """Compile the whole generated agent for the PC, as a check that the C is sound."""
    work = tempfile.mkdtemp(prefix="tlg_agent_")
    srcs = [os.path.join(gen_dir, f"{app.name}_{x}.c") for x in ("schema", "rules", "agent")]
    srcs += [os.path.join(ROOT, "core", f) for f in ("tlg.c", "tlg_plan.c", "tlg_trace.c", "tlg_agent.c", "tlg_count.c", "tlg_net.c", "tlg_mcp.c")]
    srcs += [os.path.join(ROOT, "lm", "tlg_lm.c")] if app.uses_model else []
    srcs += [os.path.join(ROOT, "drivers", "tlg_drivers_host.c")]
    objs = []
    for src in srcs:
        o = os.path.join(work, os.path.basename(src) + ".o")
        cmd = [cc, "-std=c11", "-Wall", "-Wextra", "-Wpedantic", "-Werror", "-c", "-o", o, src,
               "-I", gen_dir, "-I", os.path.join(ROOT, "core"), "-I", os.path.join(ROOT, "lm"),
               "-I", os.path.join(ROOT, "drivers")]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise BuildError(f"{os.path.basename(src)} doesn't compile (please report this):\n{r.stderr[-2000:]}")
        objs.append(o)
    del custom_c


def check_pins(app, conv, board="esp32s3", warn=None):
    """Does the agent fit the board (pins, camera, model)? See boards.py."""
    try:
        boards.check(boards.get(board) if isinstance(board, str) else board, app, conv, warn or (lambda m: None))
    except boards.BoardError as e:
        raise BuildError(str(e)) from None


def _digest(paths, extra) -> str:
    h = hashlib.sha256()
    for p in paths:
        with open(p, "rb") as f:
            h.update(f.read().replace(b"\r\n", b"\n"))      # same digest on Windows and Linux
    h.update(json.dumps(extra, sort_keys=True).encode())
    return h.hexdigest()[:16]


def train_if_needed(app, data_dir, retrain, log):
    from .toolchain import train as trainer
    files = [os.path.join(data_dir, "train.txt"), os.path.join(data_dir, "val.txt")]
    digest = _digest(files, {"model": app.schema.model, "tokens": app.schema.tokens})
    meta_path = os.path.join(app.dir, "model", f"{app.name}.json")
    if not retrain and os.path.exists(app.model_path) and os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        if meta.get("data") == digest:
            log(f"model: up to date ({meta.get('val_exact', 0):.2%} on held-out situations)")
            return meta
    try:
        import torch  # noqa: F401
    except ImportError:
        log("model: NOT trained: PyTorch isn't installed (pip install torch, or train on Colab: "
            "python agent.py build there). The agent still works: the planner decides.")
        return None
    log("model: training (the rules or tools changed)...")
    stats = trainer.train(app.schema, data_dir, app.model_path, log=lambda m: log("  " + m))
    meta = {"data": digest, **stats}
    _write(meta_path, json.dumps(meta, indent=2) + "\n")
    app._lm_loaded = False
    return meta


def _embed_viewer(out_path):
    with open(os.path.join(ROOT, "viewer", "index.html"), "rb") as f:
        raw = f.read()
    gz = gzip.compress(raw, compresslevel=9, mtime=0)
    lines = ["/* The trace viewer (viewer/index.html), gzipped. Generated by tiny-langgraph. */",
             "#pragma once", "#include <Arduino.h>", "",
             f"#define WEB_VIEWER_GZ_LEN {len(gz)}u", "", "static const uint8_t WEB_VIEWER_GZ[] PROGMEM = {"]   # PROGMEM: in flash, not RAM, on the ESP8266
    for i in range(0, len(gz), 20):
        lines.append("    " + ", ".join(f"0x{b:02x}" for b in gz[i:i + 20]) + ",")
    _write(out_path, "\n".join(lines + ["};", ""]))


def _copy_runtime(dst, arduino: bool, model: bool):
    files = [os.path.join(ROOT, "core", f) for f in sorted(os.listdir(os.path.join(ROOT, "core")))
             if f.endswith((".c", ".h"))]
    if model:
        files += [os.path.join(ROOT, "lm", f) for f in ("tlg_lm.c", "tlg_lm.h")]
    files.append(os.path.join(ROOT, "drivers", "tlg_drivers.h"))
    files.append(os.path.join(ROOT, "drivers", "tlg_drivers_arduino.cpp" if arduino else "tlg_drivers_host.c"))
    for f in files:
        shutil.copy(f, dst)


def build(app, target="esp32s3", train=True, retrain=False, quiet_model=False, log=print) -> dict:
    board = None
    if target != "c":
        try:
            board = boards.get(target)
        except boards.BoardError as e:
            raise BuildError(str(e)) from None
    if app.schema is None:
        raise BuildError("building needs an agent with tools: add a prebuilt.Act([...]) node")
    for d in ("core", "lm", "drivers", "viewer", "ports"):
        if not os.path.isdir(os.path.join(ROOT, d)):
            raise BuildError(f"can't find {d}/ next to the package: use tiny-langgraph from a clone of the repo "
                             "(pip install -e .)")
    source = os.path.basename(app.source_file)
    p = app.name
    for node, c in app.classifiers.items():
        if not c.model.fitted:
            raise BuildError(f"{node}: {c.model!r} isn't fitted yet: call .fit(...) or .load(path) in {source}")
    gen_dir = os.path.join(app.dir, "generated")
    data_dir = os.path.join(app.dir, "data")
    app.plan_states = board.plan_states if board else planner.DEFAULT_STATES

    files, conv = cagent.generate(app, gen_dir, source)
    log(f"C code: {', '.join(os.path.relpath(f, app.dir) for f in files)}")
    if board:
        log(f"board: {board.title}: {board.ram_kb} KB RAM, {board.flash_kb // 1024} MB flash"
            + ("" if board.psram else ", no PSRAM") + ("" if board.fpu else ", no FPU"))
        check_pins(app, conv, board, warn=lambda m: log(f"  careful: {m}"))

    rows = label_all(app)
    if app.uses_model:
        stats = dataset.write(app.schema, app.domain, rows, data_dir, seed=app.schema.model["seed"])
        log(f"data: {stats['situations']:,} situations, {stats['distinct_plans']} different plans")
    else:
        log(f"situations: {len(rows):,}, {len({tuple(r['plan']) for r in rows})} different plans")

    cc = _cc()
    if cc:
        n = crosscheck(app, gen_dir, rows, cc)
        compile_agent(app, gen_dir, cc, [])
        log(f"check: the C decides exactly like your Python in all {n:,} situations")
        for node, c in app.classifiers.items():
            from .models import ModelError
            try:
                k = c.model.crosscheck(app, node, gen_dir, cc, ROOT)
            except ModelError as e:
                raise BuildError(f"{node}: {e}") from None
            cost = c.model.cost()
            log(f"check: {node} ({c.model!r}) answers exactly like your Python in {k:,} cases; "
                f"adds ~{cost['flash']:,} bytes flash, {cost['ram']:,} bytes RAM")
    else:
        log("check: skipped (no C compiler found); install gcc or clang to verify the C")

    meta = None
    if not app.uses_model:
        log("model: none (no reason node): rules + planner decide")
    elif train:
        meta = train_if_needed(app, data_dir, retrain, log)
    elif not quiet_model:
        log("model: not trained (--no-train): the planner decides")
    has_model = app.uses_model and os.path.exists(app.model_path) and (meta is not None or not train)
    if has_model and not train and not os.path.exists(os.path.join(app.dir, "model", f"{p}.json")):
        has_model = False

    out = os.path.join(app.dir, "build", board.name, p) if board else os.path.join(app.dir, "build", f"{p}_c")
    os.makedirs(out, exist_ok=True)
    keep = {"config.h"} if board else set()
    for f in os.listdir(out):
        if f not in keep and os.path.isfile(os.path.join(out, f)):
            os.remove(os.path.join(out, f))
    old_cfg = os.path.join(app.dir, "build", p, "config.h")       # before boards: build/<name>/config.h
    if board and not os.path.exists(os.path.join(out, "config.h")) and os.path.exists(old_cfg):
        shutil.copy(old_cfg, out)
        log(f"config: kept your settings from {os.path.relpath(old_cfg)}")
    _copy_runtime(out, arduino=bool(board), model=app.uses_model)
    for f in files:
        shutil.copy(f, out)
    customs = [obj for _, obj in conv.drivers.values() if obj.c_header is None]
    for obj in customs:
        for cf in obj.c_files:
            src = os.path.join(app.dir, cf)
            if not os.path.exists(src):
                raise BuildError(f"{type(obj).__name__}: {cf} not found next to {source}")
            shutil.copy(src, out)
            hdr = os.path.splitext(src)[0] + ".h"
            if os.path.exists(hdr):
                shutil.copy(hdr, out)
    if has_model:
        from .toolchain.train import embed
        embed(app.model_path, os.path.join(out, f"{p}_model.h"), f"{p}_model")
    uses_camera = any(isinstance(obj, Camera) for _, obj in conv.drivers.values())

    if target == "c":
        if has_model:
            shutil.copy(app.model_path, out)
        log(f"ready: {os.path.relpath(out)}  (C files for your own firmware; drivers in tlg_drivers.h)")
        return {"out": out, "model": has_model}

    _write(os.path.join(out, "tlg_build.h"),
           "/* Generated by tiny-langgraph: the board and which optional parts this agent uses. */\n#pragma once\n"
           f'#define TLG_BOARD "{board.name}"\n'
           f"#define TLG_PLAN_STATES {app.plan_states if len(app.goals) > planner.SMALL_OUTCOMES else 0}"
           "   /* planner workspace: only for more than 8 goals */\n"
           f"#define TLG_USE_NET {int(app.mailbox is not None)}\n"
           f"#define TLG_USE_CAMERA {int(uses_camera)}\n#define TLG_USES_MODEL {int(app.uses_model)}\n"
           f"#define TLG_HAS_MODEL {int(has_model)}\n")
    setup = "".join(f"\n *        {s}" for s in board.setup)
    subs = {"@NAME@": p, "@p@": p, "@P@": p.upper(), "@SOURCE@": source, "@BOARD@": board.title,
            "@BOARD_SETUP@": setup, "@LOG_DAYS@": str(board.log_days),
            "@HOSTNAME@": p.replace("_", "-"), "@PIN@": "%04d" % secrets.randbelow(10000),
            "@MCP_KEY@": secrets.token_hex(8), "@ABOUT@": json.dumps(board_about(app))[1:-1]}
    tdir = os.path.join(ROOT, "ports", "arduino", "agent")
    with open(os.path.join(tdir, "agent_main.cpp.in")) as f:
        text = f.read()
    for k, v in subs.items():
        text = text.replace(k, v)
    _write(os.path.join(out, f"{p}_main.cpp"), text)
    _write(os.path.join(out, f"{p}.ino"),
           f"// {p} for {board.title}: generated by tiny-langgraph from {source}.\n"
           f"// The code is in {p}_main.cpp; settings in config.h.\n")
    cfg = os.path.join(out, "config.h")
    if not os.path.exists(cfg):
        with open(os.path.join(tdir, "config.h.in")) as f:
            text = f.read()
        for k, v in subs.items():
            text = text.replace(k, v)
        _write(cfg, text)
    _embed_viewer(os.path.join(out, "web_viewer.h"))
    log(f"ready: {os.path.relpath(out)}/{p}.ino  (open it in the Arduino IDE; edit config.h first)")
    log(f"  Arduino IDE, Tools menu: {'; '.join(board.setup)}")
    return {"out": out, "model": has_model, "board": board.name}


def board_about(app) -> str:
    """One line about the board for MCP clients: the first line of the State's docstring."""
    doc = (app.state_schema.__doc__ or "").strip()
    return doc.splitlines()[0].strip() if doc else ""


__all__ = ["build", "BuildError", "label_all", "crosscheck", "Driver"]
