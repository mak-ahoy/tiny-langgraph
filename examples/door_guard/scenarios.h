/* Demo situations, shared by the PC program and the board. */
#ifndef DG_SCENARIOS_H
#define DG_SCENARIOS_H

#include "door_guard_schema.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    const char          *label;
    door_guard_inputs_t  in;
    const char          *answers;   /* for the PC demo: what the "human" says to each approval ('y'/'n') */
} dg_scenario_t;

extern const dg_scenario_t dg_scenarios[];
extern const int           dg_n_scenarios;

#ifdef __cplusplus
}
#endif

#endif
