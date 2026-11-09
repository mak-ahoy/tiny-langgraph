# Tools and approvals

A tool is an action the planner may choose.

```python
def may_feed(s: Feeder) -> bool:
    return s.feed_asked == "yes" and s.fed_recently == "no"

@tool(cost=3, achieves=[goals.fed, goals.answered], needs=may_feed)
def dispense(s: Feeder):
    led.blink(3, 200)
    s.fed_at = clock.minutes()
    mailbox.reply(feed, "done")
```

## `@tool(...)`

| Argument | Meaning |
|---|---|
| `cost` | 1–1000; the planner prefers cheap plans |
| `achieves` | the goal(s) it reaches: `goals.fed` or a list |
| `needs` | condition function(s) that must be true for it to be allowed |
| `after` | goal(s) that must be reached earlier in the plan |
| `approval` | `True`: a human approves before it runs |

Up to 32 tools and 32 conditions per agent.

## Conditions

A condition is a named function of the state that returns `True` or `False`. Like rules,
conditions may only read `Choice`, `Think` and `Score` fields. The verifier checks them on the
board before every tool runs, whoever proposed the plan.

## The tool body

The body is a node: drivers, memory (`Int` fields), `note(...)` for the trace, calls to other
boards. Return nothing for success. Returning `False` marks the tool as failed: the rest of the
plan doesn't run, and the trace says so.

## Approvals

```python
@tool(cost=2, achieves=goals.alarmed, approval=True)
def sound_alarm(s: Door):
    siren.on()
```

When a plan contains an approval tool, the graph goes `verify → ask`:

- **yes:** back to `verify`, then `act`
- **no:** the tool is banned for this run, and the planner finds the best plan without it

| Where | How to answer |
|---|---|
| PC | `app.invoke(s, approve=lambda q: True)`, or check `s.waiting` and call `app.resume(s, True)` |
| Board web page | Yes / No, with the PIN from `config.h` (locked for a minute after 5 wrong PINs) |
| Board | the BOOT (FLASH) button for yes, or `y` / `n` in the Serial Monitor |

No answer within `APPROVAL_TIMEOUT_MS` (2 minutes) counts as no.
