# Coming from LangGraph

| LangGraph | tiny-langgraph |
|---|---|
| `TypedDict` state | `class S(State)` with `Choice`, `Think`, `Score`, `Int` fields: fixed types, a C struct on the board |
| `add_node`, `add_edge`, `add_conditional_edges`, `START`, `END` | the same |
| `compile()`, `invoke()`, `stream()` | the same |
| `get_graph().draw_mermaid()` | the same |
| tools + `ToolNode` | `@tool(cost, achieves, needs, after, approval)` + `prebuilt.Act([...])` |
| the LLM decides what to call | your `@rules` say what's wanted; the planner picks the tools; the verifier checks |
| `interrupt()` + checkpointer | `prebuilt.ask` / `interrupt()`; `state.waiting`, then `app.resume(state, True)` |
| multi-agent, subgraphs | agents on **different boards**, talking through contracts ([Multi-agent](../guides/multi-agent.md)) |
| tools exposed over MCP | every board is an MCP server ([MCP server](../guides/mcp.md)) |
| parallel branches | not supported: one way out of each node |
| any Python | a subset that converts to C ([The Python you can use](python-subset.md)) |

## What's different, and why

- **No LLM in the loop by default.** A microcontroller can't hold one. Your rules are the
  "reasoning", and they're checked exhaustively at build time.
- **Every decision is verifiable.** The verifier runs on the device before any tool, whoever
  proposed the plan.
- **Everything is fixed at build time:** the fields, tools, goals and contracts. That's what
  lets the build prove the C behaves like your Python.
