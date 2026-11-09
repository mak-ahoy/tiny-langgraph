#include "agent.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* A note goes to the trace (for the viewer) and to the human log. */
static void say(dg_agent_t *a, const char *fmt, ...)
{
    char text[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);
    if (a->tracer) tlg_trace_note(a->tracer, "%s", text);
    if (a->log) a->log(text, a->ctx);
}

static void tokens_str(const uint8_t *t, int n, char *buf, size_t size)
{
    size_t used = 0;
    buf[0] = '\0';
    for (int i = 0; i < n; i++) {
        int w = snprintf(buf + used, size - used, "%s%s", i ? " " : "", door_guard_token_names[t[i]]);
        if (w < 0 || (size_t)w >= size - used) return;
        used += (size_t)w;
    }
}

static const char *const LEVEL[] = { "lo", "mid", "hi" };

const char *dg_src_name(dg_src_t s)
{
    static const char *const N[] = { "none", "lm", "planner", "fallback" };
    return N[s];
}

/* ---------------- nodes ---------------- */

static void sense(void *s, tlg_run_t *run)
{
    (void)run;
    dg_agent_t *a = s;
    uint8_t t[DOOR_GUARD_PROMPT_LEN];
    char buf[200];
    int n = door_guard_encode(&a->in, t, sizeof t);
    if (n < 0) { say(a, "inputs out of range"); return; }
    tokens_str(t + 1, n - 2, buf, sizeof buf);   /* skip <bos> and <think> */
    say(a, "inputs: %s", buf);
}

static void assess(void *s, tlg_run_t *run)
{
    (void)run;
    dg_agent_t *a = s;
    char buf[128];
    door_guard_think(&a->in, &a->th);            /* the rules from schema.yaml, generated */
    a->facts = door_guard_facts(&a->in, &a->th);
    a->want = door_guard_want(&a->in, &a->th);
    tlg_outcomes_str(&door_guard_domain, a->want, buf, sizeof buf);
    say(a, "assess: visitor=%s threat=%s", LEVEL[a->th.visitor], LEVEL[a->th.threat]);
    say(a, "want: %s", buf);
}

static void reason(void *s, tlg_run_t *run)
{
    (void)run;
    dg_agent_t *a = s;
    a->plan_len = 0;
    a->src = DG_SRC_NONE;
    if (!a->lm) { say(a, "no model loaded: planner will decide"); return; }

    uint8_t prompt[DOOR_GUARD_PROMPT_LEN], out[DOOR_GUARD_SEQ_LEN];
    int pn = door_guard_encode(&a->in, prompt, sizeof prompt);
    uint32_t t0 = a->tracer && a->tracer->clock ? a->tracer->clock() : 0;
    int n = tlg_lm_generate(a->lm, prompt, pn, DOOR_GUARD_TOK_EOS, out,
                            DOOR_GUARD_SEQ_LEN - pn, &a->lm_conf);
    a->lm_ms = (a->tracer && a->tracer->clock ? a->tracer->clock() : 0) - t0;

    char buf[200];
    tokens_str(out, n, buf, sizeof buf);
    say(a, "model: %s", buf);

    door_guard_think_t lm_th;
    int len = door_guard_decode(out, n, &lm_th, a->plan, TLG_PLAN_MAX_ACTIONS);
    if (len < 0) {
        say(a, "model answer is malformed: discarded");
        return;
    }
    a->plan_len = len;
    a->src = DG_SRC_LM;
    if (lm_th.threat != a->th.threat || lm_th.visitor != a->th.visitor)
        say(a, "model disagrees with the device: it thinks visitor=%s threat=%s",
            LEVEL[lm_th.visitor], LEVEL[lm_th.threat]);
    say(a, "confidence %.2f, %lu ms", a->lm_conf, (unsigned long)a->lm_ms);
}

static void verify(void *s, tlg_run_t *run)
{
    (void)run;
    dg_agent_t *a = s;
    char plan[160];
    tlg_plan_str(&door_guard_domain, a->plan, a->plan_len, plan, sizeof plan);
    if (a->src == DG_SRC_NONE) {
        a->verdict = TLG_VERIFY_FAIL;
        snprintf(a->why, sizeof a->why, "no plan");
    } else {
        a->verdict = tlg_verify(&door_guard_domain, a->facts, a->want, a->banned,
                                a->plan, a->plan_len, a->approved, a->why, sizeof a->why);
    }
    say(a, "check %s plan [%s]: %s", dg_src_name(a->src), plan, a->why);
}

static int after_verify(const void *s)
{
    const dg_agent_t *a = s;
    if (a->verdict == TLG_VERIFY_OK) return DG_ACT;
    if (a->verdict == TLG_VERIFY_NEEDS_APPROVAL) return DG_ASK;
    return a->src == DG_SRC_PLANNER ? DG_FALLBACK : DG_PLAN;
}

