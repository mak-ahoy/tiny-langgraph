/*
 * Event hub: keeps recent trace lines so a web page (or any other client)
 * can fetch them later, starting wherever it left off.
 *
 *   - "graph" and "node" lines are kept separately and always sent to a new
 *     client, so a page opened hours later still knows the graph;
 *   - everything else goes into a ring of fixed-size slots, numbered 1, 2, 3...;
 *   - a client asks for "everything after number N" and remembers the last
 *     number it received.
 *
 * No locking inside: call it from one task, or wrap calls in a mutex.
 * No heap: you hand it the memory (on an ESP32 with PSRAM, use ps_malloc).
 */
#ifndef TLG_HUB_H
#define TLG_HUB_H

#include <stddef.h>
#include <stdint.h>

#include "tlg_trace.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TLG_HUB_STICKY_MAX 3072

typedef struct {
    uint32_t seq;
    uint16_t len;
    char     text[TLG_TRACE_LINE_MAX];
} tlg_hub_slot_t;

typedef struct {
    tlg_hub_slot_t *slots;
    uint32_t        n_slots;
    uint32_t        next_seq;            /* number the next line will get (starts at 1) */
    char            sticky[TLG_HUB_STICKY_MAX];
    size_t          sticky_len;
} tlg_hub_t;

/* Memory for N slots: N * sizeof(tlg_hub_slot_t). Returns the number of slots (0 if too small). */
uint32_t tlg_hub_init(tlg_hub_t *hub, void *mem, size_t mem_size);

/* Store one complete line (as written by the tracer, ending in '\n'). */
void     tlg_hub_put(tlg_hub_t *hub, const char *line);

/*
 * Copy lines numbered after `since` into out (as many whole lines as fit).
 * with_graph: a new client, so the graph lines come first (use since = 0 too).
 * *last gets the number of the last line copied (or stays `since` if none).
 * Returns bytes written; *more is set to 1 if lines were left out for lack of space.
 */
size_t   tlg_hub_get(const tlg_hub_t *hub, uint32_t since, int with_graph, char *out, size_t out_size,
                     uint32_t *last, int *more);

/* Oldest line number still kept (lines before it were overwritten). */
uint32_t tlg_hub_oldest(const tlg_hub_t *hub);

/* A tlg_trace_write_fn that stores into the hub passed as ctx. */
void     tlg_hub_write(const char *line, void *hub);

#ifdef __cplusplus
}
#endif

#endif /* TLG_HUB_H */
