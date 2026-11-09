/*
 * Planner + verifier for schema-defined actions.
 *
 * Actions have preconditions (facts that must hold), ordering (outcomes that
 * must already be achieved) and effects (outcomes they achieve). The planner
 * finds the cheapest plan for what you want; the verifier checks any plan,
 * from any source (a tiny LM, a remote LLM, a human), before it runs.
 *
 * The tables are normally generated from schema.yaml by tools/tlg.py.
 */
#ifndef TLG_PLAN_H
#define TLG_PLAN_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TLG_PLAN_MAX_OUTCOMES 32     /* up to 8: a full search of 2^outcomes states (on the stack);
                                        more: a pruned search in a workspace you hand over with
                                        tlg_plan_workspace(), or greedy if it doesn't fit */
#define TLG_PLAN_SMALL_OUTCOMES 8
#define TLG_PLAN_STATE_BYTES 22      /* workspace per state of the pruned search */
#define TLG_PLAN_WORKSPACE_BYTES(states) ((size_t)(states) * TLG_PLAN_STATE_BYTES)
#define TLG_PLAN_MAX_ACTIONS  32

typedef struct {
    const char *name;
    uint32_t    needs;      /* fact bits that must be set */
    uint32_t    after;      /* outcome bits that must already be achieved */
    uint32_t    achieves;   /* outcome bits this action achieves */
    uint16_t    cost;
    bool        approval;   /* a human must approve before it runs */
} tlg_action_t;

typedef struct {
    const tlg_action_t *actions;
    int                 n_actions;
    const char *const  *outcome_names;
    const uint8_t      *outcome_weights;
    int                 n_outcomes;
    const char *const  *fact_names;
    int                 n_facts;
    uint32_t            only_if_wanted;  /* outcomes a plan may only achieve if wanted */
    uint16_t            budget;          /* maximum total cost of a plan */
} tlg_domain_t;

typedef struct {
    uint8_t  actions[TLG_PLAN_MAX_ACTIONS];   /* action indices, in execution order */
    int      len;
    uint32_t achieved;
    uint16_t cost;
    int      score;     /* sum of weights of wanted outcomes achieved */
    bool     exact;     /* the best plan; false: greedy, because the search didn't fit */
} tlg_plan_t;

/*
 * Cheapest plan that achieves as much of `want` as the facts allow.
 * banned: actions that must not be used (bit per action), e.g. after a human said no.
 * Up to 8 outcomes the search is always exact. With more, it keeps only the reachable sets of
 * the outcomes that matter (wanted, or needed before some action) in the workspace; if a
 * situation needs more states than fit, the plan is greedy (best wanted weight per cost, again
 * and again) and out->exact is false. Returns 0, or -1 if the domain is too large.
 */
int tlg_plan(const tlg_domain_t *d, uint32_t facts, uint32_t want, uint32_t banned, tlg_plan_t *out);

/* Memory for the pruned search (domains with more than 8 outcomes), e.g. PSRAM on an ESP32:
 * TLG_PLAN_WORKSPACE_BYTES(states). 4-byte aligned. Without it such domains plan greedily.
 * The Python twin must use the same number of states (toolchain/planner.py). */
void     tlg_plan_workspace(void *mem, size_t bytes);
uint32_t tlg_plan_capacity(void);           /* states the workspace holds */

typedef enum {
    TLG_VERIFY_OK,
    TLG_VERIFY_NEEDS_APPROVAL,   /* valid, but contains an action a human must approve */
    TLG_VERIFY_FAIL,
} tlg_verdict_t;

/*
 * Check a plan before it runs. Fails if an action is unknown or banned, its
 * facts don't hold, it runs before what it depends on, it achieves an
 * only_if_wanted outcome nobody asked for, it is over budget, it achieves
 * less of `want` than the planner's plan, or the same for a higher cost (so a
 * model's plan is kept only if it's at least as good as what the planner finds;
 * with more than 8 outcomes the planner may be greedy, and a model can beat it).
 * `why` gets a short reason.
 */
tlg_verdict_t tlg_verify(const tlg_domain_t *d, uint32_t facts, uint32_t want, uint32_t banned,
                         const uint8_t *plan, int len, bool approved, char *why, size_t why_size);

int  tlg_plan_score(const tlg_domain_t *d, uint32_t achieved, uint32_t want);

/* "log -> capture -> notify_low" */
void tlg_plan_str(const tlg_domain_t *d, const uint8_t *plan, int len, char *buf, size_t size);

/* "logged,evidence" */
void tlg_outcomes_str(const tlg_domain_t *d, uint32_t mask, char *buf, size_t size);

#ifdef __cplusplus
}
#endif

#endif /* TLG_PLAN_H */
