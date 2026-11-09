# Drivers

Create hardware at the top of your agent file, with your pins, and use it in nodes and tools.
On the PC, outputs print what they would do and inputs return whatever you set.

```python
from tiny_langgraph.drivers import LED, Buzzer, Servo, DigitalIn, Button, AnalogIn, Camera, Clock, Wifi, Phone

light  = LED(pin=21)
motion = DigitalIn(pin=12, pulldown=True)
feeder = Servo(pin=13)
clock  = Clock()
```

## Built in

| Driver | Methods | On the PC |
|---|---|---|
| `LED(pin, active_low=False)` | `on()`, `off()`, `blink(times, ms)` | prints |
| `Buzzer(pin)` | `beep(ms)`, `tone(hz, ms)` | prints |
| `Servo(pin)` | `turn(angle)` | prints |
| `DigitalIn(pin, pullup=False, pulldown=False, active_low=False)` | `read() -> bool` | `motion.value = True` |
| `Button(pin=0)` | `pressed() -> bool` (to GND, like BOOT) | `button.value = True` |
| `AnalogIn(pin)` | `read() -> int` (0..4095) | `sensor.value = 2048` |
| `Camera(model="esp32s3_eye")` | `snapshot() -> bool`, served at `/snapshot.jpg` | |
| `Clock()` | `hour()`, `minutes()` (internet time on the board) | `clock.set(hour=22, minutes=...)` |
| `Wifi()` | `connected() -> bool` | |
| `Phone()` | `send(text)`: a message on the board's web page | prints |

`drivers.quiet()` stops the PC printing.

## Your own hardware

Write the C, and give Python a stand-in for the PC:

```python
from tiny_langgraph.drivers import CDriver

class Feeder(CDriver):
    c_files = ["feeder.c"]              # next to your agent file
    def dispense(self, portions: int) -> None:
        print("feeding", portions)      # what happens on the PC

hopper = Feeder()
```

```c
/* feeder.c: functions named <prefix>_<method>; prefix = the class name in lower case */
#include <stdint.h>
void feeder_init(void) { /* optional: runs once at startup */ }
void feeder_dispense(int32_t portions) { /* drive your motor */ }
```

Methods take and return whole numbers or booleans.
