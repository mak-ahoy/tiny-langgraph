# Results and limitations

All numbers below were measured on real boards unless marked otherwise.

## Model vs planner on the ESP32-S3

The pet watcher on an ESP32-S3-CAM, the same situations run both ways:

| | Tiny transformer (float32) | Planner only (default) |
|---|---|---|
| Proposing a plan | ~120 ms (119–145 ms) | 40 µs |
| Verifying it | included | 36–38 µs |
| Whole run, with Serial logging | ~132 ms | ~12 ms (~10 ms of it is printing) |
| Flash for the model | 184,540 bytes | 0 |
| Correct answers | 388 / 388 held out (it learned to copy the rules) | all 3,888 (it *is* the rules + planner) |

For decisions that depend only on the current moment, rules + the planner are ~3,000× faster
and right by construction, so planner-only is the default. Free heap stayed at ~181–188 KB
over 28 runs (no leak).

## Two boards and MCP

| | |
|---|---|
| Boards | watcher on an ESP32-S3, feeder on a NodeMCU, ESP-NOW on Wi-Fi channel 13 |
| Story | the watcher asks → the feeder feeds → asked again, it refuses by its own rule → the watcher tells the owner |
| NodeMCU run | 11.7 ms (planner 119 µs, verify 97 µs) |
| ESP32-S3 run | ~12 ms (planner ~33 µs, verify ~20 µs) |
| MCP over Wi-Fi (NodeMCU) | 0.3–0.5 s per call; Claude Code connects |

## Planning experiments (PC, synthetic agents)

| Agent | Board's planner | Tiny transformer (guided) |
|---|---|---|
| 32 goals, ~22 wanted at once | NodeMCU 86%, ESP32-S3 99% optimal | 99.8% |
| 32 goals, ~30 wanted at once | 88.6% on both boards | 99.8% |

Guided writing (only tools the verifier would allow) made every model plan safe. The model
earns its place only for very busy agents, and only together with the planner and the verifier.

## Limitations

- A Python subset on the device: no floats, lists or strings yet; inputs are categorical.
- Hardware tested so far: one ESP32-S3-CAM and one NodeMCU.
- Board-to-board messages aren't encrypted yet; MCP is plain HTTP with a bearer key.
- The transformer is float32 and ESP32-S3 only; an int8 engine isn't built yet.
- Up to 32 goals, 32 tools, 32 conditions per agent.

## Roadmap

- [x] Graph runtime, planner + verifier, trace viewer, Python API converted to C and checked
- [x] ESP32-S3 and NodeMCU, built-in and custom drivers, web page with approvals
- [x] Model catalog: Counting, Tree, tiny transformer (optional)
- [x] Multi-agent over ESP-NOW, MCP server on the board
- [ ] Encrypted messages between boards
- [ ] int8 model with ESP32-S3 SIMD
- [ ] More boards (Pico W, other ESP32s); ESP-IDF port
- [ ] Learning loop: log uncertain cases on the device, label them with a large LLM, retrain
