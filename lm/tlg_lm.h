#ifndef TLG_LM_H
#define TLG_LM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Inference engine for the tiny agent LM (same architecture as python/tiny_langgraph/toolchain/train.py):
 *   token embedding + learned position embedding
 *   N x [ RMSNorm -> causal self-attention -> residual,
 *         RMSNorm -> up -> SiLU -> down -> residual ]
 *   final RMSNorm -> logits via the (tied) token embedding
 *
 * Runs one token at a time with a KV cache, exactly how text is generated on the chip.
 */

typedef struct {
    int32_t vocab, seq_len, dim, layers, heads, hidden;
} tlg_lm_config_t;

typedef struct {
    const float *norm1;   /* [dim]           */
    const float *qkv;     /* [3*dim, dim]    */
    const float *proj;    /* [dim, dim]      */
    const float *norm2;   /* [dim]           */
    const float *up;      /* [hidden, dim]   */
    const float *down;    /* [dim, hidden]   */
} tlg_lm_layer_t;

#define TLG_LM_MAX_LAYERS 8

typedef struct {
    tlg_lm_config_t cfg;
    const float *tok;     /* [vocab, dim]    */
    const float *pos;     /* [seq_len, dim]  */
    tlg_lm_layer_t  layer[TLG_LM_MAX_LAYERS];
    const float *norm;    /* [dim]           */

    /* working buffers */
    float *x, *xb, *xb2, *hb, *qkvbuf, *att, *logits;
    float *key_cache, *val_cache;   /* [layers, seq_len, dim] */

    void  *blob;          /* owns the file contents when loaded from disk */
    void  *work;          /* owns the working buffers */
} tlg_lm_t;

/* Load a model file written by tools/tlg.py train. */
bool tlg_lm_load_file(tlg_lm_t *m, const char *path);

/* Use weights already in memory (e.g. flash-mapped on the ESP32). */
bool tlg_lm_init(tlg_lm_t *m, const void *data, size_t size);

void tlg_lm_free(tlg_lm_t *m);

/* Feed one token at position pos; returns logits[vocab] (valid until the next call). */
const float *tlg_lm_forward(tlg_lm_t *m, int token, int pos);

/*
 * Greedy generation: feed the prompt, then keep taking the most likely token
 * until <eos> or the sequence limit. Writes only the NEW tokens to out.
 * Returns the number of new tokens.
 *
 * confidence (optional, may be NULL): the lowest probability the model gave to
 * any token it chose. One shaky token makes the whole answer shaky.
 */
int tlg_lm_generate(tlg_lm_t *m, const uint8_t *prompt, int prompt_len, int eos,
                 uint8_t *out, int max_out, float *confidence);

/* May `token` come next, after out[0..n_out-1]? Guided writing: e.g. only tool calls that pass
 * the safety checks, so every plan the model writes is safe by construction. */
typedef bool (*tlg_lm_allow_fn)(void *ctx, const uint8_t *out, int n_out, int token);

/* Like tlg_lm_generate, but at each step only tokens allow() accepts can be chosen (the most
 * likely of them; the first one on a tie); confidence is among the allowed tokens. allow = NULL
 * is tlg_lm_generate. lm.py's generate(..., allow=) does the same. */
int tlg_lm_generate_guided(tlg_lm_t *m, const uint8_t *prompt, int prompt_len, int eos,
                           uint8_t *out, int max_out, float *confidence, tlg_lm_allow_fn allow, void *ctx);

#ifdef __cplusplus
}
#endif

#endif /* TLG_LM_H */
