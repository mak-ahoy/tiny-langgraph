# Installation

## What you need

| | Needed for |
|---|---|
| **Python 3.9+** | writing and running agents |
| A C compiler (gcc or clang) | optional: checking the C against your Python on the PC. Without one, that check is skipped (CI always runs it). |
| **Arduino IDE** with the ESP32 or ESP8266 board package | putting an agent on a board |
| PyTorch | optional: only to train the tiny transformer |

## Install

tiny-langgraph isn't on PyPI yet. Install it from the repository:

```sh
git clone https://github.com/mak-ahoy/tiny-langgraph
cd tiny-langgraph
pip install -e .                 # numpy, pyyaml
pip install -e ".[train]"        # optional: adds torch, to train a model
```

Check it works:

```sh
cd examples/pet_watcher
python agent.py
```

You should see the agent run a few situations on your PC, with simulated hardware.

## Board packages for the Arduino IDE

In **File → Preferences → Additional boards manager URLs**, add whichever you need:

```text
https://espressif.github.io/arduino-esp32/package_esp32_index.json
https://arduino.esp8266.com/stable/package_esp8266com_index.json
```

Then install **esp32** and/or **esp8266** in **Tools → Board → Boards Manager**.

??? tip "Windows: NodeMCU upload fails with 'A device attached to the system is not functioning'"
    Many NodeMCU boards use a clone CH340 USB chip, and newer CH340 drivers (3.8 and later)
    don't work with it. Install driver **3.4.2014.8**. In Device Manager, right-click the CH340
    device → **Update driver** → **Let me pick**, and choose 3.4. If uploads then time out at
    `Connecting...`, hold **FLASH**, tap **RST**, and release FLASH.

## Run the tests

```sh
python -m unittest discover -s tests -p "test_*.py"
make check          # Linux/macOS: the C tests, examples, sanitizers and everything above
```
