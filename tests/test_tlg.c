/* Unit tests for the graph runtime. Run with: make test */
#include "tlg.h"
#include <stdio.h>
#include <string.h>

static int g_checks, g_fails;

#define CHECK(cond) do { g_checks++; if (!(cond)) { g_fails++; \
    printf("    FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)

/* ---- a small graph shared by several tests ----
 *   A -> B -+-> C -> END
 *           +-> END
 */
typedef struct {
    int  go_c;          /* B routes to C when set */
    int  visits[4];
    int  route_override;
    bool use_override;
} st_t;

enum { A, B, C };

static void count(void *s, tlg_run_t *run) { (void)run; ((st_t *)s)->visits[0]++; }
static void visit_a(void *s, tlg_run_t *r) { count(s, r); ((st_t *)s)->visits[1 + A]++; }
static void visit_b(void *s, tlg_run_t *r) { count(s, r); ((st_t *)s)->visits[1 + B]++; }
static void visit_c(void *s, tlg_run_t *r) { count(s, r); ((st_t *)s)->visits[1 + C]++; }

static int route_b(const void *s)
{
    const st_t *st = s;
    if (st->use_override) return st->route_override;
    return st->go_c ? C : TLG_END;
}

static const tlg_node_t BASIC[] = {
    [A] = { "a", visit_a, TLG_EDGE(B) },
    [B] = { "b", visit_b, TLG_ROUTE(route_b, C, TLG_END) },
    [C] = { "c", visit_c, TLG_FINISH },
};
static const tlg_graph_t BASIC_G = { TLG_NODES(BASIC), .start = A };

static void test_fixed_and_conditional_edges(void)
{
    st_t st = { .go_c = 1 };
    tlg_run_t run;
    tlg_run_init(&run, &BASIC_G);
    CHECK(tlg_invoke(&BASIC_G, &run, &st) == TLG_DONE);
    CHECK(run.step == 3);
    CHECK(st.visits[1 + C] == 1);
    CHECK(run.current == TLG_END);

    st_t st2 = { .go_c = 0 };
    tlg_run_init(&run, &BASIC_G);
    CHECK(tlg_invoke(&BASIC_G, &run, &st2) == TLG_DONE);
    CHECK(run.step == 2);
    CHECK(st2.visits[1 + C] == 0);
}

static void test_finished_run_stays_finished(void)
{
    st_t st = { 0 };
    tlg_run_t run;
    tlg_run_init(&run, &BASIC_G);
    tlg_invoke(&BASIC_G, &run, &st);
    CHECK(tlg_invoke(&BASIC_G, &run, &st) == TLG_DONE);
    CHECK(st.visits[0] == 2);   /* nothing ran the second time */
}

static void test_route_to_undeclared_target(void)
{
    st_t st = { .use_override = true, .route_override = A };   /* A is not declared for B */
    tlg_run_t run;
    tlg_run_init(&run, &BASIC_G);
    CHECK(tlg_invoke(&BASIC_G, &run, &st) == TLG_ERR_BAD_ROUTE);
    CHECK(run.error_node == B);
    CHECK(run.error_value == A);
}

/* ---- endless cycle ---- */
static int always_a(const void *s) { (void)s; return A; }
static const tlg_node_t LOOP[] = {
    [A] = { "a", count, TLG_EDGE(B) },
    [B] = { "b", count, TLG_ROUTE(always_a, A, TLG_END) },
};
static const tlg_graph_t LOOP_G = { TLG_NODES(LOOP), .start = A, .max_steps = 10 };

static void test_step_limit(void)
{
    st_t st = { 0 };
    tlg_run_t run;
    tlg_run_init(&run, &LOOP_G);
    CHECK(tlg_invoke(&LOOP_G, &run, &st) == TLG_STEP_LIMIT);
    CHECK(run.step == 10);
    CHECK(st.visits[0] == 10);
}

/* ---- interrupt + resume + checkpoint ---- */
typedef struct { int answer; int asked; int finished; } hitl_t;

static void ask(void *s, tlg_run_t *run)
{
    hitl_t *h = s;
    h->asked++;
    if (h->answer == 0) tlg_interrupt(run, "need approval");
}
static int after_ask(const void *s) { return ((const hitl_t *)s)->answer == 1 ? B : TLG_END; }
static void finish(void *s, tlg_run_t *run) { (void)run; ((hitl_t *)s)->finished = 1; }

static const tlg_node_t HITL[] = {
    [A] = { "ask",    ask,    TLG_ROUTE(after_ask, B, TLG_END) },
    [B] = { "finish", finish, TLG_FINISH },
};
static const tlg_graph_t HITL_G = { TLG_NODES(HITL), .start = A };

static void test_interrupt_and_resume(void)
{
    hitl_t h = { 0 };
    tlg_run_t run;
    tlg_run_init(&run, &HITL_G);
    CHECK(tlg_invoke(&HITL_G, &run, &h) == TLG_INTERRUPTED);
    CHECK(run.current == A);
    CHECK(strcmp(run.interrupt_reason, "need approval") == 0);
    CHECK(!h.finished);

    /* still waiting: resuming without an answer interrupts again */
    CHECK(tlg_invoke(&HITL_G, &run, &h) == TLG_INTERRUPTED);

    h.answer = 1;
    CHECK(tlg_invoke(&HITL_G, &run, &h) == TLG_DONE);
    CHECK(h.finished);
    CHECK(h.asked == 3);
}

static void test_checkpoint_round_trip(void)
{
    hitl_t h = { 0 };
    tlg_run_t run;
    tlg_run_init(&run, &HITL_G);
    tlg_invoke(&HITL_G, &run, &h);

    unsigned char saved[sizeof run + sizeof h];
    memcpy(saved, &run, sizeof run);
    memcpy(saved + sizeof run, &h, sizeof h);

    tlg_run_t run2;
    hitl_t h2;
    memset(&run2, 0xAA, sizeof run2);    /* garbage, as after a reboot */
    memset(&h2, 0xAA, sizeof h2);
    memcpy(&run2, saved, sizeof run2);
    memcpy(&h2, saved + sizeof run2, sizeof h2);

    h2.answer = 2;                        /* owner says no */
    CHECK(tlg_invoke(&HITL_G, &run2, &h2) == TLG_DONE);
    CHECK(!h2.finished);
}

/* ---- on_step hook ---- */
typedef struct { int n; int node[8]; int next[8]; } rec_t;
static void record(const tlg_run_t *run, int node, int next, void *user)
{
    (void)run;
    rec_t *r = user;
    if (r->n < 8) { r->node[r->n] = node; r->next[r->n] = next; r->n++; }
}

static void test_on_step_hook(void)
{
    rec_t rec = { 0 };
    tlg_graph_t g = BASIC_G;
    g.on_step = record;
    g.user = &rec;
    st_t st = { .go_c = 1 };
    tlg_run_t run;
    tlg_run_init(&run, &g);
    tlg_invoke(&g, &run, &st);
    CHECK(rec.n == 3);
    CHECK(rec.node[0] == A && rec.next[0] == B);
    CHECK(rec.node[1] == B && rec.next[1] == C);
    CHECK(rec.node[2] == C && rec.next[2] == TLG_END);
}

/* ---- validation ---- */
static int to_c(const void *s) { (void)s; return C; }

static const tlg_node_t NO_EDGE[] = {
    [A] = { "a", count, TLG_EDGE(B) },
    [B] = { "b", count, .route = NULL },               /* forgot the edge */
};
static const tlg_node_t BAD_TARGET[] = {
    [A] = { "a", count, TLG_EDGE(7) },
};
static const tlg_node_t MISSING_ENTRY[] = {
    [A] = { "a", count, TLG_EDGE(C) },
    [C] = { "c", count, TLG_FINISH },                  /* B left empty */
};
static const tlg_node_t NO_END[] = {
    [A] = { "a", count, TLG_EDGE(B) },
    [B] = { "b", count, TLG_EDGE(A) },
};
static const tlg_node_t UNREACHABLE[] = {
    [A] = { "a", count, TLG_FINISH },
    [B] = { "b", count, TLG_FINISH },
};
static const tlg_node_t MANY_NO_ROUTE[] = {
    [A] = { "a", count, .targets = TLG_IDS(B, TLG_END), .n_targets = 2 },
    [B] = { "b", count, TLG_FINISH },
};
static const tlg_node_t ROUTED_OK[] = {
    [A] = { "a", count, TLG_EDGE(B) },
    [B] = { "b", count, TLG_ROUTE(to_c, C, TLG_END) },
    [C] = { "c", count, TLG_FINISH },
};

static int validate(const tlg_node_t *nodes, int n, char *err, size_t size)
{
    tlg_graph_t g = { .nodes = nodes, .n_nodes = n, .start = A };
    return tlg_validate(&g, err, size);
}
#define VALIDATE(arr, err) validate(arr, (int)(sizeof(arr) / sizeof(arr[0])), err, sizeof err)

static void test_validation(void)
{
    char err[96];
    CHECK(tlg_validate(&BASIC_G, err, sizeof err) == 0);
    CHECK(VALIDATE(ROUTED_OK, err) == 0);

    CHECK(VALIDATE(NO_EDGE, err) == -1);
    CHECK(strstr(err, "no outgoing edge") != NULL);

    CHECK(VALIDATE(BAD_TARGET, err) == -1);
    CHECK(strstr(err, "unknown node 7") != NULL);

    CHECK(VALIDATE(MISSING_ENTRY, err) == -1);
    CHECK(strstr(err, "no name") != NULL);

    CHECK(VALIDATE(NO_END, err) == -1);
    CHECK(strstr(err, "END cannot be reached") != NULL);

    CHECK(VALIDATE(UNREACHABLE, err) == 1);
    CHECK(strstr(err, "'b' can never be reached") != NULL);

    CHECK(VALIDATE(MANY_NO_ROUTE, err) == -1);
    CHECK(strstr(err, "no route function") != NULL);

    tlg_graph_t bad_start = BASIC_G;
    bad_start.start = 9;
    CHECK(tlg_validate(&bad_start, err, sizeof err) == -1);
}

static void test_runtime_catches_unvalidated_graphs(void)
{
    st_t st = { 0 };
    tlg_run_t run;
    tlg_graph_t g = { TLG_NODES(NO_EDGE), .start = A };
    tlg_run_init(&run, &g);
    CHECK(tlg_invoke(&g, &run, &st) == TLG_ERR_NO_EDGE);
    CHECK(run.error_node == B);

    tlg_graph_t g2 = { TLG_NODES(BAD_TARGET), .start = A };
    tlg_run_init(&run, &g2);
    CHECK(tlg_invoke(&g2, &run, &st) == TLG_ERR_BAD_NODE);
    CHECK(run.error_value == 7);

    tlg_graph_t g3 = BASIC_G;
    g3.start = -5;
    tlg_run_init(&run, &g3);
    CHECK(tlg_invoke(&g3, &run, &st) == TLG_ERR_BAD_NODE);
}

/* ---- Mermaid ---- */
static void test_mermaid(void)
{
    char buf[512];
    int n = tlg_to_mermaid(&BASIC_G, buf, sizeof buf);
    CHECK(n > 0);
    CHECK(strstr(buf, "flowchart TD") != NULL);
    CHECK(strstr(buf, "n0 --> n1") != NULL);       /* fixed edge */
    CHECK(strstr(buf, "n1 -.-> n2") != NULL);      /* conditional edge */
    CHECK(strstr(buf, "n1 -.-> END_") != NULL);

    char tiny[16];
    CHECK(tlg_to_mermaid(&BASIC_G, tiny, sizeof tiny) == -1);
}

int main(void)
{
    struct { const char *name; void (*fn)(void); } tests[] = {
        { "fixed and conditional edges",       test_fixed_and_conditional_edges },
        { "finished run stays finished",       test_finished_run_stays_finished },
        { "route to undeclared target",        test_route_to_undeclared_target },
        { "step limit stops endless cycles",   test_step_limit },
        { "interrupt and resume",              test_interrupt_and_resume },
        { "checkpoint survives a 'reboot'",    test_checkpoint_round_trip },
        { "on_step hook",                      test_on_step_hook },
        { "validation",                        test_validation },
        { "runtime catches unvalidated graphs", test_runtime_catches_unvalidated_graphs },
        { "mermaid export",                    test_mermaid },
    };
    for (size_t i = 0; i < sizeof tests / sizeof tests[0]; i++) {
        int before = g_fails;
        tests[i].fn();
        printf("[%s] %s\n", g_fails == before ? "pass" : "FAIL", tests[i].name);
    }
    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    return g_fails ? 1 : 0;
}
