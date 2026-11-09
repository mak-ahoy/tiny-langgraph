# MCP server

Every board serves the [Model Context Protocol](https://modelcontextprotocol.io) at
`http://<board>/mcp`, so AI clients like Claude Code can list a board's tools and call them.

**The client only asks.** An MCP call enters the board's mailbox like a request from another
board, the agent runs, its rules and verifier decide, and the reply is the tool's result.

## Tools a board offers

| Tool | What it is |
|---|---|
| one per served contract, e.g. `feed` | parameters as string enums (`portions`: `one`/`two`), the replies as the output schema |
| `<board>_status` | read-only: the board's current fields and its last plan |

## Connect Claude Code

```sh
claude mcp add --transport http feeder http://192.168.1.47/mcp --header "Authorization: Bearer <MCP_KEY>"
```

`MCP_KEY` is in the board's `config.h`; the build makes up a random one. Then, in a new session:

> *Ask the feeder to feed the cat one portion.*

The answer is `done`, or `refused` if the feeder's rule says it was fed recently.

## Without a board

```sh
cd examples/feeding_station
python serve.py          # the feeder on http://127.0.0.1:8765/mcp, key "feeder-demo"
```

`McpServer` in Python answers exactly like the board's C (the tests send both the same session):

```python
from tiny_langgraph.net import McpServer
McpServer(endpoint, key="secret", about="Feeds the cat").serve(8765)
```

## Details

| | |
|---|---|
| Transport | Streamable HTTP: JSON-RPC 2.0 `POST /mcp`, plain JSON answers |
| Methods | `initialize`, notifications, `ping`, `tools/list`, `tools/call` |
| Protocol versions | 2025-06-18, 2025-03-26, 2024-11-05 |
| Waiting | a call waits up to `MCP_WAIT_MS` (10 s) for the board's answer. On the ESP32 it answers `busy` while the board waits for a human approval. On the NodeMCU the agent runs during the request. |
| Fingerprints | in `_meta.tiny_langgraph` of each tool, so another PC can import the contract |

!!! warning "Local network only"
    Plain HTTP with a bearer key: the key travels unencrypted. Keep the board on your own
    network.
