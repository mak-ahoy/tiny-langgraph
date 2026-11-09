"""
Hardware for your agent. Create them at the top of your file with your pins:

    light  = LED(pin=21)
    feeder = Servo(pin=13)
    motion = DigitalIn(pin=12)

and use them in nodes and tools:  light.on(),  feeder.turn(90),  if motion.read(): ...

On your PC they are simulated: outputs print what they would do, and inputs return
whatever you set, e.g.  motion.value = True,  clock.set(hour=22).
On the board, app.build("esp32s3") wires them to the real pins.

Your own hardware: subclass CDriver, write the C functions, and give the methods a
Python body for the PC:

    class Feeder(CDriver):
        c_files = ["feeder.c"]          # next to your agent file: void feeder_dispense(int32_t portions)
        def dispense(self, portions: int) -> None:
            print("feeding", portions)
"""
from __future__ import annotations

import inspect
import typing

ECHO = True          # print simulated hardware actions on the PC


def quiet(on: bool = True):
    """Stop (or restart) printing simulated hardware actions."""
    global ECHO
    ECHO = not on


class Driver:
    c_prefix: str = ""            # C functions are named <c_prefix>_<method>
    c_struct: str | None = None   # C type of the instance (pins), if any
    c_files: list[str] = []
    c_header: str = "tlg_drivers.h"

    def __init__(self, name: str | None = None):
        self.label = name or type(self).__name__.lower()

    def _echo(self, text):
        if ECHO:
            print(f"  {self.label:>8} > {text}")

    # ---- what the C side needs ----
    def c_init(self) -> dict:
        """Fields of the C instance struct."""
        return {}

    @classmethod
    def c_methods(cls) -> dict[str, tuple[list[tuple[str, str]], str]]:
        """method name -> ([(param, type)], return type) with types int, bool, str, None."""
        out = {}
        for name, fn in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_") or name in ("c_init", "c_methods", "set"):
                continue
            hints = typing.get_type_hints(fn)
            params = []
            for p in list(inspect.signature(fn).parameters)[1:]:
                t = hints.get(p)
                if t not in (int, bool, str):
                    raise TypeError(f"{cls.__name__}.{name}: annotate '{p}' as int, bool or str")
                params.append((p, t.__name__))
            ret = hints.get("return", type(None))
            if ret not in (int, bool, type(None)):
                raise TypeError(f"{cls.__name__}.{name}: return int, bool or None")
            out[name] = (params, "None" if ret is type(None) else ret.__name__)
        return out


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------
class LED(Driver):
    c_prefix, c_struct = "tlg_led", "tlg_led_t"

    def __init__(self, pin: int, active_low: bool = False, name=None):
        super().__init__(name)
        self.pin, self.active_low, self.lit = pin, active_low, False

    def c_init(self):
        return {"pin": self.pin, "active_low": self.active_low}

    def on(self) -> None:
        self.lit = True
        self._echo("on")

    def off(self) -> None:
        self.lit = False
        self._echo("off")

    def blink(self, times: int, ms: int) -> None:
        self._echo(f"blink {times}x {ms} ms")


class Buzzer(Driver):
    c_prefix, c_struct = "tlg_buzzer", "tlg_buzzer_t"

    def __init__(self, pin: int, name=None):
        super().__init__(name)
        self.pin = pin

    def c_init(self):
        return {"pin": self.pin}

    def beep(self, ms: int) -> None:
        self._echo(f"beep {ms} ms")

    def tone(self, hz: int, ms: int) -> None:
        self._echo(f"tone {hz} Hz {ms} ms")


class Servo(Driver):
    c_prefix, c_struct = "tlg_servo", "tlg_servo_t"

    def __init__(self, pin: int, name=None):
        super().__init__(name)
        self.pin, self.angle = pin, 0

    def c_init(self):
        return {"pin": self.pin}

    def turn(self, angle: int) -> None:
        self.angle = max(0, min(180, angle))
        self._echo(f"turn to {self.angle} degrees")


