/*
 * plant_care: human-in-the-loop with a checkpoint that survives a reboot.
 *
 *   read -> assess -+-> water -> END             soil dry, tank fine
 *                   +-> ask --+-> water -> END   soil dry, tank low: ask first
 *                   |         +-> END            (owner said no)
 *                   +-> END                      soil is moist
 *
 * When the tank is low, the "ask" node pauses the run. We save the run and
 * the state as raw bytes (on a real device: flash or SD), wipe everything
 * as if the board rebooted, restore, give the owner's answer, and resume.
 */
#include "tlg.h"
#include <stdio.h>
#include <string.h>

typedef enum { ANSWER_NONE, ANSWER_YES, ANSWER_NO } answer_t;

typedef struct {
    float    moisture;   /* 0..1 from the soil sensor */
    float    tank;       /* 0..1 water left */
    answer_t answer;     /* owner's reply to the question */
    int      watered;
} plant_state_t;

enum { READ, ASSESS, ASK, WATER };

static void read_sensors(void *s, tlg_run_t *run)
{
    (void)run;
    const plant_state_t *st = s;
    printf("    read: moisture %.2f, tank %.2f\n", st->moisture, st->tank);
}

static int assess(const void *s)
{
    const plant_state_t *st = s;
    if (st->moisture >= 0.35f) return TLG_END;
    return st->tank < 0.20f ? ASK : WATER;
}

/* Interrupts until an answer exists. On resume it runs again and finds the answer. */
static void ask(void *s, tlg_run_t *run)
{
    plant_state_t *st = s;
    if (st->answer == ANSWER_NONE) tlg_interrupt(run, "tank low: water anyway?");
}

static int after_ask(const void *s)
{
    const plant_state_t *st = s;
    return st->answer == ANSWER_YES ? WATER : TLG_END;
}

static void water(void *s, tlg_run_t *run)
{
    (void)run;
    plant_state_t *st = s;
    st->watered = 1;
    st->tank -= 0.05f;
    printf("    water: pump on (tank now %.2f)\n", st->tank);
}

static const tlg_node_t NODES[] = {
    [READ]   = { "read",   read_sensors, TLG_EDGE(ASSESS) },
    [ASSESS] = { "assess", NULL,         TLG_ROUTE(assess, WATER, ASK, TLG_END) },
    [ASK]    = { "ask",    ask,          TLG_ROUTE(after_ask, WATER, TLG_END) },
    [WATER]  = { "water",  water,        TLG_FINISH },
};

static const tlg_graph_t GRAPH = { TLG_NODES(NODES), .start = READ };

/* A checkpoint is just the run plus your state, copied as bytes. */
typedef struct {
    tlg_run_t     run;
    plant_state_t state;
} checkpoint_t;

static unsigned char fake_flash[sizeof(checkpoint_t)];

static void scenario(const char *name, float moisture, float tank)
{
    printf("%s\n", name);
    plant_state_t st = { .moisture = moisture, .tank = tank };
    tlg_run_t run;
    tlg_run_init(&run, &GRAPH);

    tlg_status_t rc = tlg_invoke(&GRAPH, &run, &st);
    printf("  -> %s\n", tlg_status_str(rc));
    if (rc != TLG_INTERRUPTED) {
        printf("  watered: %s\n\n", st.watered ? "yes" : "no");
        return;
    }

    printf("  question for owner: \"%s\"\n", run.interrupt_reason);
    checkpoint_t cp = { run, st };
    memcpy(fake_flash, &cp, sizeof cp);
    printf("  checkpoint saved (%zu bytes); simulating a reboot...\n", sizeof cp);

    memset(&run, 0, sizeof run);
    memset(&st, 0, sizeof st);

    memcpy(&cp, fake_flash, sizeof cp);
    run = cp.run;
    st = cp.state;
    printf("  restored; owner answers YES, resuming at '%s'\n", tlg_node_name(&GRAPH, run.current));
    st.answer = ANSWER_YES;

    rc = tlg_invoke(&GRAPH, &run, &st);
    printf("  -> %s\n  watered: %s\n\n", tlg_status_str(rc), st.watered ? "yes" : "no");
}

int main(void)
{
    char err[96];
    if (tlg_validate(&GRAPH, err, sizeof err) < 0) {
        printf("invalid graph: %s\n", err);
        return 1;
    }
    scenario("1. soil is moist", 0.60f, 0.80f);
    scenario("2. soil dry, tank fine", 0.20f, 0.80f);
    scenario("3. soil dry, tank low (needs approval)", 0.20f, 0.10f);
    return 0;
}
