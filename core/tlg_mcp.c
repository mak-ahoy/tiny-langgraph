#include "tlg_mcp.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* ---------------------------------------------------------------- a tiny JSON reader
 * Enough for MCP requests: find a key in an object, read a string, copy a raw token (the id).
 * p..e is the text; functions return NULL when it isn't what they expect. */
static const char *ws(const char *p, const char *e)
{
    while (p < e && (*p == ' ' || *p == '\n' || *p == '\r' || *p == '\t')) p++;
    return p;
}

static const char *skip_string(const char *p, const char *e)
{
    for (p++; p < e; p++) {
        if (*p == '\\') { p++; continue; }
        if (*p == '"') return p + 1;
    }
    return NULL;
}

static const char *skip_value(const char *p, const char *e)
{
    p = ws(p, e);
    if (p >= e) return NULL;
    if (*p == '"') return skip_string(p, e);
    if (*p == '{' || *p == '[') {
        int depth = 0;
        while (p < e) {
            if (*p == '"') { p = skip_string(p, e); if (!p) return NULL; continue; }
            if (*p == '{' || *p == '[') depth++;
            if (*p == '}' || *p == ']') { if (--depth == 0) return p + 1; }
            p++;
        }
        return NULL;
    }
    while (p < e && *p != ',' && *p != '}' && *p != ']' && *p != ' ' && *p != '\n' && *p != '\r' && *p != '\t') p++;
    return p;
}

/* The value of key in the object at p, or NULL. */
static const char *find(const char *p, const char *e, const char *key)
{
    if (!p) return NULL;
    p = ws(p, e);
    if (p >= e || *p != '{') return NULL;
    p++;
    size_t klen = strlen(key);
    for (;;) {
        p = ws(p, e);
        if (p >= e || *p != '"') return NULL;
        const char *k = p + 1, *after = skip_string(p, e);
        if (!after) return NULL;
        bool match = (size_t)(after - 1 - k) == klen && memcmp(k, key, klen) == 0;
        p = ws(after, e);
        if (p >= e || *p != ':') return NULL;
        p = ws(p + 1, e);
        if (match) return p;
        p = skip_value(p, e);
        if (!p) return NULL;
        p = ws(p, e);
        if (p < e && *p == ',') { p++; continue; }
        return NULL;
    }
}

/* A JSON string's text (simple escapes) into out. */
static bool get_str(const char *v, const char *e, char *out, size_t size)
{
    if (!v || v >= e || *v != '"' || !size) return false;
    size_t n = 0;
    for (v++; v < e && *v != '"'; v++) {
        char ch = *v;
        if (ch == '\\' && v + 1 < e) {
            ch = *++v;
            if (ch == 'n') ch = '\n'; else if (ch == 't') ch = '\t'; else if (ch == 'u') return false;
        }
        if (n + 1 >= size) return false;
        out[n++] = ch;
    }
    out[n] = '\0';
    return v < e;
}

/* ---------------------------------------------------------------- the response writer */
typedef struct { char *buf; size_t size, len; bool full; } out_t;

static void put(out_t *o, const char *fmt, ...)
{
    if (o->full) return;
    va_list ap;
    va_start(ap, fmt);
    int w = vsnprintf(o->buf + o->len, o->size - o->len, fmt, ap);
    va_end(ap);
    if (w < 0 || (size_t)w >= o->size - o->len) { o->full = true; return; }
    o->len += (size_t)w;
}

static void put_str(out_t *o, const char *s)          /* a JSON string, escaped */
{
    put(o, "\"");
    for (; s && *s && !o->full; s++) {
        unsigned char ch = (unsigned char)*s;
        if (ch == '"' || ch == '\\') put(o, "\\%c", ch);
        else if (ch == '\n') put(o, "\\n");
        else if (ch < 32) put(o, "\\u%04x", ch);
        else put(o, "%c", ch);
    }
    put(o, "\"");
}

static void head(out_t *o, const char *id) { put(o, "{\"jsonrpc\":\"2.0\",\"id\":%s,", id); }

static void error(out_t *o, const char *id, int code, const char *msg)
{
    head(o, id);
    put(o, "\"error\":{\"code\":%d,\"message\":", code);
    put_str(o, msg);
    put(o, "}}");
}

