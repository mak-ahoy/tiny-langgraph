/*
 * Trace events for the viewer (viewer/index.html).
 *
 * Every event is one line:  @tlg {"k":"step", ...}
 * Lines without the @tlg prefix are ordinary log output and are shown as-is,
 * so tracing can share the serial port with your own prints.
 *
 *   graph  the graph's nodes and edges (send once, at boot)
 *   run    a run starts, with a label
 *   note   a line of reasoning from inside the current node
 *   step   a node finished: where it went next
 *   end    the run finished (done, interrupted, or an error)
 *
 * Usage:
 *   tlg_tracer_t tr = { .write = my_write, .clock = my_millis };
 *   graph.on_step = tlg_trace_on_step;  graph.user = &tr;
 *   tlg_trace_graph(&tr, &graph, "door_guard");
 *   tlg_trace_run(&tr, "intruder at night");
 *   tlg_invoke(&graph, &run, &state);   // nodes may call tlg_trace_note(&tr, ...)
 *   tlg_trace_end(&tr, &run);
 */
#ifndef TLG_TRACE_H
#define TLG_TRACE_H

#include "tlg.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TLG_TRACE_LINE_MAX 512

/* Receives one complete line, including the trailing newline. */
typedef void     (*tlg_trace_write_fn)(const char *line, void *ctx);
typedef uint32_t (*tlg_trace_clock_fn)(void);   /* milliseconds; optional */

typedef struct {
    tlg_trace_write_fn write;
    void              *ctx;
    tlg_trace_clock_fn clock;
    uint32_t           run_id;      /* incremented by tlg_trace_run */
    bool               enabled;     /* set by tlg_trace_graph; false = no output */
} tlg_tracer_t;

void tlg_trace_graph(tlg_tracer_t *tr, const tlg_graph_t *g, const char *name);
void tlg_trace_run(tlg_tracer_t *tr, const char *label);
void tlg_trace_note(tlg_tracer_t *tr, const char *fmt, ...)
#if defined(__GNUC__)
    __attribute__((format(printf, 2, 3)))
#endif
    ;
void tlg_trace_end(tlg_tracer_t *tr, const tlg_run_t *run);

/* Plug into tlg_graph_t.on_step, with .user pointing at the tracer. */
void tlg_trace_on_step(const tlg_run_t *run, int node, int next, void *user);

/* Escape a string for JSON. Returns bytes written (always NUL-terminated). */
size_t tlg_json_escape(const char *in, char *out, size_t size);

#ifdef __cplusplus
}
#endif

#endif /* TLG_TRACE_H */
