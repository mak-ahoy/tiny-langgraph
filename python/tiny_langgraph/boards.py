"""
The boards an agent can be built for. Pick one when you build:

    app.build("esp32s3")          # or: python agent.py build nodemcu
    app.build("nodemcu")

Each board is a profile: its Arduino core, what it has (PSRAM, camera, file system, pull-downs),
its pin names, the pins you must not use (an error) and the ones that need care (a warning).
The agent code is the same for every board; the build checks it fits the board you picked.
To add a board: add a profile here and its #if branch in ports/arduino/agent and the drivers.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class BoardError(Exception):
    pass


@dataclass(frozen=True)
class Board:
    name: str                       # what you pass to build()
    title: str
    core: str                       # Arduino core (Boards Manager): "esp32:esp32", "esp8266:esp8266"
    fqbn: str                       # board id for arduino-cli
    setup: tuple                    # Arduino IDE steps (Tools menu)
    ram_kb: int
    flash_kb: int
    psram: bool = False             # room for the tiny transformer (a reason node)
    fpu: bool = True
    log_days: int = 30              # data log default (config.h)
    plan_states: int = 4096         # planner workspace for agents with more than 8 goals (22 bytes each)
    pins: dict = field(default_factory=dict)          # name -> GPIO, e.g. "D1": 5
    reserved: dict = field(default_factory=dict)      # GPIO -> why it can't be used
    careful: dict = field(default_factory=dict)       # GPIO -> what to watch out for
    pulldown: frozenset | None = None                 # GPIOs with an internal pull-down (None = all)
    analog: frozenset | None = None                   # GPIOs that can read analog (None = all)
    cameras: dict = field(default_factory=dict)       # camera model -> GPIOs it takes

    def gpio(self, pin) -> int:
        """A pin as a GPIO number: 5, or a name this board knows ("D1")."""
        if isinstance(pin, bool) or not isinstance(pin, (int, str)):
            raise BoardError(f"pin {pin!r}: use a GPIO number or a pin name like 'D1'")
        if isinstance(pin, int):
            return pin
        key = pin.upper()
        if key.startswith("GPIO") and key[4:].isdigit():
            return int(key[4:])
        if key not in self.pins:
            names = ", ".join(self.pins) or "none: use GPIO numbers"
            raise BoardError(f"pin {pin!r} isn't a {self.title} pin name (names: {names})")
        return self.pins[key]

    def label(self, gpio: int) -> str:
        names = [n for n, g in self.pins.items() if g == gpio]
        return f"{names[0]} (GPIO{gpio})" if names else f"GPIO{gpio}"


ESP32S3 = Board(
    name="esp32s3", title="ESP32-S3", core="esp32:esp32",
    fqbn="esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB",
    setup=('Board "ESP32S3 Dev Module"', "Flash Size 16MB", 'PSRAM "OPI PSRAM"',
           'Partition Scheme "16M Flash (3MB APP/9.9MB FATFS)"'),
    ram_kb=320, flash_kb=16384, psram=True, fpu=True, log_days=30, plan_states=16384,   # 352 KB, in PSRAM
    reserved={19: "USB", 20: "USB", **{p: "flash" for p in range(26, 33)}, **{p: "PSRAM" for p in range(33, 38)}},
    cameras={"esp32s3_eye": frozenset({4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18}),
             "xiao_esp32s3": frozenset({10, 11, 12, 13, 14, 15, 16, 17, 18, 38, 39, 40, 47, 48})},
)

NODEMCU = Board(
    name="nodemcu", title="NodeMCU (ESP8266)", core="esp8266:esp8266",
    fqbn="esp8266:esp8266:nodemcuv2:eesz=4M2M",
    setup=('Board "NodeMCU 1.0 (ESP-12E Module)"', 'Flash Size "4MB (FS:2MB OTA:~1019KB)"'),
    ram_kb=80, flash_kb=4096, psram=False, fpu=False, log_days=10, plan_states=1024,     # 22 KB of heap
    pins={"D0": 16, "D1": 5, "D2": 4, "D3": 0, "D4": 2, "D5": 14, "D6": 12, "D7": 13, "D8": 15,
          "RX": 3, "TX": 1, "A0": 17},
    reserved={1: "the USB serial port (TX)", 3: "the USB serial port (RX)",
              **{p: "the flash chip" for p in range(6, 12)}},
    careful={0: "the FLASH button: fine as an input, but don't hold it LOW while the board starts",
             2: "the built-in LED (on when LOW); it must not be held LOW while the board starts",
             15: "must be LOW while the board starts (it has a pull-down on the board): avoid for inputs",
             16: "no PWM or interrupts on D0"},
    pulldown=frozenset({16}),
    analog=frozenset({17}),
)

BOARDS = {b.name: b for b in (ESP32S3, NODEMCU)}


def get(name: str) -> Board:
    if name not in BOARDS:
        raise BoardError(f"unknown board {name!r}; boards: {', '.join(BOARDS)} (or 'c' for just the C files)")
    return BOARDS[name]


def any_gpio(pin) -> int:
    """For the board-independent C files: a pin name resolved by the first board that knows it."""
    if isinstance(pin, int) and not isinstance(pin, bool):
        return pin
    for b in BOARDS.values():
        try:
            return b.gpio(pin)
        except BoardError:
            continue
    raise BoardError(f"pin {pin!r} isn't a pin name of any board ({', '.join(BOARDS)})")


def check(board: Board, app, conv, warn) -> None:
    """Does the agent fit this board? Raises BoardError for what can't work, warn() for what needs care."""
    from .drivers import AnalogIn, Camera, DigitalIn
    if app.uses_model and not board.psram:
        raise BoardError(f"the {board.title} has no room for the tiny transformer (a reason node needs PSRAM): "
                         "build it planner-only (no reason node), e.g. USE_MODEL = False")
    used, taken = {}, {}
    for _, obj in conv.drivers.values():
        if isinstance(obj, Camera):
            if obj.model not in board.cameras:
                raise BoardError(f"the {board.title} has no camera support ({obj.model}); "
                                 f"boards with a camera: {', '.join(b.name for b in BOARDS.values() if b.cameras)}")
            for g in board.cameras[obj.model]:
                taken[g] = f"the camera ({obj.model})"
    for g, why in board.reserved.items():
        taken.setdefault(g, why)
    for name, obj in conv.drivers.values():
        pin = getattr(obj, "pin", None)
        if pin is None:
            continue
        g = board.gpio(pin)
        where = board.label(g)
        if g in taken:
            raise BoardError(f"{name} uses {where}, which {taken[g]} needs on the {board.title}; pick another pin")
        if g in used:
            raise BoardError(f"{name} and {used[g]} both use {where}")
        used[g] = name
        if g in board.careful:
            warn(f"{name} on {where}: {board.careful[g]}")
        if isinstance(obj, DigitalIn) and obj.pulldown and board.pulldown is not None and g not in board.pulldown:
            ok = ", ".join(board.label(p) for p in sorted(board.pulldown))
            raise BoardError(f"{name}: the {board.title} has no internal pull-down on {where} (only on {ok}); "
                             "add a 10 kOhm resistor from the pin to GND and drop pulldown=True, or move it")
        if isinstance(obj, AnalogIn) and board.analog is not None and g not in board.analog:
            ok = ", ".join(board.label(p) for p in sorted(board.analog))
            raise BoardError(f"{name}: the {board.title} reads analog only on {ok}")