static void plan(void *s, tlg_run_t *run)
{
    (void)run;
    dg_agent_t *a = s;
    tlg_plan_t p;
    if (tlg_plan(&door_guard_domain, a->facts, a->want, a->banned, &p) != 0) {
        a->src = DG_SRC_NONE;
        say(a, "planner failed: domain too large");
        return;
    }
    memcpy(a->plan, p.actions, (size_t)p.len);
    a->plan_len = p.len;
    a->src = DG_SRC_PLANNER;
    a->approved = false;

    char buf[160];
    tlg_plan_str(&door_guard_domain, a->plan, a->plan_len, buf, sizeof buf);
    say(a, "planner: %s (cost %u)", buf, (unsigned)p.cost);
    uint32_t missing = a->want & ~p.achieved;
    if (missing) {
        tlg_outcomes_str(&door_guard_domain, missing, buf, sizeof buf);
        say(a, "cannot achieve %s right now", buf);
    }
}

/* Pauses the run until the host sets a->answer, then runs again. */
static void ask(void *s, tlg_run_t *run)
{
    dg_agent_t *a = s;
    char names[38] = "";      /* "approve " + names + "?" must fit tlg_run_t.interrupt_reason (48) */
    size_t used = 0;
    for (int i = 0; i < a->plan_len; i++) {
        const tlg_action_t *ac = &door_guard_domain.actions[a->plan[i]];
        if (!ac->approval) continue;
        int w = snprintf(names + used, sizeof names - used, "%s%s", used ? "+" : "", ac->name);
        if (w > 0 && (size_t)w < sizeof names - used) used += (size_t)w;
    }

    if (a->answer == DG_ANSWER_NONE) {
        char q[48];
        snprintf(q, sizeof q, "approve %s?", names);
        say(a, "waiting for a human: %s", q);
        tlg_interrupt(run, q);
        return;
    }
    if (a->answer == DG_ANSWER_YES) {
        a->approved = true;
        say(a, "human approved %s", names);
    } else {
        for (int i = 0; i < a->plan_len; i++)
            if (door_guard_domain.actions[a->plan[i]].approval) a->banned |= 1u << a->plan[i];
        a->approved = false;
        a->plan_len = 0;
        say(a, "human said no to %s: replanning without it", names);
    }
    a->answer = DG_ANSWER_NONE;   /* the next request needs a fresh answer */
}

static int after_ask(const void *s)
{
    return ((const dg_agent_t *)s)->approved ? DG_VERIFY : DG_PLAN;
}

static void fallback(void *s, tlg_run_t *run)
{
    (void)run;
    dg_agent_t *a = s;
    a->plan[0] = DOOR_GUARD_ACT_LOG;
    a->plan_len = 1;
    a->src = DG_SRC_FALLBACK;
    say(a, "nothing valid: safe fallback, log only");
}

static void act(void *s, tlg_run_t *run)
{
    (void)run;
    dg_agent_t *a = s;
    a->acted_ok = true;
    for (int i = 0; i < a->plan_len; i++) {
        bool ok = a->tool ? a->tool(a->plan[i], a->ctx) : true;
        say(a, "act: %s %s", door_guard_domain.actions[a->plan[i]].name, ok ? "ok" : "FAILED");
        if (!ok) { a->acted_ok = false; break; }
    }
}

const tlg_node_t dg_nodes[DG_N_NODES] = {
    [DG_SENSE]    = { "sense",    sense,    TLG_EDGE(DG_ASSESS) },
    [DG_ASSESS]   = { "assess",   assess,   TLG_EDGE(DG_REASON) },
    [DG_REASON]   = { "reason",   reason,   TLG_EDGE(DG_VERIFY) },
    [DG_VERIFY]   = { "verify",   verify,   TLG_ROUTE(after_verify, DG_ACT, DG_ASK, DG_PLAN, DG_FALLBACK) },
    [DG_PLAN]     = { "plan",     plan,     TLG_EDGE(DG_VERIFY) },
    [DG_ASK]      = { "ask",      ask,      TLG_ROUTE(after_ask, DG_VERIFY, DG_PLAN) },
    [DG_FALLBACK] = { "fallback", fallback, TLG_EDGE(DG_ACT) },
    [DG_ACT]      = { "act",      act,      TLG_FINISH },
};

void dg_graph(tlg_graph_t *g, dg_agent_t *a)
{
    memset(g, 0, sizeof *g);
    g->nodes = dg_nodes;
    g->n_nodes = DG_N_NODES;
    g->start = DG_SENSE;
    g->max_steps = 32;
    if (a->tracer) {
        g->on_step = tlg_trace_on_step;
        g->user = a->tracer;
    }
}

void dg_begin(dg_agent_t *a)
{
    memset(&a->th, 0, sizeof a->th);
    a->facts = a->want = a->banned = 0;
    a->plan_len = 0;
    a->src = DG_SRC_NONE;
    a->lm_conf = 0.0f;
    a->lm_ms = 0;
    a->verdict = TLG_VERIFY_FAIL;
    a->why[0] = '\0';
    a->approved = false;
    a->acted_ok = false;
    a->answer = DG_ANSWER_NONE;
}
