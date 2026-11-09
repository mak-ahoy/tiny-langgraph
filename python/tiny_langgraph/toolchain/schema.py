"""
Load and validate an agent schema (schema.yaml).

A schema describes one domain-specific agent:

  inputs:   what the agent observes; one token per slot, always in this order
  think:    what the model concludes before planning
  facts:    named conditions over inputs/think, e.g.  "wifi == up and tod == night"
  outcomes: what a plan can achieve, with weights
  actions:  tools, with preconditions (facts), ordering (after) and effects (achieves)
  scores:   optional points per slot value, summed, e.g. for "how suspicious is this"
  rules:    think (first matching rule wins) and want (outcome -> condition): the rules
            the model learns from, compiled to C for the device and used to label data
  teacher:  instead of rules, a Python file with think() and want()
  model:    transformer size and training settings
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SPECIAL_TOKENS = ["<pad>", "<bos>", "<think>", "<plan>", "<eos>"]
MAX_OUTCOMES = 32     # up to 8: full search of 2^outcomes states; more: pruned search (tlg_plan.c)
MAX_ACTIONS = 32
MAX_FACTS = 32
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
RESERVED = {"tok", "fact", "out", "act", "n", "and", "or", "not", "in", "true", "false", "th", "f"}

MODEL_DEFAULTS = {
    "dim": 48, "layers": 2, "heads": 4, "hidden": 128,
    "steps": 3000, "batch": 256, "lr": 3e-3, "seed": 0,
}


class SchemaError(Exception):
    pass


def _loader():
    """YAML 1.1 reads on/off/yes/no as booleans, which would turn a slot like
    `light: [off, on]` into [False, True]. Here only true/false are booleans."""
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        ch: [r for r in rs if r[0] != "tag:yaml.org,2002:bool"]
        for ch, rs in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver("tag:yaml.org,2002:bool",
                                 re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    return yaml, Loader


# --------------------------------------------------------------------------
# Condition expressions
#
#   slot == value    slot != value    slot in [a, b]       (input and think slots)
#   score >= 3       score < -1       (also <=, >, ==, !=)  (scores)
#   night                                                  (another fact)
#   true / false,  combined with and / or / not / ( )
# --------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"\s*(==|!=|>=|<=|>|<|\(|\)|\[|\]|,|-?[0-9]+|[A-Za-z_][A-Za-z0-9_]*)")
CMP_OPS = ("==", "!=", ">=", "<=", ">", "<")


def _tokenize(text: str) -> list[str]:
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise SchemaError(f"cannot read condition at: {text[pos:]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, text: str, slots: dict[str, list[str]],
                 scores: set[str] = frozenset(), facts: set[str] = frozenset()):
        self.text, self.slots, self.scores, self.facts = text, slots, scores, facts
        self.toks, self.i = _tokenize(text), 0

    def err(self, msg):
        return SchemaError(f"in condition {self.text!r}: {msg}")

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, expected=None):
        tok = self.peek()
        if tok is None or (expected is not None and tok != expected):
            raise self.err(f"expected {expected or 'more'}, got {tok!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.or_expr()
        if self.peek() is not None:
            raise self.err(f"unexpected {self.peek()!r}")
        return node

    def or_expr(self):
        parts = [self.and_expr()]
        while self.peek() == "or":
            self.take()
            parts.append(self.and_expr())
        return parts[0] if len(parts) == 1 else ("or", parts)

    def and_expr(self):
        parts = [self.not_expr()]
        while self.peek() == "and":
            self.take()
            parts.append(self.not_expr())
        return parts[0] if len(parts) == 1 else ("and", parts)

    def not_expr(self):
        if self.peek() == "not":
            self.take()
            return ("not", self.not_expr())
        return self.atom()

    def value(self, slot):
        v = self.take()
        if v not in self.slots[slot]:
            raise self.err(f"{v!r} is not a value of {slot!r} (values: {', '.join(self.slots[slot])})")
        return v

    def atom(self):
        tok = self.peek()
        if tok == "(":
            self.take()
            node = self.or_expr()
            self.take(")")
            return node
        if tok in ("true", "false"):
            self.take()
            return ("const", tok == "true")
        name = self.take()
        if name in self.facts:
            return ("fact", name)
        if name in self.scores:
            op = self.take()
            if op not in CMP_OPS:
                raise self.err(f"expected a comparison (>=, <=, >, <, ==, !=) after score {name!r}")
            num = self.take()
            if not re.fullmatch(r"-?[0-9]+", num):
                raise self.err(f"score {name!r} must be compared with a whole number, got {num!r}")
            return ("cmp", name, op, int(num))
        if name not in self.slots:
            known = sorted(self.slots) + sorted(self.scores) + sorted(self.facts)
            raise self.err(f"unknown name {name!r} (known: {', '.join(known)})")
        op = self.take()
        if op in ("==", "!="):
            return ("eq" if op == "==" else "ne", name, self.value(name))
        if op == "in":
            self.take("[")
            vals = [self.value(name)]
            while self.peek() == ",":
                self.take()
                vals.append(self.value(name))
            self.take("]")
            return ("in", name, vals)
        raise self.err(f"expected ==, != or in after {name!r}")


def eval_expr(node, env: dict) -> bool:
    kind = node[0]
    if kind == "const": return node[1]
    if kind == "eq":    return env[node[1]] == node[2]
    if kind == "ne":    return env[node[1]] != node[2]
    if kind == "in":    return env[node[1]] in node[2]
    if kind == "fact":  return env[node[1]]
    if kind == "cmp":
        a, op, b = env[node[1]], node[2], node[3]
        return {"==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]
    if kind == "not":   return not eval_expr(node[1], env)
    if kind == "and":   return all(eval_expr(p, env) for p in node[1])
    if kind == "or":    return any(eval_expr(p, env) for p in node[1])
    raise ValueError(kind)


def expr_names(node) -> set[str]:
    """Every slot, score and fact a condition reads."""
    kind = node[0]
    if kind == "const":
        return set()
    if kind in ("eq", "ne", "in", "fact", "cmp"):
        return {node[1]}
    if kind == "not":
        return expr_names(node[1])
    return set().union(*(expr_names(p) for p in node[1]))


# --------------------------------------------------------------------------
# Schema model
# --------------------------------------------------------------------------
@dataclass
class Slot:
    name: str
    values: list[str]
    bins: list[float] | None = None     # thresholds for turning a float into a value

    def token(self, value: str) -> str:
        return f"{self.name}_{value}"


@dataclass
class Outcome:
    name: str
    weight: int
    only_if_wanted: bool


@dataclass
class Action:
    name: str
    cost: int
    needs: list[str]        # facts that must be true
    after: list[str]        # outcomes that must already be achieved
    achieves: list[str]
    approval: bool


@dataclass
class Score:
    name: str
    points: dict[str, dict[str, int]]    # slot -> value -> points (missing values count 0)


@dataclass
class Schema:
    name: str
    description: str
    path: str
    inputs: list[Slot]
    think: list[Slot]
    facts: dict[str, tuple]           # name -> parsed expression
    fact_text: dict[str, str]
    outcomes: list[Outcome]
    actions: list[Action]
    teacher: str | None              # Python teacher file, or None when rules are used
    budget: int
    model: dict
    scores: dict[str, Score] = field(default_factory=dict)
    think_rules: dict[str, list[tuple[str, tuple, str]]] = field(default_factory=dict)  # slot -> [(value, cond, text)]
    want_rules: dict[str, tuple[tuple, str]] = field(default_factory=dict)             # outcome -> (cond, text)
    order: list[tuple[str, str]] = field(default_factory=list)   # ("think"|"score"|"fact", name), dependencies first
    tokens: list[str] = field(default_factory=list)

    @property
    def has_rules(self) -> bool:
        return bool(self.think_rules)

    @property
    def dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.path))

    @property
    def prompt_len(self) -> int:          # <bos> + inputs + <think>
        return len(self.inputs) + 2

    @property
    def seq_len(self) -> int:             # longest possible example
        return self.prompt_len + len(self.think) + 1 + len(self.actions) + 1

    def token_id(self, tok: str) -> int:
        return self._ids[tok]

    def outcome_mask(self, names) -> int:
        idx = {o.name: i for i, o in enumerate(self.outcomes)}
        mask = 0
        for n in names:
            if n not in idx:
                raise SchemaError(f"unknown outcome {n!r}")
            mask |= 1 << idx[n]
        return mask

    def _score(self, name: str, env: dict) -> int:
        return sum(vals.get(env[slot], 0) for slot, vals in self.scores[name].points.items())

    def _derive(self, inputs: dict[str, str], think: dict[str, str] | None) -> dict:
        """Work out think slots (unless given), scores and facts, dependencies first."""
        env = dict(inputs)
        if think is not None:
            env.update(think)
        for kind, name in self.order:
            if kind == "think":
                if think is None:
                    env[name] = next(v for v, cond, _ in self.think_rules[name] if eval_expr(cond, env))
            elif kind == "score":
                env[name] = self._score(name, env)
            else:
                env[name] = eval_expr(self.facts[name], env)
        return env

    def facts_of(self, inputs: dict[str, str], think: dict[str, str]) -> dict[str, bool]:
        env = self._derive(inputs, think)
        return {name: env[name] for name in self.facts}

    def scores_of(self, inputs: dict[str, str], think: dict[str, str] | None = None) -> dict[str, int]:
        env = self._derive(inputs, think)
        return {name: env[name] for name in self.scores}

    # the rules, used as a teacher (same interface as a teacher.py)
    def think_of(self, inputs: dict[str, str]) -> dict[str, str]:
        env = self._derive(inputs, None)
        return {s.name: env[s.name] for s in self.think}

    def want_of(self, inputs: dict[str, str], think: dict[str, str]) -> list[str]:
        env = self._derive(inputs, think)
        return [o.name for o in self.outcomes
                if o.name in self.want_rules and eval_expr(self.want_rules[o.name][0], env)]

    def finish(self):
        toks = list(SPECIAL_TOKENS)
        for s in self.inputs + self.think:
            toks += [s.token(v) for v in s.values]
        toks += [a.name for a in self.actions]
        seen = set()
        for t in toks:
            if t in seen:
                raise SchemaError(f"token {t!r} appears twice: rename a slot value or an action")
            seen.add(t)
        if len(toks) > 256:
            raise SchemaError(f"{len(toks)} tokens; the limit is 256")
        self.tokens = toks
        self._ids = {t: i for i, t in enumerate(toks)}

    def situation_count(self) -> int:
        n = 1
        for s in self.inputs:
            n *= len(s.values)
        return n


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _name(n, what):
    if not isinstance(n, str) or not NAME_RE.match(n):
        raise SchemaError(f"{what} name {n!r} must be lower_snake_case (a-z, 0-9, _)")
    if n in RESERVED:
        raise SchemaError(f"{what} name {n!r} is reserved")
    return n


def _list(v, what):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return v
    raise SchemaError(f"{what} must be a name or a list of names")


def _slots(section, what) -> list[Slot]:
    if not isinstance(section, dict) or not section:
        raise SchemaError(f"'{what}' must be a non-empty mapping of slot -> values")
    slots = []
    for name, spec in section.items():
        _name(name, f"{what} slot")
        bins = None
        if isinstance(spec, dict):
            values = spec.get("values")
            bins = spec.get("bins")
        else:
            values = spec
        if not isinstance(values, list) or len(values) < 2:
            raise SchemaError(f"slot {name!r} needs a list of at least 2 values")
        values = [str(v) for v in values]
        for v in values:
            if not NAME_RE.match(v):
                raise SchemaError(f"value {v!r} of slot {name!r} must be lower_snake_case")
        if len(set(values)) != len(values):
            raise SchemaError(f"slot {name!r} has duplicate values")
        if bins is not None:
            if (not isinstance(bins, list) or len(bins) != len(values) - 1
                    or any(not isinstance(b, (int, float)) for b in bins)
                    or any(bins[i] >= bins[i + 1] for i in range(len(bins) - 1))):
                raise SchemaError(f"slot {name!r}: bins must be {len(values) - 1} increasing numbers")
            bins = [float(b) for b in bins]
        slots.append(Slot(name, values, bins))
    return slots


def _unique(names, what):
    seen = set()
    for n in names:
        if n in seen:
            raise SchemaError(f"the name {n!r} is used twice among {what}")
        seen.add(n)


def _cond_text(text, what) -> str:
    if isinstance(text, bool):
        return "true" if text else "false"
    if not isinstance(text, str):
        raise SchemaError(f"{what} must be a condition, like: wifi == up")
    return text


def _scores(section, slot_values) -> dict[str, Score]:
    """scores:
         suspicion:
           tod:    { night: 2 }
           expect: { owner: -2, milkman: -2 }    # values not listed count 0"""
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SchemaError("'scores' must be a mapping of score -> {slot: {value: points}}")
    out = {}
    for name, spec in section.items():
        _name(name, "score")
        if not isinstance(spec, dict) or not spec:
            raise SchemaError(f"score {name!r} needs points, like  tod: {{ night: 2 }}")
        points = {}
        for slot, vals in spec.items():
            if slot not in slot_values:
                raise SchemaError(f"score {name!r}: unknown slot {slot!r}")
            if not isinstance(vals, dict):
                raise SchemaError(f"score {name!r}: {slot} needs value -> points, like  {{ {slot_values[slot][-1]}: 1 }}")
            pts = {}
            for v, p in vals.items():
                v = str(v)
                if v not in slot_values[slot]:
                    raise SchemaError(f"score {name!r}: {v!r} is not a value of {slot!r}")
                if not isinstance(p, int) or isinstance(p, bool) or not -1000 <= p <= 1000:
                    raise SchemaError(f"score {name!r}: points for {slot} {v} must be a whole number -1000..1000")
                pts[v] = p
            points[slot] = pts
        out[name] = Score(name, points)
    return out


