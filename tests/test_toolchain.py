"""
Toolchain tests. Run from the repo root:  python -m unittest discover -s tests -p "test_*.py"

The most important one is the cross-check: for every situation of every
example schema, the device's generated C code (think, want, facts and the
planner) must decide exactly what the Python side decided when the training
data was made. If they ever differ, the model would be taught plans that the
device then rejects.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

from tiny_langgraph.toolchain import codegen, dataset, planner, schema  # noqa: E402
from tiny_langgraph.toolchain.schema import SchemaError  # noqa: E402

DOOR = os.path.join(ROOT, "examples", "door_guard", "schema.yaml")
CC = shutil.which(os.environ.get("CC", "cc")) or shutil.which("gcc") or shutil.which("clang")


def load_text(text):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "schema.yaml")
    with open(p, "w") as f:
        f.write(textwrap.dedent(text))
    return schema.load(p)


MINIMAL = """
    name: tiny
    inputs:
      light: [off, on]
      door: [shut, open]
    think:
      worry: [lo, hi]
    facts:
      dark: light == off
      odd: (door == open and not light in [on]) or worry == hi
    outcomes:
      logged: 1
      told: { weight: 3, only_if_wanted: true }
    actions:
      log: { cost: 1, achieves: logged }
      tell: { cost: 2, needs: dark, after: logged, achieves: told }
