/*
 * The door guard agent: a tiny-langgraph graph using the schema-generated code.
 *
 *   sense -> assess -> reason -> verify -+-> act -> END
 *                                ^       +-> ask (human) -+-> verify   (approved)
 *                                |       |                +-> plan     (denied)
 *                                |       +-> plan -> verify            (LM plan rejected)
 *                                |       +-> fallback -> act           (nothing valid)
 *
 * Platform-independent: tools, logging and the model are passed in, so the
 * same file runs on a PC and on the ESP32.
 */
#ifndef DG_AGENT_H
#define DG_AGENT_H

#include "door_guard_schema.h"
#include "tlg.h"
#include "tlg_lm.h"
#include "tlg_plan.h"
#include "tlg_trace.h"

#ifdef __cplusplus
extern "C" {
#endif

enum { DG_SENSE, DG_ASSESS, DG_REASON, DG_VERIFY, DG_PLAN, DG_ASK, DG_FALLBACK, DG_ACT, DG_N_NODES };

typedef enum { DG_SRC_NONE, DG_SRC_LM, DG_SRC_PLANNER, DG_SRC_FALLBACK } dg_src_t;
typedef enum { DG_ANSWER_NONE, DG_ANSWER_YES, DG_ANSWER_NO } dg_answer_t;

typedef struct {
    /* wiring (set once) */
    tlg_lm_t     *lm;                              /* NULL = no model, planner only */
    tlg_tracer_t *tracer;                          /* NULL = no trace events */
    bool        (*tool)(int action, void *ctx);    /* runs a real action */
    void        (*log)(const char *line, void *ctx);  /* human-readable notes; may be NULL */
    void         *ctx;

    /* set before each run */
    door_guard_inputs_t in;
    dg_answer_t         answer;        /* set by the host when the run is interrupted */

    /* filled in by the graph */
    door_guard_think_t th;
    uint32_t      facts, want, banned;
    uint8_t       plan[TLG_PLAN_MAX_ACTIONS];
    int           plan_len;
    dg_src_t      src;
    float         lm_conf;
    uint32_t      lm_ms;
    tlg_verdict_t verdict;
    char          why[96];
    bool          approved;
    bool          acted_ok;
} dg_agent_t;

extern const tlg_node_t dg_nodes[DG_N_NODES];

/* Fills a graph for this agent. clock is optional (milliseconds). */
void dg_graph(tlg_graph_t *g, dg_agent_t *a);

/* Reset per-run fields; keeps wiring and inputs. */
void dg_begin(dg_agent_t *a);

const char *dg_src_name(dg_src_t s);

#ifdef __cplusplus
}
#endif

#endif
