# Flash a board

`python agent.py build <board>` makes a complete Arduino sketch in `build/<board>/<name>/`.

## 1. Edit `config.h`

The build writes `config.h` once and never overwrites it, so your settings survive rebuilds.

| Setting | What it is |
|---|---|
| `WIFI_SSID`, `WIFI_PASSWORD` | your Wi-Fi (2.4 GHz). Empty: the board starts its own network, page at `http://192.168.4.1` |
| `HOSTNAME` | the page is at `http://<hostname>.local` |
| `APPROVAL_PIN` | needed to approve risky tools from the web page (made up at build time) |
| `TIMEZONE` | a POSIX TZ string, e.g. `"PKT-5"`, `"CET-1CEST,M3.5.0,M10.5.0/3"` |
| `RUN_EVERY_MS` | how often the graph runs (default 30 s; 0 = only when you type `r`) |
| `SERIAL_LOG` | 0 prints one summary line per run (for timing) |
| `LOG_DAYS` | days of data log kept on the flash |
| `NET_KEY` | boards that talk to each other need the same key ([Multi-agent](../guides/multi-agent.md)) |
| `MCP_KEY` | the key AI clients send ([MCP server](../guides/mcp.md)) |

## 2. Tools menu

The build prints the exact settings for your board. For reference:

=== "ESP32-S3"

    | Setting | Value |
    |---|---|
    | Board | ESP32S3 Dev Module |
    | Flash Size | 16MB |
    | PSRAM | OPI PSRAM |
    | Partition Scheme | 16M Flash (3MB APP/9.9MB FATFS) |

=== "NodeMCU (ESP8266)"

    | Setting | Value |
    |---|---|
    | Board | NodeMCU 1.0 (ESP-12E Module) |
    | Flash Size | 4MB (FS:2MB OTA:~1019KB) |
    | Upload Speed | 115200 is the most reliable |

## 3. Upload and watch

Upload, open the Serial Monitor at **115200**, and press reset:

```text
tiny-langgraph: feeder on nodemcu
joining Wi-Fi "my-wifi".......
open  http://192.168.1.47/   or   http://feeder.local/
MCP:  http://192.168.1.47/mcp   2 tools, key in config.h (MCP_KEY)
free heap: 8984 bytes

[run 1]
    inputs: asked=no fed_recently=no
    want: nothing
    planner: (nothing) (cost 0)
  -> done after 6 steps (planner 119 us, verify 97 us, run 11.69 ms, free heap 8656)
```

In the Serial Monitor, `r` runs the graph now, and `y`/`n` answer a pending approval.

## 4. Open the page

Open the address it printed. The page shows every run live: the graph, the path taken, what
was wanted, the plan, and why. See [Web page and trace viewer](../guides/web.md).
