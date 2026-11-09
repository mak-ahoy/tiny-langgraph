"""
The board's data log, on your PC: turn it into training data for catalog models.

    from tiny_langgraph.datalog import load_log, samples

    rows = load_log("pet_watcher-log.csv")             # "Download data log" on the board's web page
    notice.fit(samples(rows, Pet, active=cat_visible))  # models.Counting learns your home

Each line is one run: time (local), epoch (seconds), hour, every Choice/Score field, the plan.
"""
from __future__ import annotations

import csv

FIXED = ("time", "epoch", "hour")


class LogError(Exception):
    pass


def load_log(path) -> list[dict]:
    """The rows of a log file, oldest first. Each row: the CSV columns as strings, plus
    "minute" (whole minutes since 1970, for gaps and timing) and "hour" (0..23) as numbers.
    Skips a cut-off last line (a power cut while writing) and repeated column-name lines."""
    rows, header = [], None
    with open(path, newline="", encoding="utf-8") as f:
        for n, line in enumerate(csv.reader(f), 1):
            if not line:
                continue
            if line[0] == "time":
                if header is not None and line != header:
                    raise LogError(f"{path}:{n}: the columns changed (the agent's fields changed?); "
                                   "download days of one version, or clear the log after flashing a new one")
                header = line
                continue
            if header is None:
                raise LogError(f"{path}: no column names at the top: is this a tiny-langgraph data log?")
            if len(line) != len(header):
                continue                                  # cut off by a power cut
            row = dict(zip(header, line))
            try:
                row["minute"] = int(row["epoch"]) // 60
                row["hour"] = int(row["hour"])
            except ValueError:
                continue
            rows.append(row)
    if header is None or any(c not in header for c in FIXED):
        raise LogError(f"{path}: not a tiny-langgraph data log (expected columns {', '.join(FIXED)}, ...)")
    rows.sort(key=lambda r: r["minute"])
    return rows


def samples(rows, state, active) -> list[tuple[int, int, bool]]:
    """(minute, hour, active) per run, for models.Counting.fit. `active` is your condition, run on
    a state rebuilt from each row's Choice/Score values, so you can try any condition afterwards."""
    names = [f.name for f in state.inputs()]
    missing = [n for n in names if rows and n not in rows[0]]
    if missing:
        raise LogError(f"the log has no column for {', '.join(missing)}: it was written by another version "
                       "of the agent")
    out = []
    for r in rows:
        try:
            s = state(**{n: r[n] for n in names})
        except ValueError as e:
            raise LogError(f"row at {r['time']}: {e} (written by another version of the agent?)") from None
        a = active(s)
        if not isinstance(a, bool):
            raise LogError(f"{active.__name__}() must return True or False, got {a!r}")
        out.append((r["minute"], r["hour"], a))
    return out