class Phone(Driver):
    """A message to the owner. Today it appears on the board's web page."""
    c_prefix = "tlg_phone"

    def __init__(self, name=None):
        super().__init__(name)
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)
        self._echo(f"message: {text}")


class Camera(Driver):
    """The board's camera. snapshot() keeps a photo, served at /snapshot.jpg."""
    c_prefix, c_struct = "tlg_camera", "tlg_camera_t"
    MODELS = ("esp32s3_eye", "xiao_esp32s3")

    def __init__(self, model: str = "esp32s3_eye", name=None):
        super().__init__(name)
        if model not in self.MODELS:
            raise ValueError(f"Camera model must be one of {self.MODELS} (esp32s3_eye also fits Freenove ESP32-S3 boards)")
        self.model, self.photos = model, 0

    def c_init(self):
        return {"model": self.MODELS.index(self.model)}

    def snapshot(self) -> bool:
        self.photos += 1
        self._echo(f"snapshot #{self.photos}")
        return True


# --------------------------------------------------------------------------
# Inputs (set .value on the PC to simulate them)
# --------------------------------------------------------------------------
class DigitalIn(Driver):
    """A digital sensor, e.g. a PIR motion sensor or a reed switch.

    pulldown=True holds the pin LOW when nothing drives it (a PIR, or a pin with nothing
    connected yet); without it an unconnected pin reads random values. pullup=True holds
    it HIGH instead (a switch or button to ground, usually with active_low=True)."""
    c_prefix, c_struct = "tlg_input", "tlg_input_t"

    def __init__(self, pin: int, pullup: bool = False, active_low: bool = False, pulldown: bool = False, name=None):
        super().__init__(name)
        if pullup and pulldown:
            raise ValueError(f"DigitalIn(pin={pin}): choose pullup or pulldown, not both")
        self.pin, self.pullup, self.pulldown, self.active_low, self.value = pin, pullup, pulldown, active_low, False

    def c_init(self):
        return {"pin": self.pin, "pullup": self.pullup, "pulldown": self.pulldown, "active_low": self.active_low}

    def read(self) -> bool:
        return bool(self.value)


class Button(DigitalIn):
    """A push button to ground (like BOOT on pin 0)."""

    def __init__(self, pin: int = 0, name=None):
        super().__init__(pin, pullup=True, active_low=True, name=name)

    def pressed(self) -> bool:
        return bool(self.value)


class AnalogIn(Driver):
    """An analog sensor: read() gives 0..4095."""
    c_prefix, c_struct = "tlg_analog", "tlg_analog_t"

    def __init__(self, pin: int, name=None):
        super().__init__(name)
        self.pin, self.value = pin, 0

    def c_init(self):
        return {"pin": self.pin}

    def read(self) -> int:
        return int(self.value)


class Clock(Driver):
    """Time of day (from the internet when the board has Wi-Fi) and time since start."""
    c_prefix = "tlg_clock"

    def __init__(self, name=None):
        super().__init__(name)
        self._hour, self._minutes = 12, 0

    def set(self, hour: int | None = None, minutes: int | None = None):
        """On the PC: pretend it's this hour / this many minutes since start."""
        if hour is not None:
            self._hour = hour % 24
        if minutes is not None:
            self._minutes = minutes

    def hour(self) -> int:
        return self._hour

    def minutes(self) -> int:
        return self._minutes


class Wifi(Driver):
    c_prefix = "tlg_wifi"

    def __init__(self, name=None):
        super().__init__(name)
        self.value = True

    def connected(self) -> bool:
        return bool(self.value)


# --------------------------------------------------------------------------
# Your own hardware
# --------------------------------------------------------------------------
class CDriver(Driver):
    """Base for your own drivers: C functions named <prefix>_<method>, where prefix is the
    class name in lower case (or c_prefix). An optional init() runs once at startup."""
    c_header = None

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if not cls.c_prefix:
            cls.c_prefix = "".join("_" + ch.lower() if ch.isupper() and i else ch.lower()
                                   for i, ch in enumerate(cls.__name__))
