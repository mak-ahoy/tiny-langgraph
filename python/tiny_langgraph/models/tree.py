"""
models.Tree: a small decision tree over Choice/Score fields, learned from labelled examples.

    risk = models.Tree(reads=["tod", "owner", "unusual"], max_depth=3)
    risk.fit([({"tod": "night", "owner": "away", "unusual": "alarming"}, "high"), ...])

Each split asks "is <field> == <value>?", so the tree becomes plain if/else in C. It reads
only Choice/Score fields, so the build checks the C against Python on every combination
of the fields it reads, exactly as it does for your @rules.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import tempfile

from .base import Model, ModelError

MAX_CHECK = 200_000            # combinations the C check runs through


class Tree(Model):
    kind = "tree"

    def __init__(self, reads, max_depth: int = 4, min_leaf: int = 1):
        super().__init__()
        if isinstance(reads, str) or not reads or not all(isinstance(r, str) for r in reads):
            raise ModelError('Tree(reads=[...]) takes field names, e.g. reads=["tod", "owner"]')
        if not 1 <= max_depth <= 12:
            raise ModelError("Tree max_depth must be 1..12")
        self.reads, self.max_depth, self.min_leaf = list(reads), max_depth, max(1, min_leaf)
        self.root = None
        self._pending = None                     # labels to check once bound

    def __repr__(self):
        return f"models.Tree(reads={self.reads})"

    # ---------------------------------------------------------------- Python side
    def bind(self, state, field):
        from ..state import Choice
        f = state._fields.get(field)
        if not isinstance(f, Choice) or f.kind != "choice":
            raise ModelError(f"Tree answers a Choice or Score field; {field} isn't one")
        for r in self.reads:
            rf = state._fields.get(r)
            if r == field:
                raise ModelError(f"Tree can't read the field it answers ({field})")
            if rf is None or rf.kind != "choice":
                raise ModelError(f"Tree reads {r!r}: it must be a Choice or Score field of {state.__name__}")
        super().bind(state, field)
        if self.root is not None:
            self._check_labels(self._labels(self.root))

    @property
    def fitted(self) -> bool:
        return self.root is not None

    def _check_labels(self, labels):
        values = self.state._fields[self.field].values
        bad = sorted(set(labels) - set(values))
        if bad:
            raise ModelError(f"Tree labels {', '.join(bad)} aren't values of {self.field} ({', '.join(values)})")

    def _labels(self, node):
        if node[0] == "leaf":
            return [node[1]]
        return self._labels(node[3]) + self._labels(node[4])

    def predict(self, s, memory) -> str:
        if not self.fitted:
            raise ModelError(f"{self!r} isn't fitted: call .fit(rows) or .load(path) first")
        return self._walk({r: getattr(s, r) for r in self.reads})

    def _walk(self, row):
        node, path = self.root, []
        while node[0] == "split":
            _, f, v, yes, no = node
            path.append(f"{f}{'==' if row[f] == v else '!='}{v}")
            node = yes if row[f] == v else no
        self._last = " and ".join(path) or "always"
        return node[1]

    def note(self) -> str:
        return getattr(self, "_last", "")

    def cost(self) -> dict:
        return {"flash": 12 * self._size(self.root) if self.root else 0, "ram": 0}

    def _size(self, node):
        return 1 if node[0] == "leaf" else 1 + self._size(node[3]) + self._size(node[4])

    # ---------------------------------------------------------------- fitting
    def fit(self, rows):
        """rows: [(values, label), ...]; values is a dict (or a State) with the fields in `reads`."""
        data = []
        for values, label in rows:
            get = values.get if isinstance(values, dict) else (lambda k, v=values: getattr(v, k))
            row = {r: get(r) for r in self.reads}
            if any(v is None for v in row.values()):
                raise ModelError(f"Tree.fit: a row is missing one of {self.reads}: {values!r}")
            data.append((row, label))
        if not data:
            raise ModelError("Tree.fit needs at least one example")
        self._order = list(dict.fromkeys(label for _, label in data))     # tie-break: first seen
        self._values = {r: list(dict.fromkeys(row[r] for row, _ in data)) for r in self.reads}
        self.root = self._grow(data, 0)
        if self.state is not None:
            self._check_labels(self._labels(self.root))
        return self

    def _grow(self, data, depth):
        labels = [label for _, label in data]
        counts = {k: labels.count(k) for k in self._order if k in labels}
        majority = max(counts, key=lambda k: (counts[k], -self._order.index(k)))
        if depth >= self.max_depth or len(counts) == 1 or len(data) < 2 * self.min_leaf:
            return ("leaf", majority)
        base, best = _gini(labels), None
        for f in self.reads:
            for v in self._values[f]:
                yes = [d for d in data if d[0][f] == v]
                no = [d for d in data if d[0][f] != v]
                if len(yes) < self.min_leaf or len(no) < self.min_leaf:
                    continue
                g = (len(yes) * _gini([x[1] for x in yes]) + len(no) * _gini([x[1] for x in no])) / len(data)
                if best is None or g < best[0] - 1e-12:
                    best = (g, f, v, yes, no)
        if best is None or best[0] >= base - 1e-12:
            return ("leaf", majority)
        _, f, v, yes, no = best
        return ("split", f, v, self._grow(yes, depth + 1), self._grow(no, depth + 1))

    def save(self, path):
        if not self.fitted:
            raise ModelError("nothing to save: fit it first")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"model": "tree", "reads": self.reads, "root": self.root}, f, indent=1)
        return self

    def load(self, path):
        with open(path) as f:
            data = json.load(f)
        if data.get("model") != "tree" or data.get("reads") != self.reads:
            raise ModelError(f"{path} isn't a Tree reading {self.reads}")
        self.root = _tuples(data["root"])
        if self.state is not None:
            self._check_labels(self._labels(self.root))
        return self

    # ---------------------------------------------------------------- C side
    def _fn(self, n, node):
        return f"{n.p}_tree_{node}"

    def c_rules(self, node, n):
        sig = f"int32_t {self._fn(n, node)}(const {n.p}_inputs_t *in)"
        body = [f"/* models.Tree for {node}: answers {self.field} from {', '.join(self.reads)} */", sig, "{"]
        self._c_node(self.root, n, body, 1)
        body += ["}", ""]
        return [sig + ";"], body

    def _c_node(self, node, n, out, depth):
        pad = "    " * depth
        if node[0] == "leaf":
            out.append(f"{pad}return {n.val(self.field, node[1])};")
            return
        _, f, v, yes, no = node
        out.append(f"{pad}if (in->{f} == {n.val(f, v)}) {{")
        self._c_node(yes, n, out, depth + 1)
        out.append(f"{pad}}}")
        self._c_node(no, n, out, depth)

    def c_step(self, node, n, conv, field):
        return [f"s->in.{field} = (uint8_t){self._fn(n, node)}(&s->in);"]

    def crosscheck(self, app, node, gen_dir, cc, root) -> int:
        """Every combination of the fields it reads: the C tree must answer like predict()."""
        st, p = self.state, app.name
        values = [st._fields[r].values for r in self.reads]
        combos = list(itertools.islice(itertools.product(*values), MAX_CHECK))
        work = tempfile.mkdtemp(prefix="tlg_tree_")
        src = os.path.join(work, "check.c")
        lines = [f'#include "{p}_schema.h"', "#include <stdio.h>", "int main(void) {", f"    {p}_inputs_t in = {{0}};"]
        for combo in combos:
            sets = " ".join(f"in.{r} = {_n(app).val(r, v)};" for r, v in zip(self.reads, combo))
            lines.append(f"    {sets} printf(\"%d\\n\", (int){p}_tree_{node}(&in));")
        lines += ["    return 0;", "}"]
        with open(src, "w") as f:
            f.write("\n".join(lines) + "\n")
        exe = os.path.join(work, "check")
        r = subprocess.run([cc, "-std=c11", "-O0", "-I", gen_dir, "-I", os.path.join(root, "core"), "-o", exe, src,
                            os.path.join(gen_dir, f"{p}_schema.c"), os.path.join(gen_dir, f"{p}_rules.c"),
                            os.path.join(root, "core", "tlg_plan.c")], capture_output=True, text=True)
        if r.returncode:
            raise ModelError("the Tree check doesn't compile (please report this):\n" + r.stderr[-2000:])
        out = subprocess.run([exe], check=True, capture_output=True, text=True).stdout.split()
        answers = st._fields[self.field].values
        for combo, c in zip(combos, out):
            py = self._walk(dict(zip(self.reads, combo)))
            if answers[int(c)] != py:
                raise ModelError(f"Tree: C and Python disagree on {dict(zip(self.reads, combo))}: "
                                 f"C {answers[int(c)]}, Python {py} (please report this)")
        return len(combos)


def _n(app):
    from ..toolchain import codegen
    return codegen._Names(app.schema)


def _gini(labels) -> float:
    n = len(labels)
    return 1.0 - sum((labels.count(k) / n) ** 2 for k in sorted(set(labels)))   # sorted: same sum every run


def _tuples(node):
    return ("leaf", node[1]) if node[0] == "leaf" else ("split", node[1], node[2], _tuples(node[3]), _tuples(node[4]))
