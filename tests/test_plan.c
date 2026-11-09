/* Unit tests for the planner, verifier and tracer. Run with: make test */
#include "tlg_plan.h"
#include "tlg_trace.h"

#include <stdio.h>
#include <string.h>

static int g_checks, g_fails;
#define CHECK(cond) do { g_checks++; if (!(cond)) { g_fails++; \
    printf("    FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)

/* A small hand-written domain (normally generated from schema.yaml). */
enum { F_WIFI, F_DANGER };
enum { O_LOGGED, O_PHOTO, O_TOLD, O_LOUD };
enum { A_LOG, A_PHOTO, A_TELL, A_SIREN, A_EXPENSIVE };
#define O(x) (1u << (x))
#define F(x) (1u << (x))

static const tlg_action_t ACTS[] = {
    { "log",       0,           0,          O(O_LOGGED), 1,  false },
    { "photo",     0,           0,          O(O_PHOTO),  2,  false },
    { "tell",      F(F_WIFI),   O(O_PHOTO), O(O_TOLD),   3,  false },
    { "siren",     F(F_DANGER), 0,          O(O_LOUD),   5,  true  },
    { "expensive", 0,           0,          O(O_LOGGED), 50, false },
};
static const char *const OUTS[] = { "logged", "photo", "told", "loud" };
static const uint8_t WEIGHTS[] = { 1, 2, 3, 4 };
static const char *const FACTS[] = { "wifi", "danger" };
static const tlg_domain_t D = {
    .actions = ACTS, .n_actions = 5,
    .outcome_names = OUTS, .outcome_weights = WEIGHTS, .n_outcomes = 4,
    .fact_names = FACTS, .n_facts = 2,
    .only_if_wanted = O(O_TOLD) | O(O_LOUD),
    .budget = 20,
};

static bool plan_is(const tlg_plan_t *p, const uint8_t *want, int n)
{
    return p->len == n && memcmp(p->actions, want, (size_t)n) == 0;
}

static void test_planner(void)
{
    tlg_plan_t p;
    const uint32_t all = O(O_LOGGED) | O(O_PHOTO) | O(O_TOLD);

    CHECK(tlg_plan(&D, F(F_WIFI), all, 0, &p) == 0);
    uint8_t full[] = { A_LOG, A_PHOTO, A_TELL };
    CHECK(plan_is(&p, full, 3));       /* photo comes before tell */
    CHECK(p.cost == 6 && p.achieved == all);

    CHECK(tlg_plan(&D, 0, all, 0, &p) == 0);   /* no wifi: tell is impossible */
    uint8_t relaxed[] = { A_LOG, A_PHOTO };
    CHECK(plan_is(&p, relaxed, 2));
    CHECK(p.score == 3);

    CHECK(tlg_plan(&D, F(F_WIFI), O(O_LOGGED), 0, &p) == 0);
    CHECK(p.len == 1 && p.actions[0] == A_LOG);   /* cheapest, not 'expensive' */

    CHECK(tlg_plan(&D, F(F_WIFI), O(O_LOGGED), 1u << A_LOG, &p) == 0);
    CHECK(p.len == 0);                 /* log banned, expensive is over budget */

    CHECK(tlg_plan(&D, F(F_WIFI), 0, 0, &p) == 0);
    CHECK(p.len == 0);                 /* nothing wanted, nothing done */
}

static tlg_verdict_t check(uint32_t facts, uint32_t want, const uint8_t *plan, int len,
                           bool approved, char *why)
{
    return tlg_verify(&D, facts, want, 0, plan, len, approved, why, 96);
}

static void test_verifier(void)
{
    char why[96];
    const uint32_t want = O(O_LOGGED) | O(O_PHOTO) | O(O_TOLD);

    uint8_t good[] = { A_LOG, A_PHOTO, A_TELL };
    CHECK(check(F(F_WIFI), want, good, 3, false, why) == TLG_VERIFY_OK);

    CHECK(check(0, want, good, 3, false, why) == TLG_VERIFY_FAIL);
    CHECK(strstr(why, "tell needs wifi") != NULL);

    uint8_t wrong_order[] = { A_LOG, A_TELL, A_PHOTO };
    CHECK(check(F(F_WIFI), want, wrong_order, 3, false, why) == TLG_VERIFY_FAIL);
    CHECK(strstr(why, "tell must come after photo") != NULL);

    uint8_t missing[] = { A_LOG, A_PHOTO };
    CHECK(check(F(F_WIFI), want, missing, 2, false, why) == TLG_VERIFY_FAIL);
    CHECK(strstr(why, "incomplete: misses told") != NULL);
    CHECK(check(0, want, missing, 2, false, why) == TLG_VERIFY_OK);   /* best possible without wifi */

    /* doing more than asked: telling is only allowed when wanted */
    uint8_t chatty[] = { A_LOG, A_PHOTO, A_TELL };
    CHECK(check(F(F_WIFI), O(O_LOGGED) | O(O_PHOTO), chatty, 3, false, why) == TLG_VERIFY_FAIL);
    CHECK(strstr(why, "does told, which nobody asked for") != NULL);

    uint8_t loud[] = { A_LOG, A_SIREN };
    const uint32_t want_loud = O(O_LOGGED) | O(O_LOUD);
    CHECK(check(F(F_DANGER), want_loud, loud, 2, false, why) == TLG_VERIFY_NEEDS_APPROVAL);
    CHECK(check(F(F_DANGER), want_loud, loud, 2, true, why) == TLG_VERIFY_OK);
    CHECK(check(0, want_loud, loud, 2, true, why) == TLG_VERIFY_FAIL);   /* no danger fact */

    uint8_t pricey[] = { A_EXPENSIVE };
    CHECK(check(0, O(O_LOGGED), pricey, 1, false, why) == TLG_VERIFY_FAIL);
    CHECK(strstr(why, "over budget") != NULL);

    uint8_t bogus[] = { 17 };
    CHECK(check(0, O(O_LOGGED), bogus, 1, false, why) == TLG_VERIFY_FAIL);

    CHECK(tlg_verify(&D, F(F_WIFI), want, 1u << A_TELL, good, 3, false, why, sizeof why) == TLG_VERIFY_FAIL);
    CHECK(strstr(why, "not allowed") != NULL);

    CHECK(check(0, 0, NULL, 0, false, why) == TLG_VERIFY_OK);          /* nothing wanted, nothing done */
    CHECK(check(0, O(O_LOGGED), NULL, 0, false, why) == TLG_VERIFY_FAIL);
}

/* Exhaustive: every plan the planner makes must pass the verifier. */
static void test_planner_always_verifies(void)
{
    char why[96];
    int checked = 0, bad = 0;
    for (uint32_t facts = 0; facts < 4; facts++)
        for (uint32_t want = 0; want < 16; want++)
            for (uint32_t banned = 0; banned < 32; banned++) {
                tlg_plan_t p;
                tlg_plan(&D, facts, want, banned, &p);
                tlg_verdict_t v = tlg_verify(&D, facts, want, banned, p.actions, p.len, true, why, sizeof why);
                checked++;
                if (v != TLG_VERIFY_OK && !(p.len == 0 && want != 0)) bad++;
            }
    CHECK(checked == 2048);
    CHECK(bad == 0);
}

static void test_strings(void)
{
    char buf[64];
    uint8_t p[] = { A_LOG, A_TELL };
    tlg_plan_str(&D, p, 2, buf, sizeof buf);
    CHECK(strcmp(buf, "log -> tell") == 0);
    tlg_plan_str(&D, p, 0, buf, sizeof buf);
    CHECK(strcmp(buf, "(nothing)") == 0);
    tlg_outcomes_str(&D, O(O_LOGGED) | O(O_LOUD), buf, sizeof buf);
    CHECK(strcmp(buf, "logged,loud") == 0);
    char tiny[8];
    tlg_plan_str(&D, p, 2, tiny, sizeof tiny);   /* must not overflow */
    CHECK(strlen(tiny) < sizeof tiny);
}

/* ---- tracer ---- */
static char g_out[4096];
static void capture(const char *line, void *ctx) { (void)ctx; strncat(g_out, line, sizeof g_out - strlen(g_out) - 1); }
static uint32_t fake_clock(void) { return 42; }

static void n0(void *s, tlg_run_t *r) { (void)s; (void)r; }
static const tlg_node_t TN[] = {
    [0] = { "first", n0, TLG_EDGE(1) },
    [1] = { "sec\"ond", n0, TLG_FINISH },
};

static void test_trace(void)
{
    char out[64];
    tlg_json_escape("a\"b\\c\nd\x01", out, sizeof out);
    CHECK(strcmp(out, "a\\\"b\\\\c\\nd\\u0001") == 0);
    tlg_json_escape("abcdefghij", out, 5);
    CHECK(strcmp(out, "abcd") == 0);

    tlg_tracer_t tr = { .write = capture, .clock = fake_clock };
    tlg_graph_t g = { TLG_NODES(TN), .start = 0, .on_step = tlg_trace_on_step, .user = &tr };

    g_out[0] = '\0';
    tlg_trace_note(&tr, "not enabled yet");   /* no graph sent: silent */
    CHECK(g_out[0] == '\0');

    tlg_trace_graph(&tr, &g, "test");
    tlg_trace_run(&tr, "first run");
    tlg_run_t run;
    tlg_run_init(&run, &g);
    tlg_trace_note(&tr, "value=%d", 7);
    tlg_invoke(&g, &run, NULL);
    tlg_trace_end(&tr, &run);

    CHECK(strstr(g_out, "@tlg {\"k\":\"graph\",\"name\":\"test\",\"start\":0,\"n\":2}\n") != NULL);
    CHECK(strstr(g_out, "\"name\":\"sec\\\"ond\",\"cond\":false,\"to\":[-1]") != NULL);
    CHECK(strstr(g_out, "{\"k\":\"run\",\"run\":1,\"t\":42,\"label\":\"first run\"}") != NULL);
    CHECK(strstr(g_out, "\"text\":\"value=7\"") != NULL);
    CHECK(strstr(g_out, "{\"k\":\"step\",\"run\":1,\"t\":42,\"step\":2,\"node\":1,\"next\":-1}") != NULL);
    CHECK(strstr(g_out, "\"status\":\"done\",\"steps\":2") != NULL);

    /* every line is a complete event */
    int lines = 0;
    for (char *p = g_out; (p = strchr(p, '\n')); p++) lines++;
    CHECK(lines == 8);   /* graph, 2 nodes, run, note, 2 steps, end */

    /* very long notes are truncated but still valid lines */
    g_out[0] = '\0';
    char big[600];
    memset(big, 'x', sizeof big - 1);
    big[sizeof big - 1] = '\0';
    tlg_trace_note(&tr, "%s", big);
    size_t len = strlen(g_out);
    CHECK(len < TLG_TRACE_LINE_MAX && g_out[len - 1] == '\n' && g_out[len - 2] == '}');
}

int main(void)
{
    struct { const char *name; void (*fn)(void); } tests[] = {
        { "planner: cheapest valid plan",             test_planner },
        { "verifier: rejects what it should",         test_verifier },
        { "planner output always verifies (2048)",    test_planner_always_verifies },
        { "plan and outcome strings",                 test_strings },
        { "trace events",                             test_trace },
    };
    for (size_t i = 0; i < sizeof tests / sizeof tests[0]; i++) {
        int before = g_fails;
        tests[i].fn();
        printf("[%s] %s\n", g_fails == before ? "pass" : "FAIL", tests[i].name);
    }
    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    return g_fails ? 1 : 0;
}
