"""
State: what the agent knows. Declare it like a LangGraph state, one field per line:

    class Pet(State):
        cat       = Choice("none", "seen", "in_zone")   # observed: the model reads it
        wellbeing = Think("ok", "watch", "concern")     # concluded: the model predicts it
        minutes   = Int(0)                              # memory: kept between runs

On the device the state becomes a C struct, so fields have fixed types:
  Choice  one of a few named values (a uint8_t)
  Score   ordered levels answered by a catalog model (models.X in prebuilt.Classify)
  Think   a Choice the model concludes before planning
  Int     a whole number (int32_t), for counters, timers and memory
"""
from __future__ import annotations

import re

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
INT_MIN, INT_MAX = -(2 ** 31), 2 ** 31 - 1


class _Unset:
    """A think field the rules haven't decided yet. Reading it is a mistake."""

    def __init__(self, field):
        self.field = field

    def _fail(self, *_):
        raise RuntimeError(f"the rules read '{self.field}' before deciding it; "
                           f"set s.{self.field} first")

    __eq__ = __ne__ = __bool__ = __hash__ = __lt__ = __gt__ = __contains__ = _fail

    def __repr__(self):
        return "<unset>"


class Field:
    kind = "field"

    def __set_name__(self, owner, name):
        if not NAME_RE.match(name):
            raise ValueError(f"state field {name!r} must be lower_snake_case")
        self.name = name

    def check(self, value):
        return value


class Choice(Field):
    """One of a few named values. The first one is the default."""
    kind = "choice"

    def __init__(self, *values: str, default: str | None = None):
        if len(values) < 2:
            raise ValueError("a Choice needs at least 2 values, e.g. Choice('off', 'on')")
        for v in values:
            if not isinstance(v, str) or not NAME_RE.match(v):
                raise ValueError(f"Choice value {v!r} must be a lower_snake_case string")
        if len(set(values)) != len(values):
            raise ValueError(f"Choice{values} has duplicate values")
        self.values = tuple(values)
        self.default = default if default is not None else values[0]
        if self.default not in self.values:
            raise ValueError(f"default {default!r} is not one of {values}")

    def check(self, value):
        if isinstance(value, _Unset):
            return value
        if value not in self.values:
            raise ValueError(f"{self.name} can't be {value!r}: use one of {', '.join(self.values)}")
        return value


class Think(Choice):
    """A conclusion: the model predicts it, and your rules decide it on the device."""
    kind = "think"


class Score(Choice):
    """Ordered levels a catalog model answers (prebuilt.Classify), lowest first:
    Score("no", "odd", "alarming"). Your rules read it like any Choice, and the build checks
    the rules on every level of it."""
    kind = "choice"


class Int(Field):
    """A whole number kept in the state (int32 on the device)."""
    kind = "int"

    def __init__(self, default: int = 0):
        if not isinstance(default, int) or isinstance(default, bool):
            raise ValueError("Int default must be a whole number")
        self.default = default

    def check(self, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{self.name} must be a whole number, got {value!r}")
        if not INT_MIN <= value <= INT_MAX:
            raise ValueError(f"{self.name} = {value} does not fit in 32 bits")
        return value


class AgentData:
    """Bookkeeping for the prebuilt nodes (the C struct has the same fields)."""

    def __init__(self):
        self.reset()
        self.answer = None          # None, True or False: set when resuming a paused run

    def reset(self):
        self.want = 0
        self.facts = 0
        self.banned = 0
        self.plan: list[int] = []
        self.src = "none"            # none | lm | planner
        self.lm_conf = 0.0
        self.verdict = "fail"        # ok | needs_approval | fail
        self.why = ""
        self.approved = False
        self.acted_ok = False


class State:
    """Base class for your agent's state."""
    _fields: dict[str, Field] = {}

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        fields = {}
        for base in reversed(cls.__mro__[1:]):
            fields.update(getattr(base, "_fields", {}))
        for name, value in list(vars(cls).items()):
            if isinstance(value, Field):
                fields[name] = value
        cls._fields = fields

    def __init__(self, **values):
        object.__setattr__(self, "_values", {})
        object.__setattr__(self, "_agent", AgentData())
        object.__setattr__(self, "_run", None)
        object.__setattr__(self, "_models", {})        # catalog models' memory, by node: kept between runs
        object.__setattr__(self, "_net", None)         # the network endpoint (tiny_langgraph.net), if any
        for name, f in self._fields.items():
            self._values[name] = f.default
        for name, v in values.items():
            setattr(self, name, v)

    def __getattribute__(self, name):
        if not name.startswith("_"):
            values = object.__getattribute__(self, "_values")
            if name in values:
                return values[name]
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        f = self._fields.get(name)
        if f is None:
            raise AttributeError(f"{type(self).__name__} has no field {name!r} "
                                 f"(fields: {', '.join(self._fields)})")
        self._values[name] = f.check(value)

    # ---- helpers ----
    @classmethod
    def inputs(cls) -> list[Choice]:
        return [f for f in cls._fields.values() if f.kind == "choice"]

    @classmethod
    def thinks(cls) -> list[Think]:
        return [f for f in cls._fields.values() if f.kind == "think"]

    @classmethod
    def ints(cls) -> list[Int]:
        return [f for f in cls._fields.values() if f.kind == "int"]

    def values(self) -> dict:
        return dict(self._values)

    def copy(self):
        import copy
        new = type(self)()
        new._values.update(self._values)
        new._models.update(copy.deepcopy(self._models))
        return new

    @property
    def waiting(self) -> str | None:
        """The question a paused run is waiting on, or None."""
        r = self._run
        return r.question if r is not None and r.status == "interrupted" else None

    @property
    def plan(self) -> list[str]:
        """Names of the tools the last run decided on."""
        names = getattr(self._run, "tool_names", None) or []
        return [names[a] for a in self._agent.plan]

    def __repr__(self):
        inner = ", ".join(f"{k}={v!r}" for k, v in self._values.items())
        return f"{type(self).__name__}({inner})"
