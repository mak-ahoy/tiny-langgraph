#include "tlg_net.h"

#include <string.h>

#define LOCK(n)   do { if ((n)->lock) (n)->lock(); } while (0)
#define UNLOCK(n) do { if ((n)->unlock) (n)->unlock(); } while (0)

uint32_t tlg_net_id(const char *key)
{
    uint32_t h = 2166136261u;
    for (const unsigned char *p = (const unsigned char *)key; *p; p++) h = (h ^ *p) * 16777619u;
    return h;
}

void tlg_net_init(tlg_net_t *n, const char *name, const tlg_net_served_t *served, int n_served,
                  const tlg_net_used_t *used, int n_used, const char *const *peer_names, int n_peer_names)
{
    memset(n, 0, sizeof *n);
    n->name = name;
    n->served = served;
    n->n_served = n_served;
    n->used = used;
    n->n_used = n_used;
    n->peer_names = peer_names;
    n->n_peer_names = n_peer_names;
    n->net_id = tlg_net_id("tiny-langgraph");
}

void tlg_net_tick(tlg_net_t *n, uint32_t now_ms) { n->now_ms = now_ms; }

/* ---------------------------------------------------------------- the message format */
static void put32(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24); }
static uint32_t get32(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }

static void put_name(uint8_t *p, const char *s)
{
    memset(p, 0, TLG_NET_NAME);
    for (int i = 0; i < TLG_NET_NAME - 1 && s && s[i]; i++) p[i] = (uint8_t)s[i];
}

static void get_name(char *out, const uint8_t *p)
{
    memcpy(out, p, TLG_NET_NAME);
    out[TLG_NET_NAME - 1] = '\0';
}

int tlg_net_encode(uint8_t *buf, size_t size, int type, uint32_t net_id, const char *sender,
                   const char *target, uint16_t seq, uint32_t fp, const uint8_t *values, int count)
{
    size_t need = type == TLG_NET_ANNOUNCE ? 25u + 4u * (size_t)count : type == TLG_NET_CALL ? 47u + (size_t)count : 47u;
    if (need > size || need > TLG_NET_MSG || count < 0) return 0;
    buf[0] = 'T'; buf[1] = 'L'; buf[2] = 1; buf[3] = (uint8_t)type;
    put32(buf + 4, net_id);
    put_name(buf + 8, sender);
    if (type == TLG_NET_ANNOUNCE) {               /* values: count fingerprints, little-endian */
        buf[24] = (uint8_t)count;
        memcpy(buf + 25, values, 4u * (size_t)count);
        return (int)need;
    }
    put_name(buf + 24, target);
    buf[40] = (uint8_t)seq; buf[41] = (uint8_t)(seq >> 8);
    put32(buf + 42, fp);
    if (type == TLG_NET_CALL) {
        buf[46] = (uint8_t)count;
        memcpy(buf + 47, values, (size_t)count);
    } else {
        buf[46] = values ? values[0] : 0;
    }
    return (int)need;
}

/* ---------------------------------------------------------------- the radio side */
int tlg_net_announce(tlg_net_t *n, uint8_t *buf, size_t size)
{
    uint8_t fps[4 * TLG_NET_CONTRACTS];
    for (int i = 0; i < n->n_served; i++) put32(fps + 4 * i, n->served[i].fingerprint);
    return tlg_net_encode(buf, size, TLG_NET_ANNOUNCE, n->net_id, n->name, NULL, 0, 0, fps, n->n_served);
}

static tlg_net_peer_t *peer_slot(tlg_net_t *n, const char *name)
{
    tlg_net_peer_t *free_slot = NULL, *oldest = &n->peers[0];
    for (int i = 0; i < TLG_NET_PEERS; i++) {
        tlg_net_peer_t *p = &n->peers[i];
        if (p->name[0] && strcmp(p->name, name) == 0) return p;
        if (!p->name[0] && !free_slot) free_slot = p;
        if (p->last_ms < oldest->last_ms) oldest = p;
    }
    return free_slot ? free_slot : oldest;           /* forget the board heard from longest ago */
}

