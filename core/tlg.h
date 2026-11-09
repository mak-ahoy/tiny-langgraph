/*
 * tiny-langgraph: a LangGraph-style agent runtime for microcontrollers.
 *
 * A graph is a static table of nodes. Each node has:
 *   - run:   does the work, reading and changing your state
 *   - edges: where to go next, either one fixed edge or a route function
 *            that picks among a declared list of targets
 *
 * The runtime knows nothing about your state (it is a void *), never calls
 * malloc, and keeps everything it needs in a plain tlg_run_t, so a paused
 * run can be saved to flash and resumed after a reboot.
 */
#ifndef TLG_H
#define TLG_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TLG_VERSION "0.1.0"

/* The END node: route here to finish a run. */
#define TLG_END (-1)

/* Upper limit for tlg_validate's bookkeeping (graphs can't be larger). */
#define TLG_MAX_NODES 128

typedef struct tlg_run tlg_run_t;

/* Node work. May call tlg_interrupt() to pause the run. */
typedef void (*tlg_run_fn)(void *state, tlg_run_t *run);

/* Conditional edge: read the state, return the next node id (or TLG_END).
 * Routes get read-only state on purpose: deciding where to go should not
 * change anything. */
typedef int (*tlg_route_fn)(const void *state);

typedef struct {
    const char  *name;
    tlg_run_fn   run;        /* NULL = node does no work, only routes */
    tlg_route_fn route;      /* NULL = follow the single fixed edge */
    const int   *targets;    /* every node this one can go to */
    int          n_targets;
} tlg_node_t;

/*
 * Edge helpers for node tables. They use C compound literals, so:
 *   - define node tables at file scope (outside any function), and
 *   - in .c files, not in Arduino .ino / C++ files.
 *
 *   [A] = { "a", a_run, TLG_EDGE(B) },                     always go to B
 *   [B] = { "b", NULL,  TLG_ROUTE(b_route, C, TLG_END) },  b_route picks C or END
 *   [C] = { "c", c_run, TLG_FINISH },                      always end
 */
#define TLG_IDS(...)        ((const int[]){ __VA_ARGS__ })
#define TLG_NUM_IDS(...)    ((int)(sizeof(TLG_IDS(__VA_ARGS__)) / sizeof(int)))
#define TLG_EDGE(to)        .route = NULL, .targets = TLG_IDS(to), .n_targets = 1
#define TLG_ROUTE(fn, ...)  .route = (fn), .targets = TLG_IDS(__VA_ARGS__), \
                            .n_targets = TLG_NUM_IDS(__VA_ARGS__)
#define TLG_FINISH          TLG_EDGE(TLG_END)

/* Called after every node: next == node means the run was interrupted there. */
typedef void (*tlg_step_fn)(const tlg_run_t *run, int node, int next, void *user);

typedef struct {
    const tlg_node_t *nodes;
    int               n_nodes;
    int               start;
    uint32_t          max_steps;   /* guard against endless cycles; 0 = 64 */
    tlg_step_fn       on_step;     /* optional: tracing, logging, streaming */
    void             *user;        /* passed to on_step */
} tlg_graph_t;

/* Fills .nodes and .n_nodes from a node array. */
#define TLG_NODES(arr) .nodes = (arr), .n_nodes = (int)(sizeof(arr) / sizeof((arr)[0]))

typedef enum {
    TLG_READY,          /* initialised, not started yet */
    TLG_DONE,           /* reached END */
    TLG_INTERRUPTED,    /* a node paused the run; call tlg_invoke again to resume */
    TLG_STEP_LIMIT,     /* max_steps reached: probably an endless cycle */
    TLG_ERR_BAD_NODE,   /* tried to run a node id that doesn't exist */
    TLG_ERR_BAD_ROUTE,  /* a route returned a target it didn't declare */
    TLG_ERR_NO_EDGE,    /* a node has nowhere to go */
} tlg_status_t;

/*
 * Everything the runtime needs to continue a run. Plain data with no
 * pointers, so saving it (plus your state) is a checkpoint.
 */
struct tlg_run {
    int          current;              /* node to run next */
    uint32_t     step;                 /* nodes executed so far */
    tlg_status_t status;
    bool         interrupt_requested;
    char         interrupt_reason[48];
    int          error_node;           /* node involved in an error */
    int          error_value;          /* e.g. the bad id a route returned */
};

void         tlg_run_init(tlg_run_t *run, const tlg_graph_t *g);

/*
 * Run until END, an interrupt, the step limit, or an error.
 * If the run was interrupted, this resumes it by running the interrupted
 * node again, so that node should check whether its answer has arrived
 * before interrupting (see examples/plant_care).
 */
tlg_status_t tlg_invoke(const tlg_graph_t *g, tlg_run_t *run, void *state);

/* Call from inside a node to pause after it finishes. */
void         tlg_interrupt(tlg_run_t *run, const char *reason);

/*
 * Check a graph before running it: every node has a name and an edge,
 * every edge points somewhere real, and END can be reached from start.
 * Returns 0 if valid, 1 if valid with a warning (unreachable node),
 * -1 if invalid. A message goes to err when it's non-NULL.
 */
int          tlg_validate(const tlg_graph_t *g, char *err, size_t err_size);

/* Write the graph as a Mermaid flowchart. Returns length, or -1 if buf is too small. */
int          tlg_to_mermaid(const tlg_graph_t *g, char *buf, size_t size);

const char  *tlg_node_name(const tlg_graph_t *g, int id);
const char  *tlg_status_str(tlg_status_t s);

#ifdef __cplusplus
}
#endif

#endif /* TLG_H */
