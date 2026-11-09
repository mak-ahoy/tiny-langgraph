"""
Tests for board profiles (python/tiny_langgraph/boards.py): pin names, what each board can't do,
and that build() makes a project per board.

    python -m unittest discover -s tests -p "test_*.py"
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_python_api import HOME, load  # noqa: E402
from tiny_langgraph import boards, cagent  # noqa: E402
from tiny_langgraph.build import BuildError, build, check_pins  # noqa: E402

PORCH = os.path.join(ROOT, "examples", "porch_light", "agent.py")


def porch(**swap):
    with open(PORCH) as f:
        src = f.read()
    for old, new in swap.items():
        assert old in src, old
        src = src.replace(old, new)
    return load(src)


def check(m, board):
    _, conv = cagent.generate(m.app, tempfile.mkdtemp(), "agent.py")
    warnings = []
    check_pins(m.app, conv, board, warn=warnings.append)
    return warnings


class Pins(unittest.TestCase):
    def test_names(self):
        self.assertEqual(boards.NODEMCU.gpio("D1"), 5)
        self.assertEqual(boards.NODEMCU.gpio("d4"), 2)
        self.assertEqual(boards.NODEMCU.gpio("GPIO14"), 14)
        self.assertEqual(boards.ESP32S3.gpio(21), 21)
        with self.assertRaisesRegex(boards.BoardError, "isn't a ESP32-S3 pin name"):
            boards.ESP32S3.gpio("D1")
        self.assertEqual(boards.any_gpio("D7"), 13)

    def test_get(self):
        self.assertIs(boards.get("nodemcu"), boards.NODEMCU)
        with self.assertRaisesRegex(boards.BoardError, "unknown board 'uno'"):
            boards.get("uno")


class NodeMCU(unittest.TestCase):
    def test_porch_light_fits_with_warnings(self):
        warnings = check(porch(), "nodemcu")
        self.assertTrue(any("D0 (GPIO16)" in w for w in warnings), warnings)
        self.assertTrue(any("D4 (GPIO2): the built-in LED" in w for w in warnings), warnings)

    def test_no_pulldown_except_d0(self):
        m = porch(**{'motion = DigitalIn(pin="D0", pulldown=True)': 'motion = DigitalIn(pin="D1", pulldown=True)'})
        with self.assertRaisesRegex(BuildError, "no internal pull-down on D1 \\(GPIO5\\).*10 kOhm resistor"):
            check(m, "nodemcu")

    def test_serial_pins_are_taken(self):
        m = porch(**{'buzzer = Buzzer(pin="D5")': 'buzzer = Buzzer(pin="TX")'})
        with self.assertRaisesRegex(BuildError, "TX \\(GPIO1\\), which the USB serial port"):
            check(m, "nodemcu")

    def test_no_model_and_no_camera(self):
        m = load(HOME)                                   # has a reason node (the tiny transformer)
        with self.assertRaisesRegex(BuildError, "no room for the tiny transformer"):
            check(m, "nodemcu")
        cam = load(HOME.replace("siren = LED(pin=2)", "siren = LED(pin=4)\ncam = Camera()")
                   .replace("    siren.blink(3, 100)", "    siren.blink(3, 100)\n    cam.snapshot()")
                   .replace('graph.add_node("reason", prebuilt.reason)\n', "")
                   .replace('graph.add_edge("assess", "reason")\ngraph.add_edge("reason", "verify")\n',
                            'graph.add_edge("assess", "plan")\n'))
        with self.assertRaisesRegex(BuildError, "has no camera support"):
            check(cam, "nodemcu")


class Build(unittest.TestCase):
    def test_a_project_per_board(self):
        m = porch()
        logs = []
        r = build(m.app, "nodemcu", log=logs.append)
        self.assertEqual(os.path.relpath(r["out"], m.app.dir), os.path.join("build", "nodemcu", "porch_light"))
        files = os.listdir(r["out"])
        self.assertIn("porch_light.ino", files)
        with open(os.path.join(r["out"], "tlg_build.h")) as f:
            self.assertIn('#define TLG_BOARD "nodemcu"', f.read())
        with open(os.path.join(r["out"], "config.h")) as f:
            self.assertIn("#define LOG_DAYS         10", f.read())        # the NodeMCU's smaller default
        with open(os.path.join(r["out"], "porch_light_main.cpp")) as f:
            self.assertIn('Board "NodeMCU 1.0 (ESP-12E Module)"', f.read())
        self.assertTrue(any("Tools menu: Board \"NodeMCU 1.0" in x for x in logs), logs)

    def test_old_config_is_kept(self):
        """Before boards existed the sketch was build/<name>/: its config.h (Wi-Fi, PIN) carries over."""
        m = porch()
        old = os.path.join(m.app.dir, "build", "porch_light")
        os.makedirs(old)
        with open(os.path.join(old, "config.h"), "w") as f:
            f.write("#define WIFI_SSID \"my wifi\"\n")
        r = build(m.app, "nodemcu", log=lambda _: None)
        with open(os.path.join(r["out"], "config.h")) as f:
            self.assertIn("my wifi", f.read())

    def test_unknown_board(self):
        with self.assertRaisesRegex(BuildError, "unknown board 'uno'.*esp32s3, nodemcu"):
            build(porch().app, "uno", log=lambda _: None)


if __name__ == "__main__":
    unittest.main()
