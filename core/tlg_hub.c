#include "tlg_hub.h"

#include <string.h>

uint32_t tlg_hub_init(tlg_hub_t *hub, void *mem, size_t mem_size)
{
    memset(hub, 0, sizeof *hub);
    hub->slots = mem;
    hub->n_slots = (uint32_t)(mem_size / sizeof(tlg_hub_slot_t));
    hub->next_seq = 1;
    if (hub->n_slots == 0) hub->slots = NULL;
    return hub->n_slots;
}

static int is_kind(const char *line, const char *kind)
{
    /* the tracer always writes  @tlg {"k":"<kind>",... */
    const char *p = strstr(line, "{\"k\":\"");
    if (!p) return 0;
    p += 6;
    size_t n = strlen(kind);
    return strncmp(p, kind, n) == 0 && p[n] == '"';
}

void tlg_hub_put(tlg_hub_t *hub, const char *line)
{
    size_t len = strlen(line);
    if (len == 0) return;

    if (is_kind(line, "graph")) hub->sticky_len = 0;      /* a new graph replaces the old */
    if (is_kind(line, "graph") || is_kind(line, "node")) {
        if (hub->sticky_len + len < TLG_HUB_STICKY_MAX) {
            memcpy(hub->sticky + hub->sticky_len, line, len);
            hub->sticky_len += len;
        }
        return;
    }
    if (!hub->slots) return;

    tlg_hub_slot_t *s = &hub->slots[hub->next_seq % hub->n_slots];
    if (len >= sizeof s->text) len = sizeof s->text - 1;   /* tracer lines always fit */
    memcpy(s->text, line, len);
    s->len = (uint16_t)len;
    s->seq = hub->next_seq++;
}

uint32_t tlg_hub_oldest(const tlg_hub_t *hub)
{
    if (hub->next_seq <= hub->n_slots) return 1;
    return hub->next_seq - hub->n_slots;
}

size_t tlg_hub_get(const tlg_hub_t *hub, uint32_t since, int with_graph, char *out, size_t size,
                   uint32_t *last, int *more)
{
    size_t used = 0;
    *last = since;
    *more = 0;
    if (size == 0) return 0;

    if (with_graph) {
        if (hub->sticky_len < size) {
            memcpy(out, hub->sticky, hub->sticky_len);
            used = hub->sticky_len;
        } else {
            *more = 1;
        }
    }

    uint32_t first = since + 1;
    uint32_t oldest = tlg_hub_oldest(hub);
    if (first < oldest) first = oldest;     /* the client fell behind: skip what's gone */

    for (uint32_t seq = first; hub->slots && seq < hub->next_seq; seq++) {
        const tlg_hub_slot_t *s = &hub->slots[seq % hub->n_slots];
        if (s->seq != seq) continue;
        if (used + s->len >= size) { *more = 1; break; }
        memcpy(out + used, s->text, s->len);
        used += s->len;
        *last = seq;
    }
    out[used < size ? used : size - 1] = '\0';
    return used;
}

void tlg_hub_write(const char *line, void *hub)
{
    tlg_hub_put((tlg_hub_t *)hub, line);
}
