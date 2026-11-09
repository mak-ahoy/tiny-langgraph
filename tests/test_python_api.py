"""
Tests for the Python API: running on the PC, approvals, the Python-to-C converter
(including its error messages), and C-vs-Python agreement for rules and for nodes.

    python -m unittest discover -s tests -p "test_*.py"
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

from tiny_langgraph import GraphError, drivers  # noqa: E402
from tiny_langgraph.build import BuildError, build, check_pins  # noqa: E402
from tiny_langgraph import cagent  # noqa: E402
from tiny_langgraph.transpile import SubsetError  # noqa: E402

CC = shutil.which(os.environ.get("CC", "cc")) or shutil.which("gcc") or shutil.which("clang")
drivers.quiet()

HOME = '''
from tiny_langgraph import *
from tiny_langgraph.drivers import LED, DigitalIn, Clock, Camera

door = DigitalIn(pin=1)
siren = LED(pin=2)
clock = Clock()
POINTS = {"quiet": 0, "noise": 2, "loud": 5}

class Home(State):
    sound = Choice("quiet", "noise", "loud")
    night = Choice("no", "yes")
    worry = Think("low", "high")
    count = Int(0)
    total = Int(0)
    q = Int(0)
    r = Int(0)
    m = Int(0)

goals = Goals(logged=1, alarmed=Goal(5, only_if_wanted=True))

def is_night(s):
    return s.night == "yes"

def worried(s):
    return s.worry == "high"

@tool(cost=1, achieves=goals.logged)
def log(s):
    pass

@tool(cost=4, achieves=goals.alarmed, needs=worried, approval=True)
def alarm(s):
    siren.blink(3, 100)

def score(s):
    return POINTS[s.sound] + (2 if is_night(s) else 0)

@rules
def assess(s):
    s.worry = "high" if score(s) >= 5 else "low"
    want(goals.logged)
    if worried(s):
        want(goals.alarmed)

def sense(s):
    s.sound = "loud" if door.read() else "quiet"
    s.night = "yes" if clock.hour() >= 22 else "no"
    s.count += 1
    total = 0
    for i in range(1, 5):
        total += i * s.count
    s.total = total
    s.q = (-7 - s.count) // 2
    s.r = (-7 - s.count) % 3
    s.m = max(min(s.count, 3), abs(-2))

graph = StateGraph(Home)
graph.add_node("sense", sense)
graph.add_node("assess", assess)
graph.add_node("reason", prebuilt.reason)
graph.add_node("verify", prebuilt.verify)
graph.add_node("plan", prebuilt.plan)
graph.add_node("ask", prebuilt.ask)
graph.add_node("act", prebuilt.Act([log, alarm]))
graph.add_edge(START, "sense")
graph.add_edge("sense", "assess")
graph.add_edge("assess", "reason")
graph.add_edge("reason", "verify")
graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])
graph.add_edge("plan", "verify")
graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])
graph.add_edge("act", END)
app = graph.compile(name="home")
'''

# The same agent without a model: sense -> assess -> plan -> verify -> (ask) -> act
PLANNER_ONLY = (HOME.replace('graph.add_node("reason", prebuilt.reason)\n', "")
                .replace('graph.add_edge("assess", "reason")\ngraph.add_edge("reason", "verify")\n',
                         'graph.add_edge("assess", "plan")\n'))
assert PLANNER_ONLY.count("reason") == 0

_n = 0


def load(source):
    """Write an agent file to a temp folder and import it."""
    global _n
    _n += 1
    d = tempfile.mkdtemp(prefix="tlg_api_")
    path = os.path.join(d, "agent.py")
    with open(path, "w") as f:
        f.write(textwrap.dedent(source))
    spec = importlib.util.spec_from_file_location(f"tlg_test_agent_{_n}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Running(unittest.TestCase):
    def setUp(self):
        self.m = load(HOME)

    def test_quiet(self):
        m = self.m
        m.clock.set(hour=12)
        s = m.app.invoke(m.Home())
        self.assertEqual(s.worry, "low")
        self.assertEqual(s.plan, ["log"])
        self.assertIsNone(s.waiting)
        self.assertEqual(s.count, 1)

    def test_approval_pauses_and_resumes(self):
        m = self.m
        m.door.value = True
        m.clock.set(hour=23)
        s = m.app.invoke(m.Home())
        self.assertEqual(s.waiting, "approve alarm?")
        m.app.resume(s, True)
        self.assertIsNone(s.waiting)
        self.assertEqual(s.plan, ["log", "alarm"])

    def test_refusal_replans_without_the_tool(self):
        m = self.m
        m.door.value = True
        s = m.app.invoke(m.Home(), approve=lambda q: False)
        self.assertEqual(s.plan, ["log"])
        self.assertTrue(s._agent.acted_ok)

    def test_memory_survives_runs(self):
        m = self.m
        s = m.Home()
        for _ in range(3):
            m.app.invoke(s)
        self.assertEqual(s.count, 3)

    def test_trace_file(self):
        m = self.m
        path = os.path.join(tempfile.mkdtemp(), "run.log")
        m.app.invoke(m.Home(), trace=path, label="first")
        lines = open(path).read().splitlines()
        self.assertTrue(lines[0].startswith('@tlg {"k":"graph","name":"home"'))
        self.assertIn('"k":"end"', lines[-1])
        self.assertIn('"status":"done"', lines[-1])

    def test_field_validation(self):
        m = self.m
        with self.assertRaisesRegex(ValueError, "use one of quiet, noise, loud"):
            m.Home(sound="banging")
        with self.assertRaisesRegex(AttributeError, "no field"):
            m.Home().colour = "red"

    def test_mermaid(self):
        text = self.m.app.get_graph().draw_mermaid()
        self.assertIn('n0["sense"]', text)
        self.assertIn("-.->", text)


class PlannerOnly(unittest.TestCase):
    """No reason node: the planner proposes, the verifier still checks, approvals still work."""

    def setUp(self):
        self.m = load(PLANNER_ONLY)

    def test_uses_model(self):
        self.assertFalse(self.m.app.uses_model)
        self.assertTrue(load(HOME).app.uses_model)

    def test_quiet(self):
        m = self.m
        m.clock.set(hour=12)
        s = m.app.invoke(m.Home())
        self.assertEqual(s.plan, ["log"])
        self.assertEqual(s._agent.verdict, "ok")

    def test_approval_pauses_and_resumes(self):
        m = self.m
        m.door.value = True
        m.clock.set(hour=23)
        s = m.app.invoke(m.Home())
        self.assertEqual(s.waiting, "approve alarm?")
        m.app.resume(s, True)
        self.assertEqual(s.plan, ["log", "alarm"])

    def test_refusal_replans_without_the_tool(self):
        m = self.m
        m.door.value = True
        s = m.app.invoke(m.Home(), approve=lambda q: False)
        self.assertEqual(s.plan, ["log"])
        self.assertTrue(s._agent.acted_ok)

    def test_no_model_c(self):
        m = self.m
        gen = os.path.join(m.app.dir, "generated")
        cagent.generate(m.app, gen, "agent.py")
        text = open(os.path.join(gen, "home_agent.c")).read()
        self.assertNotIn("tlg_lm", text)
        self.assertNotIn("pre_reason", text)

    def test_esp32s3_build_has_no_model(self):
        m = self.m
        logs = []
        r = build(m.app, "esp32s3", log=logs.append)
        self.assertFalse(r["model"])
        self.assertTrue(any("model: none (no reason node)" in x for x in logs), logs)
        self.assertFalse(any(x.startswith("model: training") for x in logs), logs)
        self.assertFalse(os.path.exists(os.path.join(m.app.dir, "data")))
        files = os.listdir(r["out"])
        self.assertNotIn("tlg_lm.c", files)
        self.assertNotIn("home_model.h", files)
        flags = open(os.path.join(r["out"], "tlg_build.h")).read()
        self.assertIn("#define TLG_USES_MODEL 0", flags)
        self.assertIn("#define TLG_HAS_MODEL 0", flags)


@unittest.skipUnless(CC, "no C compiler")
class Build(unittest.TestCase):
    def test_build_c_checks_every_situation(self):
        m = load(HOME)
        logs = []
        build(m.app, "c", train=False, log=logs.append)
        self.assertTrue(any("exactly like your Python in all 6 situations" in x for x in logs), logs)
        self.assertTrue(os.path.exists(os.path.join(m.app.dir, "build", "home_c", "home_agent.c")))

    def test_build_c_planner_only(self):
        m = load(PLANNER_ONLY)
        logs = []
        build(m.app, "c", log=logs.append)
        self.assertTrue(any("exactly like your Python in all 6 situations" in x for x in logs), logs)
        self.assertNotIn("tlg_lm.c", os.listdir(os.path.join(m.app.dir, "build", "home_c")))

    def test_planner_and_verify_are_timed(self):
        """With clock_us set, a run records the time spent in tlg_plan and tlg_verify."""
        m = load(PLANNER_ONLY)
        gen = os.path.join(m.app.dir, "generated")
        cagent.generate(m.app, gen, "agent.py")
        main = os.path.join(gen, "main.c")
        with open(main, "w") as f:
            f.write(textwrap.dedent("""
                #include "home_agent.h"
                #include <stdio.h>
                static uint32_t t;
                static uint32_t fake_us(void) { return t += 10; }   /* every reading is 10 us later */
                int main(void) {
                    home_state_t s;
                    home_state_init(&s);
                    home_begin(&s);
                    s.agent.clock_us = fake_us;
                    tlg_graph_t g;
                    home_graph(&g, &s);
                    tlg_run_t run;
                    tlg_run_init(&run, &g);
                    tlg_status_t st = tlg_invoke(&g, &run, &s);
                    printf("%d %lu %lu\\n", (int)st, (unsigned long)s.agent.plan_us, (unsigned long)s.agent.verify_us);
                    return 0;
                }
            """))
        exe = os.path.join(gen, "home")
        srcs = [main] + [os.path.join(gen, f"home_{x}.c") for x in ("schema", "rules", "agent")]
        srcs += [os.path.join(ROOT, "core", f) for f in ("tlg.c", "tlg_plan.c", "tlg_trace.c", "tlg_agent.c")]
        srcs += [os.path.join(ROOT, "drivers", "tlg_drivers_host.c")]
        subprocess.run([CC, "-std=c11", "-Wall", "-Werror", "-I", gen, "-I", os.path.join(ROOT, "core"),
                        "-I", os.path.join(ROOT, "drivers"), "-o", exe] + srcs, check=True)
        out = subprocess.run([exe], check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(out[-1], "1 10 10")      # TLG_DONE, one planner call, one verify call

    def test_nodes_do_the_same_maths_in_c(self):
        """sense() uses //, % on negatives, min/max/abs, a for loop: run it in C and in Python."""
        m = load(HOME)
        gen = os.path.join(m.app.dir, "generated")
        cagent.generate(m.app, gen, "agent.py")
        main = os.path.join(gen, "main.c")
        with open(main, "w") as f:
            f.write(textwrap.dedent("""
                #include "home_agent.h"
                #include <stdio.h>
                int main(void) {
                    home_state_t s; tlg_run_t run = {0};
                    home_state_init(&s);
                    for (int i = 0; i < 5; i++) {
                        home_nodes[HOME_NODE_SENSE].run(&s, &run);
                        printf("%ld %ld %ld %ld %ld\\n", (long)s.count, (long)s.total, (long)s.q, (long)s.r, (long)s.m);
                    }
                    return 0;
                }
            """))
        exe = os.path.join(gen, "home")
        srcs = [main] + [os.path.join(gen, f"home_{x}.c") for x in ("schema", "rules", "agent")]
        srcs += [os.path.join(ROOT, "core", f) for f in ("tlg.c", "tlg_plan.c", "tlg_trace.c", "tlg_agent.c")]
        srcs += [os.path.join(ROOT, "lm", "tlg_lm.c"), os.path.join(ROOT, "drivers", "tlg_drivers_host.c")]
        subprocess.run([CC, "-std=c11", "-Wall", "-Werror", "-I", gen, "-I", os.path.join(ROOT, "core"),
                        "-I", os.path.join(ROOT, "lm"), "-I", os.path.join(ROOT, "drivers"), "-o", exe] + srcs + ["-lm"],
                       check=True)
        c_out = subprocess.run([exe], check=True, capture_output=True, text=True).stdout.split("\n")
        c_lines = [x for x in c_out if x and not x.startswith("    [")]
        s = m.Home()
        py_lines = []
        for _ in range(5):
            m.sense(s)
            py_lines.append(f"{s.count} {s.total} {s.q} {s.r} {s.m}")
        self.assertEqual(c_lines, py_lines)


