#include "tlg_lm.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------
 * Math building blocks
 * ------------------------------------------------------------ */

/* out[rows] = W[rows, cols] @ x[cols]   (PyTorch Linear layout: [out, in]) */
static void matvec(float *out, const float *W, const float *x, int rows, int cols)
{
    for (int r = 0; r < rows; r++) {
        const float *w = W + (size_t)r * cols;
        float s = 0.0f;
        for (int c = 0; c < cols; c++) s += w[c] * x[c];
        out[r] = s;
    }
}

static void rmsnorm(float *out, const float *x, const float *weight, int n)
{
    float ss = 0.0f;
    for (int i = 0; i < n; i++) ss += x[i] * x[i];
    float inv = 1.0f / sqrtf(ss / n + 1e-5f);
    for (int i = 0; i < n; i++) out[i] = x[i] * inv * weight[i];
}

static void softmax(float *x, int n)
{
    float max = x[0];
    for (int i = 1; i < n; i++) if (x[i] > max) max = x[i];
    float sum = 0.0f;
    for (int i = 0; i < n; i++) { x[i] = expf(x[i] - max); sum += x[i]; }
    for (int i = 0; i < n; i++) x[i] /= sum;
}

static float silu(float x)
{
    return x / (1.0f + expf(-x));
}

/* ------------------------------------------------------------
 * Loading
 * ------------------------------------------------------------ */
static bool alloc_work(tlg_lm_t *m)
{
    const tlg_lm_config_t *c = &m->cfg;
    size_t kv = (size_t)c->layers * c->seq_len * c->dim;
    size_t n = (size_t)c->dim * 3 + c->hidden + (size_t)c->dim * 3 + c->seq_len + c->vocab + kv * 2;
    float *w = calloc(n, sizeof(float));
    if (!w) return false;
    m->work = w;
    m->x = w;               w += c->dim;
    m->xb = w;              w += c->dim;
    m->xb2 = w;             w += c->dim;
    m->hb = w;              w += c->hidden;
    m->qkvbuf = w;          w += 3 * c->dim;
    m->att = w;             w += c->seq_len;
    m->logits = w;          w += c->vocab;
    m->key_cache = w;       w += kv;
    m->val_cache = w;
    return true;
}

bool tlg_lm_init(tlg_lm_t *m, const void *data, size_t size)
{
    const uint8_t *p = data;
    if (size < 4 + sizeof(tlg_lm_config_t) || memcmp(p, "TLM1", 4) != 0) {
        fprintf(stderr, "tlg_lm: not a TLM1 file\n");
        return false;
    }
    memcpy(&m->cfg, p + 4, sizeof m->cfg);
    const tlg_lm_config_t *c = &m->cfg;
    if (c->layers < 1 || c->layers > TLG_LM_MAX_LAYERS || c->heads < 1 || c->dim % c->heads != 0) {
        fprintf(stderr, "tlg_lm: bad config\n");
        return false;
    }

    size_t d = c->dim, h = c->hidden;
    size_t per_layer = d + 3 * d * d + d * d + d + h * d + d * h;
    size_t floats = (size_t)c->vocab * d + (size_t)c->seq_len * d + per_layer * c->layers + d;
    size_t need = 4 + sizeof(tlg_lm_config_t) + floats * sizeof(float);
    if (size != need) {
        fprintf(stderr, "tlg_lm: size %zu, expected %zu for this config\n", size, need);
        return false;
    }

    /* walk the weights in the order python/tiny_langgraph/toolchain/train.py wrote them */
    const float *w = (const float *)(p + 4 + sizeof(tlg_lm_config_t));
    m->tok = w;  w += (size_t)c->vocab * d;
    m->pos = w;  w += (size_t)c->seq_len * d;
    for (int l = 0; l < c->layers; l++) {
        tlg_lm_layer_t *L = &m->layer[l];
        L->norm1 = w; w += d;
        L->qkv   = w; w += 3 * d * d;
        L->proj  = w; w += d * d;
        L->norm2 = w; w += d;
        L->up    = w; w += h * d;
        L->down  = w; w += d * h;
    }
    m->norm = w;

    return alloc_work(m);
}

bool tlg_lm_load_file(tlg_lm_t *m, const char *path)
{
    memset(m, 0, sizeof *m);
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); return false; }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    void *buf = malloc((size_t)size);
    bool ok = buf && fread(buf, 1, (size_t)size, f) == (size_t)size;
    fclose(f);
    if (!ok) { free(buf); return false; }
    m->blob = buf;
    if (!tlg_lm_init(m, buf, (size_t)size)) { tlg_lm_free(m); return false; }
    return true;
}

void tlg_lm_free(tlg_lm_t *m)
{
    free(m->blob);
    free(m->work);
    m->blob = m->work = NULL;
}

