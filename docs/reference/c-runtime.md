# C runtime and schema.yaml

Under the Python API is a small C11 runtime you can also use directly.

| | |
|---|---|
| `core/tlg` | the graph: static node tables, edges, routes, interrupts, checkpoints, validation |
| `core/tlg_plan` | the planner and verifier |
| `core/tlg_trace`, `tlg_hub` | trace lines, and the buffer the web page reads |
| `core/tlg_net` | messages between boards (contracts, mailbox, announcements) |
| `core/tlg_mcp` | the MCP endpoint |
| `core/tlg_count` | `models.Counting` |
| `lm/tlg_lm` | the transformer inference engine (float32) |
| `drivers/` | `tlg_drivers.h`, with PC (prints) and Arduino implementations |

## A graph in C

```c
static const tlg_node_t NODES[] = {
    [SENSE]  = { "sense",  sense,  TLG_EDGE(ASSESS) },
    [VERIFY] = { "verify", verify, TLG_ROUTE(after_verify, ACT, ASK, PLAN, FALLBACK) },
    ...
};
```

- **No heap:** graphs are static tables, so nothing fails at runtime because memory ran out.
- **Every route declares where it can go,** so `tlg_validate()` catches broken graphs before they run.
- **A paused run is plain data** (`tlg_run_t`): save it and resume after a reboot (`examples/plant_care`).

```sh
make demo      # hello, plant_care, and the door guard with its on-device model
make trace     # build/door_guard.log, for the viewer
```

## schema.yaml

The door guard (`examples/door_guard`) is written the lower-level way: the agent's inputs,
scores, rules and tools in `schema.yaml`, and the graph by hand in C.

```yaml
scores:                     # points per observation, added up
  suspicion:
    tod:    { night: 2 }
    expect: { owner: -2, milkman: -2, courier: -2 }
rules:
  think:
    threat:                 # first matching rule wins
      - hi: visitor == hi and suspicion >= 3
      - lo: true
  want:
    informed_high: threat == hi
```

```sh
python tools/tlg.py check examples/door_guard/schema.yaml   # validate
python tools/tlg.py all   examples/door_guard/schema.yaml   # generate C, make data, train
```
