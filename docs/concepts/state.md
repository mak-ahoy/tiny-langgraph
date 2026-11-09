# State and fields

The state is what the agent knows. Like a LangGraph `TypedDict`, but with fixed types: on the
board it's a C struct.

```python
class Pet(State):
    """watches the cat and feeds it"""              # one line about the agent (shown to MCP clients)
    cat          = Choice("none", "seen", "in_zone")
    meal         = Choice("not_due", "due", "overdue")
    mischief     = Think("no", "yes")
    unusual      = Score("no", "odd", "alarming")
    fed_at       = Int(-1000)
```

## Field types

| Type | What it holds | Who sets it | Rules may read it |
|---|---|---|---|
| `Choice(*values, default=None)` | one of a few named values; the first is the default | your nodes (usually `sense`) | yes |
| `Think(*values)` | a conclusion | your `@rules` (or a model, in a `reason` node) | yes |
| `Score(*levels)` | ordered levels, lowest first | a catalog model, through `prebuilt.Classify` | yes |
| `Int(default=0)` | a whole number (int32): memory between runs, counters, timestamps | your nodes and tools | **no** |

Values are `lower_snake_case` strings. Setting a value that isn't one of the field's values
raises an error on the PC, so mistakes show up before you flash.

## Why rules can't read `Int`

The build checks your agent by running **every combination** of `Choice`, `Think` and `Score`
values through both the C and the Python. That's only possible because those fields have a
few values each. An `Int` has four billion, so rules and conditions may not read them. Turn a
number into a `Choice` in a node instead:

```python
def sense(s: Pet):
    s.fed_recently = "yes" if clock.minutes() - s.fed_at < 180 else "no"   # Int -> Choice
```

## Fields the framework fills

Some prebuilt nodes expect fields with specific names. The graph checks they exist, and if
one is missing it prints the line to add:

| Node | Fields |
|---|---|
| `prebuilt.Mailbox(serves=[feed])` | `feed_asked = Choice("no", "yes")`, `feed_<param> = Choice(<its values>)` |
| `prebuilt.Mailbox(uses={feeder: [feed]})` | `feeder_online = Choice("no", "yes")`, `feed_reply = Choice("none", <replies>)` |
| `prebuilt.Classify({"unusual": ...})` | `unusual = Score(...)` |

## After a run

| | |
|---|---|
| `s.plan` | the tool names that ran, e.g. `['dispense']` |
| `s.waiting` | the question, if the run paused for a human; otherwise `None` |
