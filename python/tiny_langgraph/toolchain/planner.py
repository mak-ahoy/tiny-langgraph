"""
Python twin of core/tlg_plan.c. Must give identical results: the dataset is
labeled here and the device verifies with the C version, so any difference
would teach the model plans the device rejects. tests/test_toolchain.py checks
the two agree on every situation of the door_guard example.
"""
from __future__ import annotations

from dataclasses import dataclass

from .schema import Schema

INF = 0x7FFF


@dataclass
class Domain:
    names: list[str]
    needs: list[int]        # fact bits
    after: list[int]        # outcome bits
    achieves: list[int]
    cost: list[int]
    approval: list[bool]
    weights: list[int]
    only_if_wanted: int
    budget: int
    fact_index: dict[str, int]


def domain_of(schema: Schema) -> Domain:
    fidx = {f: i for i, f in enumerate(schema.facts)}
    return Domain(
        names=[a.name for a in schema.actions],
        needs=[sum(1 << fidx[f] for f in a.needs) for a in schema.actions],
        after=[schema.outcome_mask(a.after) for a in schema.actions],
        achieves=[schema.outcome_mask(a.achieves) for a in schema.actions],
        cost=[a.cost for a in schema.actions],
        approval=[a.approval for a in schema.actions],
        weights=[o.weight for o in schema.outcomes],
        only_if_wanted=sum(1 << i for i, o in enumerate(schema.outcomes) if o.only_if_wanted),
        budget=schema.budget,
        fact_index=fidx,
    )


def fact_mask(d: Domain, facts: dict[str, bool]) -> int:
    return sum(1 << d.fact_index[f] for f, v in facts.items() if v)


def score(d: Domain, props: int, want: int) -> int:
    hit = props & want
    return sum(w for i, w in enumerate(d.weights) if hit & (1 << i))


SMALL_OUTCOMES = 8         # up to this many outcomes: the full search (tlg_plan.c)
STATE_BYTES = 22           # TLG_PLAN_STATE_BYTES: workspace per state of the pruned search
DEFAULT_STATES = 4096      # pruned-search states when no board says otherwise (boards.py)


def plan(d: Domain, facts: int, want: int, banned: int = 0, states: int = DEFAULT_STATES):
    """Cheapest action set reaching the best reachable part of `want`.
    Returns (actions in execution order, achieved mask, cost, score)."""
    return plan_ex(d, facts, want, banned, states)[:4]


def plan_ex(d: Domain, facts: int, want: int, banned: int = 0, states: int = DEFAULT_STATES):
    """Like plan(), plus whether it's exact: with more than 8 outcomes the search keeps at most
    `states` states (the board's workspace) and falls back to greedy when a situation needs more."""
    if len(d.weights) > SMALL_OUTCOMES:
        cap = min(states, 65534)
        return _pruned(d, facts, want, banned, cap) if cap else _greedy(d, facts, want, banned)
    return (*_full(d, facts, want, banned), True)


def _greedy(d: Domain, facts: int, want: int, banned: int):
    """Again and again the usable action with the most new wanted weight per cost, compared as
    gain_a * cost_b > gain_b * cost_a (no rounding); ties: the lower index. Like greedy() in C."""
    forbid = d.only_if_wanted & ~want
    props = used = cost = 0
    while True:
        best, best_gain = -1, 0
        for a in range(len(d.names)):
            if (banned | used) & (1 << a) or (facts & d.needs[a]) != d.needs[a]:
                continue
            if (props & d.after[a]) != d.after[a] or d.achieves[a] & forbid or cost + d.cost[a] > d.budget:
                continue
            gain = score(d, d.achieves[a] & ~props, want)
            if gain <= 0:
                continue
            if best < 0 or gain * d.cost[best] > best_gain * d.cost[a]:
                best, best_gain = a, gain
        if best < 0:
            break
        used |= 1 << best
        props |= d.achieves[best]
        cost += d.cost[best]
    return order(d, [a for a in range(len(d.names)) if used >> a & 1]), props, cost, score(d, props, want), False


