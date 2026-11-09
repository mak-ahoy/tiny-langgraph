/*
 * Bookkeeping shared by the prebuilt agent nodes (reason, verify, plan, ask, act)
 * that tiny-langgraph generates from a Python agent. One tlg_agent_t lives in the
 * agent's state struct.
 */
#ifndef TLG_AGENT_H
#define TLG_AGENT_H

#include "tlg.h"
#include "tlg_plan.h"
#include "tlg_trace.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { TLG_SRC_NONE, TLG_SRC_LM, TLG_SRC_PLANNER } tlg_src_t;
typedef enum { TLG_ANSWER_NONE, TLG_ANSWER_YES, TLG_ANSWER_NO } tlg_answer_t;

typedef struct {
    /* wiring (set once) */
    void         *lm;                                 /* a tlg_lm_t *, or NULL: planner only */
    tlg_tracer_t *tracer;                             /* NULL = no trace events */
    void        (*log)(const char *line, void *ctx);  /* readable notes; may be NULL */
    void         *ctx;
    uint32_t    (*clock_us)(void);                    /* microseconds, for timing; may be NULL */

    /* set by the host when a run paused for approval */
    tlg_answer_t  answer;

    /* filled in during a run */
    uint32_t      facts, want, banned;
    uint8_t       plan[TLG_PLAN_MAX_ACTIONS];
    int           plan_len;
    tlg_src_t     src;
    float         lm_conf;
    uint32_t      lm_ms;
    uint32_t      plan_us, verify_us;                 /* summed over the run (verify can run twice) */
    tlg_verdict_t verdict;
    char          why[96];
    bool          approved;
    bool          acted_ok;
} tlg_agent_t;

/* Reset the per-run fields; keeps the wiring and a pending answer. */
void        tlg_agent_begin(tlg_agent_t *a);

/* A note for the trace (viewer) and the readable log. */
void        tlg_agent_say(tlg_agent_t *a, const char *fmt, ...)
#if defined(__GNUC__)
    __attribute__((format(printf, 2, 3)))
#endif
    ;

const char *tlg_agent_src_name(tlg_src_t s);

/* Microseconds from clock_us, or 0 without one. */
static inline uint32_t tlg_agent_us(const tlg_agent_t *a) { return a->clock_us ? a->clock_us() : 0; }

#ifdef __cplusplus
}
#endif

#endif /* TLG_AGENT_H */