@unittest.skipUnless(CC, "no C compiler")
class DataLog(unittest.TestCase):
    def test_log_row_after_a_run(self):
        """The board's data log line: every Choice field, then the plan that ran."""
        m = load(HOME)
        gen = os.path.join(m.app.dir, "generated")
        cagent.generate(m.app, gen, "agent.py")
        main = os.path.join(gen, "main.c")
        with open(main, "w") as f:
            f.write(textwrap.dedent("""
                #include "home_agent.h"
                #include <stdio.h>
                int main(void) {
                    home_state_t s; char buf[128];
                    home_state_init(&s);
                    home_begin(&s);
                    tlg_graph_t g; home_graph(&g, &s);
                    tlg_run_t run; tlg_run_init(&run, &g);
                    tlg_invoke(&g, &run, &s);
                    home_log_header(buf, sizeof buf); printf("%s\\n", buf);
                    home_log_row(&s, buf, sizeof buf); printf("%s\\n", buf);
                    char tiny[6];                                    /* too small: cut, never overflows */
                    int n = home_log_row(&s, tiny, sizeof tiny); printf("%d %s\\n", n > 5, tiny);
                    return 0;
                }
            """))
        exe = os.path.join(gen, "home")
        srcs = [main] + [os.path.join(gen, f"home_{x}.c") for x in ("schema", "rules", "agent")]
        srcs += [os.path.join(ROOT, "core", f) for f in ("tlg.c", "tlg_plan.c", "tlg_trace.c", "tlg_agent.c")]
        srcs += [os.path.join(ROOT, "lm", "tlg_lm.c"), os.path.join(ROOT, "drivers", "tlg_drivers_host.c")]
        subprocess.run([CC, "-std=c11", "-Wall", "-Werror", "-I", gen, "-I", os.path.join(ROOT, "core"),
                        "-I", os.path.join(ROOT, "lm"), "-I", os.path.join(ROOT, "drivers"), "-o", exe] + srcs + ["-lm"],
                       check=True)
        out = [x for x in subprocess.run([exe], check=True, capture_output=True, text=True).stdout.splitlines()
               if not x.startswith("    [")]                          # the host drivers print what they do
        self.assertEqual(out[0], "sound,night,plan")
        self.assertRegex(out[1], r"^(quiet|noise|loud),(no|yes),(log|log\+alarm|nothing|-)$")
        self.assertEqual(out[2], "1 " + out[1][:5])


