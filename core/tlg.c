#include "tlg.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define TLG_DEFAULT_MAX_STEPS 64u

static bool in_range(const tlg_graph_t *g, int id)
{
    return id >= 0 && id < g->n_nodes;
}

static bool declares(const tlg_node_t *n, int id)
{
    for (int i = 0; i < n->n_targets; i++)
        if (n->targets[i] == id) return true;
    return false;
}

void tlg_run_init(tlg_run_t *run, const tlg_graph_t *g)
{
    memset(run, 0, sizeof *run);
    run->current = g->start;
    run->status = TLG_READY;
    run->error_node = TLG_END;
}

void tlg_interrupt(tlg_run_t *run, const char *reason)
{
    run->interrupt_requested = true;
    snprintf(run->interrupt_reason, sizeof run->interrupt_reason, "%s", reason ? reason : "");
}

static tlg_status_t fail(tlg_run_t *run, tlg_status_t st, int node, int value)
{
    run->status = st;
    run->error_node = node;
    run->error_value = value;
    return st;
}

tlg_status_t tlg_invoke(const tlg_graph_t *g, tlg_run_t *run, void *state)
{
    /* finished or failed runs stay that way until tlg_run_init */
    if (run->status != TLG_READY && run->status != TLG_INTERRUPTED) return run->status;

    const uint32_t max = g->max_steps ? g->max_steps : TLG_DEFAULT_MAX_STEPS;
    run->status = TLG_READY;
    run->interrupt_requested = false;
    run->interrupt_reason[0] = '\0';

    for (;;) {
        const int id = run->current;
        if (!in_range(g, id))   return fail(run, TLG_ERR_BAD_NODE, id, id);
        if (run->step >= max)   return fail(run, TLG_STEP_LIMIT, id, (int)run->step);

        const tlg_node_t *n = &g->nodes[id];
        if (n->run) n->run(state, run);
        run->step++;

        if (run->interrupt_requested) {
            run->status = TLG_INTERRUPTED;      /* current stays on this node */
            if (g->on_step) g->on_step(run, id, id, g->user);
            return TLG_INTERRUPTED;
        }

        int next;
        if (n->route) {
            next = n->route(state);
            if (!declares(n, next)) return fail(run, TLG_ERR_BAD_ROUTE, id, next);
        } else if (n->n_targets == 1 && n->targets) {
            next = n->targets[0];
        } else {
            return fail(run, TLG_ERR_NO_EDGE, id, 0);
        }

        if (g->on_step) g->on_step(run, id, next, g->user);

        if (next == TLG_END) {
            run->current = TLG_END;
            run->status = TLG_DONE;
            return TLG_DONE;
        }
        if (!in_range(g, next)) return fail(run, TLG_ERR_BAD_NODE, id, next);
        run->current = next;
    }
}

/* ------------------------------------------------------------ */

static int report(char *err, size_t n, int rc, const char *fmt, ...)
{
    if (err && n) {
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(err, n, fmt, ap);
        va_end(ap);
    }
    return rc;
}

int tlg_validate(const tlg_graph_t *g, char *err, size_t n)
{
    if (err && n) err[0] = '\0';
    if (!g->nodes || g->n_nodes <= 0) return report(err, n, -1, "graph has no nodes");
    if (g->n_nodes > TLG_MAX_NODES)
        return report(err, n, -1, "graph has %d nodes, limit is %d", g->n_nodes, TLG_MAX_NODES);
    if (!in_range(g, g->start)) return report(err, n, -1, "start node %d does not exist", g->start);

    for (int i = 0; i < g->n_nodes; i++) {
        const tlg_node_t *nd = &g->nodes[i];
        if (!nd->name) return report(err, n, -1, "node %d has no name (missing table entry?)", i);
        if (!nd->targets || nd->n_targets <= 0)
            return report(err, n, -1, "node '%s' has no outgoing edge", nd->name);
        if (!nd->route && nd->n_targets != 1)
            return report(err, n, -1, "node '%s' has %d edges but no route function",
                          nd->name, nd->n_targets);
        for (int k = 0; k < nd->n_targets; k++) {
            int t = nd->targets[k];
            if (t != TLG_END && !in_range(g, t))
                return report(err, n, -1, "node '%s' has an edge to unknown node %d", nd->name, t);
        }
    }

    /* walk every path from start */
    bool seen[TLG_MAX_NODES] = { false };
    int stack[TLG_MAX_NODES], top = 0;
    bool end_reachable = false;
    seen[g->start] = true;
    stack[top++] = g->start;
    while (top > 0) {
        const tlg_node_t *nd = &g->nodes[stack[--top]];
        for (int k = 0; k < nd->n_targets; k++) {
            int t = nd->targets[k];
            if (t == TLG_END) { end_reachable = true; continue; }
            if (!seen[t]) { seen[t] = true; stack[top++] = t; }
        }
    }
    if (!end_reachable) return report(err, n, -1, "END cannot be reached from start");

    for (int i = 0; i < g->n_nodes; i++)
        if (!seen[i]) return report(err, n, 1, "node '%s' can never be reached", g->nodes[i].name);
    return 0;
}

/* ------------------------------------------------------------ */

typedef struct { char *buf; size_t size, len; bool overflow; } sbuf_t;

static void put(sbuf_t *b, const char *fmt, ...)
{
    if (b->overflow) return;
    va_list ap;
    va_start(ap, fmt);
    int w = vsnprintf(b->buf + b->len, b->size - b->len, fmt, ap);
    va_end(ap);
    if (w < 0 || (size_t)w >= b->size - b->len) { b->overflow = true; return; }
    b->len += (size_t)w;
}

static void put_id(sbuf_t *b, int id)
{
    if (id == TLG_END) put(b, "END_");
    else put(b, "n%d", id);
}

int tlg_to_mermaid(const tlg_graph_t *g, char *buf, size_t size)
{
    if (!buf || size == 0) return -1;
    sbuf_t b = { buf, size, 0, false };
    buf[0] = '\0';

    put(&b, "flowchart TD\n");
    put(&b, "  START_([start]) --> ");
    put_id(&b, g->start);
    put(&b, "\n");
    for (int i = 0; i < g->n_nodes; i++)
        put(&b, "  n%d[\"%s\"]\n", i, g->nodes[i].name ? g->nodes[i].name : "?");
    put(&b, "  END_([end])\n");

    /* solid = fixed edge, dashed = chosen by a route function */
    for (int i = 0; i < g->n_nodes; i++) {
        const tlg_node_t *nd = &g->nodes[i];
        for (int k = 0; k < nd->n_targets && nd->targets; k++) {
            put(&b, "  n%d %s ", i, nd->route ? "-.->" : "-->");
            put_id(&b, nd->targets[k]);
            put(&b, "\n");
        }
    }
    return b.overflow ? -1 : (int)b.len;
}

const char *tlg_node_name(const tlg_graph_t *g, int id)
{
    if (id == TLG_END) return "END";
    if (!in_range(g, id) || !g->nodes[id].name) return "?";
    return g->nodes[id].name;
}

const char *tlg_status_str(tlg_status_t s)
{
    switch (s) {
    case TLG_READY:         return "ready";
    case TLG_DONE:          return "done";
    case TLG_INTERRUPTED:   return "interrupted";
    case TLG_STEP_LIMIT:    return "step limit reached";
    case TLG_ERR_BAD_NODE:  return "bad node id";
    case TLG_ERR_BAD_ROUTE: return "route returned an undeclared target";
    case TLG_ERR_NO_EDGE:   return "node has no outgoing edge";
    }
    return "?";
}
