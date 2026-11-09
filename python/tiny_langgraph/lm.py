"""Run a TLM1 model on the PC with numpy (the same maths as lm/tlg_lm.c), so invoke() can
use the trained model exactly like the board does."""
from __future__ import annotations

import struct

import numpy as np


class TinyLM:
    def __init__(self, path: str):
        data = open(path, "rb").read()
        if data[:4] != b"TLM1":
            raise ValueError(f"{path} is not a TLM1 model")
        vocab, seq, dim, layers, heads, hidden = struct.unpack("<6i", data[4:28])
        self.vocab, self.seq_len, self.dim, self.heads = vocab, seq, dim, heads
        w = np.frombuffer(data[28:], dtype="<f4")
        pos = 0

        def take(*shape):
            nonlocal pos
            n = int(np.prod(shape))
            out = w[pos:pos + n].reshape(shape)
            pos += n
            return out

        self.tok, self.pos = take(vocab, dim), take(seq, dim)
        self.layers = []
        for _ in range(layers):
            self.layers.append(dict(norm1=take(dim), qkv=take(3 * dim, dim), proj=take(dim, dim),
                                    norm2=take(dim), up=take(hidden, dim), down=take(dim, hidden)))
        self.norm = take(dim)
        if pos != w.size:
            raise ValueError(f"{path}: size doesn't match its config")

    @staticmethod
    def _rms(x, wt):
        return x / np.sqrt((x * x).mean(-1, keepdims=True) + 1e-5) * wt

    def logits(self, ids: list[int]) -> np.ndarray:
        T, d, hd = len(ids), self.dim, self.dim // self.heads
        x = self.tok[ids] + self.pos[:T]
        mask = np.triu(np.full((T, T), -np.inf, dtype=np.float32), 1)
        for L in self.layers:
            q, k, v = np.split(self._rms(x, L["norm1"]) @ L["qkv"].T, 3, axis=-1)
            out = np.empty_like(x)
            for h in range(self.heads):
                sl = slice(h * hd, (h + 1) * hd)
                a = q[:, sl] @ k[:, sl].T / np.sqrt(hd) + mask
                a = np.exp(a - a.max(-1, keepdims=True))
                a /= a.sum(-1, keepdims=True)
                out[:, sl] = a @ v[:, sl]
            x = x + out @ L["proj"].T
            hmid = self._rms(x, L["norm2"]) @ L["up"].T
            x = x + (hmid / (1 + np.exp(-hmid))) @ L["down"].T
        return self._rms(x[-1], self.norm) @ self.tok.T

    def generate(self, prompt: list[int], eos: int, allow=None) -> tuple[list[int], float]:
        """Greedy, like tlg_lm_generate: new tokens and the lowest chosen-token probability.
        allow(out_so_far, token) -> bool: guided writing, like tlg_lm_generate_guided."""
        if not 1 <= len(prompt) < self.seq_len:
            return [], 1.0
        seq, out, conf = list(prompt), [], 1.0
        while len(out) < self.seq_len - len(prompt):          # like max_out in C: at most seq_len - prompt
            lg = self.logits(seq).astype(np.float64)
            ok = np.ones(len(lg), dtype=bool) if allow is None else np.array([allow(out, v) for v in range(len(lg))])
            best = int(np.argmax(np.where(ok, lg, -np.inf))) if ok.any() else eos      # first max among allowed
            s = np.exp(lg[ok] - lg[best]).sum()
            conf = min(conf, float(1.0 / s) if s > 0 else 1.0)
            out.append(best)
            if best == eos or len(seq) >= self.seq_len:
                break
            seq.append(best)
        return out, conf
