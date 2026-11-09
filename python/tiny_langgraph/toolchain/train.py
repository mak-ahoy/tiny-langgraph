"""Train the tiny transformer on data/train.txt and export it for the C engine (lm/tlg_lm.c)."""
from __future__ import annotations

import os
import struct
import time

from .schema import Schema


def _model_classes():
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class RMSNorm(nn.Module):
        def __init__(self, d, eps=1e-5):
            super().__init__()
            self.eps = eps
            self.weight = nn.Parameter(torch.ones(d))

        def forward(self, x):
            return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps) * self.weight

    class Block(nn.Module):
        def __init__(self, d, heads, hidden):
            super().__init__()
            self.heads = heads
            self.norm1 = RMSNorm(d)
            self.qkv = nn.Linear(d, 3 * d, bias=False)
            self.proj = nn.Linear(d, d, bias=False)
            self.norm2 = RMSNorm(d)
            self.up = nn.Linear(d, hidden, bias=False)
            self.down = nn.Linear(hidden, d, bias=False)

        def forward(self, x):
            B, T, C = x.shape
            q, k, v = self.qkv(self.norm1(x)).split(C, dim=2)
            q, k, v = (t.view(B, T, self.heads, C // self.heads).transpose(1, 2) for t in (q, k, v))
            a = F.scaled_dot_product_attention(q, k, v, is_causal=True)
            x = x + self.proj(a.transpose(1, 2).reshape(B, T, C))
            return x + self.down(F.silu(self.up(self.norm2(x))))

    class TinyLM(nn.Module):
        def __init__(self, vocab, seq_len, dim, layers, heads, hidden):
            super().__init__()
            self.tok = nn.Embedding(vocab, dim)
            self.pos = nn.Embedding(seq_len, dim)
            self.blocks = nn.ModuleList(Block(dim, heads, hidden) for _ in range(layers))
            self.norm = RMSNorm(dim)
            nn.init.normal_(self.tok.weight, std=0.02)
            nn.init.normal_(self.pos.weight, std=0.02)

        def forward(self, idx):
            x = self.tok(idx) + self.pos(torch.arange(idx.shape[1], device=idx.device))
            for b in self.blocks:
                x = b(x)
            return self.norm(x) @ self.tok.weight.T

    return TinyLM


def export_bin(model, cfg: dict, path: str):
    """TLM1 format: magic, 6 x int32 config, then float32 weights in a fixed order."""
    order = [model.tok.weight, model.pos.weight]
    for b in model.blocks:
        order += [b.norm1.weight, b.qkv.weight, b.proj.weight, b.norm2.weight, b.up.weight, b.down.weight]
    order.append(model.norm.weight)
    with open(path, "wb") as f:
        f.write(b"TLM1")
        f.write(struct.pack("<6i", *(cfg[k] for k in ("vocab", "seq_len", "dim", "layers", "heads", "hidden"))))
        for t in order:
            f.write(t.detach().cpu().float().contiguous().numpy().tobytes())


def train(schema: Schema, data_dir: str, out_path: str, log=print) -> dict:
    import torch
    import torch.nn.functional as F

    m = schema.model
    torch.manual_seed(m["seed"])
    device = "cuda" if torch.cuda.is_available() else "cpu"
    V, T = len(schema.tokens), schema.seq_len
    PAD, THINK, PLAN, EOS = (schema.token_id(t) for t in ("<pad>", "<think>", "<plan>", "<eos>"))

    def load(name):
        with open(os.path.join(data_dir, name)) as f:
            return [[schema.token_id(t) for t in line.split()] for line in f if line.strip()]

    def tensors(seqs):
        X = torch.full((len(seqs), T), PAD, dtype=torch.long)
        Y = torch.full((len(seqs), T), -100, dtype=torch.long)
        for r, ids in enumerate(seqs):
            k = ids.index(THINK)
            for t in range(len(ids) - 1):
                X[r, t] = ids[t]
                if t + 1 > k:
                    Y[r, t] = ids[t + 1]
        return X.to(device), Y.to(device)

    train_seqs, val_seqs = load("train.txt"), load("val.txt")
    Xtr, Ytr = tensors(train_seqs)

    cfg = dict(vocab=V, seq_len=T, dim=m["dim"], layers=m["layers"], heads=m["heads"], hidden=m["hidden"])
    model = _model_classes()(**cfg).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    log(f"model: {n_params:,} parameters ({n_params * 4 / 1024:.0f} KB as float32), device {device}")

    opt = torch.optim.AdamW(model.parameters(), lr=m["lr"], weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=m["lr"], total_steps=m["steps"], pct_start=0.1)
    t0 = time.time()
    for step in range(1, m["steps"] + 1):
        ix = torch.randint(0, Xtr.shape[0], (m["batch"],), device=device)
        loss = F.cross_entropy(model(Xtr[ix]).view(-1, V), Ytr[ix].view(-1), ignore_index=-100)
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
        if step == 1 or step % 500 == 0:
            log(f"  step {step:5d}  loss {loss.item():.4f}  ({time.time() - t0:.0f}s)")

    # evaluate exactly as the device will: greedy generation from the prompt
    model.eval()
    exact = 0
    with torch.no_grad():
        for ids in val_seqs:
            k = ids.index(THINK) + 1
            seq = ids[:k]
            while len(seq) < T:
                nxt = int(model(torch.tensor([seq], device=device))[0, -1].argmax())
                seq.append(nxt)
                if nxt == EOS:
                    break
            exact += seq == ids
    acc = exact / len(val_seqs)
    log(f"held-out exact answers: {exact}/{len(val_seqs)} ({acc:.2%})")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    export_bin(model, cfg, out_path)
    log(f"wrote {out_path} ({os.path.getsize(out_path):,} bytes)")
    return {"params": n_params, "val_exact": acc, "bytes": os.path.getsize(out_path)}


def embed(bin_path: str, out_path: str, symbol: str):
    """Turn a model .bin into a C header (a const array the ESP32 keeps in flash)."""
    data = open(bin_path, "rb").read()
    if data[:4] != b"TLM1" or len(data) % 4:
        raise ValueError(f"{bin_path} is not a TLM1 model")
    words = struct.unpack(f"<{len(data) // 4}I", data)
    S = symbol.upper()
    lines = [f"/* Generated by tiny-langgraph from {os.path.basename(bin_path)}. Do not edit. */",
             "#pragma once", "#include <stdint.h>", "",
             f"#define {S}_LEN {len(data)}u", "",
             f"static const uint32_t {symbol}_words[{len(words)}] = {{"]
    for i in range(0, len(words), 8):
        lines.append("    " + ", ".join(f"0x{w:08x}" for w in words[i:i + 8]) + ",")
    lines += ["};", "", f"#define {S} ((const void *){symbol}_words)", ""]
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
