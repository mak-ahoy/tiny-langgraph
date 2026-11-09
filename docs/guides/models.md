# Models

Models in tiny-langgraph **fill state fields**; they never pick actions. Your rules read the
fields like any other, and the verifier checks every plan, so a model can't make the agent do
something your rules and conditions forbid. Every model's C is checked against its Python at
build time.

## The catalog: `prebuilt.Classify`

```python
from tiny_langgraph import Score, models, prebuilt

class Room(State):
    seen    = Choice("no", "yes")
    tod     = Choice("day", "night")
    unusual = Score("no", "odd", "alarming")      # answered by a model
    risk    = Score("low", "high")

def active(s: Room) -> bool:
    return s.seen == "yes"

NOTICE = models.Counting(active=active, clock=clock)
RISK   = models.Tree(reads=["tod", "unusual"], max_depth=3)

graph.add_node("notice", prebuilt.Classify({"unusual": "Is the activity unusual for this room?"}, model=NOTICE))
graph.add_node("risk",   prebuilt.Classify({"risk": "Is this worth an alert?"}, model=RISK))
```

Then your rules react: `if s.unusual == "alarming": want(goals.alerted)`.

### `models.Counting`: what's normal here

Learns how active a place usually is at each hour of the day, from your own data, and flags
what isn't normal.

| Argument | Meaning |
|---|---|
| `active` | a condition: is something happening this run? |
| `clock` | your `Clock()` driver |
| `checks` | any of `"quiet"` (nothing for unusually long), `"busy"` (much more than usual), `"slow"` (a slow day) |
| `odd` | probability below which a moment is `odd` (0 < odd < 0.1, default 0.05) |
| `false_alarms_per_week` | the alarm threshold is picked from your normal days to stay within this (default 1) |

It answers a `Score` with 2 levels (no, yes) or 3 (no, odd, alarming). Fit it on two weeks or
more of normal days from the board's data log:

```python
from tiny_langgraph.datalog import load_log, samples
rows = load_log("log.csv")                       # downloaded from the board's web page
NOTICE.fit(samples(rows, Room, active))
NOTICE.save("notice.json")                       # later: models.Counting(...).load("notice.json")
```

In our experiments on simulated cats, this beat a small transformer at spotting unusual days.

### `models.Tree`: a decision tree

```python
RISK.fit([({"tod": "night", "unusual": "alarming"}, "high"), ...])    # (values, label) rows
```

It reads `Choice`/`Score` fields and answers a `Choice` or `Score`. The build checks the C on
every combination of its inputs.

## The tiny transformer: `prebuilt.reason`

A `reason` node replaces `plan` with a small transformer (~46k parameters) trained on your
`@rules` and the planner. The build trains it if PyTorch is installed (~1.5 min on a laptop
CPU), and retrains only when the rules or tools change.

```python
graph.add_node("reason", prebuilt.reason)
graph.add_edge("assess", "reason")
graph.add_edge("reason", "verify")
```

Writing is **guided**: at each step the model may only pick tools the verifier would allow, and
the verifier keeps a model plan only if it's at least as good as the planner's.

!!! note "When is it worth it?"
    For decisions that depend only on the current moment, it adds nothing: on an ESP32-S3 it
    takes ~120 ms where the planner takes 40 µs, for the same answers. In our planning
    experiments it won only for very busy agents, where the board can't search all plans
    (~30 goals wanted at once: 99.8% optimal vs 88.6%). ESP32-S3 only.