def _rules(section, think, outcomes, parse):
    """rules:
         think:
           threat:                    # first matching rule wins; the last must be  value: true
             - hi: visitor == hi and suspicion >= 3
             - lo: true
         want:
           logged: true               # outcomes not listed are never wanted
           help:   threat == hi and owner == away"""
    if section is None:
        return {}, {}
    if not isinstance(section, dict) or set(section) - {"think", "want"}:
        raise SchemaError("'rules' must have 'think' and 'want'")
    raw_think, raw_want = section.get("think") or {}, section.get("want") or {}
    if not isinstance(raw_think, dict) or not isinstance(raw_want, dict):
        raise SchemaError("'rules.think' and 'rules.want' must be mappings")

    slots = {s.name: s for s in think}
    missing = [s for s in slots if s not in raw_think]
    if missing:
        raise SchemaError(f"rules.think has no rules for {', '.join(missing)}")
    think_rules = {}
    for slot_name in slots:                       # keep the schema's slot order
        rules = raw_think[slot_name]
        what = f"rules.think.{slot_name}"
        if not isinstance(rules, list) or not rules:
            raise SchemaError(f"{what} must be a list like  - hi: suspicion >= 3")
        parsed = []
        for i, r in enumerate(rules):
            if not isinstance(r, dict) or len(r) != 1:
                raise SchemaError(f"{what}, rule {i + 1}: write it as  - value: condition")
            (value, cond), = r.items()
            value = str(value)
            if value not in slots[slot_name].values:
                raise SchemaError(f"{what}, rule {i + 1}: {value!r} is not a value of {slot_name!r} "
                                  f"(values: {', '.join(slots[slot_name].values)})")
            text = _cond_text(cond, f"{what}, rule {i + 1}")
            parsed.append((value, parse(cond, what), text))
        if parsed[-1][1] != ("const", True):
            raise SchemaError(f"{what}: the last rule must be  - <value>: true  so every situation gets a value")
        think_rules[slot_name] = parsed
    extra = set(raw_think) - set(slots)
    if extra:
        raise SchemaError(f"rules.think: {', '.join(sorted(extra))} is not a think slot")

    names = {o.name for o in outcomes}
    want_rules = {}
    for oname, cond in raw_want.items():
        if oname not in names:
            raise SchemaError(f"rules.want: unknown outcome {oname!r}")
        want_rules[oname] = (parse(cond, f"rules.want.{oname}"), _cond_text(cond, f"rules.want.{oname}"))
    if not want_rules:
        raise SchemaError("rules.want is empty: say when each outcome is wanted")
    return think_rules, want_rules


