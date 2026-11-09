/* Unit tests for the event hub. Run with: make test */
#include "tlg_hub.h"

#include <stdio.h>
#include <string.h>

static int g_checks, g_fails;
#define CHECK(cond) do { g_checks++; if (!(cond)) { g_fails++; \
    printf("    FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)

static tlg_hub_slot_t mem[4];
static tlg_hub_t hub;

static void put_step(int i)
{
    char line[64];
    snprintf(line, sizeof line, "@tlg {\"k\":\"step\",\"step\":%d}\n", i);
    tlg_hub_put(&hub, line);
}

static int count_lines(const char *s)
{
    int n = 0;
    for (; *s; s++) n += *s == '\n';
    return n;
}

static void test_basic(void)
{
    CHECK(tlg_hub_init(&hub, mem, sizeof mem) == 4);
    char out[2048];
    uint32_t last;
    int more;

    /* graph only: a new client gets it, and asking again with since=0 but no graph flag is empty */
    tlg_hub_put(&hub, "@tlg {\"k\":\"graph\",\"name\":\"g\"}\n");
    tlg_hub_put(&hub, "@tlg {\"k\":\"node\",\"id\":0}\n");
    size_t n = tlg_hub_get(&hub, 0, 1, out, sizeof out, &last, &more);
    CHECK(n > 0 && count_lines(out) == 2 && last == 0 && !more);
    CHECK(tlg_hub_get(&hub, 0, 0, out, sizeof out, &last, &more) == 0);

    put_step(1);
    put_step(2);
    tlg_hub_get(&hub, 0, 0, out, sizeof out, &last, &more);
    CHECK(count_lines(out) == 2 && last == 2);
    tlg_hub_get(&hub, 1, 0, out, sizeof out, &last, &more);
    CHECK(count_lines(out) == 1 && strstr(out, "\"step\":2") && last == 2);
    CHECK(tlg_hub_get(&hub, 2, 0, out, sizeof out, &last, &more) == 0 && last == 2);
}

static void test_wraparound(void)
{
    tlg_hub_init(&hub, mem, sizeof mem);
    char out[2048];
    uint32_t last;
    int more;
    for (int i = 1; i <= 10; i++) put_step(i);
    CHECK(tlg_hub_oldest(&hub) == 7);

    /* a client that fell behind gets what is still kept: 7..10 */
    tlg_hub_get(&hub, 2, 0, out, sizeof out, &last, &more);
    CHECK(count_lines(out) == 4 && strstr(out, "\"step\":7") && last == 10);
    CHECK(strstr(out, "\"step\":6") == NULL);
}

static void test_small_buffer(void)
{
    tlg_hub_init(&hub, mem, sizeof mem);
    for (int i = 1; i <= 3; i++) put_step(i);
    char out[40];       /* fits one 28-byte step line at a time, never a partial one */
    uint32_t last;
    int more;
    size_t n = tlg_hub_get(&hub, 0, 0, out, sizeof out, &last, &more);
    CHECK(n > 0 && count_lines(out) == 1 && last == 1 && more == 1);
    n = tlg_hub_get(&hub, last, 0, out, sizeof out, &last, &more);
    CHECK(count_lines(out) == 1 && last == 2 && more == 1);
    n = tlg_hub_get(&hub, last, 0, out, sizeof out, &last, &more);
    CHECK(count_lines(out) == 1 && last == 3 && more == 0);
    (void)n;
}

static void test_new_graph_replaces_old(void)
{
    tlg_hub_init(&hub, mem, sizeof mem);
    char out[2048];
    uint32_t last;
    int more;
    tlg_hub_put(&hub, "@tlg {\"k\":\"graph\",\"name\":\"old\"}\n");
    tlg_hub_put(&hub, "@tlg {\"k\":\"graph\",\"name\":\"new\"}\n");
    tlg_hub_get(&hub, 0, 1, out, sizeof out, &last, &more);
    CHECK(strstr(out, "old") == NULL && strstr(out, "new") != NULL);
}

static void test_no_memory(void)
{
    char tiny[8];
    CHECK(tlg_hub_init(&hub, tiny, sizeof tiny) == 0);
    put_step(1);   /* must not crash */
    char out[256];
    uint32_t last;
    int more;
    CHECK(tlg_hub_get(&hub, 0, 0, out, sizeof out, &last, &more) == 0);
}

/* The tracer can write straight into a hub. */
static const tlg_node_t N[] = { [0] = { "only", NULL, TLG_FINISH } };

static void test_with_tracer(void)
{
    tlg_hub_init(&hub, mem, sizeof mem);
    tlg_tracer_t tr = { .write = tlg_hub_write, .ctx = &hub };
    tlg_graph_t g = { TLG_NODES(N), .start = 0, .on_step = tlg_trace_on_step, .user = &tr };
    tlg_trace_graph(&tr, &g, "t");
    tlg_trace_run(&tr, "r");
    tlg_run_t run;
    tlg_run_init(&run, &g);
    tlg_invoke(&g, &run, NULL);
    tlg_trace_end(&tr, &run);

    char out[2048];
    uint32_t last;
    int more;
    tlg_hub_get(&hub, 0, 1, out, sizeof out, &last, &more);
    CHECK(count_lines(out) == 5);   /* graph, node, run, step, end */
    CHECK(last == 3);
}

int main(void)
{
    struct { const char *name; void (*fn)(void); } tests[] = {
        { "hub: graph, then lines in order",      test_basic },
        { "hub: wraparound keeps the newest",     test_wraparound },
        { "hub: never sends a partial line",      test_small_buffer },
        { "hub: a new graph replaces the old",    test_new_graph_replaces_old },
        { "hub: works with no slot memory",       test_no_memory },
        { "hub: tracer writes into it",           test_with_tracer },
    };
    for (size_t i = 0; i < sizeof tests / sizeof tests[0]; i++) {
        int before = g_fails;
        tests[i].fn();
        printf("[%s] %s\n", g_fails == before ? "pass" : "FAIL", tests[i].name);
    }
    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    return g_fails ? 1 : 0;
}