static void queue_out(tlg_net_t *n, const uint8_t *msg, int len)
{
    if (len <= 0) return;
    if (n->out_count == TLG_NET_OUT) { n->dropped++; return; }
    int slot = (n->out_head + n->out_count) % TLG_NET_OUT;
    memcpy(n->out[slot], msg, (size_t)len);
    n->out_len[slot] = (uint8_t)len;
    n->out_count++;
}

void tlg_net_receive(tlg_net_t *n, const uint8_t *msg, size_t len, uint32_t now_ms)
{
    if (len < 25 || msg[0] != 'T' || msg[1] != 'L' || msg[2] != 1) return;
    LOCK(n);
    n->now_ms = now_ms;
    if (get32(msg + 4) != n->net_id) { UNLOCK(n); return; }             /* another network */
    char sender[TLG_NET_NAME];
    get_name(sender, msg + 8);
    if (msg[3] == TLG_NET_ANNOUNCE) {
        int count = msg[24];
        if (count <= TLG_NET_CONTRACTS * 2 && len >= 25u + 4u * (size_t)count && strcmp(sender, n->name) != 0) {
            tlg_net_peer_t *p = peer_slot(n, sender);
            memcpy(p->name, sender, TLG_NET_NAME);
            p->last_ms = now_ms;
            p->n_fp = (uint8_t)count;
            for (int i = 0; i < count; i++) p->fp[i] = get32(msg + 25 + 4 * i);
        }
        UNLOCK(n);
        return;
    }
    char target[TLG_NET_NAME];
    if (len < 47) { UNLOCK(n); return; }
    get_name(target, msg + 24);
    if (strcmp(target, n->name) != 0) { UNLOCK(n); return; }            /* not for us */
    uint16_t seq = (uint16_t)(msg[40] | msg[41] << 8);
    uint32_t fp = get32(msg + 42);
    if (msg[3] == TLG_NET_CALL) {
        int count = msg[46];
        for (int c = 0; c < n->n_served; c++) {
            if (n->served[c].fingerprint != fp) continue;
            if (count != n->served[c].n_params || len < 47u + (size_t)count) break;
            if (n->inbox_len[c] == TLG_NET_QUEUE) { n->dropped++; break; }
            tlg_net_request_t *r = &n->inbox[c][n->inbox_len[c]++];
            memcpy(r->from, sender, TLG_NET_NAME);
            r->seq = seq;
            memcpy(r->values, msg + 47, (size_t)count);
            r->runs = 0;
            break;
        }
    } else if (msg[3] == TLG_NET_REPLY) {
        for (int u = 0; u < n->n_used; u++) {
            if (n->used[u].fingerprint != fp || n->sent_seq[u] != seq) continue;
            if (strcmp(sender, n->peer_names[n->used[u].peer]) != 0) continue;
            if (n->reply_len[u] < 2) n->reply[u][n->reply_len[u]++] = (uint8_t)(msg[46] + 1);
            break;
        }
    }
    UNLOCK(n);
}

int tlg_net_next_out(tlg_net_t *n, uint8_t *buf, size_t size)
{
    LOCK(n);
    int len = 0;
    if (n->out_count && n->out_len[n->out_head] <= size) {
        len = n->out_len[n->out_head];
        memcpy(buf, n->out[n->out_head], (size_t)len);
        n->out_head = (uint8_t)((n->out_head + 1) % TLG_NET_OUT);
        n->out_count--;
    }
    UNLOCK(n);
    return len;
}

/* ---------------------------------------------------------------- the agent side */
static void pop_request(tlg_net_t *n, int c)
{
    memmove(&n->inbox[c][0], &n->inbox[c][1], sizeof n->inbox[c][0] * (size_t)(n->inbox_len[c] - 1));
    n->inbox_len[c]--;
}