static void tool_result(out_t *o, const char *id, const char *text, const char *reply, bool is_error)
{
    head(o, id);
    put(o, "\"result\":{\"content\":[{\"type\":\"text\",\"text\":");
    put_str(o, text);
    put(o, "}]");
    if (reply) {
        put(o, ",\"structuredContent\":{\"reply\":");
        put_str(o, reply);
        put(o, "}");
    }
    put(o, ",\"isError\":%s}}", is_error ? "true" : "false");
}

static void list_tools(tlg_mcp_t *m, out_t *o, const char *id)
{
    head(o, id);
    put(o, "\"result\":{\"tools\":[");
    for (int c = 0; c < m->n_contracts; c++) {
        const tlg_mcp_contract_t *k = &m->contracts[c];
        char about[200];
        snprintf(about, sizeof about, "%s (a request: %s's own rules decide)", k->about && *k->about ? k->about : k->name,
                 m->board);
        put(o, "%s{\"name\":", c ? "," : "");
        put_str(o, k->name);
        put(o, ",\"description\":");
        put_str(o, about);
        put(o, ",\"inputSchema\":{\"type\":\"object\",\"properties\":{");
        for (int p = 0; p < k->n_params; p++) {
            put(o, "%s", p ? "," : "");
            put_str(o, k->param_names[p]);
            put(o, ":{\"type\":\"string\",\"enum\":[");
            for (int v = 0; v < k->n_values[p]; v++) { put(o, "%s", v ? "," : ""); put_str(o, k->values[p][v]); }
            put(o, "]}");
        }
        put(o, "},\"required\":[");
        for (int p = 0; p < k->n_params; p++) { put(o, "%s", p ? "," : ""); put_str(o, k->param_names[p]); }
        put(o, "],\"additionalProperties\":false},\"outputSchema\":{\"type\":\"object\",\"properties\":{\"reply\":"
               "{\"type\":\"string\",\"enum\":[");
        for (int r = 0; r < k->n_replies; r++) { put(o, "%s", r ? "," : ""); put_str(o, k->replies[r]); }
        put(o, "]}},\"required\":[\"reply\"]},\"_meta\":{\"tiny_langgraph\":{\"fingerprint\":\"%08lx\",\"replies\":[",
            (unsigned long)k->fingerprint);
        for (int r = 0; r < k->n_replies; r++) { put(o, "%s", r ? "," : ""); put_str(o, k->replies[r]); }
        put(o, "]}}}");
    }
    put(o, "%s{\"name\":\"%s_status\",\"description\":", m->n_contracts ? "," : "", m->board);
    char about[160];
    snprintf(about, sizeof about, "What %s observes right now, and its last plan (read-only)", m->board);
    put_str(o, about);
    put(o, ",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}}]}}");
}

static void call_tool(tlg_mcp_t *m, out_t *o, const char *id, const char *params, const char *e)
{
    char name[48], status_name[48];
    if (!get_str(find(params, e, "name"), e, name, sizeof name)) {
        error(o, id, -32602, "tools/call needs params.name");
        return;
    }
    snprintf(status_name, sizeof status_name, "%s_status", m->board);
    if (strcmp(name, status_name) == 0) {
        char text[600];
        if (!m->status || m->status(m->ctx, text, sizeof text) <= 0) snprintf(text, sizeof text, "{}");
        tool_result(o, id, text, NULL, false);
        return;
    }
    int c = -1;
    for (int i = 0; i < m->n_contracts; i++)
        if (strcmp(m->contracts[i].name, name) == 0) c = i;
    if (c < 0) {
        char msg[96];
        snprintf(msg, sizeof msg, "unknown tool %s", name);
        error(o, id, -32602, msg);
        return;
    }
    const tlg_mcp_contract_t *k = &m->contracts[c];
    const char *args = find(params, e, "arguments");
    uint8_t values[TLG_NET_PARAMS] = { 0 };
    for (int p = 0; p < k->n_params; p++) {
        char v[48], msg[160];
        int found = -1;
        if (get_str(find(args, e, k->param_names[p]), e, v, sizeof v))
            for (int i = 0; i < k->n_values[p]; i++)
                if (strcmp(k->values[p][i], v) == 0) found = i;
        if (found < 0) {
            int w = snprintf(msg, sizeof msg, "%s must be one of", k->param_names[p]);
            for (int i = 0; i < k->n_values[p] && w > 0 && (size_t)w < sizeof msg; i++)
                w += snprintf(msg + w, sizeof msg - (size_t)w, "%s %s", i ? "," : "", k->values[p][i]);
            error(o, id, -32602, msg);
            return;
        }
        values[p] = (uint8_t)found;
    }
    uint16_t seq;
    if (!tlg_net_local_call(m->net, c, values, k->n_params, &seq)) {
        tool_result(o, id, "busy: too many requests are waiting; try again later", NULL, true);
        return;
    }
    if (!m->run_now || !m->run_now(m->ctx, m->wait_ms)) {
        tool_result(o, id, "busy: the board is in the middle of something; it will still decide on its next run",
                    NULL, true);
        return;
    }
    uint8_t r = tlg_net_local_result(m->net, c, seq);
    if (!r) {
        char msg[160];
        snprintf(msg, sizeof msg, "no answer within %lu ms (waiting for a human's approval?); it may still decide later",
                 (unsigned long)m->wait_ms);
        tool_result(o, id, msg, NULL, true);
        return;
    }
    tool_result(o, id, k->replies[r - 1], k->replies[r - 1], false);
}

