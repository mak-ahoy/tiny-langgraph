# The Python you can use

Nodes, tools, conditions and rules run on the board as C, so they use the parts of Python
that make sense on a microcontroller. Anything else stops the build with the file, the line,
and what to use instead:

```text
agent.py:131: floats aren't supported yet; use whole numbers (e.g. tenths of a degree)
```

## Supported

| | Example |
|---|---|
| `if` / `elif` / `else` | |
| `for i in range(...)`, `break`, `continue` | `for i in range(3): buzzer.beep(100)` |
| `return` | |
| setting fields, `+=` | `s.count += 1` |
| local whole numbers and booleans | `late = clock.hour() >= 22` |
| comparisons, `in` a tuple | `s.cat in ("seen", "in_zone")` |
| `and`, `or`, `not` | |
| `+ - * // %` | `//` and `%` round like Python, also for negative numbers |
| `x if c else y` | `s.tod = "night" if h >= 22 else "day"` |
| `min`, `max`, `abs` | |
| calls to your other functions that take `s` | `if may_feed(s): ...` |
| driver methods | `servo.turn(90)` |
| lookup tables indexed by a `Choice` field | `POINTS[s.cam]`, with `POINTS = {...}` at the top of the file |
| `note("message", values...)` | a line in the trace |
| `want(goals.x)` | in `@rules` only |
| `interrupt("question")` | pause for a human |
| `peer.call(contract, "value")`, `mailbox.reply(contract, "value")` | [Multi-agent](../guides/multi-agent.md); values are checked at build time |

## Not supported (yet)

| | Instead |
|---|---|
| floats | whole numbers: tenths of a degree, milliseconds |
| lists and dicts at runtime | `Int` fields; lookup tables at the top of the file |
| strings other than `Choice` values and literal messages | `Choice` fields |
| `while` loops | `for i in range(n)`: a loop that can't hang the board |
| recursion, lambdas, keyword arguments, imports inside functions | |

## Rules and conditions

`@rules` functions and conditions (`needs=`) may only read `Choice`, `Think` and `Score`
fields: no `Int` fields, no hardware, no `note()`. The build runs them on every combination of
those fields. See [State and fields](../concepts/state.md#why-rules-cant-read-int).

## Limits per agent

32 goals, 32 tools, 32 conditions; up to 256 tokens in a trained model's vocabulary.
