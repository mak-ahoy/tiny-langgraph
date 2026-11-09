#include "tlg_plan.h"

#include <stdio.h>
#include <string.h>

#define INF 0x7FFF
#define MAX_STATES (1u << TLG_PLAN_SMALL_OUTCOMES)   /* the full search: 256 states, on the stack */
#define NONE 0xFFFFu

/* The pruned search's workspace (tlg_plan_workspace): per state a mask, its cost, where it came
 * from and by which action, whether it's settled; a hash from mask to state; a heap of
 * (cost, state) to visit next. No malloc: the caller hands over the memory. */
static struct {
    uint32_t *mask;
    uint16_t *dist, *prev, *hash;
    uint8_t  *via, *done;
    uint16_t *heap;          /* pairs: cost, state */
    uint32_t  cap, hash_size, heap_cap;
} ws;

void tlg_plan_workspace(void *mem, size_t bytes)
{
    uint32_t n = mem ? (uint32_t)(bytes / TLG_PLAN_STATE_BYTES) : 0;
    if (n > 65534) n = 65534;                    /* state numbers are 16-bit; 0xFFFF = none */
    ws.cap = n;
    if (!n) return;
    uint32_t h = 1;
    while (h * 2 <= 2 * n) h *= 2;               /* the largest power of two <= 2n (> n) */
    uint8_t *p = (uint8_t *)mem;
    ws.mask = (uint32_t *)p;  p += 4u * n;
    ws.dist = (uint16_t *)p;  p += 2u * n;
    ws.prev = (uint16_t *)p;  p += 2u * n;
    ws.via  = p;              p += n;
    ws.done = p;              p += n;
    ws.hash = (uint16_t *)p;  p += 4u * n;       /* room for 2n slots; h of them are used */
    ws.heap = (uint16_t *)p;                     /* 2n pairs */
    ws.hash_size = h;
    ws.heap_cap = 2 * n;
}

uint32_t tlg_plan_capacity(void) { return ws.cap; }

int tlg_plan_score(const tlg_domain_t *d, uint32_t achieved, uint32_t want)
{
    uint32_t hit = achieved & want;
    int s = 0;
    for (int i = 0; i < d->n_outcomes; i++)
        if (hit & (1u << i)) s += d->outcome_weights[i];
    return s;
}

/* Canonical order: repeatedly take the lowest-index action whose 'after' holds. */
static int order(const tlg_domain_t *d, uint32_t used, uint8_t *out)
{
    uint32_t props = 0;
    int len = 0;
    while (used) {
        int picked = -1;
        for (int a = 0; a < d->n_actions; a++) {
            if (!(used & (1u << a))) continue;
            if ((props & d->actions[a].after) == d->actions[a].after) { picked = a; break; }
        }
        if (picked < 0) return -1;
        out[len++] = (uint8_t)picked;
        props |= d->actions[picked].achieves;
        used &= ~(1u << picked);
    }
    return len;
}

static int finish(const tlg_domain_t *d, uint32_t used, uint32_t achieved, unsigned cost, uint32_t want,
                  bool exact, tlg_plan_t *out)
{
    out->len = order(d, used, out->actions);
    if (out->len < 0) return -1;
    out->achieved = achieved;
    out->cost = (uint16_t)cost;
    out->score = tlg_plan_score(d, achieved, want);
    out->exact = exact;
    return 0;
}

/* Greedy: again and again, the usable action with the most new wanted weight per cost
 * (compared as gain_a * cost_b > gain_b * cost_a, so no rounding; ties: the lower index). */
static int greedy(const tlg_domain_t *d, uint32_t facts, uint32_t want, uint32_t banned, tlg_plan_t *out)
{
    const uint32_t forbid = d->only_if_wanted & ~want;
    uint32_t props = 0, used = 0;
    unsigned cost = 0;
    for (;;) {
        int best = -1, best_gain = 0;
        for (int a = 0; a < d->n_actions; a++) {
            const tlg_action_t *ac = &d->actions[a];
            if ((banned | used) & (1u << a)) continue;
            if ((facts & ac->needs) != ac->needs) continue;
            if ((props & ac->after) != ac->after) continue;
            if (ac->achieves & forbid) continue;
            if (cost + ac->cost > d->budget) continue;
            int gain = tlg_plan_score(d, ac->achieves & ~props, want);
            if (gain <= 0) continue;
            if (best < 0 || (long)gain * d->actions[best].cost > (long)best_gain * ac->cost) {
                best = a;
                best_gain = gain;
            }
        }
        if (best < 0) break;
        used |= 1u << best;
        props |= d->actions[best].achieves;
        cost += d->actions[best].cost;
    }
    return finish(d, used, props, cost, want, false, out);
}

