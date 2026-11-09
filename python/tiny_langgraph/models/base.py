"""
The contract every catalog model follows, so any of them plugs into prebuilt.Classify:

  bind(state, field)   check it can answer this field from this state (at compile time)
  fit(...)             learn, on the PC (each model takes its own kind of data)
  predict(s, memory)   the answer (one of the field's values), in Python
  note()               a short explanation of the last answer, for the trace
  functions()          your Python functions it calls (converted to C like conditions)
  cost()               flash and RAM it adds on the board

and for the board (called by the C generator):
  c_includes()         runtime headers it needs
  c_memory()           C type of its memory in the state struct (kept between runs), or None
  c_rules(node, n)     pure C functions for <name>_rules.c (prototype, code)
  c_tables(node, n)    constant tables for <name>_agent.c
  c_init(node)         lines for <name>_state_init (s = the state)
  c_step(node, n, conv, field)   lines in the node that set s->in.<field>
  crosscheck(...)      compile its C and check it answers exactly like predict()
"""
from __future__ import annotations


class ModelError(Exception):
    pass


class Model:
    kind = "model"

    def __init__(self):
        self.state = None
        self.field = None

    # ---- Python side ----
    def bind(self, state, field):
        self.state, self.field = state, field

    @property
    def fitted(self) -> bool:
        return True

    def new_memory(self):
        return None

    def predict(self, s, memory) -> str:
        raise NotImplementedError

    def note(self) -> str:
        return ""

    def functions(self) -> list:
        return []

    def cost(self) -> dict:
        return {"flash": 0, "ram": 0}

    # ---- C side ----
    def c_includes(self) -> list[str]:
        return []

    def c_memory(self) -> str | None:
        return None

    def c_rules(self, node, n) -> tuple[list[str], list[str]]:
        return [], []

    def c_tables(self, node, n) -> list[str]:
        return []

    def c_init(self, node) -> list[str]:
        return []

    def c_step(self, node, n, conv, field) -> list[str]:
        raise NotImplementedError

    def crosscheck(self, app, node, gen_dir, cc, root) -> int:
        return 0
