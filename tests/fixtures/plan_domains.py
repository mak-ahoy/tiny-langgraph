"""
Synthetic agent domains of any size and an independent exact planner, used by
tests/test_planner_big.py to check the board's planner (core/tlg_plan.c) on big agents.

A domain is what a tiny-langgraph agent compiles to:
  inputs   N Choice fields (2-4 values each)                    what it observes
  facts    F conditions on 1-2 inputs                           needs= of the tools
  goals    G goals with weights, some only_if_wanted            what it can achieve
  tools    T tools: cost, needs facts, after goals, achieve goals
  rules    per goal: "want it when these inputs have these values"    the @rules

For one situation (values of the inputs) the rules give `want` and the facts give `facts`;
a planner then picks tools. Planners here:
  exact_full    Dijkstra over all 2^G achieved-sets, like core/tlg_plan.c (memory 2^G)
  exact_pruned  the same answer, visiting only reachable states of the goals that matter
  greedy        repeatedly the tool with the best wanted-weight per cost (linear, not optimal)
and `check` is the hard safety verifier (linear in the plan): needs, order, nothing unwanted.
"""
from __future__ import annotations

import heapq
import random
from dataclasses import dataclass

BYTES_PER_STATE = 5          # tlg_plan.c: dist (2) + prev (1) + via (1) + done (1)


@dataclass
class Domain:
    name: str
    values: list[int]                  # values per input
    facts: list[list[tuple[int, int]]] # each fact: true if ANY (input, value) matches
    weights: list[int]                 # per goal
    only_if_wanted: int                # goal bits
    rules: list[list[tuple[int, int]]] # per goal: wanted if ALL (input, value) match ([] = always)
    cost: list[int]                    # per tool
    needs: list[int]                   # fact bits per tool
    after: list[int]                   # goal bits per tool
    achieves: list[int]                # goal bits per tool

    @property
    def n_inputs(self):
        return len(self.values)

    @property
    def n_goals(self):
        return len(self.weights)

    @property
    def n_tools(self):
        return len(self.cost)

    def situations_count(self) -> int:
        n = 1
        for v in self.values:
            n *= v
        return n

    def want_facts(self, sit) -> tuple[int, int]:
        """What the rules want and which facts hold, in this situation (cheap: linear)."""
        want = sum(1 << g for g, rule in enumerate(self.rules) if all(sit[i] == v for i, v in rule))
        facts = sum(1 << f for f, fact in enumerate(self.facts) if any(sit[i] == v for i, v in fact))
        return want, facts

    def score(self, achieved: int, want: int) -> int:
        hit = achieved & want
        return sum(w for g, w in enumerate(self.weights) if hit >> g & 1)