static bool before(uint32_t i, uint32_t j)          /* heap order: lower cost, then lower mask */
{
    uint16_t di = ws.heap[2 * i], dj = ws.heap[2 * j];
    return di < dj || (di == dj && ws.mask[ws.heap[2 * i + 1]] < ws.mask[ws.heap[2 * j + 1]]);
}

static void heap_swap(uint32_t i, uint32_t j)
{
    uint16_t a = ws.heap[2 * i], b = ws.heap[2 * i + 1];
    ws.heap[2 * i] = ws.heap[2 * j]; ws.heap[2 * i + 1] = ws.heap[2 * j + 1];
    ws.heap[2 * j] = a;              ws.heap[2 * j + 1] = b;
}

/* The pruned search: Dijkstra over the reachable sets of the outcomes that matter. Settles
 * states in (cost, mask) order and keeps the first cheapest way to each, like the full search. */
static int pruned(const tlg_domain_t *d, uint32_t facts, uint32_t want, uint32_t banned, tlg_plan_t *out)
{
    const uint32_t forbid = d->only_if_wanted & ~want;
    uint32_t relevant = want;
    for (int a = 0; a < d->n_actions; a++) relevant |= d->actions[a].after;
    int shift = 32;
    for (uint32_t h = ws.hash_size; h > 1; h >>= 1) shift--;
    for (uint32_t i = 0; i < ws.hash_size; i++) ws.hash[i] = NONE;

    uint32_t count = 1, heap_len = 1;
    ws.mask[0] = 0; ws.dist[0] = 0; ws.prev[0] = NONE; ws.via[0] = 0; ws.done[0] = 0;
    ws.hash[0] = 0;                                  /* mask 0 hashes to slot 0 */
    ws.heap[0] = 0; ws.heap[1] = 0;

    while (heap_len) {
        uint32_t u = ws.heap[1];
        uint16_t du = ws.heap[0];
        heap_swap(0, --heap_len);                   /* pop the minimum, then sift down */
        for (uint32_t i = 0;;) {
            uint32_t l = 2 * i + 1, r = l + 1, m = i;
            if (l < heap_len && before(l, m)) m = l;
            if (r < heap_len && before(r, m)) m = r;
            if (m == i) break;
            heap_swap(i, m);
            i = m;
        }
        if (ws.done[u] || du != ws.dist[u]) continue;   /* a stale entry */
        ws.done[u] = 1;
        for (int a = 0; a < d->n_actions; a++) {
            const tlg_action_t *ac = &d->actions[a];
            if (banned & (1u << a)) continue;
            if ((facts & ac->needs) != ac->needs) continue;
            if ((ws.mask[u] & ac->after) != ac->after) continue;
            if (ac->achieves & forbid) continue;
            uint32_t v = (ws.mask[u] | ac->achieves) & relevant;
            if (v == ws.mask[u]) continue;
            uint32_t nd = (uint32_t)ws.dist[u] + ac->cost;
            if (nd > d->budget) continue;
            uint32_t slot = (v * 2654435761u) >> shift;
            while (ws.hash[slot] != NONE && ws.mask[ws.hash[slot]] != v) slot = (slot + 1) & (ws.hash_size - 1);
            uint32_t s = ws.hash[slot];
            if (s == NONE) {
                if (count == ws.cap) return greedy(d, facts, want, banned, out);   /* doesn't fit */
                s = count++;
                ws.hash[slot] = (uint16_t)s;
                ws.mask[s] = v; ws.dist[s] = INF; ws.done[s] = 0;
            } else if (nd >= ws.dist[s]) {
                continue;
            }
            ws.dist[s] = (uint16_t)nd; ws.prev[s] = (uint16_t)u; ws.via[s] = (uint8_t)a;
            if (heap_len == ws.heap_cap) return greedy(d, facts, want, banned, out);
            ws.heap[2 * heap_len] = (uint16_t)nd; ws.heap[2 * heap_len + 1] = (uint16_t)s;
            for (uint32_t i = heap_len++; i > 0 && before(i, (i - 1) / 2); i = (i - 1) / 2) heap_swap(i, (i - 1) / 2);
        }
    }

    uint32_t best = 0;                               /* best score, then lowest cost, then lowest mask */
    for (uint32_t s = 1; s < count; s++) {
        if (ws.dist[s] == INF) continue;
        int sc = tlg_plan_score(d, ws.mask[s], want), bs = tlg_plan_score(d, ws.mask[best], want);
        if (sc > bs || (sc == bs && (ws.dist[s] < ws.dist[best] ||
                                     (ws.dist[s] == ws.dist[best] && ws.mask[s] < ws.mask[best]))))
            best = s;
    }
    uint32_t used = 0, achieved = 0;
    for (uint32_t s = best; s != 0; s = ws.prev[s]) used |= 1u << ws.via[s];
    for (int a = 0; a < d->n_actions; a++)
        if (used & (1u << a)) achieved |= d->actions[a].achieves;
    return finish(d, used, achieved, ws.dist[best], want, true, out);
}