bool tlg_net_take_request(tlg_net_t *n, int c, uint8_t *values, char *from)
{
    LOCK(n);
    while (n->inbox_len[c] && n->inbox[c][0].runs >= TLG_NET_KEEP_RUNS) pop_request(n, c);   /* nobody answered */
    bool got = n->inbox_len[c] > 0;
    n->answering[c] = got;
    if (got) {
        tlg_net_request_t *r = &n->inbox[c][0];
        r->runs++;
        memcpy(values, r->values, TLG_NET_PARAMS);
        memcpy(from, r->from, TLG_NET_NAME);
    }
    UNLOCK(n);
    return got;
}

bool tlg_net_online(tlg_net_t *n, int peer)
{
    LOCK(n);
    bool ok = false;
    for (int i = 0; i < TLG_NET_PEERS && !ok; i++) {
        tlg_net_peer_t *p = &n->peers[i];
        if (!p->name[0] || strcmp(p->name, n->peer_names[peer]) != 0) continue;
        if (n->now_ms - p->last_ms > TLG_NET_ONLINE_MS) break;
        ok = true;
        for (int u = 0; u < n->n_used && ok; u++) {            /* it must offer every contract we use on it */
            if (n->used[u].peer != peer) continue;
            bool offered = false;
            for (int k = 0; k < p->n_fp; k++) offered |= p->fp[k] == n->used[u].fingerprint;
            ok = offered;
        }
    }
    UNLOCK(n);
    return ok;
}

uint8_t tlg_net_take_reply(tlg_net_t *n, int u)
{
    LOCK(n);
    uint8_t r = 0;
    if (n->reply_len[u]) {
        r = n->reply[u][0];
        n->reply[u][0] = n->reply[u][1];
        n->reply_len[u]--;
    }
    UNLOCK(n);
    return r;
}

bool tlg_net_call(tlg_net_t *n, int u, const uint8_t *values, int count)
{
    uint8_t msg[TLG_NET_MSG];
    LOCK(n);
    uint16_t seq = ++n->seq;
    n->sent_seq[u] = seq;
    int len = tlg_net_encode(msg, sizeof msg, TLG_NET_CALL, n->net_id, n->name, n->peer_names[n->used[u].peer],
                             seq, n->used[u].fingerprint, values, count);
    bool ok = len > 0 && n->out_count < TLG_NET_OUT;
    queue_out(n, msg, len);
    UNLOCK(n);
    return ok;
}

bool tlg_net_local_call(tlg_net_t *n, int c, const uint8_t *values, int count, uint16_t *seq)
{
    LOCK(n);
    if (c < 0 || c >= n->n_served || count != n->served[c].n_params || n->inbox_len[c] == TLG_NET_QUEUE) {
        UNLOCK(n);
        return false;
    }
    tlg_net_request_t *r = &n->inbox[c][n->inbox_len[c]++];
    memset(r, 0, sizeof *r);
    memcpy(r->from, "@mcp", 5);
    r->seq = ++n->seq;
    memcpy(r->values, values, (size_t)count);
    *seq = r->seq;
    UNLOCK(n);
    return true;
}

uint8_t tlg_net_local_result(tlg_net_t *n, int c, uint16_t seq)
{
    LOCK(n);
    uint8_t r = n->local_seq[c] == seq ? n->local_reply[c] : 0;
    UNLOCK(n);
    return r;
}

bool tlg_net_reply(tlg_net_t *n, int c, uint8_t reply)
{
    uint8_t msg[TLG_NET_MSG];
    LOCK(n);
    if (!n->answering[c] || !n->inbox_len[c]) { UNLOCK(n); return false; }
    tlg_net_request_t *r = &n->inbox[c][0];
    if (r->from[0] == '@') {                         /* asked over HTTP: keep the answer here */
        n->local_seq[c] = r->seq;
        n->local_reply[c] = (uint8_t)(reply + 1);
        pop_request(n, c);
        n->answering[c] = false;
        UNLOCK(n);
        return true;
    }
    int len = tlg_net_encode(msg, sizeof msg, TLG_NET_REPLY, n->net_id, n->name, r->from, r->seq,
                             n->served[c].fingerprint, &reply, 1);
    bool ok = len > 0 && n->out_count < TLG_NET_OUT;
    queue_out(n, msg, len);
    pop_request(n, c);
    n->answering[c] = false;
    UNLOCK(n);
    return ok;
}
