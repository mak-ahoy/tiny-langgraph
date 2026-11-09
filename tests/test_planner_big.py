"""
The planner for agents with more than 8 goals (core/tlg_plan.c, toolchain/planner.py): a pruned
search in a fixed workspace, greedy when a situation doesn't fit.

  - C and Python decide the same on every situation, for several workspace sizes (the invariant)
  - exact plans really are optimal (checked against a separate exact planner, tests/fixtures/plan_domains.py)
  - small workspaces fall back to greedy, and greedy plans are still safe

    python -m unittest discover -s tests -p "test_*.py"
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

import plan_domains as research  # noqa: E402
from tiny_langgraph.toolchain import planner  # noqa: E402

CC = shutil.which(os.environ.get("CC", "cc")) or shutil.which("gcc") or shutil.which("clang")
CASES = [  # goals, tools (the framework's limit is 32), dense, share always wanted, workspace states
    (12, 24, False, 0.0, 4096),
    (20, 32, True, 0.0, 4096),
    (32, 32, True, 0.0, 4096),
    (32, 32, True, 0.5, 512),          # many goals at once in a small workspace: often greedy
    (16, 32, True, 0.3, 64),
]


def as_planner(dom):
    T = dom.n_tools
    return planner.Domain(names=[f"t{i}" for i in range(T)], needs=dom.needs, after=dom.after,
                          achieves=dom.achieves, cost=dom.cost, approval=[False] * T, weights=dom.weights,
                          only_if_wanted=dom.only_if_wanted, budget=1000, fact_index={})


def situations(dom, n, seed):
    rng = random.Random(seed)
    out = []
    for sit in research.sample(dom, n, seed=seed):
        want, facts = dom.want_facts(sit)
        banned = (1 << rng.randrange(dom.n_tools)) if rng.random() < 0.2 else 0
        out.append((facts, want, banned))
    return out


def run_c(dom, sits, states):
    """Every situation through tlg_plan(): 'exact|cost|score|achieved|actions' per line."""
    pd = as_planner(dom)
    acts = ",\n".join(f'    {{ "t{a}", {pd.needs[a]}u, {pd.after[a]}u, {pd.achieves[a]}u, {pd.cost[a]}, false }}'
                      for a in range(dom.n_tools))
    rows = ",\n".join(f"    {{ {f}u, {w}u, {b}u }}" for f, w, b in sits)
    src = f"""#include "tlg_plan.h"