def expect_error(test, body, message, kind=SubsetError, where=True):
    src = HOME.replace("def sense(s):\n", "def sense(s):\n" + textwrap.indent(textwrap.dedent(body), "    ") + "\n")
    m = load(src)
    with test.assertRaisesRegex(kind, message) as cm:
        cagent.generate(m.app, tempfile.mkdtemp(), "agent.py")
    if where:
        test.assertRegex(str(cm.exception), r"agent\.py:\d+: ")


class ConverterErrors(unittest.TestCase):
    def test_while(self):
        expect_error(self, "while True:\n    pass", "while loops aren't supported")

    def test_float(self):
        expect_error(self, "s.count = 1.5", "floats aren't supported")

    def test_lists(self):
        expect_error(self, "x = [1, 2]", "lists, dicts and sets can't be built")

    def test_bad_choice_value(self):
        expect_error(self, 's.sound = "boom"', "'boom' is not a value of sound")

    def test_choice_ordering(self):
        expect_error(self, 'x = s.sound > "noise"', "only be compared with == and !=")

    def test_true_division(self):
        expect_error(self, "s.count = s.count / 2", "use //")

    def test_rules_cant_read_memory(self):
        src = HOME.replace('    s.worry = "high" if score(s) >= 5 else "low"',
                           '    s.worry = "high" if s.count > 3 else "low"')
        m = load(src)
        with self.assertRaisesRegex(SubsetError, "can only read Choice and Think fields"):
            cagent.generate(m.app, tempfile.mkdtemp(), "agent.py")

    def test_rules_cant_use_hardware(self):
        src = HOME.replace("    want(goals.logged)\n", "    want(goals.logged)\n    siren.on()\n")
        m = load(src)
        with self.assertRaisesRegex(SubsetError, "can't use hardware"):
            cagent.generate(m.app, tempfile.mkdtemp(), "agent.py")

    def test_unknown_driver_method(self):
        expect_error(self, "siren.explode()", "LED has no method explode")


