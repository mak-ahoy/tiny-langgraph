"""
The parts that make a graph an agent: goals, tools, rules.

    goals = Goals(recorded=1, fed=Goal(5, only_if_wanted=True))

    def can_feed(s) -> bool:                 # a condition: checked on the device too
        return s.meal != "not_due" and s.fed_recently == "no"

    @tool(cost=3, achieves=goals.fed, needs=can_feed)
    def dispense(s):                         # what the tool does
        feeder.turn(90)

    @rules                                   # the model learns these; the device checks it against them
    def assess(s):
        s.wellbeing = "ok"
        want(goals.recorded)
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass

_context = contextvars.ContextVar("tiny_langgraph_context", default=None)


class AgentError(Exception):
    pass


# --------------------------------------------------------------------------
# Goals
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Goal:
    """Something a plan can achieve. A higher weight matters more when not everything
    is possible. only_if_wanted: a plan doing this when nobody asked is rejected."""
    weight: int = 1
    only_if_wanted: bool = False
    name: str = ""
    index: int = -1

    def __repr__(self):
        return f"goals.{self.name}" if self.name else f"Goal({self.weight})"


class Goals:
    """All the goals of an agent, in one place:  goals = Goals(logged=1, fed=Goal(5, only_if_wanted=True))"""

    def __init__(self, **goals):
        if not goals:
            raise AgentError("Goals() needs at least one goal")
        if len(goals) > 32:
            raise AgentError("at most 32 goals (up to 8 the planner searches every combination; more: "
                             "a pruned search in the board's memory, greedy if it doesn't fit)")
        self._list = []
        for i, (name, g) in enumerate(goals.items()):
            if isinstance(g, int) and not isinstance(g, bool):
                g = Goal(g)
            if not isinstance(g, Goal):
                raise AgentError(f"goal {name}: use a weight or Goal(weight, only_if_wanted=True)")
            if not 0 <= g.weight <= 255:
                raise AgentError(f"goal {name}: weight must be 0..255")
            g = Goal(g.weight, g.only_if_wanted, name, i)
            self._list.append(g)
            setattr(self, name, g)

    def __iter__(self):
        return iter(self._list)

    def __len__(self):
        return len(self._list)

    def mask(self, goals) -> int:
        return sum(1 << g.index for g in goals)

    def names(self, mask: int) -> str:
        names = [g.name for g in self._list if mask & (1 << g.index)]
        return ",".join(names) if names else "nothing"


def _goal_list(v, what) -> list[Goal]:
    if v is None:
        return []
    items = list(v) if isinstance(v, (list, tuple)) else [v]
    for g in items:
        if not isinstance(g, Goal) or not g.name:
            raise AgentError(f"{what}: use goals from your Goals(...), like goals.fed (got {g!r})")
    return items


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
class Tool:
    def __init__(self, fn, cost, achieves, needs, after, approval):
        self.fn = fn
        self.name = fn.__name__
        self.cost = cost
        self.achieves = _goal_list(achieves, f"tool {self.name}: achieves")
        self.after = _goal_list(after, f"tool {self.name}: after")
        self.needs = list(needs) if isinstance(needs, (list, tuple)) else ([needs] if needs else [])
        self.approval = approval
        if not self.achieves:
            raise AgentError(f"tool {self.name} must achieve a goal: @tool(achieves=goals.something)")
        if not 1 <= cost <= 1000:
            raise AgentError(f"tool {self.name}: cost must be 1..1000")
        for f in self.needs:
            if not callable(f) or getattr(f, "__name__", "<lambda>") == "<lambda>":
                raise AgentError(f"tool {self.name}: needs= takes named functions like  def can_feed(s): ...")
        self.__doc__ = fn.__doc__
        self.__name__ = self.name

    def __call__(self, s):
        return self.fn(s)

    def __repr__(self):
        return f"<tool {self.name}>"


def tool(cost: int = 1, achieves=None, needs=None, after=None, approval: bool = False):
    """Mark a function as a tool the planner may use.

    cost      how expensive it is (the planner prefers cheap plans)
    achieves  the goal(s) it reaches
    needs     condition function(s) that must be true for it to be allowed
    after     goal(s) that must be reached first
    approval  a human must approve it before it runs
    """
    def wrap(fn):
        return Tool(fn, cost, achieves, needs, after, approval)
    return wrap


def rules(fn):
    """Mark the function that decides the Think fields and what's wanted.
    The model is trained to imitate it, and the device checks the model against it."""
    fn._tlg_rules = True
    return fn


def want(*goals):
    """Inside your @rules function: these goals should be achieved."""
    ctx = _context.get()
    if ctx is None or not ctx.in_rules:
        raise AgentError("want() can only be called inside the @rules function")
    for g in _goal_list(list(goals), "want()"):
        ctx.state._agent.want |= 1 << g.index


def note(text: str, *values):
    """Add a line to the trace (shown in the viewer and the log)."""
    ctx = _context.get()
    if ctx is not None:
        parts = [text] + [str(v) for v in values]
        ctx.say(" ".join(parts))


def interrupt(question: str):
    """Pause the run after this node; it runs again when resumed."""
    ctx = _context.get()
    if ctx is None:
        raise AgentError("interrupt() can only be called inside a node")
    ctx.run.interrupt_requested = True
    ctx.run.question = question[:47]
