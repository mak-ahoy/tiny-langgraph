"""
models.Counting: is right now unusual for this home? Statistics, no labels needed.

    def cat_visible(s: Pet) -> bool:                  # what to watch: a condition, like needs=
        return s.cat != "none"

    notice = models.Counting(active=cat_visible, clock=clock)
    notice.fit(samples)                               # [(minute, hour, active), ...] from normal days
    notice.save("model/notice.json")                  # later: models.Counting(...).load(...)

Once per run it takes one sample (is `active` true?) and keeps:
  quiet  minutes since the last activity
  busy   bouts of activity in the last 2 hours (a bout starts after 5 still minutes)
  slow   bouts in the last 12 hours
then asks how likely each is at this hour of the day, from tables fitted on the home's
own normal days. The alarm threshold is set so the fit data would have raised at most
`false_alarms_per_week` alarms. Answers a Score with 2 levels (no/yes) or 3 (no/odd/alarming):
  no        common at this hour on normal days
  odd       rarer than `odd` (default 0.05: 1 moment in 20)
  alarming  rarer than the alarm threshold, set by fit() from false_alarms_per_week
With 2 levels, odd counts as no.

In our experiments on simulated cats this beat a small transformer at spotting unusual days.
The C twin is core/tlg_count.c; both do the same double maths in the same order.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import subprocess
import tempfile

from ..drivers import Clock
from .base import Model, ModelError

BOUT_GAP = 5
CHECKS = ("quiet", "busy", "slow")
NEVER = 1e300                      # a quiet threshold that is never reached
MERGE = 60                         # minutes: alarms closer than this are one episode
MAX_GAP = 10                       # minutes between samples: longer means the board was off (a restart)


class _Memory:
    """The Python twin of tlg_count_t."""

    def __init__(self):
        self.started = self.seen = False
        self.first = self.last_active = 0
        self.bucket_hour = [-1] * 24
        self.bucket = [0] * 24


def _update(m: _Memory, active: bool, minute: int):
    if not m.started:
        m.started = True
        m.first = m.last_active = minute
    abs_hour = minute // 60
    if active:
        if not m.seen or minute - m.last_active >= BOUT_GAP:
            slot = abs_hour % 24
            if m.bucket_hour[slot] != abs_hour:
                m.bucket_hour[slot] = abs_hour
                m.bucket[slot] = 0
            m.bucket[slot] = min(m.bucket[slot] + 1, 65535)
        m.seen = True
        m.last_active = minute


def _bouts(m: _Memory, lo: int, hi: int) -> int:
    return sum(b for h, b in zip(m.bucket_hour, m.bucket) if lo <= h <= hi)


def _counts(m: _Memory, minute: int):
    abs_hour = minute // 60
    return minute - m.last_active, _bouts(m, abs_hour - 1, abs_hour), _bouts(m, abs_hour - 11, abs_hour)


def _cdf(lam: float, e: float, k: int) -> float:
    """P(X <= k), Poisson with mean lam, e = exp(-lam): the same steps as poisson_cdf() in C."""
    term = e
    total = e
    for i in range(1, k + 1):
        term = term * lam / i
        total += term
    return total


class Counting(Model):
    kind = "counting"

    def __init__(self, active, clock, checks=CHECKS, false_alarms_per_week: float = 1.0, odd: float = 0.05):
        super().__init__()
        if not inspect.isfunction(active) or active.__name__ == "<lambda>":
            raise ModelError("Counting(active=...) takes a condition: a def function of the state returning True/False")
        if not isinstance(clock, Clock):
            raise ModelError("Counting(clock=...) takes your Clock() driver")
        bad = set(checks) - set(CHECKS)
        if bad or not checks:
            raise ModelError(f"Counting checks are {', '.join(CHECKS)}; got {', '.join(sorted(bad)) or 'none'}")
        self.active, self.clock = active, clock
        self.checks = tuple(c for c in CHECKS if c in checks)
        if not 0 < odd < 0.1:
            raise ModelError("Counting(odd=...) is a probability between 0 and 0.1, e.g. 0.05 (1 moment in 20)")
        if false_alarms_per_week < 0:
            raise ModelError("Counting(false_alarms_per_week=...) can't be negative")
        self.budget, self.odd = false_alarms_per_week, odd
        self.table = None
        self._last = ""

    def __repr__(self):
        return f"models.Counting(active={self.active.__name__})"

    # ---------------------------------------------------------------- Python side
    def bind(self, state, field):
        from ..state import Score
        f = state._fields.get(field)
        if not isinstance(f, Score) or len(f.values) not in (2, 3):
            raise ModelError(f"Counting answers a Score with 2 levels (no, yes) or 3 (no, odd, alarming); "
                             f"declare {field} = Score(...)")
        super().bind(state, field)

    @property
    def fitted(self) -> bool:
        return self.table is not None

    def new_memory(self):
        return _Memory()

    def _answer(self, level: int) -> str:
        values = self.state._fields[self.field].values
        if len(values) == 2:
            return values[1] if level == 2 else values[0]
        return values[level]

    def level(self, m: _Memory, active: bool, minute: int, hour: int) -> int:
        """One run, exactly like tlg_count_step(): 0 no, 1 odd, 2 alarming."""
        t = self.table
        _update(m, active, minute)
        quiet, busy, slow = _counts(m, minute)
        h = hour % 24
        level = 0
        if "quiet" in self.checks and t["excess"][h] > 0:
            z = (quiet - t["top"][h]) / t["excess"][h]
            level = max(level, 2 if z >= t["quiet_alarm"] else 1 if z >= t["quiet_odd"] else 0)
        known = minute - m.first                         # minutes of memory: none after a reboot
        if "busy" in self.checks and known >= 2 * 60 and busy > 0:
            level = max(level, self._level_of(1.0 - _cdf(t["lam_busy"][h], t["e_busy"][h], busy - 1)))
        if "slow" in self.checks and known >= 12 * 60:
            level = max(level, self._level_of(_cdf(t["lam_slow"][h], t["e_slow"][h], slow)))
        self._last = f"quiet {quiet} min, {busy} bouts in 2 h, {slow} in 12 h"
        return level

    def _level_of(self, p):
        t = self.table
        return 2 if p <= t["p_alarm"] else 1 if p <= t["p_odd"] else 0

    def predict(self, s, memory) -> str:
        if not self.fitted:
            raise ModelError(f"{self!r} isn't fitted: call .fit(samples) or .load(path) first")
        active = self.active(s)
        if not isinstance(active, bool):
            raise ModelError(f"{self.active.__name__}() must return True or False, got {active!r}")
        return self._answer(self.level(memory, active, self.clock.minutes(), self.clock.hour()))

    def note(self) -> str:
        return self._last

    def functions(self) -> list:
        return [self.active]

    def cost(self) -> dict:
        return {"flash": 6 * 24 * 8 + 48, "ram": 4 + 4 + 4 + 24 * 4 + 24 * 2}

    # ---------------------------------------------------------------- fitting
    def fit(self, samples):
        """samples: [(minute, hour, active), ...], one per run, from normal days (two weeks or
        more), e.g. tiny_langgraph.datalog.samples(...). The first 2/3 give the tables, the rest
        sets the alarm threshold. A gap of more than 10 minutes (the board was off) counts as a
        restart, not as a long quiet spell."""
        rows = sorted((int(m), int(h), bool(a)) for m, h, a in samples)
        if len(rows) < 100 or rows[-1][0] - rows[0][0] < 3 * 1440:
            raise ModelError("Counting.fit needs at least 3 days of samples (two weeks or more is better)")
        mem, feats, prev = _Memory(), [], None
        for minute, hour, active in rows:
            if prev is not None and minute - prev > MAX_GAP:
                mem = _Memory()                          # the board was off: start again, as after a reboot
            prev = minute
            _update(mem, active, minute)
            feats.append((minute, hour % 24, *_counts(mem, minute), minute - mem.first))
        split = len(feats) * 2 // 3
        learn = [f for f in feats[:split] if f[5] >= 12 * 60]               # memory full: 12 h since a start
        check = feats[split:]
        if not learn:
            raise ModelError("Counting.fit: no stretch of 12 h without gaps in the first 2/3 of the samples")
        by_hour = [[f for f in learn if f[1] == h] or learn for h in range(24)]   # no data: use all hours
        t = {"top": [], "excess": [], "lam_busy": [], "e_busy": [], "lam_slow": [], "e_slow": []}
        for fs in by_hour:
            quiet = sorted(f[2] for f in fs)
            top = quiet[int(len(quiet) * 0.9)]
            over = [q - top for q in quiet if q > top]
            t["top"].append(float(top))
            t["excess"].append(max(30.0, sum(over) / len(over)) if over else 30.0)
            for name, col in (("busy", 3), ("slow", 4)):
                lam = max(0.05, sum(f[col] for f in fs) / len(fs))
                t[f"lam_{name}"].append(lam)
                t[f"e_{name}"].append(math.exp(-lam))
        t.update(quiet_odd=math.log(0.1 / self.odd), quiet_alarm=NEVER, p_odd=self.odd, p_alarm=-1.0)
        self.table = t
        # the threshold: as many alarms as the budget allows on the held-out normal days
        ps = [self._p(f, t) for f in check]
        weeks = max(1e-9, (check[-1][0] - check[0][0]) / 1440 / 7)
        cands = sorted({p for p in ps if p <= self.odd})   # alarming is never laxer than odd
        thr = -1.0
        for cand in cands:
            if _episodes([f[0] for f, p in zip(check, ps) if p <= cand]) > self.budget * weeks:
                break
            thr = cand
        if thr < 0:          # even the rarest normal moment is too common: alarm only at 10x rarer than that
            thr = (cands[0] if cands else self.odd / 5) / 10
        t["p_alarm"] = thr
        t["quiet_alarm"] = math.log(0.1 / thr) if thr > 0 else NEVER
        return self

    def _p(self, f, t):
        """The smallest probability of the checks (for setting the threshold), with the same
        warm-up as level(): busy and slow only once memory covers 2 h / 12 h."""
        _, h, quiet, busy, slow, known = f
        ps = [1.0]
        if "quiet" in self.checks and quiet > t["top"][h]:
            ps.append(0.1 * math.exp(-(quiet - t["top"][h]) / t["excess"][h]))
        if "busy" in self.checks and known >= 2 * 60 and busy > 0:
            ps.append(1.0 - _cdf(t["lam_busy"][h], t["e_busy"][h], busy - 1))
        if "slow" in self.checks and known >= 12 * 60:
            ps.append(_cdf(t["lam_slow"][h], t["e_slow"][h], slow))
        return min(ps)

    def save(self, path):
        if not self.fitted:
            raise ModelError("nothing to save: fit it first")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"model": "counting", "checks": list(self.checks), "table": self.table}, f, indent=1)
        return self

    def load(self, path):
        with open(path) as f:
            data = json.load(f)
        if data.get("model") != "counting":
            raise ModelError(f"{path} isn't a Counting model")
        self.checks, self.table = tuple(data["checks"]), data["table"]
        self.odd = self.table["p_odd"]
        return self

    # ---------------------------------------------------------------- C side
    def c_includes(self):
        return ["tlg_count.h"]

    def c_memory(self):
        return "tlg_count_t"

    def c_table(self, name) -> str:
        t = self.table

        def arr(xs):
            return "{ " + ", ".join(_c_double(x) for x in xs) + " }"
        bits = " | ".join(f"TLG_COUNT_{c.upper()}" for c in self.checks)
        fields = [f".{k} = {arr(t[k])}" for k in ("top", "excess", "lam_busy", "e_busy", "lam_slow", "e_slow")]
        fields += [f".{k} = {_c_double(t[k])}" for k in ("quiet_odd", "quiet_alarm", "p_odd", "p_alarm")]
        fields.append(f".checks = {bits}")
        return f"static const tlg_count_table_t {name} = {{\n    " + ",\n    ".join(fields) + "\n};"

    def c_tables(self, node, n):
        return [f"/* models.Counting for {node}: what's normal at each hour, fitted on the PC */",
                self.c_table(f"COUNT_{node.upper()}"), ""]

    def c_init(self, node):
        return [f"tlg_count_init(&s->m_{node});"]

    def c_step(self, node, n, conv, field):
        values = self.state._fields[field].values
        answer = [values[0], values[1] if len(values) == 3 else values[0], values[-1]]
        active = conv.cname(conv.funcs[self.active])
        return [f"static const uint8_t ANSWER[3] = {{ {', '.join(n.val(field, v) for v in answer)} }};",
                f"bool active = {active}(&s->in, &s->th);",
                f"s->in.{field} = ANSWER[tlg_count_step(&s->m_{node}, &COUNT_{node.upper()}, active, "
                "tlg_clock_minutes(), tlg_clock_hour())];"]

    def crosscheck(self, app, node, gen_dir, cc, root) -> int:
        """Run the same samples through tlg_count.c and through level(): every answer must match."""
        import random
        rng = random.Random(0)
        samples, minute = [], 0
        for _ in range(6000):                            # ~a week of runs with mixed habits
            minute += rng.choice((1, 1, 2, 5, 30, 90))
            hour = (minute // 60) % 24
            samples.append((minute, hour, rng.random() < (0.6 if 7 <= hour < 22 else 0.05)))
        mem = _Memory()
        py = [self.level(mem, a, m, h) for m, h, a in samples]
        work = tempfile.mkdtemp(prefix="tlg_count_")
        src = os.path.join(work, "check.c")
        rows = ",\n".join(f"    {{ {m}, {h}, {int(a)} }}" for m, h, a in samples)
        with open(src, "w") as f:
            f.write('#include "tlg_count.h"\n#include <stdio.h>\n'
                    + self.c_table("T") + "\n"
                    + f"static const int S[][3] = {{\n{rows}\n}};\n"
                    + "int main(void) {\n    tlg_count_t m;\n    tlg_count_init(&m);\n"
                    + f"    for (int i = 0; i < {len(samples)}; i++)\n"
                    + "        putchar('0' + tlg_count_step(&m, &T, S[i][2], S[i][0], S[i][1]));\n"
                    + "    return 0;\n}\n")
        exe = os.path.join(work, "check")
        core = os.path.join(root, "core")
        r = subprocess.run([cc, "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror", "-I", core, "-o", exe, src,
                            os.path.join(core, "tlg_count.c")], capture_output=True, text=True)
        if r.returncode:
            raise ModelError("the Counting check doesn't compile (please report this):\n" + r.stderr[-2000:])
        c = subprocess.run([exe], check=True, capture_output=True, text=True).stdout
        if c != "".join(map(str, py)):
            i = next(k for k in range(len(py)) if k >= len(c) or c[k] != str(py[k]))
            raise ModelError(f"Counting: C and Python disagree at sample {i} {samples[i]}: "
                             f"C {c[i:i + 1] or '?'}, Python {py[i]} (please report this)")
        return len(samples)


def _c_double(x: float) -> str:
    """A C literal that reads back as exactly the same double."""
    if x >= NEVER:
        return "1e300"
    r = repr(float(x))
    return r if ("." in r or "e" in r or "inf" in r) else r + ".0"


def _episodes(minutes) -> int:
    n, last = 0, None
    for m in minutes:
        if last is None or m - last >= MERGE:
            n += 1
        last = m
    return n