def _order(think_rules, scores, facts) -> list[tuple[str, str]]:
    """Order think slots (with rules), scores and facts so everything is computed after
    what it reads. A loop (a fact reading a think slot whose rules read that fact) is an error."""
    deps = {}
    for slot, rules in think_rules.items():
        deps[("think", slot)] = set().union(*(expr_names(c) for _, c, _ in rules))
    for name, sc in scores.items():
        deps[("score", name)] = set(sc.points)
    for name, expr in facts.items():
        deps[("fact", name)] = expr_names(expr)
    by_name = {name: key for key in deps for name in [key[1]]}

    order, state = [], {}

    def visit(key, path):
        if state.get(key) == "done":
            return
        if state.get(key) == "busy":
            loop = path[path.index(key):] + [key]
            raise SchemaError("these depend on each other in a loop: " + " -> ".join(n for _, n in loop))
        state[key] = "busy"
        for n in sorted(deps[key]):
            if n in by_name:
                visit(by_name[n], path + [key])
        state[key] = "done"
        order.append(key)

    for key in deps:
        visit(key, [])
    return order


def load(path: str) -> Schema:
    with open(path, encoding="utf-8") as f:
        yaml, loader = _loader()
        raw = yaml.load(f, Loader=loader)
    if not isinstance(raw, dict):
        raise SchemaError("schema must be a YAML mapping")

    name = _name(raw.get("name"), "agent")
    inputs = _slots(raw.get("inputs"), "inputs")
    think = _slots(raw.get("think"), "think")
    names = [s.name for s in inputs + think]
    if len(set(names)) != len(names):
        raise SchemaError("input and think slots must have different names")
    slot_values = {s.name: s.values for s in inputs + think}

    scores = _scores(raw.get("scores"), slot_values)
    raw_facts = raw.get("facts") or {}
    if not isinstance(raw_facts, dict):
        raise SchemaError("'facts' must be a mapping of name -> condition")
    for fname in raw_facts:
        _name(fname, "fact")
    _unique([*slot_values, *scores, *raw_facts], "slots, scores and facts")

    def parse(text, what):
        return _Parser(_cond_text(text, what), slot_values, set(scores), set(raw_facts)).parse()

    facts, fact_text = {}, {}
    for fname, text in raw_facts.items():
        fact_text[fname] = _cond_text(text, f"fact {fname!r}")
        facts[fname] = parse(text, f"fact {fname!r}")
    if len(facts) > MAX_FACTS:
        raise SchemaError(f"at most {MAX_FACTS} facts")

    outcomes = []
    for oname, spec in (raw.get("outcomes") or {}).items():
        _name(oname, "outcome")
        if isinstance(spec, int):
            outcomes.append(Outcome(oname, spec, False))
        elif isinstance(spec, dict):
            outcomes.append(Outcome(oname, int(spec.get("weight", 1)), bool(spec.get("only_if_wanted", False))))
        else:
            raise SchemaError(f"outcome {oname!r}: use a weight or {{weight: N, only_if_wanted: true}}")
        if not 0 <= outcomes[-1].weight <= 255:
            raise SchemaError(f"outcome {oname!r}: weight must be 0..255")
    if not outcomes or len(outcomes) > MAX_OUTCOMES:
        raise SchemaError(f"need 1..{MAX_OUTCOMES} outcomes (the planner searches 2^outcomes states)")
    outcome_names = {o.name for o in outcomes}

    actions = []
    for aname, spec in (raw.get("actions") or {}).items():
        _name(aname, "action")
        spec = spec or {}
        unknown = set(spec) - {"cost", "needs", "after", "achieves", "approval"}
        if unknown:
            raise SchemaError(f"action {aname!r}: unknown keys {sorted(unknown)}")
        a = Action(aname, int(spec.get("cost", 1)), _list(spec.get("needs"), "needs"),
                   _list(spec.get("after"), "after"), _list(spec.get("achieves"), "achieves"),
                   bool(spec.get("approval", False)))
        if not 1 <= a.cost <= 1000:
            raise SchemaError(f"action {aname!r}: cost must be 1..1000")
        for f_ in a.needs:
            if f_ not in facts:
                raise SchemaError(f"action {aname!r} needs unknown fact {f_!r}")
        for o in a.after + a.achieves:
            if o not in outcome_names:
                raise SchemaError(f"action {aname!r} refers to unknown outcome {o!r}")
        if not a.achieves:
            raise SchemaError(f"action {aname!r} achieves nothing")
        actions.append(a)
    if not actions or len(actions) > MAX_ACTIONS:
        raise SchemaError(f"need 1..{MAX_ACTIONS} actions")

    think_rules, want_rules = _rules(raw.get("rules"), think, outcomes, parse)
    if think_rules and "teacher" in raw:
        raise SchemaError("use either 'rules' or 'teacher', not both")
    teacher = None if think_rules else str(raw.get("teacher", "teacher.py"))
    order = _order(think_rules, scores, facts)

    model = dict(MODEL_DEFAULTS)
    model.update(raw.get("model") or {})
    if model["dim"] % model["heads"] != 0:
        raise SchemaError("model.dim must be divisible by model.heads")

    s = Schema(
        name=name,
        description=str(raw.get("description", "")),
        path=path,
        inputs=inputs, think=think,
        facts=facts, fact_text=fact_text,
        outcomes=outcomes, actions=actions,
        teacher=teacher,
        budget=int(raw.get("budget", 40)),
        model=model,
        scores=scores, think_rules=think_rules, want_rules=want_rules, order=order,
    )
    s.finish()
    return s