#include <stdio.h>
static const tlg_action_t A[] = {{
{acts}
}};
static const char *const O[] = {{ {", ".join(f'"o{g}"' for g in range(dom.n_goals))} }};
static const uint8_t W[] = {{ {", ".join(map(str, dom.weights))} }};
static const tlg_domain_t D = {{ A, {dom.n_tools}, O, W, {dom.n_goals}, NULL, 0, {dom.only_if_wanted}u, 1000 }};
static const uint32_t S[][3] = {{
{rows}
}};
static uint32_t ws[({states} * TLG_PLAN_STATE_BYTES + 3) / 4];
int main(void) {{
    tlg_plan_workspace(ws, sizeof ws);
    for (unsigned i = 0; i < sizeof S / sizeof S[0]; i++) {{
        tlg_plan_t p;
        if (tlg_plan(&D, S[i][0], S[i][1], S[i][2], &p) != 0) {{ printf("error\\n"); continue; }}
        printf("%d|%u|%d|%lu|", (int)p.exact, (unsigned)p.cost, p.score, (unsigned long)p.achieved);
        for (int k = 0; k < p.len; k++) printf("%s%d", k ? " " : "", p.actions[k]);
        printf("\\n");
    }}
    return 0;
}}
"""
    work = tempfile.mkdtemp(prefix="tlg_bigplan_")
    path, exe = os.path.join(work, "check.c"), os.path.join(work, "check")
    with open(path, "w") as f:
        f.write(src)
    subprocess.run([CC, "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(ROOT, "core"),
                    "-o", exe, path, os.path.join(ROOT, "core", "tlg_plan.c")], check=True)
    return subprocess.run([exe], check=True, capture_output=True, text=True).stdout.splitlines()


class Python(unittest.TestCase):
    def test_exact_plans_are_optimal_and_safe(self):
        checked = greedy_seen = 0
        for goals, tools, dense, always, states in CASES:
            dom = research.generate(30, goals, tools, dense=dense, always=always)
            pd = as_planner(dom)
            for facts, want, banned in situations(dom, 150, seed=goals):
                acts, achieved, cost, score, exact = planner.plan_ex(pd, facts, want, 0, states)
                self.assertTrue(research.check(dom, facts, want, acts)[0])          # always safe
                if not exact:
                    greedy_seen += 1
                    continue
                best, _ = research.exact_pruned(dom, facts, want, limit=1 << 16)
                if best is None:
                    continue
                self.assertEqual((score, cost), (dom.score(research.achieved(dom, best), want),
                                                 research.plan_cost(dom, best)), (goals, facts, want))
                checked += 1
        self.assertGreater(checked, 500)
        self.assertGreater(greedy_seen, 0)                       # the fallback was exercised

    def test_up_to_8_goals_is_the_full_search(self):
        dom = research.generate(20, 8, 16, dense=True)
        pd = as_planner(dom)
        for facts, want, banned in situations(dom, 100, seed=8):
            self.assertTrue(planner.plan_ex(pd, facts, want, banned, states=1)[4])   # no workspace needed


@unittest.skipUnless(CC, "no C compiler")
class CAgreesWithPython(unittest.TestCase):
    def test_every_situation(self):
        for goals, tools, dense, always, states in CASES:
            dom = research.generate(30, goals, tools, dense=dense, always=always)
            pd = as_planner(dom)
            sits = situations(dom, 200, seed=goals + 1)
            c = run_c(dom, sits, states)
            for (facts, want, banned), line in zip(sits, c):
                acts, achieved, cost, score, exact = planner.plan_ex(pd, facts, want, banned, states)
                py = f"{int(exact)}|{cost}|{score}|{achieved}|{' '.join(map(str, acts))}"
                self.assertEqual(line, py, f"{dom.name}, {states} states: facts={facts} want={want} banned={banned}")

    def test_no_workspace_means_greedy(self):
        dom = research.generate(30, 12, 24)
        sits = situations(dom, 50, seed=3)
        for line in run_c(dom, sits, states=0):
            self.assertTrue(line.startswith("0|"), line)


def big_agent(n_goals=12):
    """A plainly written agent with n_goals goals: one tool per goal, plus one that does three."""
    lines = ["from tiny_langgraph import *",
             "from tiny_langgraph.drivers import DigitalIn",
             "",
             "door = DigitalIn(pin=4)",
             "",
             "class Big(State):",
             '    a = Choice("x", "y", "z")',
             '    b = Choice("x", "y")',
             '    c = Choice("x", "y", "z")',
             "",
             "goals = Goals(" + ", ".join(f"g{i}=Goal({1 + i % 5}, only_if_wanted={i % 3 == 0})"
                                          for i in range(n_goals)) + ")",
             "",
             "def b_is_y(s: Big) -> bool:",
             '    return s.b == "y"',
             ""]
    for i in range(n_goals):
        needs = ", needs=b_is_y" if i % 4 == 0 else ""
        lines += [f"@tool(cost={1 + i % 4}, achieves=goals.g{i}{needs})", f"def t{i}(s: Big):", "    pass", ""]
    lines += ["@tool(cost=5, achieves=[goals.g1, goals.g2, goals.g4])", "def combo(s: Big):", "    pass", "",
              "def sense(s: Big):", '    s.b = "y" if door.read() else "x"', "",
              "@rules", "def assess(s: Big):", "    want(goals.g0)"]
    for i in range(1, n_goals):
        lines += [f'    if s.{"abc"[i % 3]} == "{"yyz"[i % 3]}":', f"        want(goals.g{i})"]
    tools = ", ".join(f"t{i}" for i in range(n_goals))
    lines += ["",
              "graph = StateGraph(Big)",
              'graph.add_node("sense", sense)',
              'graph.add_node("assess", assess)',
              'graph.add_node("plan", prebuilt.plan)',
              'graph.add_node("verify", prebuilt.verify)',
              'graph.add_node("ask", prebuilt.ask)',
              f'graph.add_node("act", prebuilt.Act([{tools}, combo]))',
              'graph.add_edge(START, "sense")',
              'graph.add_edge("sense", "assess")',
              'graph.add_edge("assess", "plan")',
              'graph.add_edge("plan", "verify")',
              'graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])',
              'graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])',
              'graph.add_edge("act", END)',
              'app = graph.compile(name="big")']
    return "\n".join(lines) + "\n"


@unittest.skipUnless(CC, "no C compiler")
class Framework(unittest.TestCase):
    def test_a_12_goal_agent_builds_and_checks(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from test_python_api import load
        from tiny_langgraph.build import build
        m = load(big_agent(12))
        logs = []
        build(m.app, "c", log=logs.append)
        self.assertTrue(any("exactly like your Python in all 18 situations" in x for x in logs), logs)
        r = build(m.app, "esp32s3", log=lambda _: None)
        with open(os.path.join(r["out"], "tlg_build.h")) as f:
            self.assertIn("#define TLG_PLAN_STATES 16384", f.read())
        s = m.Big(c="z")                        # b (from the door) = y and c = z: g1, g2 and g4 are wanted
        m.door.value = True
        m.app.invoke(s)
        self.assertIn("combo", s.plan)          # one tool for all three (cost 5) beats t1 + t2 + t4 (cost 6)
        self.assertNotIn("t2", s.plan)

    def test_keep_the_better_plan(self):
        """Same points as the planner's plan but dearer: the verifier rejects it, in Python and C."""
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from test_python_api import load
        m = load(big_agent(12))
        m.door.value = True                         # b = y: g1, g2 and g4 are wanted with c = z
        s = m.Big(c="z")
        m.app.invoke(s)
        a, names = s._agent, [t.name for t in m.app.tools]
        cheap = [names.index(n) for n in s.plan]
        dear = [names.index(n) for n in s.plan if n != "combo"] + [names.index(x) for x in ("t1", "t2", "t4")]
        self.assertEqual(m.app.verify(a.facts, a.want, 0, cheap, True)[0], "ok")
        verdict, why = m.app.verify(a.facts, a.want, 0, sorted(dear), True)
        self.assertEqual(verdict, "fail")
        self.assertRegex(why, r"costs \d+; the planner found the same for \d+")
        # the same two plans through tlg_verify in C
        dom = m.app.domain
        acts = ",\n".join(f'    {{ "{n}", {dom.needs[i]}u, {dom.after[i]}u, {dom.achieves[i]}u, {dom.cost[i]}, false }}'
                          for i, n in enumerate(dom.names))
        src = f"""#include "tlg_plan.h"
#include <stdio.h>
static const tlg_action_t A[] = {{
{acts}
}};
static const char *const O[] = {{ {", ".join(f'"g{i}"' for i in range(len(dom.weights)))} }};
static const uint8_t W[] = {{ {", ".join(map(str, dom.weights))} }};
static const tlg_domain_t D = {{ A, {len(dom.names)}, O, W, {len(dom.weights)}, NULL, 0, {dom.only_if_wanted}u, {dom.budget} }};
static uint32_t ws[(4096 * TLG_PLAN_STATE_BYTES + 3) / 4];
int main(void) {{
    tlg_plan_workspace(ws, sizeof ws);
    const uint8_t cheap[] = {{ {", ".join(map(str, cheap))} }}, dear[] = {{ {", ".join(map(str, sorted(dear)))} }};
    char why[96];
    tlg_verdict_t v1 = tlg_verify(&D, {a.facts}u, {a.want}u, 0, cheap, {len(cheap)}, true, why, sizeof why);
    printf("%d|%s\\n", (int)v1, why);
    tlg_verdict_t v2 = tlg_verify(&D, {a.facts}u, {a.want}u, 0, dear, {len(dear)}, true, why, sizeof why);
    printf("%d|%s\\n", (int)v2, why);
    return 0;
}}
"""
        work = tempfile.mkdtemp(prefix="tlg_verify_")
        path, exe = os.path.join(work, "check.c"), os.path.join(work, "check")
        with open(path, "w") as f:
            f.write(src)
        subprocess.run([CC, "-std=c11", "-Wall", "-Werror", "-I", os.path.join(ROOT, "core"), "-o", exe, path,
                        os.path.join(ROOT, "core", "tlg_plan.c")], check=True)
        out = subprocess.run([exe], check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(out[0], "0|ok")
        self.assertEqual(out[1], f"2|{why}")


if __name__ == "__main__":
    unittest.main()