def generate(n_inputs: int, n_goals: int, n_tools: int, seed: int = 0, n_facts: int | None = None,
             dense: bool = False, always: float = 0.0) -> Domain:
    """A random but agent-like domain: every goal has 1-3 tools that achieve it (with different
    costs and conditions, so planning is a real trade-off), some tools must come after others,
    and the rules want each goal in a few situations.

    dense=True: the hard case for planners. Many goals are wanted at once (short rules), more
    tools achieve several goals, and more tools must come after another goal.
    always: the share of goals that are always wanted (no rule): the stress test, since the
    pruned planner's memory grows with how many goals are wanted at the same time."""
    rng = random.Random(f"scale-{n_inputs}-{n_goals}-{n_tools}-{seed}-{'dense' if dense else 'sparse'}-{always}")
    p_multi, p_after, rule_len = (0.5, 0.4, (1, 1, 2)) if dense else (0.25, 0.2, (1, 2, 2, 3))
    values = [rng.choice((2, 2, 3, 3, 4)) for _ in range(n_inputs)]
    n_facts = n_facts or max(4, n_tools // 2)

    def literal():
        i = rng.randrange(n_inputs)
        return (i, rng.randrange(values[i]))

    facts = [[literal() for _ in range(rng.choice((1, 1, 2)))] for _ in range(n_facts)]
    weights = [rng.randint(1, 9) for _ in range(n_goals)]
    only = sum(1 << g for g in range(n_goals) if g > 0 and rng.random() < 0.5)
    rules = [[]] + [[] if rng.random() < always else [literal() for _ in range(rng.choice(rule_len))]
                    for _ in range(n_goals - 1)]
    cost, needs, after, achieves = [], [], [], []
    owners = list(range(n_goals)) + [rng.randrange(n_goals) for _ in range(max(0, n_tools - n_goals))]
    rng.shuffle(owners)
    for t in range(n_tools):
        g = owners[t]
        ach = 1 << g
        if rng.random() < p_multi:                    # some tools achieve a second goal too
            ach |= 1 << rng.randrange(n_goals)
        cost.append(rng.randint(1, 5))
        needs.append(sum(1 << rng.randrange(n_facts) for _ in range(rng.choice((0, 1, 1, 2)))))
        aft = 0
        if rng.random() < p_after and n_goals > 1:    # must come after another goal
            other = rng.randrange(n_goals)
            if other != g:
                aft = 1 << other
        after.append(aft)
        achieves.append(ach)
    tag = ("_dense" if dense else "") + (f"_all{int(always * 100)}" if always else "")
    return Domain(f"N{n_inputs}_G{n_goals}_T{n_tools}{tag}_s{seed}", values, facts, weights, only, rules,
                  cost, needs, after, achieves)


def sample(dom: Domain, n: int, seed: int = 0) -> list[list[int]]:
    rng = random.Random(f"sit-{dom.name}-{seed}")
    return [[rng.randrange(v) for v in dom.values] for _ in range(n)]


# ---------------------------------------------------------------- the hard verifier
def check(dom: Domain, facts: int, want: int, plan: list[int]) -> tuple[bool, str]:
    """Safety only (no "is it the best?"): each tool's conditions hold, it comes after what it
    needs, and the plan achieves nothing unwanted that's only_if_wanted. One pass: linear."""
    forbid = dom.only_if_wanted & ~want
    done = 0
    for t in plan:
        if not 0 <= t < dom.n_tools:
            return False, "unknown tool"
        if facts & dom.needs[t] != dom.needs[t]:
            return False, "condition not met"
        if done & dom.after[t] != dom.after[t]:
            return False, "out of order"
        if dom.achieves[t] & forbid:
            return False, "does something unwanted"
        if plan.count(t) > 1:
            return False, "repeats a tool"
        done |= dom.achieves[t]
    return True, "ok"


def achieved(dom: Domain, plan: list[int]) -> int:
    a = 0
    for t in plan:
        a |= dom.achieves[t]
    return a


def plan_cost(dom: Domain, plan: list[int]) -> int:
    return sum(dom.cost[t] for t in plan)


# ---------------------------------------------------------------- planners
def _dijkstra(dom: Domain, facts: int, want: int, relevant: int, limit: int | None = None):
    """Cheapest way to each reachable set of relevant goals. Returns (dist, prev, states kept),
    or (None, None, states) once it would keep more than `limit` states."""
    forbid = dom.only_if_wanted & ~want
    usable = [t for t in range(dom.n_tools)
              if facts & dom.needs[t] == dom.needs[t] and not dom.achieves[t] & forbid]
    dist, prev = {0: 0}, {0: None}
    heap, done = [(0, 0)], set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        for t in usable:
            if u & dom.after[t] != dom.after[t]:
                continue
            v = (u | dom.achieves[t]) & relevant
            if v == u:
                continue
            nd = d + dom.cost[t]
            if nd < dist.get(v, 1 << 30):
                dist[v], prev[v] = nd, (u, t)
                heapq.heappush(heap, (nd, v))
                if limit is not None and len(dist) > limit:
                    return None, None, len(dist)
    return dist, prev, len(dist)


def _best(dom, dist, prev, want):
    best = max(dist, key=lambda s: (dom.score(s, want), -dist[s]))
    plan, s = [], best
    while prev[s] is not None:
        s, t = prev[s]
        plan.append(t)
    return plan[::-1]


def exact_pruned(dom: Domain, facts: int, want: int, limit: int | None = None) -> tuple[list[int] | None, int]:
    """Optimal plan; only goals that are wanted or that some tool must come after are tracked.
    Returns (plan, states it had to keep: its memory); plan is None past `limit` states."""
    relevant = want
    for a in dom.after:
        relevant |= a
    dist, prev, states = _dijkstra(dom, facts, want, relevant, limit)
    if dist is None:
        return None, states
    return _best(dom, dist, prev, want), states


def exact_full_states(dom: Domain) -> int:
    """States tlg_plan.c keeps whatever the situation: every subset of goals."""
    return 1 << dom.n_goals


def greedy(dom: Domain, facts: int, want: int) -> list[int]:
    """Repeatedly take the usable tool with the most new wanted weight per cost. Linear work,
    no memory, but it can miss a cheaper combination or a needed first step."""
    forbid = dom.only_if_wanted & ~want
    done, plan, used = 0, [], set()
    while True:
        best, best_gain = None, 0.0
        for t in range(dom.n_tools):
            if t in used or facts & dom.needs[t] != dom.needs[t] or dom.achieves[t] & forbid:
                continue
            if done & dom.after[t] != dom.after[t]:
                continue
            gain = dom.score(dom.achieves[t] & ~done, want) / dom.cost[t]
            if gain > best_gain:
                best, best_gain = t, gain
        if best is None:
            return plan
        plan.append(best)
        used.add(best)
        done |= dom.achieves[best]