class GraphErrors(unittest.TestCase):
    def test_no_start(self):
        with self.assertRaisesRegex(GraphError, "START"):
            load(HOME.replace('graph.add_edge(START, "sense")', ""))

    def test_dead_end(self):
        with self.assertRaisesRegex(GraphError, "no way out"):
            load(HOME.replace('graph.add_edge("act", END)', ""))

    def test_two_edges(self):
        with self.assertRaisesRegex(GraphError, "already has a way out"):
            load(HOME.replace('graph.add_edge("plan", "verify")',
                              'graph.add_edge("plan", "verify")\ngraph.add_edge("plan", "act")'))

    def test_pins(self):
        m = load(HOME.replace("siren = LED(pin=2)", "siren = LED(pin=12)\ncam = Camera()").replace(
            "    siren.blink(3, 100)", "    siren.blink(3, 100)\n    cam.snapshot()"))
        files, conv = cagent.generate(m.app, tempfile.mkdtemp(), "agent.py")
        with self.assertRaisesRegex(BuildError, "GPIO12, which the camera"):
            check_pins(m.app, conv)

    def test_pulldown(self):
        """An unconnected sensor pin can be held LOW, so it doesn't read random values."""
        m = load(HOME.replace("door = DigitalIn(pin=1)", "door = DigitalIn(pin=1, pulldown=True)"))
        files, _ = cagent.generate(m.app, tempfile.mkdtemp(), "agent.py")
        agent_c = next(f for f in files if f.endswith("home_agent.c"))
        with open(agent_c) as f:
            self.assertIn(".pulldown = true", f.read())
        with self.assertRaisesRegex(ValueError, "pullup or pulldown, not both"):
            drivers.DigitalIn(pin=4, pullup=True, pulldown=True)


if __name__ == "__main__":
    unittest.main()
