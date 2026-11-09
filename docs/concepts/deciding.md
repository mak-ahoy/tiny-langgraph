# Rules, planner, verifier

How an agent decides, in three steps: **rules** say what's wanted, the **planner** finds how,
and the **verifier** checks the plan before anything runs.

## Goals

```python
goals = Goals(
    logged=1,                                   # weight 1
    fed=Goal(5, only_if_wanted=True),           # weight 5; never do it unless wanted
    told=Goal(3, only_if_wanted=True),
)
```

- **Weight** (0–255) is how much a goal matters when not everything is possible.
- **`only_if_wanted=True`**: a plan that achieves this goal when the rules didn't ask for it is
  rejected. Use it for anything with a side effect, like feeding or alarms.

Up to 32 goals per agent.

## Rules

```python
@rules
def assess(s: Watcher):
    if s.meal == "due" and s.waiting == "no":
        if s.feeder_online == "yes":
            want(goals.fed)
        else:
            want(goals.told)
    if s.feed_reply == "refused":
        want(goals.told)
```

The `@rules` function runs every time. It calls `want(...)` and may set `Think` fields. It may
only read `Choice`, `Think` and `Score` fields: no `Int` fields, no hardware, no `note()`. That
way the build can check it on every situation.

## Planner

`prebuilt.plan` finds the plan with the **highest total weight of wanted goals**, and among
those, the **lowest cost**. It only uses tools whose conditions (`needs=`) are true now, and
keeps their order (`after=`).

| Goals | How it searches | Memory |
|---|---|---|
| up to 8 | every combination (exact) | ~1.25 KB of stack |
| 9–32 | a pruned exact search in a workspace | ESP32-S3: 16,384 states (352 KB, PSRAM). NodeMCU: 1,024 states (22 KB) |
| doesn't fit | greedy (best weight per cost), noted in the trace | none |

The Python planner and the C planner match step for step, including tie-breaking, so the build
can compare their answers.

## Verifier

`prebuilt.verify` checks a plan before anything runs. A plan fails if:

- a tool's condition isn't true now: `dispense needs may_feed`
- a tool comes before a goal it must follow
- it achieves an `only_if_wanted` goal nobody asked for
- it misses something the planner could achieve
- it costs more than the planner's plan for the same goals
- it uses a tool a human just refused

If a tool needs approval, the plan goes to `ask` first. If the plan fails, the graph replans.
The verifier is plain rules that run in time proportional to the plan's length. A model may
propose plans ([Models](../guides/models.md)), but the verifier always has the last word.

## Why not just an LLM?

For decisions that depend only on the current moment, rules + the planner are right by
construction and take microseconds. Measured on an ESP32-S3: planning ~40 µs and verifying
~37 µs, against ~120 ms for a tiny transformer that only learned to copy the rules. A model
earns its place only where the planner can't reach. See
[Results and limitations](../reference/results.md).
