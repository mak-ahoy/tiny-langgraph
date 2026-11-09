#include "tlg_trace.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

size_t tlg_json_escape(const char *in, char *out, size_t size)
{
    size_t o = 0;
    if (size == 0) return 0;
    for (const unsigned char *p = (const unsigned char *)in; *p; p++) {
        char tmp[8];
        const char *rep = NULL;
        switch (*p) {
        case '"':  rep = "\\\""; break;
        case '\\': rep = "\\\\"; break;
        case '\n': rep = "\\n";  break;
        case '\r': rep = "\\r";  break;
        case '\t': rep = "\\t";  break;
        default:
            if (*p < 0x20) { snprintf(tmp, sizeof tmp, "\\u%04x", *p); rep = tmp; }
        }
        size_t need = rep ? strlen(rep) : 1;
        if (o + need >= size) break;            /* truncate rather than overflow */
        if (rep) { memcpy(out + o, rep, need); o += need; }
        else out[o++] = (char)*p;
    }
    out[o] = '\0';
    return o;
}

static uint32_t now(const tlg_tracer_t *tr)
{
    return tr->clock ? tr->clock() : 0;
}

static void emit(tlg_tracer_t *tr, const char *fmt, ...)
{
    if (!tr || !tr->enabled || !tr->write) return;
    char line[TLG_TRACE_LINE_MAX];
    int n = snprintf(line, sizeof line, "@tlg ");
    va_list ap;
    va_start(ap, fmt);
    int w = vsnprintf(line + n, sizeof line - (size_t)n - 2, fmt, ap);
    va_end(ap);
    if (w < 0) return;
    size_t len = strlen(line);
    if (len > 0 && line[len - 1] != '}') {
        /* truncated: close the JSON so the viewer can still parse it */
        if (len > sizeof line - 4) len = sizeof line - 4;
        line[len++] = '"';
        line[len++] = '}';
    }
    line[len++] = '\n';
    line[len] = '\0';
    tr->write(line, tr->ctx);
}

void tlg_trace_graph(tlg_tracer_t *tr, const tlg_graph_t *g, const char *name)
{
    if (!tr) return;
    tr->enabled = true;

    /* nodes and edges may not fit one line on big graphs: send them in pieces */
    char esc[64];
    tlg_json_escape(name ? name : "graph", esc, sizeof esc);
    emit(tr, "{\"k\":\"graph\",\"name\":\"%s\",\"start\":%d,\"n\":%d}", esc, g->start, g->n_nodes);
    for (int i = 0; i < g->n_nodes; i++) {
        const tlg_node_t *nd = &g->nodes[i];
        char targets[160];
        size_t used = 0;
        targets[0] = '\0';
        for (int k = 0; k < nd->n_targets && nd->targets; k++) {
            int w = snprintf(targets + used, sizeof targets - used, "%s%d", k ? "," : "", nd->targets[k]);
            if (w < 0 || (size_t)w >= sizeof targets - used) break;
            used += (size_t)w;
        }
        tlg_json_escape(nd->name ? nd->name : "?", esc, sizeof esc);
        emit(tr, "{\"k\":\"node\",\"id\":%d,\"name\":\"%s\",\"cond\":%s,\"to\":[%s]}",
             i, esc, nd->route ? "true" : "false", targets);
    }
}

void tlg_trace_run(tlg_tracer_t *tr, const char *label)
{
    if (!tr) return;
    tr->run_id++;
    char esc[160];
    tlg_json_escape(label ? label : "", esc, sizeof esc);
    emit(tr, "{\"k\":\"run\",\"run\":%lu,\"t\":%lu,\"label\":\"%s\"}",
         (unsigned long)tr->run_id, (unsigned long)now(tr), esc);
}

void tlg_trace_note(tlg_tracer_t *tr, const char *fmt, ...)
{
    if (!tr || !tr->enabled) return;
    char text[256], esc[384];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);
    tlg_json_escape(text, esc, sizeof esc);
    emit(tr, "{\"k\":\"note\",\"run\":%lu,\"t\":%lu,\"text\":\"%s\"}",
         (unsigned long)tr->run_id, (unsigned long)now(tr), esc);
}

void tlg_trace_on_step(const tlg_run_t *run, int node, int next, void *user)
{
    tlg_tracer_t *tr = user;
    emit(tr, "{\"k\":\"step\",\"run\":%lu,\"t\":%lu,\"step\":%lu,\"node\":%d,\"next\":%d}",
         (unsigned long)tr->run_id, (unsigned long)now(tr), (unsigned long)run->step, node, next);
}

void tlg_trace_end(tlg_tracer_t *tr, const tlg_run_t *run)
{
    if (!tr) return;
    char esc[96];
    tlg_json_escape(run->interrupt_reason, esc, sizeof esc);
    emit(tr, "{\"k\":\"end\",\"run\":%lu,\"t\":%lu,\"status\":\"%s\",\"steps\":%lu,\"reason\":\"%s\"}",
         (unsigned long)tr->run_id, (unsigned long)now(tr), tlg_status_str(run->status),
         (unsigned long)run->step, esc);
}
