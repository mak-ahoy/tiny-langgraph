"""
StateGraph: build the graph like in LangGraph, run it on your PC, build it for a board.

    graph = StateGraph(Pet)
    graph.add_node("sense", sense)
    graph.add_edge(START, "sense")
    ...
    app = graph.compile()
    app.invoke(Pet(cat="in_zone"))        # on your PC
    app.build("esp32s3")                  # for the board

Rules match the C runtime (core/tlg.h), so the board behaves exactly like your PC:
each node has one way out (an edge or a route), routes list where they can go,
and a run stops after max_steps so a cycle can't hang the device.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import time

from . import agent as _agent
from . import prebuilt as _pb
from .agent import AgentError, Goals
from .state import NAME_RE, State, _Unset

START = "__start__"
END = "__end__"
_C_WORDS = {"auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else",
            "enum", "extern", "float", "for", "goto", "if", "int", "long", "register", "return",
            "short", "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
            "unsigned", "void", "volatile", "while", "bool", "true", "false", "main", "s", "in", "th"}


class GraphError(Exception):
    pass


def _snake(name: str) -> str:
    out = ""
    for i, ch in enumerate(name):
        out += ("_" if ch.isupper() and i else "") + ch.lower()
    return out


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------
class StateGraph:
    def __init__(self, state_schema: type):
        if not (isinstance(state_schema, type) and issubclass(state_schema, State)):
            raise GraphError("StateGraph takes your State class, e.g. StateGraph(Pet)")
        self.state_schema = state_schema
        self.nodes: dict[str, object] = {}
        self.edges: dict[str, str] = {}
        self.branches: dict[str, tuple[object, dict[str, str]]] = {}
        self.start: str | None = None

    def add_node(self, node, action=None):
        """add_node("name", function_or_prebuilt)  or  add_node(function)"""
        if action is None:
            if not callable(node):
                raise GraphError("add_node needs a name and a function")
            node, action = node.__name__, node
        if not isinstance(node, str) or not NAME_RE.match(node) or node in _C_WORDS:
            raise GraphError(f"node name {node!r} must be lower_snake_case and not a C keyword")
        if node in self.nodes:
            raise GraphError(f"there is already a node called {node!r}")
        if isinstance(action, _agent.Tool):
            raise GraphError(f"{node}: tools run inside prebuilt.Act([...]), not as nodes")
        if isinstance(action, _pb.PrebuiltRoute):
            raise GraphError(f"{node}: {action!r} is a route; use add_conditional_edges")
        if not (isinstance(action, _pb.Prebuilt) or inspect.isfunction(action)):
            raise GraphError(f"{node}: a node is a function taking the state, or a prebuilt node")
        self.nodes[node] = action
        return self

    def _check_free(self, source):
        if source in self.edges or source in self.branches:
            raise GraphError(f"{source!r} already has a way out. Each node has one edge or one "
                             "conditional edge (no parallel branches on a microcontroller)")

    def add_edge(self, start: str, end: str):
        if start == START:
            if self.start is not None:
                raise GraphError("the graph already has a START edge")
            self.start = end
            return self
        self._check_free(start)
        self.edges[start] = end
        return self

    def add_conditional_edges(self, source: str, path, path_map=None):
        """path(state) returns a key; path_map lists the possible nodes (list) or maps keys to nodes (dict)."""
        self._check_free(source)
        if path_map is None:
            if isinstance(path, _pb.PrebuiltRoute):
                path_map = list(path.keys)
            else:
                raise GraphError(f"{source}: list where the route can go, e.g. "
                                 f"add_conditional_edges({source!r}, {getattr(path, '__name__', 'route')}, ['a', 'b'])")
        if isinstance(path_map, (list, tuple)):
            path_map = {k: k for k in path_map}
        if not isinstance(path_map, dict) or not path_map:
            raise GraphError(f"{source}: path_map must be a list or dict of nodes")
        if isinstance(path, _pb.PrebuiltRoute):
            missing = set(path.keys) - set(path_map)
            if missing:
                raise GraphError(f"{source}: {path!r} can return {', '.join(sorted(missing))}; "
                                 "add them to the path map")
        elif not inspect.isfunction(path):
            raise GraphError(f"{source}: the route must be a function taking the state")
        self.branches[source] = (path, dict(path_map))
        return self

    def set_entry_point(self, node: str):
        return self.add_edge(START, node)

    def set_finish_point(self, node: str):
        return self.add_edge(node, END)

    def compile(self, name: str | None = None, max_steps: int = 32) -> "CompiledGraph":
        return CompiledGraph(self, name or _snake(self.state_schema.__name__), max_steps)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
class Run:
    def __init__(self, start: int):
        self.current = start
        self.step = 0
        self.status = "ready"
        self.interrupt_requested = False
        self.question = ""
        self.tool_names: list[str] = []


class _Ctx:
    def __init__(self, app, state, run, notes, node="", verbose=False):
        self.app, self.state, self.run, self.notes = app, state, run, notes
        self.node, self.verbose = node, verbose
        self.in_rules = False

    def say(self, text):
        self.notes.append(text)
        if self.app._tracer:
            self.app._tracer.note(text)
        if self.verbose:
            shown = text[len(self.node) + 2:] if text.startswith(self.node + ": ") else text
            print(f"  {self.node:>8} | {shown}")


class _Tracer:
    """Writes the same @tlg lines as core/tlg_trace.c, so viewer/index.html can show PC runs."""

    def __init__(self, path):
        self.path = path
        open(path, "w", encoding="utf-8").close()
        self.run_id = 0
        self.t0 = time.monotonic()

    def _emit(self, obj):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("@tlg " + json.dumps(obj, separators=(",", ":")) + "\n")

    def _t(self):
        return int((time.monotonic() - self.t0) * 1000)

    def graph(self, app):
        self._emit({"k": "graph", "name": app.name, "start": app.start, "n": len(app.names)})
        for i, n in enumerate(app.names):
            kind, *rest = app.next[i]
            to = [rest[0]] if kind == "edge" else rest[2]
            self._emit({"k": "node", "id": i, "name": n, "cond": kind == "route", "to": to})

    def run(self, label):
        self.run_id += 1
        self._emit({"k": "run", "run": self.run_id, "t": self._t(), "label": label})

    def note(self, text):
        self._emit({"k": "note", "run": self.run_id, "t": self._t(), "text": text})

    def step(self, run, node, nxt):
        self._emit({"k": "step", "run": self.run_id, "t": self._t(), "step": run.step, "node": node, "next": nxt})

    def end(self, run):
        self._emit({"k": "end", "run": self.run_id, "t": self._t(), "status": run.status,
                    "steps": run.step, "reason": run.question})


class CompiledGraph:
    def __init__(self, g: StateGraph, name: str, max_steps: int):
        if not NAME_RE.match(name) or name in _C_WORDS:
            raise GraphError(f"agent name {name!r} must be lower_snake_case")
        self.name, self.state_schema, self.max_steps = name, g.state_schema, max_steps
        self.names = list(g.nodes)
        self.actions = [g.nodes[n] for n in self.names]
        if not self.names:
            raise GraphError("the graph has no nodes")
        idx = {n: i for i, n in enumerate(self.names)}
        idx[END] = -1

        def target(src, dst):
            if dst not in idx:
                raise GraphError(f"{src} goes to {dst!r}, which is not a node (nodes: {', '.join(self.names)})")
            return idx[dst]

        if g.start is None:
            raise GraphError("add an edge from START: graph.add_edge(START, 'first_node')")
        self.start = target("START", g.start)
        if self.start < 0:
            raise GraphError("START can't go straight to END")
        self.next = []
        for n in self.names:
            if n in g.edges:
                self.next.append(("edge", target(n, g.edges[n])))
            elif n in g.branches:
                fn, pm = g.branches[n]
                mapping = {k: target(n, v) for k, v in pm.items()}
                targets = list(dict.fromkeys(mapping.values()))
                self.next.append(("route", fn, mapping, targets))
            else:
                raise GraphError(f"node {n!r} has no way out: add an edge (maybe to END)")
        self._check_reachable()

        # ---- agent parts ----
        kinds = [getattr(a, "kind", None) for a in self.actions]
        acts = [a for a in self.actions if isinstance(a, _pb.Act)]
        self.rules_fn = None
        rules = [a for a in self.actions if getattr(a, "_tlg_rules", False)]
        if len(rules) > 1:
            raise GraphError("only one @rules function per agent")
        self.rules_fn = rules[0] if rules else None
        if len(acts) > 1:
            raise GraphError("only one prebuilt.Act node per agent")
        self.tools = acts[0].tools if acts else []
        self.uses_model = "reason" in kinds       # no reason node: rules + planner decide, nothing to train
        uses_agent = any(k in ("reason", "verify", "plan", "ask", "act") for k in kinds)
        if uses_agent and not acts:
            raise GraphError("the prebuilt nodes need a prebuilt.Act([your tools]) node")
        if uses_agent and not self.rules_fn:
            raise GraphError("the prebuilt nodes need your @rules function as a node (it decides what's wanted)")
        if self.state_schema.thinks() and not self.rules_fn:
            raise GraphError("your state has Think fields: add a @rules function that decides them")

        self._bind_classifiers()
        self._bind_mailbox()
        self.goals = self._find_goals() if self.tools else None
        self.facts = []
        for t in self.tools:
            for f in t.needs:
                if f not in self.facts:
                    self.facts.append(f)
            for gl in t.achieves + t.after:
                if self.goals and getattr(self.goals, gl.name, None) is not gl:
                    raise GraphError(f"tool {t.name}: {gl!r} is not from your Goals(...)")
        if len(self.facts) > 32:
            raise GraphError("at most 32 conditions (needs=) per agent")
        self.schema = self._schema() if self.tools else None
        self.domain = None
        self.plan_states = 4096        # the planner's workspace on the board (boards.py); build() sets it
        if self.schema:
            from .toolchain import planner
            self.domain = planner.domain_of(self.schema)
        self.source_file = self._source_file()
        self.dir = os.path.dirname(os.path.abspath(self.source_file))
        self._lm = None
        self._lm_loaded = False
        self._tracer = None

    def _source_file(self) -> str:
        """The file the agent is written in (its outputs go next to it)."""
        try:
            return inspect.getfile(self.state_schema)
        except TypeError:                      # module not in sys.modules (exec'd by hand)
            for a in self.actions:
                code = getattr(a, "__code__", None)
                if code is not None:
                    return code.co_filename
            return os.path.join(os.getcwd(), "agent.py")

    def _bind_classifiers(self):
        """Each Classify node's model must be able to answer its field; each Score field is
        answered by exactly one Classify node."""
        from .models import ModelError
        from .state import Score
        self.classifiers = {}                         # node name -> Classify
        answered = {}
        for name, a in zip(self.names, self.actions):
            if not isinstance(a, _pb.Classify):
                continue
            f = self.state_schema._fields.get(a.field)
            if f is None:
                raise GraphError(f"{name}: Classify answers {a.field!r}, which isn't a field of "
                                 f"{self.state_schema.__name__}")
            if a.field in answered:
                raise GraphError(f"{a.field} is answered by two Classify nodes ({answered[a.field]}, {name})")
            if any(c.model is a.model for c in self.classifiers.values()):
                raise GraphError(f"{name}: each Classify node needs its own model object")
            try:
                a.model.bind(self.state_schema, a.field)
            except ModelError as e:
                raise GraphError(f"{name}: {e}") from None
            answered[a.field] = name
            self.classifiers[name] = a
        for f in self.state_schema._fields.values():
            if isinstance(f, Score) and f.name not in answered:
                raise GraphError(f"{f.name} is a Score: add a node that answers it, "
                                 f'prebuilt.Classify({{"{f.name}": "..."}}, model=models.X(...))')

    def _bind_mailbox(self):
        """At most one Mailbox; the state has every field it fills, with the right values."""
        from . import net
        boxes = [a for a in self.actions if isinstance(a, _pb.Mailbox)]
        if len(boxes) > 1:
            raise GraphError("one prebuilt.Mailbox per agent (list every contract in it)")
        self.mailbox = boxes[0] if boxes else None
        if not self.mailbox:
            return
        st, missing = self.state_schema, []
        for name, values in net.fields_for(self.mailbox).items():
            f = st._fields.get(name)
            want = f'{name} = Choice({", ".join(repr(v) for v in values)})'
            if f is None:
                missing.append(want)
            elif f.kind != "choice" or tuple(f.values) != values:
                raise GraphError(f"Mailbox fills {name}: declare it as  {want}")
        if missing:
            raise GraphError(f"the mailbox fills these fields: add them to {st.__name__}:\n    "
                             + "\n    ".join(missing))

    def _check_reachable(self):
        seen, todo, ends = set(), [self.start], False
        while todo:
            i = todo.pop()
            if i < 0:
                ends = True
                continue
            if i in seen:
                continue
            seen.add(i)
            kind, *rest = self.next[i]
            todo += [rest[0]] if kind == "edge" else rest[2]
        if not ends:
            raise GraphError("END can't be reached from START")
        unreachable = [self.names[i] for i in range(len(self.names)) if i not in seen]
        if unreachable:
            raise GraphError(f"these nodes can't be reached from START: {', '.join(unreachable)}")

    def _find_goals(self) -> Goals:
        found = []
        for fn in [t.fn for t in self.tools] + ([self.rules_fn] if self.rules_fn else []):
            for v in fn.__globals__.values():
                if isinstance(v, Goals) and v not in found:
                    found.append(v)
        if len(found) != 1:
            raise GraphError("define your goals once at the top of the file: goals = Goals(...)")
        return found[0]

    def _schema(self):
        from .toolchain.schema import Action, Outcome, Schema, Slot, MODEL_DEFAULTS, SchemaError
        st = self.state_schema
        fact_names = [f.__name__ for f in self.facts]
        try:
            s = Schema(
                name=self.name, description=(inspect.getdoc(st) or "").split("\n")[0], path="agent.py",
                inputs=[Slot(f.name, list(f.values)) for f in st.inputs()],
                think=[Slot(f.name, list(f.values)) for f in st.thinks()],
                facts={n: ("const", True) for n in fact_names}, fact_text={n: n for n in fact_names},
                outcomes=[Outcome(g.name, g.weight, g.only_if_wanted) for g in self.goals],
                actions=[Action(t.name, t.cost, [f.__name__ for f in t.needs], [x.name for x in t.after],
                                [x.name for x in t.achieves], t.approval) for t in self.tools],
                teacher=None, budget=1000, model=dict(MODEL_DEFAULTS))
            s.finish()
        except SchemaError as e:
            raise GraphError(str(e)) from None
        if not st.inputs():
            raise GraphError("the state needs at least one Choice field for the model to read")
        return s

    # ---- the model ----
    @property
    def model_path(self) -> str:
        return os.path.join(self.dir, "model", f"{self.name}.bin")

    def lm(self):
        if not self._lm_loaded:
            self._lm_loaded = True
            if self.schema and os.path.exists(self.model_path):
                from .lm import TinyLM
                m = TinyLM(self.model_path)
                if m.vocab == len(self.schema.tokens):
                    self._lm = m
                else:
                    print(f"note: {self.model_path} was trained for another vocabulary; "
                          "rebuild to retrain it", file=sys.stderr)
        return self._lm

    # ---- running ----
    def invoke(self, state: State, *, approve=None, start: str | None = None, trace: str | None = None,
               verbose: bool = False, label: str = ""):
        """Run the graph once. approve(question) -> bool answers approval questions; without it,
        a run that needs approval pauses: check state.waiting, then call resume(state, True/False)."""
        for _ in self.stream(state, approve=approve, start=start, trace=trace, verbose=verbose, label=label):
            pass
        return state

    def resume(self, state: State, answer: bool, **kw):
        """Continue a paused run with the human's answer."""
        if state.waiting is None:
            raise GraphError("this state isn't waiting for an answer")
        state._agent.answer = bool(answer)
        for _ in self._steps(state, resume=True, **kw):
            pass
        return state

    def stream(self, state: State, *, approve=None, start=None, trace=None, verbose=False, label=""):
        """Like invoke, but yields (node name, notes) after every step."""
        if not isinstance(state, self.state_schema):
            raise GraphError(f"invoke takes a {self.state_schema.__name__}(...)")
        run = Run(self.names.index(start) if start else self.start)
        run.tool_names = [t.name for t in self.tools]
        object.__setattr__(state, "_run", run)
        state._agent.reset()
        state._agent.answer = None
        for f in self.state_schema.thinks():
            state._values[f.name] = f.default
        if trace:
            if self._tracer is None or self._tracer.path != trace:
                self._tracer = _Tracer(trace)
                self._tracer.graph(self)
        else:
            self._tracer = None
        if self._tracer:
            self._tracer.run(label)
        yield from self._steps(state, approve=approve, verbose=verbose)

    def _steps(self, state, resume=False, approve=None, verbose=False, **_):
        run = state._run
        while True:
            if run.step >= self.max_steps:
                run.status = "step limit reached"
                break
            i = run.current
            notes = []
            ctx = _Ctx(self, state, run, notes, self.names[i], verbose)
            run.interrupt_requested = False
            token = _agent._context.set(ctx)
            try:
                self._run_node(i, state, ctx)
            finally:
                _agent._context.reset(token)
            run.step += 1
            if run.interrupt_requested:
                run.status = "interrupted"
                if self._tracer:
                    self._tracer.step(run, i, i)
                yield self.names[i], notes
                if approve is None:
                    break
                state._agent.answer = bool(approve(run.question))
                if verbose:
                    print(f"  {'':>8} | answer: {'yes' if state._agent.answer else 'no'}")
                continue
            nxt = self._next(i, state)
            run.current = nxt
            if self._tracer:
                self._tracer.step(run, i, nxt)
            yield self.names[i], notes
            if nxt < 0:
                run.status = "done"
                run.question = ""
                break
        if self._tracer:
            self._tracer.end(run)

    def _next(self, i, state) -> int:
        kind, *rest = self.next[i]
        if kind == "edge":
            return rest[0]
        fn, mapping, _ = rest
        key = getattr(self, "_route_" + fn.kind)(state) if isinstance(fn, _pb.PrebuiltRoute) else fn(state)
        if key == END:
            key = END
        if key not in mapping:
            raise GraphError(f"route {getattr(fn, '__name__', fn)} after {self.names[i]!r} returned {key!r}; "
                             f"it may only return {', '.join(map(repr, mapping))}")
        return mapping[key]

    def _run_node(self, i, s, ctx):
        a = self.actions[i]
        if isinstance(a, _pb.Prebuilt):
            getattr(self, "_node_" + a.kind)(s, ctx)
        elif getattr(a, "_tlg_rules", False):
            self._node_rules(s, ctx)
        else:
            a(s)

    # ---- the prebuilt nodes (lm/ and the generated C do the same on the board) ----
    def _want_str(self, mask):
        return self.goals.names(mask) if self.goals else "nothing"

    def run_rules(self, s, ctx=None):
        """Run the @rules function: decides the Think fields and the wanted goals, then the conditions."""
        for f in self.state_schema.thinks():
            s._values[f.name] = _Unset(f.name)
        s._agent.want = 0
        own = ctx is None
        if own:
            ctx = _Ctx(self, s, Run(0), [])
            token = _agent._context.set(ctx)
        ctx.in_rules = True
        try:
            self.rules_fn(s)
        finally:
            ctx.in_rules = False
            if own:
                _agent._context.reset(token)
        for f in self.state_schema.thinks():
            if isinstance(s._values[f.name], _Unset):
                raise AgentError(f"{self.rules_fn.__name__}() didn't decide {f.name} for {s!r}")
        s._agent.facts = self.facts_mask(s)

    def facts_mask(self, s) -> int:
        m = 0
        for k, f in enumerate(self.facts):
            v = f(s)
            if not isinstance(v, bool):
                raise AgentError(f"{f.__name__}() must return True or False, got {v!r}")
            m |= v << k
        return m

    def _node_rules(self, s, ctx):
        ctx.say("inputs: " + " ".join(f"{f.name}={getattr(s, f.name)}" for f in self.state_schema.inputs()))
        self.run_rules(s, ctx)
        if self.state_schema.thinks():
            ctx.say("think: " + " ".join(f"{f.name}={getattr(s, f.name)}" for f in self.state_schema.thinks()))
        ctx.say("want: " + self._want_str(s._agent.want))

    def encode(self, s) -> list[int]:
        sc = self.schema
        return ([sc.token_id("<bos>")] + [sc.token_id(f"{f.name}_{getattr(s, f.name)}") for f in self.state_schema.inputs()]
                + [sc.token_id("<think>")])

    def guide(self, a):
        """Guided writing: once the model starts the plan, only <eos> or a tool call that passes the
        safety checks (unused, conditions hold, after what it needs, nothing unwanted) can come
        next, so its plans are safe by construction. Like allow_plan() in the generated C."""
        sc, d = self.schema, self.domain
        plan_tok, eos, first = sc.token_id("<plan>"), sc.token_id("<eos>"), sc.token_id(self.tools[0].name)
        forbid = d.only_if_wanted & ~a.want

        def allow(out, tok):
            if plan_tok not in out:
                return True                                # still writing its conclusions
            if tok == eos:
                return True
            t = tok - first
            if not 0 <= t < len(self.tools):
                return False
            used = done = 0
            for x in out[out.index(plan_tok) + 1:]:
                used |= 1 << (x - first)
                done |= d.achieves[x - first]
            return (not (used | a.banned) & (1 << t) and (a.facts & d.needs[t]) == d.needs[t]
                    and (done & d.after[t]) == d.after[t] and not d.achieves[t] & forbid)
        return allow

    def _node_reason(self, s, ctx):
        a = s._agent
        a.plan, a.src = [], "none"
        lm = self.lm()
        if lm is None:
            ctx.say("no model loaded: planner will decide")
            return
        sc = self.schema
        out, conf = lm.generate(self.encode(s), sc.token_id("<eos>"), allow=self.guide(a))
        a.lm_conf = conf
        ctx.say("model: " + " ".join(sc.tokens[t] for t in out))
        thinks = self.state_schema.thinks()
        th, plan, i = {}, [], 0
        ok = True
        for f in thinks:
            tok = sc.tokens[out[i]] if i < len(out) else ""
            if not tok.startswith(f.name + "_") or tok[len(f.name) + 1:] not in f.values:
                ok = False
                break
            th[f.name] = tok[len(f.name) + 1:]
            i += 1
        first_act = sc.token_id(self.tools[0].name)
        if ok and (i >= len(out) or sc.tokens[out[i]] != "<plan>"):
            ok = False
        i += 1
        while ok and i < len(out) and sc.tokens[out[i]] != "<eos>":
            if not first_act <= out[i] < first_act + len(self.tools) or len(plan) >= 32:
                ok = False
            else:
                plan.append(out[i] - first_act)
            i += 1
        if not ok or i >= len(out):
            ctx.say("model answer is malformed: discarded")
            return
        a.plan, a.src = plan, "lm"
        if any(th[f.name] != getattr(s, f.name) for f in thinks):
            ctx.say("model disagrees with the device: it thinks " + " ".join(f"{k}={v}" for k, v in th.items()))
        ctx.say(f"confidence {conf:.2f}")

    def _node_mailbox(self, s, ctx):
        from . import net
        net.run_mailbox(self.mailbox, s, ctx)

    def _node_classify(self, s, ctx):
        c = self.classifiers[ctx.node]
        memory = s._models.get(ctx.node)
        if memory is None and ctx.node not in s._models:
            memory = s._models[ctx.node] = c.model.new_memory()
        setattr(s, c.field, c.model.predict(s, memory))
        why = c.model.note()
        ctx.say(f"{c.field}={getattr(s, c.field)}" + (f" ({why})" if why else ""))

    def plan_str(self, plan):
        return " -> ".join(self.tools[x].name for x in plan) if plan else "(nothing)"

    def verify(self, facts, want, banned, plan, approved):
        """Python twin of tlg_verify(): returns (verdict, why)."""
        d, sc = self.domain, self.schema
        fact_names, goal_names = list(sc.facts), [o.name for o in sc.outcomes]

        def first(names, mask):
            return next((names[k] for k in range(len(names)) if mask & (1 << k)), "?")

        props = cost = 0
        needs_approval = False
        for x in plan:
            n = d.names[x]
            if banned & (1 << x):
                return "fail", f"{n} is not allowed now"
            if facts & d.needs[x] != d.needs[x]:
                return "fail", f"{n} needs {first(fact_names, d.needs[x] & ~facts)}"
            if props & d.after[x] != d.after[x]:
                return "fail", f"{n} must come after {first(goal_names, d.after[x] & ~props)}"
            props |= d.achieves[x]
            cost += d.cost[x]
            needs_approval |= d.approval[x]
        unwanted = props & d.only_if_wanted & ~want
        if unwanted:
            return "fail", f"does {first(goal_names, unwanted)}, which nobody asked for"
        if cost > d.budget:
            return "fail", f"over budget (cost {cost} > {d.budget})"
        from .toolchain import planner
        _, achieved, best_cost, best = planner.plan(d, facts, want, banned, self.plan_states)
        if planner.score(d, props, want) < best:
            return "fail", f"incomplete: misses {self.goals.names(achieved & want & ~props)}"
        if planner.score(d, props, want) == best and cost > best_cost:      # keep the better plan
            return "fail", f"costs {cost}; the planner found the same for {best_cost}"
        if needs_approval and not approved:
            return "needs_approval", "needs human approval"
        return "ok", "ok"

    def _node_verify(self, s, ctx):
        a = s._agent
        if a.src == "none":
            a.verdict, a.why = "fail", "no plan"
        else:
            a.verdict, a.why = self.verify(a.facts, a.want, a.banned, a.plan, a.approved)
        ctx.say(f"check {a.src} plan [{self.plan_str(a.plan)}]: {a.why}")
        if a.verdict == "fail" and a.src == "planner":     # can't happen; never act on it anyway
            a.plan, a.verdict = [], "ok"
            ctx.say("nothing valid: doing nothing")

    def _route_after_verify(self, s):
        v = s._agent.verdict
        return "act" if v == "ok" else "ask" if v == "needs_approval" else "plan"

    def _node_plan(self, s, ctx):
        from .toolchain import planner
        a = s._agent
        plan, achieved, cost, _, exact = planner.plan_ex(self.domain, a.facts, a.want, a.banned, self.plan_states)
        a.plan, a.src, a.approved = plan, "planner", False
        ctx.say(f"planner: {self.plan_str(plan)} (cost {cost})")
        if not exact:
            ctx.say(f"planner: greedy (the best plan needs more than {self.plan_states} states)")
        missing = a.want & ~achieved
        if missing:
            ctx.say(f"cannot achieve {self.goals.names(missing)} right now")

    def _node_ask(self, s, ctx):
        a = s._agent
        names = "+".join(self.tools[x].name for x in a.plan if self.tools[x].approval)[:37]
        if a.answer is None:
            q = f"approve {names}?"[:47]
            ctx.say(f"waiting for a human: {q}")
            _agent.interrupt(q)
            return
        if a.answer:
            a.approved = True
            ctx.say(f"human approved {names}")
        else:
            for x in a.plan:
                if self.tools[x].approval:
                    a.banned |= 1 << x
            a.approved, a.plan = False, []
            ctx.say(f"human said no to {names}: replanning without it")
        a.answer = None

    def _route_after_ask(self, s):
        return "verify" if s._agent.approved else "plan"

    def _node_act(self, s, ctx):
        a = s._agent
        a.acted_ok = True
        if a.verdict != "ok":
            ctx.say("plan was not checked: nothing done")
            a.acted_ok = False
            return
        for x in a.plan:
            r = self.tools[x].fn(s)
            ok = r is None or bool(r)
            ctx.say(f"{self.tools[x].name} {'ok' if ok else 'FAILED'}")
            if not ok:
                a.acted_ok = False
                break

    # ---- looking at it ----
    def get_graph(self):
        return _GraphView(self)

    def build(self, target: str = "esp32s3", **kw):
        """Train the model (if the rules changed), write the C code, check it against
        this Python file in every situation, and make the project for `target`:
          "esp32s3"  an Arduino sketch for an ESP32-S3 board, with the web viewer
          "nodemcu"  the same for a NodeMCU (ESP8266): planner-only, no camera
          "c"        just the C files, to use in your own firmware
        (boards.py lists them; the build checks the agent fits the board)"""
        from .build import build
        return build(self, target, **kw)

    def cli(self, argv=None):
        """`python agent.py build`, `python agent.py check`, `python agent.py mermaid`."""
        import argparse
        ap = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]))
        sub = ap.add_subparsers(dest="cmd")
        b = sub.add_parser("build", help="train, generate C, check, make the board project")
        from .boards import BOARDS
        b.add_argument("target", nargs="?", default="esp32s3", choices=[*BOARDS, "c"],
                       help="the board (" + ", ".join(f"{k}: {v.title}" for k, v in BOARDS.items()) + ") or c")
        b.add_argument("--retrain", action="store_true", help="train even if the rules didn't change")
        b.add_argument("--no-train", action="store_true", help="skip training (planner only)")
        sub.add_parser("check", help="check the agent and that C and Python agree, without training")
        sub.add_parser("mermaid", help="print the graph as a Mermaid diagram")
        args = ap.parse_args(argv)
        if args.cmd == "build":
            self.build(args.target, retrain=args.retrain, train=not args.no_train)
        elif args.cmd == "check":
            self.build("c", train=False, quiet_model=True)
        elif args.cmd == "mermaid":
            print(self.get_graph().draw_mermaid())
        else:
            ap.print_help()


class _GraphView:
    def __init__(self, app):
        self.app = app

    def draw_mermaid(self) -> str:
        a = self.app
        ref = lambda i: "END_" if i < 0 else f"n{i}"  # noqa: E731
        lines = ["flowchart TD", f"  START_([start]) --> {ref(a.start)}"]
        lines += [f'  n{i}["{n}"]' for i, n in enumerate(a.names)]
        lines.append("  END_([end])")
        for i, (kind, *rest) in enumerate(a.next):
            for t in ([rest[0]] if kind == "edge" else rest[2]):
                lines.append(f"  n{i} {'-.->' if kind == 'route' else '-->'} {ref(t)}")
        return "\n".join(lines)