int tlg_mcp_handle(tlg_mcp_t *m, const char *body, size_t len, const char *authorization,
                   char *out, size_t size, int *http)
{
    out_t o = { out, size, 0, false };
    const char *e = body + len;
    out[0] = '\0';
    if (m->key && *m->key) {
        char want[96];
        snprintf(want, sizeof want, "Bearer %s", m->key);
        if (!authorization || strcmp(authorization, want) != 0) {
            *http = 401;
            put(&o, "{\"error\":\"send the board's MCP key: Authorization: Bearer <MCP_KEY from config.h>\"}");
            return (int)o.len;
        }
    }
    char method[48];
    const char *idv = find(body, e, "id");
    if (!get_str(find(body, e, "method"), e, method, sizeof method)) {
        *http = 400;
        error(&o, "null", -32600, "not a JSON-RPC request");
        return (int)o.len;
    }
    if (!idv) {                                       /* a notification: nothing to answer */
        *http = 202;
        return 0;
    }
    char id[40];
    const char *ide = skip_value(idv, e);
    size_t n = ide && ide > idv && (size_t)(ide - idv) < sizeof id ? (size_t)(ide - idv) : 0;
    memcpy(id, idv, n);
    id[n] = '\0';
    if (!n) strcpy(id, "null");
    *http = 200;
    const char *params = find(body, e, "params");

    if (strcmp(method, "initialize") == 0) {
        char asked[24] = "";
        get_str(find(params, e, "protocolVersion"), e, asked, sizeof asked);
        const char *version = TLG_MCP_VERSION;
        if (!strcmp(asked, "2025-06-18") || !strcmp(asked, "2025-03-26") || !strcmp(asked, "2024-11-05")) version = asked;
        char about[240];
        snprintf(about, sizeof about, "%s. Each tool is a request: the board's own rules decide, and its answer is the "
                 "result.", m->about && *m->about ? m->about : m->board);
        head(&o, id);
        put(&o, "\"result\":{\"protocolVersion\":");
        put_str(&o, version);
        put(&o, ",\"capabilities\":{\"tools\":{\"listChanged\":false}},\"serverInfo\":{\"name\":");
        put_str(&o, m->board);
        put(&o, ",\"version\":\"tiny-langgraph 0.4\"},\"instructions\":");
        put_str(&o, about);
        put(&o, "}}");
    } else if (strcmp(method, "ping") == 0) {
        head(&o, id);
        put(&o, "\"result\":{}}");
    } else if (strcmp(method, "tools/list") == 0) {
        list_tools(m, &o, id);
    } else if (strcmp(method, "tools/call") == 0) {
        call_tool(m, &o, id, params, e);
    } else {
        char msg[80];
        snprintf(msg, sizeof msg, "method not found: %s", method);
        error(&o, id, -32601, msg);
    }
    if (o.full) {
        o.len = 0;
        o.full = false;
        error(&o, id, -32603, "the answer doesn't fit the board's buffer");
    }
    return (int)o.len;
}