/* ------------------------------------------------------------
 * Forward pass for one token
 * ------------------------------------------------------------ */
const float *tlg_lm_forward(tlg_lm_t *m, int token, int pos)
{
    const tlg_lm_config_t *c = &m->cfg;
    const int d = c->dim, hd = d / c->heads;
    const float scale = 1.0f / sqrtf((float)hd);

    /* embedding: what the token is + where it is */
    for (int i = 0; i < d; i++) m->x[i] = m->tok[(size_t)token * d + i] + m->pos[(size_t)pos * d + i];

    for (int l = 0; l < c->layers; l++) {
        const tlg_lm_layer_t *L = &m->layer[l];
        float *kc = m->key_cache + ((size_t)l * c->seq_len) * d;
        float *vc = m->val_cache + ((size_t)l * c->seq_len) * d;

        /* --- attention --- */
        rmsnorm(m->xb, m->x, L->norm1, d);
        matvec(m->qkvbuf, L->qkv, m->xb, 3 * d, d);
        const float *q = m->qkvbuf;
        memcpy(kc + (size_t)pos * d, m->qkvbuf + d, d * sizeof(float));      /* remember k */
        memcpy(vc + (size_t)pos * d, m->qkvbuf + 2 * d, d * sizeof(float));  /* remember v */

        for (int h = 0; h < c->heads; h++) {
            const float *qh = q + h * hd;
            /* how much should this token look at each earlier token? */
            for (int t = 0; t <= pos; t++) {
                const float *kh = kc + (size_t)t * d + h * hd;
                float s = 0.0f;
                for (int i = 0; i < hd; i++) s += qh[i] * kh[i];
                m->att[t] = s * scale;
            }
            softmax(m->att, pos + 1);
            /* weighted mix of their values */
            float *out = m->xb2 + h * hd;
            for (int i = 0; i < hd; i++) out[i] = 0.0f;
            for (int t = 0; t <= pos; t++) {
                const float *vh = vc + (size_t)t * d + h * hd;
                for (int i = 0; i < hd; i++) out[i] += m->att[t] * vh[i];
            }
        }
        matvec(m->xb, L->proj, m->xb2, d, d);
        for (int i = 0; i < d; i++) m->x[i] += m->xb[i];

        /* --- feed-forward --- */
        rmsnorm(m->xb, m->x, L->norm2, d);
        matvec(m->hb, L->up, m->xb, c->hidden, d);
        for (int i = 0; i < c->hidden; i++) m->hb[i] = silu(m->hb[i]);
        matvec(m->xb, L->down, m->hb, d, c->hidden);
        for (int i = 0; i < d; i++) m->x[i] += m->xb[i];
    }

    /* scores for every token in the vocabulary */
    rmsnorm(m->xb, m->x, m->norm, d);
    matvec(m->logits, m->tok, m->xb, c->vocab, d);
    return m->logits;
}

int tlg_lm_generate(tlg_lm_t *m, const uint8_t *prompt, int prompt_len, int eos,
                 uint8_t *out, int max_out, float *confidence)
{
    return tlg_lm_generate_guided(m, prompt, prompt_len, eos, out, max_out, confidence, NULL, NULL);
}

int tlg_lm_generate_guided(tlg_lm_t *m, const uint8_t *prompt, int prompt_len, int eos,
                           uint8_t *out, int max_out, float *confidence, tlg_lm_allow_fn allow, void *ctx)
{
    const int limit = m->cfg.seq_len;
    if (prompt_len < 1 || prompt_len >= limit) return 0;

    const float *logits = NULL;
    for (int i = 0; i < prompt_len; i++) logits = tlg_lm_forward(m, prompt[i], i);

    int n = 0, pos = prompt_len;
    float min_p = 1.0f;
    while (n < max_out) {
        int best = -1;                           /* the first most likely allowed token */
        for (int v = 0; v < m->cfg.vocab; v++)
            if ((!allow || allow(ctx, out, n, v)) && (best < 0 || logits[v] > logits[best])) best = v;
        if (best < 0) best = eos;                /* nothing allowed: stop */

        /* probability of the chosen token among the allowed ones = 1 / sum(exp(logit - best_logit)) */
        float sum = 0.0f;
        for (int v = 0; v < m->cfg.vocab; v++)
            if (!allow || allow(ctx, out, n, v)) sum += expf(logits[v] - logits[best]);
        float p = sum > 0.0f ? 1.0f / sum : 1.0f;
        if (p < min_p) min_p = p;

        out[n++] = (uint8_t)best;
        if (best == eos || pos >= limit) break;
        logits = tlg_lm_forward(m, best, pos++);
    }
    if (confidence) *confidence = min_p;
    return n;
}