"""


class Expressions(unittest.TestCase):
    def test_parse_and_eval(self):
        s = load_text(MINIMAL)
        env = {"light": "off", "door": "open", "worry": "lo"}
        self.assertEqual(s.facts_of({"light": "off", "door": "open"}, {"worry": "lo"}), {"dark": True, "odd": True})
        self.assertEqual(s.facts_of({"light": "on", "door": "open"}, {"worry": "lo"}), {"dark": False, "odd": False})
        self.assertEqual(s.facts_of({"light": "on", "door": "shut"}, {"worry": "hi"}), {"dark": False, "odd": True})
        self.assertTrue(schema.eval_expr(("const", True), env))

    def test_errors(self):
        for bad, msg in [
            ("light == dim", "not a value"),
            ("lamp == on", "unknown name"),
            ("light on", "expected"),
            ("(light == on", r"expected \)"),
            ("light == on and", "expected"),
        ]:
            with self.assertRaisesRegex(SchemaError, msg):
                load_text(MINIMAL.replace("light == off", bad))


class Validation(unittest.TestCase):
    def test_bad_schemas(self):
        cases = [
            (MINIMAL.replace("needs: dark", "needs: bright"), "unknown fact"),
            (MINIMAL.replace("after: logged", "after: nope"), "unknown outcome"),
            (MINIMAL.replace("name: tiny", "name: Tiny"), "lower_snake_case"),
            (MINIMAL.replace("  log: { cost: 1", "  door_open: { cost: 1"), "appears twice"),
            (MINIMAL.replace("worry: [lo, hi]", "worry: [lo]"), "at least 2"),
            (MINIMAL.replace("cost: 2,", "cost: 2, colour: red,"), "unknown keys"),
        ]
        for text, msg in cases:
            with self.assertRaisesRegex(SchemaError, msg, msg=msg):
                load_text(text)

    def test_too_many_outcomes(self):
        outs = "\n".join(f"      o{i}: 1" for i in range(33))        # up to 32 (9+ use the pruned search)
        text = MINIMAL.replace("      logged: 1\n      told: { weight: 3, only_if_wanted: true }", outs)
        text = text.replace("achieves: logged", "achieves: o0").replace("after: logged, achieves: told", "achieves: o1")
        with self.assertRaisesRegex(SchemaError, "outcomes"):
            load_text(text)

    def test_tokens(self):
        s = load_text(MINIMAL)
        self.assertEqual(s.tokens[:5], ["<pad>", "<bos>", "<think>", "<plan>", "<eos>"])
        self.assertEqual(s.tokens[5:], ["light_off", "light_on", "door_shut", "door_open",
                                        "worry_lo", "worry_hi", "log", "tell"])
        self.assertEqual(s.prompt_len, 4)
        self.assertEqual(s.seq_len, 4 + 1 + 1 + 2 + 1)


RULES = MINIMAL.replace("""    outcomes:""", """    scores:
      risk:
        door:  { open: 2 }
        light: { off: 1 }
    rules:
      think:
        worry:
          - hi: risk >= 3
          - hi: dark and door == open
          - lo: true
      want:
        logged: true
        told: worry == hi
    outcomes:""")


class Rules(unittest.TestCase):
    def test_think_and_want(self):
        s = load_text(RULES)
        self.assertTrue(s.has_rules)
        self.assertIsNone(s.teacher)
        self.assertEqual(s.scores_of({"light": "off", "door": "open"}), {"risk": 3})
        self.assertEqual(s.think_of({"light": "off", "door": "open"}), {"worry": "hi"})
        self.assertEqual(s.think_of({"light": "on", "door": "open"}), {"worry": "lo"})
        self.assertEqual(s.want_of({"light": "on", "door": "open"}, {"worry": "lo"}), ["logged"])
        self.assertEqual(s.want_of({"light": "on", "door": "open"}, {"worry": "hi"}), ["logged", "told"])
        # odd reads worry, worry's rules read dark: dark before worry before odd
        order = [n for _, n in s.order]
        self.assertLess(order.index("dark"), order.index("worry"))
        self.assertLess(order.index("worry"), order.index("odd"))

    def test_comparisons(self):
        s = load_text(RULES)
        env = {"risk": -2}
        for op, n, want in [(">=", -2, True), (">", -2, False), ("<", 0, True), ("<=", -3, False),
                            ("==", -2, True), ("!=", -2, False)]:
            self.assertEqual(schema.eval_expr(("cmp", "risk", op, n), env), want, (op, n))
        self.assertIn(("cmp", "risk", ">=", 3), [c for _, c, _ in s.think_rules["worry"]])

    def test_errors(self):
        cases = [
            (RULES.replace("- lo: true", "- lo: door == shut"), "last rule must be"),
            (RULES.replace("- hi: risk >= 3", "- maybe: risk >= 3"), "not a value of"),
            (RULES.replace("risk >= 3", "risk >= high"), "whole number"),
            (RULES.replace("risk >= 3", "risk == open"), "whole number"),
            (RULES.replace("door:  { open: 2 }", "door:  { ajar: 2 }"), "not a value of"),
            (RULES.replace("door:  { open: 2 }", "lamp:  { on: 2 }"), "unknown slot"),
            (RULES.replace("told: worry == hi", "shouted: worry == hi"), "unknown outcome"),
            (RULES.replace("name: tiny", "name: tiny\n    teacher: t.py"), "either 'rules' or 'teacher'"),
            (RULES.replace("dark: light == off", "dark: light == off and odd"), "loop"),
            (RULES.replace("      risk:", "      dark:"), "used twice"),
        ]
        for text, msg in cases:
            with self.assertRaisesRegex(SchemaError, msg, msg=msg):
                load_text(text)

    def test_rules_label_the_data(self):
        s = load_text(RULES)
        t = dataset.load_teacher(s)
        row = dataset.label(s, t, planner.domain_of(s), {"light": "off", "door": "open"})
        self.assertEqual(row["think"], {"worry": "hi"})
        self.assertEqual(row["tokens"][-4:], ["<plan>", "log", "tell", "<eos>"])


class Planner(unittest.TestCase):
    def test_plans(self):
        s = load_text(MINIMAL)
        d = planner.domain_of(s)
        dark = 1 << d.fact_index["dark"]
        both = s.outcome_mask(["logged", "told"])
        acts, achieved, cost, _ = planner.plan(d, dark, both)
        self.assertEqual([d.names[a] for a in acts], ["log", "tell"])
        self.assertEqual((achieved, cost), (both, 3))
        acts, achieved, _, _ = planner.plan(d, 0, both)          # not dark: can't tell
        self.assertEqual([d.names[a] for a in acts], ["log"])
        acts, _, _, _ = planner.plan(d, dark, s.outcome_mask(["logged"]))
        self.assertEqual([d.names[a] for a in acts], ["log"])     # told not wanted: never done


class DoorGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = schema.load(DOOR)
        cls.teacher = dataset.load_teacher(cls.s)
        cls.dom = planner.domain_of(cls.s)

    def test_teacher_sanity(self):
        def ask(**kw):
            base = dict(cam="none", audio="none", heat="none", tod="afternoon", owner="home",
                        wifi="up", expect="none", recent="none", last="none")
            base.update(kw)
            return dataset.label(self.s, self.teacher, self.dom, base)

        quiet = ask(tod="night", owner="away")
        self.assertEqual(quiet["think"]["threat"], "lo")           # night alone is not a threat
        self.assertEqual(quiet["tokens"][-3:], ["<plan>", "log", "<eos>"])
        intruder = ask(cam="person_hi", heat="present", tod="night", owner="away")
        self.assertEqual(intruder["think"], {"visitor": "hi", "threat": "hi"})
        names = [self.dom.names[a] for a in intruder["plan"]]
        self.assertIn("notify_high", names)
        self.assertNotIn("alarm", names)                           # led_flash is cheaper at night
        no_wifi = ask(cam="person_hi", heat="present", tod="night", owner="away", wifi="down")
        self.assertNotIn("notify_high", [self.dom.names[a] for a in no_wifi["plan"]])



EXAMPLES = sorted(os.path.join(ROOT, "examples", d, "schema.yaml")
                  for d in os.listdir(os.path.join(ROOT, "examples"))
                  if os.path.exists(os.path.join(ROOT, "examples", d, "schema.yaml")))
FIXTURES = [os.path.join(ROOT, "tests", "fixtures", "pet_watcher_schema.yaml")]

HARNESS = """
#include "{p}_schema.h"
#include <stdio.h>
/* every situation: index | think values | want mask | plan */
int main(void)
{{
    {p}_inputs_t in;
    for (uint32_t i = 0; {p}_situation(i, &in); i++) {{
        {p}_think_t th;
        {p}_think(&in, &th);
        uint32_t facts = {p}_facts(&in, &th);
        uint32_t want = {p}_want(&in, &th);
        tlg_plan_t p;
        tlg_plan(&{p}_domain, facts, want, 0, &p);
        printf("%u|{think_fmt}|%u|", (unsigned)i{think_args}, (unsigned)want);
        for (int k = 0; k < p.len; k++) printf("%s%d", k ? " " : "", p.actions[k]);
        printf("\\n");
    }}
    return 0;
}}
"""


class CrossCheck(unittest.TestCase):
    """For every example schema with rules: the generated C and the Python that labels the
    training data agree on think, want and plan in every possible situation."""

    def test_examples_exist(self):
        self.assertGreaterEqual(len(EXAMPLES + FIXTURES), 2)

    def test_generated_code_is_up_to_date(self):
        for path in EXAMPLES:
            s = schema.load(path)
            out = tempfile.mkdtemp()
            codegen.generate(s, out, "schema.yaml")
            for name in (f"{s.name}_schema.h", f"{s.name}_schema.c"):
                with self.subTest(name), open(os.path.join(out, name)) as a, \
                        open(os.path.join(os.path.dirname(path), "generated", name)) as b:
                    self.assertEqual(a.read(), b.read(), f"{name} is stale: run python tools/tlg.py gen {path}")

    @unittest.skipUnless(CC, "no C compiler")
    def test_c_and_python_agree_everywhere(self):
        for path in EXAMPLES + FIXTURES:
            s = schema.load(path)
            if not s.has_rules:
                continue
            with self.subTest(s.name):
                self._check(s)

    def _check(self, s):
        p = s.name
        gen = tempfile.mkdtemp()
        codegen.generate(s, gen, "schema.yaml")
        harness = os.path.join(gen, "crosscheck.c")
        with open(harness, "w") as f:
            f.write(HARNESS.format(p=p, think_fmt=" ".join(["%d"] * len(s.think)),
                                   think_args="".join(f", th.{t.name}" for t in s.think)))
        exe = os.path.join(gen, "crosscheck")
        subprocess.run([CC, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(ROOT, "core"),
                        "-I", gen, "-o", exe, harness, os.path.join(gen, f"{p}_schema.c"),
                        os.path.join(ROOT, "core", "tlg_plan.c")], check=True)
        lines = subprocess.run([exe], check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(len(lines), s.situation_count())

        teacher, dom = dataset.load_teacher(s), planner.domain_of(s)
        mismatches = []
        for line, situation in zip(lines, dataset.situations(s)):
            _, th, want, plan = line.split("|")
            py = dataset.label(s, teacher, dom, situation)
            py_th = [slot.values.index(py["think"][slot.name]) for slot in s.think]
            c_plan = [int(x) for x in plan.split()] if plan else []
            if [int(x) for x in th.split()] != py_th or int(want) != py["want"] or c_plan != py["plan"]:
                mismatches.append((situation, line, py["think"], py["want"], py["plan"]))
        self.assertEqual(mismatches[:3], [], f"{s.name}: {len(mismatches)} situations differ")


if __name__ == "__main__":
    unittest.main()
