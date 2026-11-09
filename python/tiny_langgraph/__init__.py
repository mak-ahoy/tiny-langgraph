"""
tiny-langgraph: LangGraph-style agents for microcontrollers.

Write the agent in Python like a LangGraph graph, run it on your PC, then
app.build("esp32s3") turns it into C for the board, with a tiny language model
trained on your rules. See examples/pet_watcher/agent.py.
"""
from . import drivers, models, prebuilt
from .agent import Goal, Goals, interrupt, note, rules, tool, want
from .graph import END, START, CompiledGraph, GraphError, StateGraph
from .net import Contract, Network, Peer
from .state import Choice, Int, Score, State, Think

__version__ = "0.4.0"

__all__ = ["State", "Choice", "Score", "Think", "Int", "StateGraph", "START", "END", "CompiledGraph", "GraphError",
           "Goal", "Goals", "tool", "rules", "want", "note", "interrupt", "prebuilt", "drivers", "models", "Contract", "Peer", "Network"]
