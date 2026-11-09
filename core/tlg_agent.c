#include "tlg_agent.h"

#include <stdarg.h>
#include <stdio.h>

void tlg_agent_begin(tlg_agent_t *a)
{
    a->facts = a->want = a->banned = 0;
    a->plan_len = 0;
    a->src = TLG_SRC_NONE;
    a->lm_conf = 0.0f;
    a->lm_ms = 0;
    a->plan_us = a->verify_us = 0;
    a->verdict = TLG_VERIFY_FAIL;
    a->why[0] = '\0';
    a->approved = false;
    a->acted_ok = false;
    a->answer = TLG_ANSWER_NONE;
}

void tlg_agent_say(tlg_agent_t *a, const char *fmt, ...)
{
    char text[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);
    if (a->tracer) tlg_trace_note(a->tracer, "%s", text);
    if (a->log) a->log(text, a->ctx);
}

const char *tlg_agent_src_name(tlg_src_t s)
{
    static const char *const N[] = { "none", "lm", "planner" };
    return (unsigned)s < 3 ? N[s] : "?";
}
