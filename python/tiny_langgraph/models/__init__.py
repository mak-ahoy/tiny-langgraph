"""
The model catalog: pick a model, put it in a prebuilt.Classify node, wire your graph.

    from tiny_langgraph import models, prebuilt, Score

    class Pet(State):
        cat     = Choice("none", "seen", "in_zone")
        unusual = Score("no", "odd", "alarming")          # answered by a model, read by your rules

    notice = models.Counting(active=cat_visible, clock=clock).load("model/notice.json")
    graph.add_node("notice", prebuilt.Classify({"unusual": "Is the cat's behaviour unusual?"}, model=notice))

A model only fills in a field. Your @rules decide what's wanted and the verifier checks every
plan, whichever model you pick.

Available now:
  Counting   statistics: learns what's normal at each hour (quiet spells, bouts in 2 h / 12 h)
             from the home's own logs, no labels. Measured best for "is this unusual?"
  Tree       a decision tree over Choice/Score fields, learned from labelled examples
"""
from .base import Model, ModelError
from .counting import Counting
from .tree import Tree

__all__ = ["Model", "ModelError", "Counting", "Tree"]
