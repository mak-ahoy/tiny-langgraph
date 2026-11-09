"""
Python subset -> C.

Your nodes, routes, rules, conditions and tools are converted to C for the board.
They can use the parts of Python that make sense on a microcontroller:

  if / elif / else, for i in range(n), break, continue, return, pass
  s.field = ...   s.count += 1          local variables (whole numbers or True/False)
  ==  !=  <  <=  >  >=  in (a, b)   and  or  not   + - *  //  %   x if c else y
  min()  max()  abs()                    calls to your other functions and to drivers
  TABLE[s.choice]                        a dict at the top of the file: value -> number

Not available (there is no heap and no text on the device): lists, dicts you build at
runtime, strings other than Choice values and messages, floats, imports, classes,
while loops (they could hang the board), recursion.
Anything else stops the build with the line number and what to use instead.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from . import agent as _agent
from .drivers import Driver
from .graph import END
from .state import State

C_RESERVED = {
    "auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else", "enum",
    "extern", "float", "for", "goto", "if", "int", "long", "register", "return", "short", "signed",
    "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned", "void", "volatile", "while",
    "bool", "true", "false", "NULL", "s", "in", "th", "want", "run", "st", "a", "i", "min", "max", "abs",
    "log", "exp", "printf", "main", "inline", "restrict",
}


class SubsetError(Exception):
    pass


def _type_name(t):
    if isinstance(t, tuple):
        return f"a {t[1]} value" if t[0] == "choice" else "text"
    return {"int": "a whole number", "bool": "True/False"}.get(t, str(t))


class FuncInfo:
    def __init__(self, fn, role):
        self.fn = fn
        self.name = fn.__name__
        self.role = role           # fact, pure, rules, node, route, tool, helper
        self.tree = None
        self.file = inspect.getsourcefile(fn) or "?"
        self.line0 = fn.__code__.co_firstlineno
        try:
            src = textwrap.dedent(inspect.getsource(fn))
        except OSError:
            raise SubsetError(f"can't read the source of {self.name}(); define it in a .py file") from None
        mod = ast.parse(src)
        self.tree = mod.body[0]
        if not isinstance(self.tree, ast.FunctionDef):
            raise SubsetError(f"{self.name} must be a plain def function (no lambdas)")
        args = self.tree.args
        if (len(args.args) != 1 or args.vararg or args.kwarg or args.kwonlyargs or args.defaults
                or args.posonlyargs):
            raise SubsetError(f"{self.where(self.tree)}: {self.name}() must take exactly one argument, the state (s)")
        self.s = args.args[0].arg
        self.ret = None             # return type, filled in by the converter
        self.cname_override = None

    def where(self, node) -> str:
        import os
        rel = os.path.relpath(self.file) if os.path.isabs(self.file) else self.file
        name = rel if len(rel) < len(self.file) and not rel.startswith("..") else self.file
        return f"{name}:{self.line0 + getattr(node, 'lineno', 1) - 1}"


class Converter:
    """Turns the functions of a compiled graph into C."""

    def __init__(self, app, names):
        self.app = app
        self.n = names               # toolchain.codegen._Names
        self.P, self.p = names.P, names.p
        self.st: type[State] = app.state_schema
        self.fields = self.st._fields
        self.funcs: dict[object, FuncInfo] = {}
        self.drivers: dict[int, tuple[str, Driver]] = {}
        self.tables: dict[tuple[str, str], tuple[str, list[int]]] = {}
        self.table_users: dict[tuple[str, str], set[str]] = {}     # "rules" (rules.c) or "agent" (agent.c)
        self.uses_note = False

    # ------------------------------------------------------------ discovery
    def collect(self):
        """Find every function the graph reaches, and whether it must be pure (only reads
        the observed and concluded fields: conditions, rules and what they call)."""
        app = self.app
        roots = []
        for f in app.facts:
            roots.append((f, "fact"))
        for c in getattr(app, "classifiers", {}).values():     # conditions a catalog model watches
            for f in c.model.functions():
                roots.append((f, "fact"))
        if app.rules_fn:
            roots.append((app.rules_fn, "rules"))
        for a in app.actions:
            if callable(a) and not getattr(a, "_tlg_rules", False) and not hasattr(a, "kind"):
                roots.append((a, "node"))
        for kind, *rest in app.next:
            if kind == "route" and not hasattr(rest[0], "kind"):
                roots.append((rest[0], "route"))
        for t in app.tools:
            roots.append((t.fn, "tool"))

        for fn, role in roots:
            if fn in self.funcs:
                prev = self.funcs[fn].role
                if {prev, role} == {"fact", "rules"} or prev in ("node", "route", "tool") or role != "fact":
                    if prev != role:
                        raise SubsetError(f"{fn.__name__}() is used as a {prev} and as a {role}; "
                                          "give each job its own function")
                continue
            self.funcs[fn] = FuncInfo(fn, role)

        # helpers called from these: pure if reached from a pure function
        pure = [f for f, i in self.funcs.items() if i.role in ("fact", "rules")]
        todo = list(pure)
        while todo:
            fn = todo.pop()
            for callee in self._calls(self.funcs[fn]):
                info = self.funcs.get(callee)
                if info is None:
                    self.funcs[callee] = FuncInfo(callee, "pure")
                    todo.append(callee)
                elif info.role in ("node", "route", "tool", "rules"):
                    raise SubsetError(f"{self.funcs[fn].name}() calls {info.name}(), which is a {info.role}")
        todo = [f for f, i in self.funcs.items() if i.role in ("node", "route", "tool")]
        while todo:
            fn = todo.pop()
            for callee in self._calls(self.funcs[fn]):
                info = self.funcs.get(callee)
                if info is None:
                    self.funcs[callee] = FuncInfo(callee, "helper")
                    todo.append(callee)
                elif info.role in ("node", "route", "tool", "rules"):
                    raise SubsetError(f"{self.funcs[fn].name}() calls {info.name}(), which is a {info.role}; "
                                      "move the shared part into a helper function")
        self._check_cycles()

    def _calls(self, info):
        out = []
        body = ast.Module(body=info.tree.body, type_ignores=[])       # not the decorators
        for node in ast.walk(body):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                v = self._global(info, node.func.id, quiet=True)
                if inspect.isfunction(v) and v not in (_agent.want, _agent.note, _agent.interrupt):
                    out.append(v)
                if isinstance(v, _agent.Tool):
                    raise SubsetError(f"{info.where(node)}: tools can't be called directly; "
                                      "the planner decides when they run")
        return out

    def _check_cycles(self):
        state = {}

        def visit(fn, path):
            if state.get(fn) == "done":
                return
            if state.get(fn) == "busy":
                raise SubsetError("recursion isn't supported: " + " -> ".join(f.__name__ for f in path + [fn]))
            state[fn] = "busy"
            for c in self._calls(self.funcs[fn]):
                visit(c, path + [fn])
            state[fn] = "done"

        for fn in list(self.funcs):
            visit(fn, [])

    def _global(self, info, name, quiet=False):
        g = info.fn.__globals__
        if name in g:
            return g[name]
        b = g.get("__builtins__", {})
        b = b if isinstance(b, dict) else vars(b)
        if name in ("min", "max", "abs", "range", "True", "False"):
            return b.get(name)
        if quiet:
            return None
        raise SubsetError(f"unknown name {name!r}")

    # ------------------------------------------------------------ naming
    def cname(self, info) -> str:
        if info.cname_override:
            return info.cname_override
        return {"fact": f"{self.p}_{info.name}", "pure": f"{self.p}_{info.name}", "rules": f"{self.p}_rules",
                "node": f"node_{info.name}", "route": f"route_{info.name}", "tool": f"tool_{info.name}",
                "helper": f"fn_{info.name}"}[info.role]

    def driver_name(self, obj: Driver) -> str:
        key = id(obj)
        if key not in self.drivers:
            names = [k for k, v in self._all_globals().items() if v is obj]
            name = names[0] if names else f"driver{len(self.drivers)}"
            self.drivers[key] = (name, obj)
        return self.drivers[key][0]

    def _all_globals(self):
        out = {}
        for info in self.funcs.values():
            out.update(info.fn.__globals__)
        return out

    def signature(self, info) -> str:
        P, p = self.P, self.p
        ret = {"bool": "bool", "int": "int32_t", None: "void"}[info.ret]
        if info.role in ("fact", "pure"):
            return f"{ret} {self.cname(info)}(const {p}_inputs_t *in, const {p}_think_t *th)"
        if info.role == "rules":
            return f"void {p}_rules(const {p}_inputs_t *in, {p}_think_t *th, uint32_t *want)"
        if info.role == "node":
            return f"static void {self.cname(info)}(void *st, tlg_run_t *run)"
        if info.role == "route":
            return f"static int {self.cname(info)}(const void *st)"
        if info.role == "tool":
            return f"static bool {self.cname(info)}({p}_state_t *s)"
        return f"static {ret} {self.cname(info)}({p}_state_t *s)"

    # ------------------------------------------------------------ conversion
    def convert(self, info, route_map=None) -> str:
        fc = _Func(self, info, route_map)
        fc.run(collect=True)                 # first pass: types of locals and returns
        body = fc.run(collect=False)
        if info.role in ("fact",) and info.ret != "bool":
            raise SubsetError(f"{info.where(info.tree)}: condition {info.name}() must return True or False")
        decls = [f"    {'bool' if t == 'bool' else 'int32_t'} {fc.local_c(n)} = 0;"
                 for n, t in fc.locals.items() if n not in fc.loop_vars]
        unused = []
        if info.role in ("fact", "pure"):
            unused = ["    (void)in; (void)th;"]
        elif info.role == "rules":
            unused = ["    (void)in; (void)th; (void)want;"]
        elif info.role == "node":
            unused = [f"    {self.p}_state_t *s = st;", "    (void)s; (void)run;"]
        elif info.role == "route":
            unused = [f"    const {self.p}_state_t *s = st;", "    (void)s;"]
        elif info.role in ("tool", "helper"):
            unused = ["    (void)s;"]
        tail = []
        if info.role == "tool":
            tail = ["    return true;"]
        elif info.role in ("fact", "pure", "helper") and info.ret is not None:
            tail = [f"    return {'false' if info.ret == 'bool' else '0'};   /* not reached in Python */"]
        elif info.role == "route":
            tail = ["    return TLG_END;   /* Python would return None here: not a declared target */"]
        doc = ast.get_docstring(info.tree)
        where = info.file.replace("\\", "/").split("/")[-1]       # the file name only, also for Windows paths
        head = [f"/* {info.name}() from {where}:{info.line0}"
                + (f": {doc.splitlines()[0].replace('*/', '* /')}" if doc else "") + " */"]
        return "\n".join(head + [self.signature(info), "{"] + unused + decls + body + tail + ["}", ""])


class _Func:
    def __init__(self, conv: Converter, info: FuncInfo, route_map):
        self.c, self.info, self.route_map = conv, info, route_map or {}
        self.locals: dict[str, str] = {}
        self.loop_vars: set[str] = set()
        self.collect = False

    def err(self, node, msg):
        return SubsetError(f"{self.info.where(node)}: {msg}")

    def local_c(self, name):
        return name + "_" if name in C_RESERVED or name.startswith("tlg_") else name

    @property
    def pure(self):
        return self.info.role in ("fact", "pure", "rules")

    def run(self, collect):
        self.collect = collect
        self.loop_depth = 0
        if not collect:
            pass
        out = []
        body = self.info.tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]                          # docstring
        self.block(body, out, 1)
        return out

    # ------------------------------------------------------------ statements
    def block(self, stmts, out, depth):
        for st in stmts:
            self.stmt(st, out, depth)

    def stmt(self, st, out, d):
        pad = "    " * d
        if isinstance(st, ast.Pass):
            return
        if isinstance(st, ast.Expr):
            v = st.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                return                               # a string used as a comment
            if not isinstance(v, ast.Call):
                raise self.err(st, "this line does nothing on its own")
            code, _ = self.call(v, statement=True)
            out.append(f"{pad}{code};")
            return
        if isinstance(st, (ast.Assign, ast.AnnAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            if len(targets) != 1 or st.value is None:
                raise self.err(st, "assign one thing at a time (a = b = c and a, b = ... aren't supported)")
            self.assign(targets[0], st.value, out, pad)
            return
        if isinstance(st, ast.AugAssign):
            self.augassign(st, out, pad)
            return
        if isinstance(st, ast.If):
            self.if_chain(st, out, d)
            return
        if isinstance(st, ast.For):
            self.for_range(st, out, d)
            return
        if isinstance(st, (ast.Break, ast.Continue)):
            if self.loop_depth == 0:
                raise self.err(st, "break/continue outside a loop")
            out.append(f"{pad}{'break' if isinstance(st, ast.Break) else 'continue'};")
            return
        if isinstance(st, ast.Return):
            out.append(pad + self.ret(st))
            return
        if isinstance(st, ast.While):
            raise self.err(st, "while loops aren't supported (one that never ends would hang the board); "
                               "use for i in range(n)")
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            raise self.err(st, "imports inside functions aren't supported; drivers go at the top of the file")
        if isinstance(st, (ast.Global, ast.Nonlocal)):
            raise self.err(st, "global variables can't change on the device; keep changing values in the state (Int fields)")
        raise self.err(st, f"{type(st).__name__} statements aren't supported on the device")

    def if_chain(self, st, out, d):
        pad = "    " * d
        first = True
        while True:
            cond = _strip(self.cond(st.test))
            out.append(f"{pad}{'if' if first else '} else if'} ({cond}) {{")
            self.block(st.body, out, d + 1)
            first = False
            if len(st.orelse) == 1 and isinstance(st.orelse[0], ast.If):
                st = st.orelse[0]
                continue
            if st.orelse:
                out.append(f"{pad}}} else {{")
                self.block(st.orelse, out, d + 1)
            out.append(f"{pad}}}")
            return

    def for_range(self, st, out, d):
        pad = "    " * d
        it = st.iter
        if not (isinstance(st.target, ast.Name) and isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
                and it.func.id == "range" and 1 <= len(it.args) <= 2 and not it.keywords):
            raise self.err(st, "only  for i in range(n)  or  for i in range(a, b)  loops are supported")
        if st.orelse:
            raise self.err(st, "for ... else isn't supported")
        lo, hi = (("0", it.args[0]) if len(it.args) == 1 else (self.int_expr(it.args[0]), it.args[1]))
        hi = self.int_expr(hi)
        name = st.target.id
        if name in self.locals and self.locals[name] != "int":
            raise self.err(st, f"{name} was True/False before; use another name for the loop")
        self.locals[name] = "int"
        self.loop_vars.add(name)
        v = self.local_c(name)
        out.append(f"{pad}for (int32_t {v} = {lo}; {v} < {hi}; {v}++) {{")
        self.loop_depth += 1
        self.block(st.body, out, d + 1)
        self.loop_depth -= 1
        out.append(f"{pad}}}")

    def field(self, node):
        """s.<field> -> (Field, C lvalue/rvalue)."""
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == self.info.s):
            return None
        f = self.c.fields.get(node.attr)
        if f is None:
            raise self.err(node, f"the state has no field {node.attr!r} (fields: {', '.join(self.c.fields)})")
        role = self.info.role
        if f.kind == "int" and self.pure:
            raise self.err(node, f"{self.info.name}() can only read Choice and Think fields (it must give the same "
                                 f"answer for the same situation); s.{f.name} is memory")
        if self.pure:
            return f, ("in->" if f.kind == "choice" else "th->") + f.name
        base = "s->in." if f.kind == "choice" else "s->th." if f.kind == "think" else "s->"
        del role
        return f, base + f.name

    def assign(self, target, value, out, pad):
        fr = self.field(target)
        if fr:
            f, lv = fr
            role = self.info.role
            if role in ("fact", "pure", "route"):
                raise self.err(target, f"{self.info.name}() can't change the state ({role}s only read it)")
            if role == "rules" and f.kind != "think":
                raise self.err(target, f"the rules decide Think fields only; set s.{f.name} in a node")
            if f.kind in ("choice", "think"):
                out.append(f"{pad}{lv} = {self.choice_expr(value, f)};")
            else:
                out.append(f"{pad}{lv} = {self.int_expr(value)};")
            return
        if isinstance(target, ast.Name):
            code, t = self.expr(value)
            if t not in ("int", "bool"):
                raise self.err(value, f"local variables hold whole numbers or True/False, not {_type_name(t)}; "
                                      "assign Choice values straight to a state field")
            prev = self.locals.get(target.id)
            if prev and prev != t:
                raise self.err(target, f"{target.id} was {_type_name(prev)}, now {_type_name(t)}; use another name")
            if target.id in self.c.fields:
                pass
            self.locals[target.id] = t
            out.append(f"{pad}{self.local_c(target.id)} = {code};")
            return
        raise self.err(target, "you can assign to s.<field> or to a local variable")

    def augassign(self, st, out, pad):
        ops = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*"}
        fr = self.field(st.target)
        if fr:
            f, lv = fr
            if self.info.role in ("fact", "pure", "route", "rules"):
                raise self.err(st, f"{self.info.name}() can't change s.{f.name}")
            if f.kind != "int":
                raise self.err(st, f"s.{f.name} is a Choice; += works on Int fields")
        elif isinstance(st.target, ast.Name):
            if self.locals.get(st.target.id) != "int" and not self.collect:
                raise self.err(st, f"{st.target.id} must be a whole number assigned earlier")
            self.locals.setdefault(st.target.id, "int")
            lv = self.local_c(st.target.id)
        else:
            raise self.err(st, "+= works on s.<field> or a local variable")
        rhs = self.int_expr(st.value)
        if type(st.op) in ops:
            out.append(f"{pad}{lv} {ops[type(st.op)]}= {rhs};")
        elif isinstance(st.op, ast.FloorDiv):
            out.append(f"{pad}{lv} = tlg_floordiv({lv}, {rhs});")
        elif isinstance(st.op, ast.Mod):
            out.append(f"{pad}{lv} = tlg_mod({lv}, {rhs});")
        else:
            raise self.err(st, "only += -= *= //= %= are supported")

    def ret(self, st):
        role, v = self.info.role, st.value
        if role == "route":
            if v is None:
                raise self.err(st, "a route must return where to go")
            return f"return {self.route_target(v)};"
        if role in ("node", "rules"):
            if v is not None:
                raise self.err(st, "nodes don't return a value; change the state instead")
            return "return;"
        if role == "tool":
            if v is None:
                return "return true;"
            return f"return {self.cond(v)};"
        if v is None:
            raise self.err(st, f"{self.info.name}() must return a value (it's used in conditions)")
        code, t = self.expr(v)
        if t not in ("int", "bool"):
            raise self.err(st, f"{self.info.name}() can return a whole number or True/False")
        if self.info.ret not in (None, t):
            raise self.err(st, f"{self.info.name}() returns {_type_name(self.info.ret)} elsewhere")
        self.info.ret = t
        return f"return {code};"

    def route_target(self, v) -> str:
        if isinstance(v, ast.IfExp):
            return f"({self.cond(v.test)} ? {self.route_target(v.body)} : {self.route_target(v.orelse)})"
        key = None
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            key = v.value
        elif isinstance(v, ast.Name) and self.c._global(self.info, v.id, quiet=True) == END:
            key = END
        if key is None:
            raise self.err(v, "a route returns a node name (\"act\"), END, or  a if cond else b")
        if key not in self.route_map:
            raise self.err(v, f"route returns {key!r}, but add_conditional_edges only lists "
                              f"{', '.join(map(repr, self.route_map))}")
        t = self.route_map[key]
        return "TLG_END" if t < 0 else f"{self.c.P}_NODE_{self.c.app.names[t].upper()}"

    # ------------------------------------------------------------ expressions
    def cond(self, node) -> str:
        """An expression used as a truth value: ints are allowed (non-zero = true)."""
        if isinstance(node, ast.BoolOp):
            op = " && " if isinstance(node.op, ast.And) else " || "
            return "(" + op.join(self.cond(x) for x in node.values) + ")"
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return f"!{self.cond(node.operand)}"
        code, t = self.expr(node)
        if t == "bool":
            return code
        if t == "int":
            return f"({code} != 0)"
        raise self.err(node, f"compare it, e.g. s.x == \"value\" (this is {_type_name(t)})")

    def int_expr(self, node) -> str:
        code, t = self.expr(node)
        if t not in ("int", "bool"):
            raise self.err(node, f"expected a whole number, got {_type_name(t)}")
        return code

    def choice_expr(self, node, f) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in f.values:
                raise self.err(node, f"{node.value!r} is not a value of {f.name} ({', '.join(f.values)})")
            return self.c.n.val(f.name, node.value)
        if isinstance(node, ast.IfExp):
            return f"({self.cond(node.test)} ? {self.choice_expr(node.body, f)} : {self.choice_expr(node.orelse, f)})"
        code, t = self.expr(node)
        if isinstance(t, tuple) and t[0] == "choice":
            other = self.c.fields[t[1]]
            if tuple(other.values) == tuple(f.values):
                return code
        raise self.err(node, f"s.{f.name} takes one of {', '.join(map(repr, f.values))}")

    def expr(self, node) -> tuple[str, object]:
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return ("true" if v else "false"), "bool"
            if isinstance(v, int):
                if not -(2 ** 31) <= v < 2 ** 31:
                    raise self.err(node, f"{v} doesn't fit in 32 bits")
                return str(v), "int"
            if isinstance(v, str):
                return "", ("str", v)
            if isinstance(v, float):
                raise self.err(node, "floats aren't supported yet; use whole numbers (e.g. tenths of a degree)")
            raise self.err(node, f"{v!r} isn't supported on the device")
        if isinstance(node, ast.Name):
            if node.id in self.locals:
                return self.local_c(node.id), self.locals[node.id]
            if node.id == self.info.s:
                raise self.err(node, f"use a field, like {self.info.s}.something")
            v = self.c._global(self.info, node.id, quiet=True)
            if isinstance(v, bool):
                return ("true" if v else "false"), "bool"
            if isinstance(v, int):
                return f"{v}", "int"
            if v is None and self.collect:
                return "0", "int"
            if v is None:
                raise self.err(node, f"{node.id!r} is used before it gets a value")
            raise self.err(node, f"{node.id} ({type(v).__name__}) can't be used as a value on the device")
        if isinstance(node, ast.Attribute):
            fr = self.field(node)
            if fr:
                f, code = fr
                return code, ("int" if f.kind == "int" else ("choice", f.name))
            raise self.err(node, "only state fields (s.x) can be read this way; call driver methods with ()")
        if isinstance(node, ast.Compare):
            return self.compare(node), "bool"
        if isinstance(node, ast.BoolOp):
            parts = []
            for x in node.values:
                code, t = self.expr(x)
                if t != "bool":
                    raise self.err(x, "and/or need True/False on both sides; compare numbers first (x > 0)")
                parts.append(code)
            op = " && " if isinstance(node.op, ast.And) else " || "
            return "(" + op.join(parts) + ")", "bool"
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return f"!{self.cond(node.operand)}", "bool"
            if isinstance(node.op, ast.USub):
                return f"(-{self.int_expr(node.operand)})", "int"
            if isinstance(node.op, ast.UAdd):
                return self.int_expr(node.operand), "int"
            raise self.err(node, "~ isn't supported")
        if isinstance(node, ast.BinOp):
            a, b = self.int_expr(node.left), self.int_expr(node.right)
            ops = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*"}
            if type(node.op) in ops:
                return f"({a} {ops[type(node.op)]} {b})", "int"
            if isinstance(node.op, ast.FloorDiv):
                return f"tlg_floordiv({a}, {b})", "int"
            if isinstance(node.op, ast.Mod):
                return f"tlg_mod({a}, {b})", "int"
            if isinstance(node.op, ast.Div):
                raise self.err(node, "use // (whole-number division); there are no floats on the device")
            raise self.err(node, "only + - * // % are supported")
        if isinstance(node, ast.IfExp):
            c = self.cond(node.test)
            a, ta = self.expr(node.body)
            b, tb = self.expr(node.orelse)
            if ta != tb or ta not in ("int", "bool"):
                raise self.err(node, "x if c else y: both sides must be numbers or True/False "
                                     "(for a Choice, assign it straight to the field)")
            return f"({c} ? {a} : {b})", ta
        if isinstance(node, ast.Call):
            code, t = self.call(node)
            if t is None:
                raise self.err(node, "this call doesn't return a value")
            return code, t
        if isinstance(node, ast.Subscript):
            return self.subscript(node), "int"
        if isinstance(node, ast.JoinedStr):
            raise self.err(node, "f-strings aren't supported; note() takes a message and values: note(\"moves\", s.moves)")
        if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.Tuple, ast.ListComp, ast.DictComp,
                             ast.SetComp, ast.GeneratorExp)):
            raise self.err(node, "lists, dicts and sets can't be built on the device (no heap); "
                                 "use state fields, or a table at the top of the file")
        if isinstance(node, ast.Lambda):
            raise self.err(node, "lambdas aren't supported; use a def function")
        raise self.err(node, f"{type(node).__name__} isn't supported on the device")

    def compare(self, node) -> str:
        parts, left = [], node.left
        for op, right in zip(node.ops, node.comparators):
            parts.append(self.compare_one(left, op, right, node))
            left = right
        return parts[0] if len(parts) == 1 else "(" + " && ".join(parts) + ")"

    def compare_one(self, left, op, right, node):
        if isinstance(op, (ast.In, ast.NotIn)):
            if not isinstance(right, (ast.Tuple, ast.List)) or not right.elts:
                raise self.err(node, "in needs a list of values written out: x in (\"a\", \"b\")")
            ors = " || ".join(self.compare_one(left, ast.Eq(), e, node) for e in right.elts)
            return f"({ors})" if isinstance(op, ast.In) else f"!({ors})"
        if isinstance(op, (ast.Is, ast.IsNot)):
            raise self.err(node, "use == and != instead of is")
        cops = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
        a, ta = self.expr(left)
        b, tb = self.expr(right)
        if isinstance(ta, tuple) and ta[0] == "str":
            (a, ta), (b, tb), left, right = (b, tb), (a, ta), right, left
        if isinstance(ta, tuple) and ta[0] == "choice":
            if type(op) not in (ast.Eq, ast.NotEq):
                raise self.err(node, f"Choice fields can only be compared with == and != (s.{ta[1]})")
            f = self.c.fields[ta[1]]
            if isinstance(tb, tuple) and tb[0] == "str":
                if tb[1] not in f.values:
                    raise self.err(right, f"{tb[1]!r} is not a value of {f.name} ({', '.join(f.values)})")
                b = self.c.n.val(f.name, tb[1])
            elif isinstance(tb, tuple) and tb[0] == "choice":
                if tuple(self.c.fields[tb[1]].values) != tuple(f.values):
                    raise self.err(node, f"s.{ta[1]} and s.{tb[1]} have different values")
            else:
                raise self.err(node, f"compare s.{f.name} with one of its values: {', '.join(map(repr, f.values))}")
            return f"({a} {cops[type(op)]} {b})"
        if ta not in ("int", "bool") or tb not in ("int", "bool"):
            raise self.err(node, f"can't compare {_type_name(ta)} with {_type_name(tb)}")
        return f"({a} {cops[type(op)]} {b})"

    def subscript(self, node) -> str:
        if not isinstance(node.value, ast.Name):
            raise self.err(node, "only TABLE[s.field] lookups are supported")
        table = self.c._global(self.info, node.value.id, quiet=True)
        fr = self.field(node.slice) if isinstance(node.slice, ast.Attribute) else None
        if not isinstance(table, dict) or not fr or fr[0].kind == "int":
            raise self.err(node, "lookups must be a dict at the top of the file indexed by a Choice field: "
                                 "POINTS[s.cam]")
        f, code = fr
        missing = [v for v in f.values if v not in table]
        if missing:
            raise self.err(node, f"{node.value.id} has no entry for {', '.join(missing)} (needs every value of {f.name})")
        vals = []
        for v in f.values:
            x = table[v]
            if isinstance(x, bool) or not isinstance(x, int):
                raise self.err(node, f"{node.value.id}[{v!r}] must be a whole number")
            vals.append(x)
        cname = f"T_{node.value.id.upper()}_{f.name.upper()}"
        self.c.tables[(node.value.id, f.name)] = (cname, vals)
        self.c.table_users.setdefault((node.value.id, f.name), set()).add("rules" if self.pure else "agent")
        return f"{cname}[{code}]"

    def call(self, node, statement=False) -> tuple[str, object]:
        if node.keywords:
            raise self.err(node, "keyword arguments aren't supported in calls; pass values in order")
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            obj = self.c._global(self.info, fn.value.id, quiet=True)
            from . import net as _net
            from . import prebuilt as _pb
            if isinstance(obj, _net.Peer) and fn.attr == "call":
                return self.net_call(node, obj)
            if isinstance(obj, _pb.Mailbox) and fn.attr == "reply":
                return self.net_reply(node, obj)
            if isinstance(obj, Driver):
                return self.driver_call(node, obj, fn.attr)
            if fn.value.id == self.info.s:
                raise self.err(node, "the state has no methods; read and set its fields")
            raise self.err(node, f"{fn.value.id}.{fn.attr}() isn't a driver method")
        if not isinstance(fn, ast.Name):
            raise self.err(node, "call functions by name")
        name = fn.id
        if name in ("min", "max"):
            if len(node.args) != 2:
                raise self.err(node, f"{name}() takes two numbers on the device")
            return f"tlg_{name}({self.int_expr(node.args[0])}, {self.int_expr(node.args[1])})", "int"
        if name == "abs":
            return f"tlg_abs({self.int_expr(node.args[0])})", "int"
        v = self.c._global(self.info, name, quiet=True)
        if v is _agent.want:
            if self.info.role != "rules":
                raise self.err(node, "want() belongs in the @rules function")
            bits = []
            for a in node.args:
                g = self.goal(a)
                bits.append(f"{self.c.P}_OUT({g.name.upper()})")
            return f"*want |= {' | '.join(bits)}", None
        if v is _agent.note:
            return self.note(node), None
        if v is _agent.interrupt:
            if self.info.role != "node":
                raise self.err(node, "interrupt() can only be called in a node")
            if len(node.args) != 1 or not (isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                raise self.err(node, "interrupt(\"question?\") takes one message")
            return f"tlg_interrupt(run, {_c_str(node.args[0].value[:47])})", None
        if inspect.isfunction(v):
            info = self.c.funcs.get(v)
            if info is None:
                raise self.err(node, f"{name}() isn't part of this agent")
            if len(node.args) != 1 or not (isinstance(node.args[0], ast.Name) and node.args[0].id == self.info.s):
                raise self.err(node, f"call it with the state: {name}({self.info.s})")
            if info.role in ("fact", "pure"):
                if info.ret is None and not self.collect:
                    info.ret = self._ret_type(info)
                args = "in, th" if self.pure else "&s->in, &s->th"
                return f"{self.c.cname(info)}({args})", info.ret or self._ret_type(info)
            if self.pure:
                raise self.err(node, f"{name}() reads memory or hardware, so {self.info.name}() can't call it")
            return f"{self.c.cname(info)}(s)", info.ret if info.ret else self._ret_type(info)
        if isinstance(v, _agent.Tool):
            raise self.err(node, "tools can't be called directly; the planner decides when they run")
        raise self.err(node, f"{name}() isn't available on the device")

    def _ret_type(self, info):
        if info.ret is None:
            sub = _Func(self.c, info, None)
            sub.run(collect=True)
        return info.ret

    def goal(self, a):
        if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name):
            obj = self.c._global(self.info, a.value.id, quiet=True)
            g = getattr(obj, a.attr, None)
            if isinstance(g, _agent.Goal):
                return g
        raise self.err(a, "want() takes goals, like want(goals.fed)")

    def note(self, node):
        if self.pure:
            raise self.err(node, "note() isn't available in conditions and rules (they also run while making "
                                 "training data); add notes in a node")
        args = node.args
        if not args or not (isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)):
            raise self.err(node, "note(\"message\", values...) starts with a message")
        fmt, vals = [args[0].value.replace("%", "%%")], []
        for a in args[1:]:
            code, t = self.expr(a)
            if t == "int":
                fmt.append("%ld")
                vals.append(f"(long){code}")
            elif t == "bool":
                fmt.append("%s")
                vals.append(f'({code} ? "True" : "False")')
            elif isinstance(t, tuple) and t[0] == "choice":
                fmt.append("%s")
                vals.append(f"V_{t[1].upper()}[{code}]")
            else:
                raise self.err(a, "note() can show numbers, True/False and Choice fields")
        self.c.uses_note = True
        return f"tlg_agent_say(&s->agent, {_c_str(' '.join(fmt))}{''.join(', ' + v for v in vals)})"

    def _contract_arg(self, node):
        from .net import Contract
        a = node.args[0] if node.args else None
        c = self.c._global(self.info, a.id, quiet=True) if isinstance(a, ast.Name) else None
        if not isinstance(c, Contract):
            raise self.err(node, "the first value is a contract, like feeder.call(feed, \"one\")")
        return c

    def _net_allowed(self, node, what):
        if self.pure or self.info.role == "route":
            raise self.err(node, f"{self.info.name}() can't {what} ({self.info.role}s must only read the state)")

    def net_call(self, node, peer):
        """feeder.call(feed, "one") -> queue a call: the contract and values become numbers."""
        self._net_allowed(node, "call other boards")
        c = self._contract_arg(node)
        mb = self.c.app.mailbox
        used = [(p, k) for p, ks in (mb.uses.items() if mb else ()) for k in ks]
        if (peer, c) not in used:
            raise self.err(node, f"add it to the mailbox: prebuilt.Mailbox(uses={{{peer.name}: [{c.name}]}})")
        values = node.args[1:]
        if len(values) != len(c.params):
            raise self.err(node, f"{c.name} takes {len(c.params)} value(s): {', '.join(c.params) or 'none'}")
        idx = []
        for (p, allowed), a in zip(c.params.items(), values):
            if not (isinstance(a, ast.Constant) and a.value in allowed):
                raise self.err(a, f"{p} is written out, one of {', '.join(map(repr, allowed))}")
            idx.append(str(allowed.index(a.value)))
        vals = f"(const uint8_t[]){{ {', '.join(idx)} }}" if idx else "NULL"
        return f"{self.c.p}_net_call(s, {used.index((peer, c))}, {vals}, {len(idx)})", None

    def net_reply(self, node, mailbox):
        """mailbox.reply(feed, "done") -> answer this run's request."""
        self._net_allowed(node, "reply to other boards")
        c = self._contract_arg(node)
        if c not in mailbox.serves:
            raise self.err(node, f"add it to the mailbox: prebuilt.Mailbox(serves=[{c.name}])")
        if len(node.args) != 2 or not (isinstance(node.args[1], ast.Constant) and node.args[1].value in c.replies):
            raise self.err(node, f"mailbox.reply({c.name}, ...) takes one of {', '.join(map(repr, c.replies))}")
        return f"{self.c.p}_net_reply(s, {mailbox.serves.index(c)}, {c.replies.index(node.args[1].value)})", None

    def driver_call(self, node, obj, method):
        if self.pure or self.info.role == "route":
            raise self.err(node, f"{self.info.name}() can't use hardware ({self.info.role}s must only read the state)")
        methods = obj.c_methods()
        if method not in methods:
            raise self.err(node, f"{type(obj).__name__} has no method {method}() "
                                 f"(it has {', '.join(sorted(methods)) or 'none'})")
        params, ret = methods[method]
        if len(node.args) != len(params):
            raise self.err(node, f"{method}() takes {len(params)} value(s): {', '.join(p for p, _ in params)}")
        args = []
        if obj.c_struct:
            args.append(f"&drv_{self.c.driver_name(obj)}")
        else:
            self.c.driver_name(obj)
        for (pname, ptype), a in zip(params, node.args):
            if ptype == "str":
                if not (isinstance(a, ast.Constant) and isinstance(a.value, str)):
                    raise self.err(a, f"{pname} must be written out as text, like \"hello\"")
                args.append(_c_str(a.value))
            elif ptype == "bool":
                args.append(self.cond(a))
            else:
                args.append(self.int_expr(a))
        t = None if ret == "None" else ret
        return f"{obj.c_prefix}_{method}({', '.join(args)})", t


def _c_str(text: str) -> str:
    out = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + "".join(ch if 32 <= ord(ch) < 127 else f"\\x{ord(ch):02x}\"\"" for ch in out) + '"'


def _strip(code: str) -> str:
    """(a == b) -> a == b, when the outer parentheses wrap the whole expression."""
    if not (code.startswith("(") and code.endswith(")")):
        return code
    depth = 0
    for i, ch in enumerate(code):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and i < len(code) - 1:
            return code
    return code[1:-1]
