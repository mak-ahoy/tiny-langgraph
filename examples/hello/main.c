/*
 * hello: the smallest useful graph.
 * perceive -> decide -> (act | END)
 */
#include "tlg.h"
#include <stdio.h>

typedef struct {
    int sensor;          /* input: a raw reading */
    int saw_something;   /* written by perceive, read by decide */
} hello_state_t;

enum { PERCEIVE, DECIDE, ACT };

static void perceive(void *s, tlg_run_t *run)
{
    (void)run;
    hello_state_t *st = s;
    st->saw_something = st->sensor > 50;
}

static int decide(const void *s)
{
    const hello_state_t *st = s;
    return st->saw_something ? ACT : TLG_END;
}

static void act(void *s, tlg_run_t *run)
{
    (void)s; (void)run;
    printf("    act: doing something about it\n");
}

static const tlg_node_t NODES[] = {
    [PERCEIVE] = { "perceive", perceive, TLG_EDGE(DECIDE) },
    [DECIDE]   = { "decide",   NULL,     TLG_ROUTE(decide, ACT, TLG_END) },
    [ACT]      = { "act",      act,      TLG_FINISH },
};

static void trace(const tlg_run_t *run, int node, int next, void *user)
{
    const tlg_graph_t *g = user;
    printf("    step %lu: %s -> %s\n", (unsigned long)run->step,
           tlg_node_name(g, node), tlg_node_name(g, next));
}

static tlg_graph_t GRAPH = { TLG_NODES(NODES), .start = PERCEIVE, .on_step = trace };

int main(void)
{
    GRAPH.user = &GRAPH;

    char err[96];
    if (tlg_validate(&GRAPH, err, sizeof err) < 0) {
        printf("invalid graph: %s\n", err);
        return 1;
    }

    const int readings[] = { 80, 10 };
    for (int i = 0; i < 2; i++) {
        hello_state_t st = { .sensor = readings[i] };
        tlg_run_t run;
        tlg_run_init(&run, &GRAPH);
        printf("sensor = %d\n", st.sensor);
        tlg_status_t rc = tlg_invoke(&GRAPH, &run, &st);
        printf("  -> %s after %lu steps\n\n", tlg_status_str(rc), (unsigned long)run.step);
    }

    char mermaid[512];
    if (tlg_to_mermaid(&GRAPH, mermaid, sizeof mermaid) > 0) printf("graph (Mermaid):\n%s", mermaid);
    return 0;
}