def _pruned(d: Domain, facts: int, want: int, banned: int, cap: int):
    """Dijkstra over the reachable sets of the outcomes that matter, settling in (cost, mask)
    order, at most `cap` states and 2*cap heap entries: like pruned() in tlg_plan.c."""
    import heapq
    forbid = d.only_if_wanted & ~want
    relevant = want
    for a in d.after:
        relevant |= a
    mask, dist, prev, via, done, index = [0], [0], [None], [0], [False], {0: 0}
    heap, heap_cap = [(0, 0, 0)], 2 * cap
    while heap:
        du, _, u = heapq.heappop(heap)
        if done[u] or du != dist[u]:
            continue
        done[u] = True
        for a in range(len(d.names)):
            if banned & (1 << a) or (facts & d.needs[a]) != d.needs[a]:
                continue
            if (mask[u] & d.after[a]) != d.after[a] or d.achieves[a] & forbid:
                continue
            v = (mask[u] | d.achieves[a]) & relevant
            if v == mask[u]:
                continue
            nd = dist[u] + d.cost[a]
            if nd > d.budget:
                continue
            s = index.get(v)
            if s is None:
                if len(mask) == cap:
                    return _greedy(d, facts, want, banned)          # doesn't fit
                s = len(mask)
                index[v] = s
                mask.append(v), dist.append(INF), prev.append(None), via.append(0), done.append(False)
            elif nd >= dist[s]:
                continue
            dist[s], prev[s], via[s] = nd, u, a
            if len(heap) == heap_cap:
                return _greedy(d, facts, want, banned)
            heapq.heappush(heap, (nd, v, s))
    best = 0
    for s in range(1, len(mask)):
        if dist[s] == INF:
            continue
        sc, bs = score(d, mask[s], want), score(d, mask[best], want)
        if sc > bs or (sc == bs and (dist[s] < dist[best] or (dist[s] == dist[best] and mask[s] < mask[best]))):
            best = s
    used, s = set(), best
    while s != 0:
        used.add(via[s])
        s = prev[s]
    achieved = 0
    for a in used:
        achieved |= d.achieves[a]
    return order(d, used), achieved, dist[best], score(d, achieved, want), True


def _full(d: Domain, facts: int, want: int, banned: int = 0):
    """The full search over all 2^outcomes states (up to 8 outcomes), as before."""
    n_states = 1 << len(d.weights)
    forbid = d.only_if_wanted & ~want
    dist = [INF] * n_states
    prev = [0] * n_states
    via = [0] * n_states
    done = [False] * n_states
    dist[0] = 0

    while True:
        u = -1
        for i in range(n_states):
            if not done[i] and dist[i] < INF and (u < 0 or dist[i] < dist[u]):
                u = i
        if u < 0:
            break
        done[u] = True
        for a in range(len(d.names)):
            if banned & (1 << a):
                continue
            if (facts & d.needs[a]) != d.needs[a]:
                continue
            if (u & d.after[a]) != d.after[a]:
                continue
            v = u | d.achieves[a]
            if v == u or (v & forbid):
                continue
            nd = dist[u] + d.cost[a]
            if nd <= d.budget and nd < dist[v]:
                dist[v], prev[v], via[v] = nd, u, a

    best = 0
    for v in range(n_states):
        if dist[v] == INF:
            continue
        sc, bs = score(d, v, want), score(d, best, want)
        if sc > bs or (sc == bs and dist[v] < dist[best]):
            best = v

    used = set()
    v = best
    while v != 0:
        used.add(via[v])
        v = prev[v]
    return order(d, used), best, dist[best], score(d, best, want)


def order(d: Domain, used) -> list[int]:
    """Canonical execution order: lowest action index whose 'after' is satisfied."""
    remaining = sorted(used)
    props, out = 0, []
    while remaining:
        for a in remaining:
            if (props & d.after[a]) == d.after[a]:
                out.append(a)
                props |= d.achieves[a]
                remaining.remove(a)
                break
        else:
            raise RuntimeError("no valid order (cyclic 'after'?)")
    return out