/* Must match python/tiny_langgraph/toolchain/planner.py step for step (including tie-breaking). */
int tlg_plan(const tlg_domain_t *d, uint32_t facts, uint32_t want, uint32_t banned, tlg_plan_t *out)
{
    if (d->n_outcomes > TLG_PLAN_MAX_OUTCOMES || d->n_actions > TLG_PLAN_MAX_ACTIONS) return -1;
    if (d->n_outcomes > TLG_PLAN_SMALL_OUTCOMES)
        return ws.cap ? pruned(d, facts, want, banned, out) : greedy(d, facts, want, banned, out);

    const uint32_t n_states = 1u << d->n_outcomes;
    const uint32_t forbid = d->only_if_wanted & ~want;
    uint16_t dist[MAX_STATES];
    uint8_t  prev[MAX_STATES], via[MAX_STATES];
    bool     done[MAX_STATES];

    for (uint32_t i = 0; i < n_states; i++) { dist[i] = INF; done[i] = false; }
    dist[0] = 0;

    for (;;) {
        int u = -1;
        for (uint32_t i = 0; i < n_states; i++)
            if (!done[i] && dist[i] < INF && (u < 0 || dist[i] < dist[u])) u = (int)i;
        if (u < 0) break;
        done[u] = true;

        for (int a = 0; a < d->n_actions; a++) {
            const tlg_action_t *ac = &d->actions[a];
            if (banned & (1u << a)) continue;
            if ((facts & ac->needs) != ac->needs) continue;
            if (((uint32_t)u & ac->after) != ac->after) continue;
            uint32_t v = (uint32_t)u | ac->achieves;
            if (v == (uint32_t)u || (v & forbid)) continue;
            uint32_t nd = (uint32_t)dist[u] + ac->cost;
            if (nd <= d->budget && nd < dist[v]) {
                dist[v] = (uint16_t)nd;
                prev[v] = (uint8_t)u;
                via[v] = (uint8_t)a;
            }
        }
    }

    uint32_t best = 0;
    for (uint32_t v = 0; v < n_states; v++) {
        if (dist[v] == INF) continue;
        int sc = tlg_plan_score(d, v, want), bs = tlg_plan_score(d, best, want);
        if (sc > bs || (sc == bs && dist[v] < dist[best])) best = v;
    }

    uint32_t used = 0;
    for (uint32_t v = best; v != 0; v = prev[v]) used |= 1u << via[v];
    return finish(d, used, best, dist[best], want, true, out);
}

static const char *first_name(const char *const *names, int n, uint32_t mask)
{
    for (int i = 0; i < n; i++)
        if (mask & (1u << i)) return names ? names[i] : "?";
    return "?";
}

static tlg_verdict_t fail(char *why, size_t n, const char *fmt, const char *a, const char *b)
{
    if (why && n) snprintf(why, n, fmt, a, b);
    return TLG_VERIFY_FAIL;
}

