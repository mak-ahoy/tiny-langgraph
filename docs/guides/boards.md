# Boards

```sh
python agent.py build esp32s3     # default
python agent.py build nodemcu
python agent.py build c           # just the C files, for your own firmware
```

| | ESP32-S3 | NodeMCU (ESP8266) |
|---|---|---|
| RAM | 320 KB + 8 MB PSRAM | 80 KB (~9–40 KB free, depending on the agent) |
| Flash | 16 MB | 4 MB |
| Agent runs | in its own task (the web page stays responsive) | in `loop()` |
| Planner workspace (more than 8 goals) | 16,384 states in PSRAM | 1,024 states |
| Tiny transformer (`prebuilt.reason`) | yes | no: not enough RAM |
| Camera | ESP32-S3-EYE and XIAO ESP32S3 pin maps | no |
| Data log | FFat, 30 days by default | LittleFS, 10 days |
| Measured plan + verify | ~40 + ~37 µs | ~120 + ~100 µs |

## Pins

Use the board's own pin names: GPIO numbers on the ESP32-S3, and `"D0"`…`"D8"`, `"A0"` on a
NodeMCU. The build checks the pins against the board and stops with an explanation if
something can't work:

| Check | Example |
|---|---|
| two drivers on one pin | stops |
| a pin used by USB, flash, PSRAM or the camera | stops |
| `pulldown=True` on a NodeMCU pin other than D0 | stops: add a 10 kΩ resistor to GND instead |
| analog input on a NodeMCU pin other than A0 | stops |
| a NodeMCU boot pin (D3, D4, D8) | a warning: fine as an input, but it mustn't be held LOW (D8: HIGH) while the board starts |

## Free memory on the NodeMCU

The sketch prints `free heap` at boot and after every run. The NodeMCU needs a few KB free for
Wi-Fi and web requests. The two-board feeder example has ~9 KB free: enough, but tight.

## Your own firmware

`build c` writes plain C11 (`<name>_agent.c/.h`, `<name>_rules.c` and the runtime) with drivers
behind `tlg_drivers.h`. See [C runtime](../reference/c-runtime.md).
