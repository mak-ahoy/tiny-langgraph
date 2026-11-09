/*
 * The board as an MCP server (Model Context Protocol, "Streamable HTTP": JSON-RPC 2.0 POSTed
 * to /mcp). Outside AI clients list the board's contracts as tools and call them. A call is a
 * REQUEST: it enters the same mailbox as calls from other boards (tlg_net_local_call), the
 * agent's own rules and verifier decide, and its reply is the tool's result.
 *
 * Methods: initialize, notifications (202, no body), ping, tools/list, tools/call.
 * Tools: one per served contract (string parameters with enum values; the replies as the
 * output schema; the fingerprint in _meta.tiny_langgraph), plus <board>_status (read-only:
 * what the board observes now and its last plan). Access: "Authorization: Bearer <key>".
 * Must answer like McpServer.handle() in python/tiny_langgraph/net.py (tests/test_net.py).
 * No malloc: the caller gives the output buffer.
 */
#ifndef TLG_MCP_H
#define TLG_MCP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "tlg_net.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TLG_MCP_VERSION "2025-06-18"     /* the newest protocol version it speaks */

typedef struct {
    const char          *name, *about;
    int                  n_params;
    const char *const   *param_names;      /* [n_params] */
    const uint8_t       *n_values;         /* [n_params] */
    const char *const *const *values;      /* [n_params][n_values[i]] */
    int                  n_replies;
    const char *const   *replies;
    uint32_t             fingerprint;
} tlg_mcp_contract_t;

typedef struct {
    const char               *board;       /* the agent's name */
    const char               *about;       /* one line about the board */
    const tlg_mcp_contract_t *contracts;   /* the contracts it serves, in tlg_net's order */
    int                       n_contracts;
    tlg_net_t                *net;
    const char               *key;         /* clients send "Authorization: Bearer <key>"; "" = no key */
    uint32_t                  wait_ms;     /* how long tools/call waits for the agent's answer */
    bool                    (*run_now)(void *ctx, uint32_t wait_ms);       /* run the agent now; false = busy */
    int                     (*status)(void *ctx, char *buf, size_t size);  /* a JSON object: fields + last plan */
    void                     *ctx;
} tlg_mcp_t;

/* One POST to /mcp. Writes the response body to out and the HTTP status to *http (200 with a
 * body; 202 without one, for notifications; 401 wrong key; 400 not JSON-RPC). Returns its length. */
int tlg_mcp_handle(tlg_mcp_t *m, const char *body, size_t len, const char *authorization,
                   char *out, size_t size, int *http);

#ifdef __cplusplus
}
#endif

#endif /* TLG_MCP_H */