tlg_verdict_t tlg_verify(const tlg_domain_t *d, uint32_t facts, uint32_t want, uint32_t banned,
                         const uint8_t *plan, int len, bool approved, char *why, size_t n)
{
    uint32_t props = 0;
    unsigned cost = 0;
    bool needs_approval = false;
    if (why && n) why[0] = '\0';

    for (int i = 0; i < len; i++) {
        if (plan[i] >= d->n_actions) return fail(why, n, "unknown action%s%s", "", "");
        const tlg_action_t *a = &d->actions[plan[i]];
        if (banned & (1u << plan[i]))
            return fail(why, n, "%s is not allowed now%s", a->name, "");
        if ((facts & a->needs) != a->needs)
            return fail(why, n, "%s needs %s", a->name,
                        first_name(d->fact_names, d->n_facts, a->needs & ~facts));
        if ((props & a->after) != a->after)
            return fail(why, n, "%s must come after %s", a->name,
                        first_name(d->outcome_names, d->n_outcomes, a->after & ~props));
        props |= a->achieves;
        cost += a->cost;
        needs_approval |= a->approval;
    }

    uint32_t unwanted = props & d->only_if_wanted & ~want;
    if (unwanted)
        return fail(why, n, "does %s, which nobody asked for%s",
                    first_name(d->outcome_names, d->n_outcomes, unwanted), "");
    if (cost > d->budget) {
        if (why && n) snprintf(why, n, "over budget (cost %u > %u)", cost, (unsigned)d->budget);
        return TLG_VERIFY_FAIL;
    }

    tlg_plan_t best;
    if (tlg_plan(d, facts, want, banned, &best) != 0) return fail(why, n, "domain too large%s%s", "", "");
    int score = tlg_plan_score(d, props, want);
    if (score < best.score) {
        char missing[96];
        tlg_outcomes_str(d, best.achieved & want & ~props, missing, sizeof missing);
        return fail(why, n, "incomplete: misses %s%s", missing, "");
    }
    if (score == best.score && cost > best.cost) {      /* keep the better plan: the planner's is cheaper */
        if (why && n) snprintf(why, n, "costs %u; the planner found the same for %u", cost, (unsigned)best.cost);
        return TLG_VERIFY_FAIL;
    }

    if (needs_approval && !approved) {
        if (why && n) snprintf(why, n, "needs human approval");
        return TLG_VERIFY_NEEDS_APPROVAL;
    }
    if (why && n) snprintf(why, n, "ok");
    return TLG_VERIFY_OK;
}

static void join(char *buf, size_t size, const char *sep, const char *const *names,
                 const int *idx, int count)
{
    size_t used = 0;
    buf[0] = '\0';
    for (int i = 0; i < count; i++) {
        int w = snprintf(buf + used, size - used, "%s%s", i ? sep : "", names[idx[i]]);
        if (w < 0 || (size_t)w >= size - used) return;
        used += (size_t)w;
    }
}

void tlg_plan_str(const tlg_domain_t *d, const uint8_t *plan, int len, char *buf, size_t size)
{
    if (!size) return;
    if (len <= 0) { snprintf(buf, size, "(nothing)"); return; }
    const char *names[TLG_PLAN_MAX_ACTIONS];
    int idx[TLG_PLAN_MAX_ACTIONS];
    for (int i = 0; i < d->n_actions && i < TLG_PLAN_MAX_ACTIONS; i++) names[i] = d->actions[i].name;
    int k = 0;
    for (int i = 0; i < len && k < TLG_PLAN_MAX_ACTIONS; i++)
        if (plan[i] < d->n_actions) idx[k++] = plan[i];
    join(buf, size, " -> ", names, idx, k);
}

void tlg_outcomes_str(const tlg_domain_t *d, uint32_t mask, char *buf, size_t size)
{
    if (!size) return;
    int idx[32], k = 0;
    for (int i = 0; i < d->n_outcomes; i++)
        if (mask & (1u << i)) idx[k++] = i;
    if (k == 0) { snprintf(buf, size, "nothing"); return; }
    join(buf, size, ",", d->outcome_names, idx, k);
}
