/*
 * Counting: is right now unusual for this home? (models.Counting in Python)
 *
 * Once per run it gets one sample: is the thing it watches active (e.g. "a cat is
 * visible")? From the samples it keeps, in plain data:
 *   quiet  minutes since the last activity
 *   busy   bouts of activity in the last 2 hours (a bout starts after >= 5 min still)
 *   slow   bouts in the last 12 hours
 * and compares them with what's normal at this hour of the day, from tables learned on the
 * PC from the home's own logs. Answer: 0 no, 1 odd, 2 alarming.
 *
 * The maths is +, -, *, / and comparisons on doubles, in the same order as the Python
 * twin (models/counting.py), so both give exactly the same answer. No malloc.
 */
#ifndef TLG_COUNT_H
#define TLG_COUNT_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TLG_COUNT_BOUT_GAP 5       /* minutes still before activity counts as a new bout */
#define TLG_COUNT_QUIET    1u      /* checks */
#define TLG_COUNT_BUSY     2u
#define TLG_COUNT_SLOW     4u

/* What's normal, by hour of day (generated from the Python fit). */
typedef struct {
    double  top[24];            /* 90th percentile of quiet minutes */
    double  excess[24];         /* beyond top, quiet spells thin out with this scale */
    double  lam_busy[24];       /* mean bouts in 2 h, and exp(-mean) */
    double  e_busy[24];
    double  lam_slow[24];       /* mean bouts in 12 h, and exp(-mean) */
    double  e_slow[24];
    double  quiet_odd;          /* (quiet - top) / excess at or beyond this: odd */
    double  quiet_alarm;        /*                                         alarming */
    double  p_odd;              /* busy/slow probabilities at or below this: odd */
    double  p_alarm;            /*                                           alarming */
    uint8_t checks;             /* TLG_COUNT_QUIET | TLG_COUNT_BUSY | TLG_COUNT_SLOW */
} tlg_count_table_t;

/* The memory, kept between runs. */
typedef struct {
    bool     started, seen;
    int32_t  first;             /* minute of the first run: busy/slow wait until they have 2 h / 12 h */
    int32_t  last_active;       /* minute of the last activity (or of the first run) */
    int32_t  bucket_hour[24];   /* the absolute hour each bucket counts, -1 = empty */
    uint16_t bucket[24];        /* bouts that started in that hour */
} tlg_count_t;

void tlg_count_init(tlg_count_t *m);

/* One run: update with this sample, return 0 no, 1 odd, 2 alarming.
 * minute: minutes since the board started; hour: hour of the day (0..23). */
int  tlg_count_step(tlg_count_t *m, const tlg_count_table_t *t, bool active, int32_t minute, int32_t hour);

#ifdef __cplusplus
}
#endif

#endif /* TLG_COUNT_H */
