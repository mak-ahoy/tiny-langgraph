#include "tlg_count.h"

void tlg_count_init(tlg_count_t *m)
{
    m->started = m->seen = false;
    m->first = m->last_active = 0;
    for (int i = 0; i < 24; i++) {
        m->bucket_hour[i] = -1;
        m->bucket[i] = 0;
    }
}

/* bouts that started in absolute hours [from, to] */
static int bouts(const tlg_count_t *m, int32_t from, int32_t to)
{
    int n = 0;
    for (int i = 0; i < 24; i++)
        if (m->bucket_hour[i] >= from && m->bucket_hour[i] <= to) n += m->bucket[i];
    return n;
}

/* P(X <= k) for a Poisson count with mean lam; e = exp(-lam) */
static double poisson_cdf(double lam, double e, int k)
{
    double term = e, sum = e;
    for (int i = 1; i <= k; i++) {
        term = term * lam / i;
        sum += term;
    }
    return sum;
}

static int level_of(double p, const tlg_count_table_t *t)
{
    return p <= t->p_alarm ? 2 : p <= t->p_odd ? 1 : 0;
}

int tlg_count_step(tlg_count_t *m, const tlg_count_table_t *t, bool active, int32_t minute, int32_t hour)
{
    if (!m->started) {
        m->started = true;
        m->first = m->last_active = minute;   /* quiet counts from the first run */
    }
    int32_t abs_hour = minute / 60;
    if (active) {
        if (!m->seen || minute - m->last_active >= TLG_COUNT_BOUT_GAP) {
            int slot = (int)(abs_hour % 24);
            if (m->bucket_hour[slot] != abs_hour) {
                m->bucket_hour[slot] = abs_hour;
                m->bucket[slot] = 0;
            }
            if (m->bucket[slot] < UINT16_MAX) m->bucket[slot]++;
        }
        m->seen = true;
        m->last_active = minute;
    }
    int h = (int)(((hour % 24) + 24) % 24), level = 0, l;
    if ((t->checks & TLG_COUNT_QUIET) && t->excess[h] > 0) {
        double z = ((double)(minute - m->last_active) - t->top[h]) / t->excess[h];
        l = z >= t->quiet_alarm ? 2 : z >= t->quiet_odd ? 1 : 0;
        if (l > level) level = l;
    }
    int32_t known = minute - m->first;        /* minutes of memory: none after a reboot */
    if ((t->checks & TLG_COUNT_BUSY) && known >= 2 * 60) {
        int k = bouts(m, abs_hour - 1, abs_hour);
        if (k > 0) {
            l = level_of(1.0 - poisson_cdf(t->lam_busy[h], t->e_busy[h], k - 1), t);
            if (l > level) level = l;
        }
    }
    if ((t->checks & TLG_COUNT_SLOW) && known >= 12 * 60) {
        l = level_of(poisson_cdf(t->lam_slow[h], t->e_slow[h], bouts(m, abs_hour - 11, abs_hour)), t);
        if (l > level) level = l;
    }
    return level;
}
