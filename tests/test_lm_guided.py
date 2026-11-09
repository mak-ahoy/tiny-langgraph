"""
The model engine in C (lm/tlg_lm.c) and in numpy (python/tiny_langgraph/lm.py) write the same
tokens, with and without guided writing, on the pet watcher's trained model. And with the agent's
guide, every plan the model writes passes the safety checks.

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

from tiny_langgraph.lm import TinyLM  # noqa: E402

CC = shutil.which(os.environ.get("CC", "cc")) or shutil.which("gcc") or shutil.which("clang")
MODEL = os.path.join(ROOT, "examples", "pet_watcher", "model", "pet_watcher.bin")

HARNESS = r"""
#include "tlg_lm.h"
#include <stdio.h>
#include <stdlib.h>
/* a guide for the test: after the first token, no token whose id is a multiple of 3 */
static bool no_threes(void *ctx, const uint8_t *out, int n, int token)
{
    (void)ctx; (void)out;
    return n == 0 || token % 3 != 0;
}
int main(int argc, char **argv)
{
    if (argc < 4) return 4;
    FILE *f = fopen(argv[1], "rb");
    fseek(f, 0, SEEK_END); long size = ftell(f); fseek(f, 0, SEEK_SET);
    unsigned char *w = malloc((size_t)size);
    if (fread(w, 1, (size_t)size, f) != (size_t)size) return 1;
    fclose(f);
    static tlg_lm_t lm;
    if (!tlg_lm_init(&lm, w, (size_t)size)) return 2;
    int guided = atoi(argv[2]), pn;
    uint8_t prompt[64], out[64];
    while (scanf("%d", &pn) == 1) {
        for (int i = 0; i < pn; i++) { int t; if (scanf("%d", &t) != 1) return 3; prompt[i] = (uint8_t)t; }
        int n = tlg_lm_generate_guided(&lm, prompt, pn, atoi(argv[3]), out, lm.cfg.seq_len - pn, NULL,
                                       guided ? no_threes : NULL, NULL);
        for (int i = 0; i < n; i++) printf("%s%d", i ? " " : "", out[i]);
        printf("\n");
    }
    return 0;
}
"""


def pet_watcher():
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from test_python_api import load
    os.environ["USE_MODEL"] = "1"
    try:
        with open(os.path.join(ROOT, "examples", "pet_watcher", "agent.py")) as f:
            src = f.read()
        m = load(src)
    finally:
        os.environ.pop("USE_MODEL", None)
    m.app.dir = os.path.join(ROOT, "examples", "pet_watcher")       # its committed model
    return m


def prompts(m, n=40):
    rng = random.Random(0)
    out = []
    for _ in range(n):
        s = m.Pet(**{f.name: rng.choice(f.values) for f in m.Pet.inputs()})
        out.append(m.app.encode(s))
    return out


@unittest.skipUnless(os.path.exists(MODEL), "no committed pet watcher model")
class Agent(unittest.TestCase):
    def test_guided_plans_are_safe(self):
        m = pet_watcher()
        rng = random.Random(1)
        for _ in range(40):
            s = m.Pet(**{f.name: rng.choice(f.values) for f in m.Pet.inputs()})
            m.app.run_rules(s)
            s._agent.banned = 1 << rng.randrange(len(m.app.tools)) if rng.random() < 0.5 else 0
            m.app._node_reason(s, _Quiet())
            a = s._agent
            if a.src == "lm":
                verdict, why = m.app.verify(a.facts, a.want, a.banned, a.plan, True)
                self.assertNotIn("not allowed", why)
                self.assertNotRegex(why, "needs|must come after|nobody asked")


class _Quiet:
    def say(self, text):
        pass


@unittest.skipUnless(CC and os.path.exists(MODEL), "no C compiler or no committed model")
class CAgreesWithNumpy(unittest.TestCase):
    def test_tokens(self):
        m = pet_watcher()
        eos = m.app.schema.token_id("<eos>")
        lm = TinyLM(MODEL)
        ps = prompts(m)
        work = tempfile.mkdtemp(prefix="tlg_lm_")
        src, exe = os.path.join(work, "check.c"), os.path.join(work, "check")
        with open(src, "w") as f:
            f.write(HARNESS)
        subprocess.run([CC, "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(ROOT, "lm"),
                        "-o", exe, src, os.path.join(ROOT, "lm", "tlg_lm.c"), "-lm"], check=True)
        stdin = "\n".join(f"{len(p)} " + " ".join(map(str, p)) for p in ps) + "\n"
        for guided in (0, 1):
            c = subprocess.run([exe, MODEL, str(guided), str(eos)], input=stdin, check=True,
                               capture_output=True, text=True).stdout.splitlines()
            allow = (lambda out, tok: not out or tok % 3 != 0) if guided else None
            for p, line in zip(ps, c):
                py, _ = lm.generate(p, eos, allow=allow)
                self.assertEqual(line, " ".join(map(str, py)), f"guided={guided}, prompt {p}")


if __name__ == "__main__":
    unittest.main()
