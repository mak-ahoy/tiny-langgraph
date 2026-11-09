/*
 * Agents on different boards (tiny_langgraph.net): the mailbox, in C. No malloc, no radio: the
 * sketch moves the bytes (ESP-NOW broadcast today) and calls tlg_net_receive / tlg_net_next_out.
 *
 * A board ANNOUNCES the fingerprints of the contracts it serves every few seconds; a peer is
 * online while it's heard from and offers every contract we use on it, with the same fingerprint.
 * A CALL is a request, never a command: it waits in the receiver's inbox until its agent's rules
 * decide and a tool REPLIES (or it's dropped after TLG_NET_KEEP_RUNS runs). The reply reaches
 * the caller's next run.
 *
 * Messages (little-endian; must match encode()/decode() in python/tiny_langgraph/net.py):
 *   0 'T' 1 'L' 2 version (1) 3 type (1 announce, 2 call, 3 reply)
 *   4..7 network id (from NET_KEY: boards with another key ignore us)   8..23 sender name
 *   announce: 24 count, then count x u32 fingerprints
 *   call:     24..39 target name, 40..41 sequence, 42..45 fingerprint, 46 count, 47.. values
 *   reply:    24..39 target name, 40..41 sequence, 42..45 fingerprint, 46 reply
 */
#ifndef TLG_NET_H
#define TLG_NET_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TLG_NET_NAME       16      /* board names, with the 0 */
#define TLG_NET_MSG        250     /* one ESP-NOW message */
#define TLG_NET_CONTRACTS  8       /* served, and used, per board */
#define TLG_NET_PARAMS     4
#define TLG_NET_QUEUE      4       /* requests waiting per served contract */
#define TLG_NET_PEERS      8       /* boards remembered */
#define TLG_NET_OUT        8       /* messages waiting to be sent */
#define TLG_NET_KEEP_RUNS  3       /* runs a request waits for a reply before it's dropped */
#define TLG_NET_ONLINE_MS  15000   /* a peer is online while heard from within this */
#define TLG_NET_ANNOUNCE_MS 5000   /* how often to announce */

enum { TLG_NET_ANNOUNCE = 1, TLG_NET_CALL = 2, TLG_NET_REPLY = 3 };

typedef struct { uint32_t fingerprint; uint8_t n_params; } tlg_net_served_t;  /* contracts this board offers */
typedef struct { uint8_t peer; uint32_t fingerprint; } tlg_net_used_t;         /* contracts it calls, on which peer */

typedef struct {
    char     from[TLG_NET_NAME];
    uint16_t seq;
    uint8_t  values[TLG_NET_PARAMS];
    uint8_t  runs;                           /* receiver runs that saw it without replying */
} tlg_net_request_t;

typedef struct {
    char     name[TLG_NET_NAME];
    uint32_t last_ms;                        /* when we last heard its announcement */
    uint8_t  n_fp;
    uint32_t fp[TLG_NET_CONTRACTS * 2];      /* the contracts it serves */
} tlg_net_peer_t;

typedef struct {
    /* fixed by the generated agent (tlg_net_init) */
    const char             *name;
    const tlg_net_served_t *served;
    int                     n_served;
    const tlg_net_used_t   *used;
    int                     n_used;
    const char *const      *peer_names;
    int                     n_peer_names;
    uint32_t                net_id;          /* tlg_net_id(NET_KEY), set by the sketch */
    void                  (*lock)(void);     /* the sketch's lock, if the radio and the agent run in */
    void                  (*unlock)(void);   /* different tasks (ESP32); NULL = no locking */

    /* the mailbox */
    uint32_t          now_ms;                /* updated by tlg_net_tick and tlg_net_receive */
    tlg_net_request_t inbox[TLG_NET_CONTRACTS][TLG_NET_QUEUE];
    uint8_t           inbox_len[TLG_NET_CONTRACTS];
    bool              answering[TLG_NET_CONTRACTS];    /* this run handles inbox[c][0] */
    uint8_t           reply[TLG_NET_CONTRACTS][2];     /* per used contract: queued answers (index + 1) */
    uint8_t           reply_len[TLG_NET_CONTRACTS];
    uint16_t          sent_seq[TLG_NET_CONTRACTS];     /* the last call's sequence, per used contract */
    tlg_net_peer_t    peers[TLG_NET_PEERS];
    uint8_t           out[TLG_NET_OUT][TLG_NET_MSG];
    uint8_t           out_len[TLG_NET_OUT];
    uint8_t           out_head, out_count;
    uint16_t          seq;
    uint32_t          dropped;               /* messages that didn't fit a queue */
    uint16_t          local_seq[TLG_NET_CONTRACTS];    /* calls from this board's own web/MCP endpoint: */
    uint8_t           local_reply[TLG_NET_CONTRACTS];  /* their answers stay here (index + 1), not radioed */
} tlg_net_t;

/* FNV-1a of the network key: boards with different keys ignore each other's messages. */
uint32_t tlg_net_id(const char *key);

void tlg_net_init(tlg_net_t *n, const char *name, const tlg_net_served_t *served, int n_served,
                  const tlg_net_used_t *used, int n_used, const char *const *peer_names, int n_peer_names);
void tlg_net_tick(tlg_net_t *n, uint32_t now_ms);

/* ---- the radio side (the sketch) ---- */
int  tlg_net_announce(tlg_net_t *n, uint8_t *buf, size_t size);         /* bytes to broadcast */
void tlg_net_receive(tlg_net_t *n, const uint8_t *msg, size_t len, uint32_t now_ms);
int  tlg_net_next_out(tlg_net_t *n, uint8_t *buf, size_t size);         /* a queued message, or 0 */

/* ---- the agent side (the generated mailbox node and tools) ---- */
/* The oldest request for served contract c, now being answered (counts the run; drops it after
 * TLG_NET_KEEP_RUNS). Copies its values and sender. Returns false if none. */
bool    tlg_net_take_request(tlg_net_t *n, int c, uint8_t *values, char *from);
bool    tlg_net_online(tlg_net_t *n, int peer);              /* heard recently, its contracts match ours */
uint8_t tlg_net_take_reply(tlg_net_t *n, int u);             /* 0 none, else 1 + the reply's index */
bool    tlg_net_call(tlg_net_t *n, int u, const uint8_t *values, int count);   /* queue a call */
bool    tlg_net_reply(tlg_net_t *n, int c, uint8_t reply);   /* answer this run's request on c */

/* ---- calls that arrive over HTTP (the MCP endpoint): the same inbox, the same rules ---- */
/* Queue a request on served contract c from "@mcp"; *seq identifies it. False if the inbox is full. */
bool    tlg_net_local_call(tlg_net_t *n, int c, const uint8_t *values, int count, uint16_t *seq);
/* Its answer once the agent replied: 0 none yet, else 1 + the reply's index. */
uint8_t tlg_net_local_result(tlg_net_t *n, int c, uint16_t seq);

/* ---- the message format (also used by tests) ---- */
int  tlg_net_encode(uint8_t *buf, size_t size, int type, uint32_t net_id, const char *sender,
                    const char *target, uint16_t seq, uint32_t fp, const uint8_t *values, int count);

#ifdef __cplusplus
}
#endif

#endif /* TLG_NET_H */
