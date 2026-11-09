"""
Ready-made nodes for the decide-and-act loop, like LangGraph's prebuilt ToolNode:

    graph.add_node("reason", prebuilt.reason)       # the tiny model proposes a plan
    graph.add_node("verify", prebuilt.verify)       # checked against your rules and tools
    graph.add_node("plan",   prebuilt.plan)         # the planner repairs a rejected plan
    graph.add_node("ask",    prebuilt.ask)          # a human approves tools marked approval=True
    graph.add_node("act",    prebuilt.Act([log, snapshot, notify]))   # runs the plan's tools

    graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])
    graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])

after_verify returns "act" (plan is fine), "ask" (needs approval) or "plan" (rejected).
after_ask returns "verify" (approved) or "plan" (refused: replan without those tools).
Pass a dict instead of a list to send them to nodes with other names.
"""
from __future__ import annotations

from .agent import AgentError, Tool


class Prebuilt:
    def __init__(self, kind: str):
        self.kind = kind

    def __repr__(self):
        return f"prebuilt.{self.kind}"


class Act(Prebuilt):
    """Runs the tools of the chosen plan, in order. The list order is the tools' order
    everywhere (model vocabulary, planner tie-breaking), so append new tools at the end."""

    def __init__(self, tools: list):
        super().__init__("act")
        self.tools = list(tools)
        if not self.tools:
            raise AgentError("Act needs at least one tool")
        if len(self.tools) > 32:
            raise AgentError("at most 32 tools")
        for t in self.tools:
            if not isinstance(t, Tool):
                raise AgentError(f"{t!r} is not a tool: decorate it with @tool(...)")
        names = [t.name for t in self.tools]
        if len(set(names)) != len(names):
            raise AgentError("two tools have the same name")

    def __repr__(self):
        return f"prebuilt.Act([{', '.join(t.name for t in self.tools)}])"


class Classify(Prebuilt):
    """Asks a catalog model a typed question about the state and writes the answer into a field:

        prebuilt.Classify({"unusual": "Is the cat's behaviour unusual right now?"}, model=models.Counting(...))

    The field is a Score (or Choice) of your state; your rules read it like any other field."""

    def __init__(self, questions: dict, model):
        super().__init__("classify")
        from .models import Model
        if not isinstance(questions, dict) or len(questions) != 1:
            raise AgentError('Classify takes one question for now: {"field": "the question"}; '
                             "add another Classify node for another field")
        (self.field, self.question), = questions.items()
        if not isinstance(self.field, str) or not isinstance(self.question, str):
            raise AgentError('Classify questions look like {"unusual": "Is the cat\'s behaviour unusual?"}')
        if not isinstance(model, Model):
            raise AgentError(f"Classify(model=...) takes a model from the catalog (models.Counting, models.Tree, ...), "
                             f"got {model!r}")
        self.model = model

    def __repr__(self):
        return f"prebuilt.Classify({self.field} <- {self.model!r})"


class Mailbox(Prebuilt):
    """Talking to other boards (tiny_langgraph.net). Put it right after your sense node:

        mailbox = prebuilt.Mailbox(serves=[feed], uses={feeder: [feed]})
        graph.add_node("mailbox", mailbox)

    serves: contracts this board offers. A request fills <contract>_asked ("no"/"yes") and
            <contract>_<param>; your rules decide; a tool answers with mailbox.reply(feed, "done").
    uses:   {Peer: [contracts]} this board calls. It fills <peer>_online ("no"/"yes": seen, and
            its contracts match ours) and <contract>_reply ("none" or the answer, a run later);
            a tool calls with feeder.call(feed, "one")."""

    def __init__(self, serves=(), uses=None):
        super().__init__("mailbox")
        from .net import Contract, Peer
        self.serves = list(serves)
        self.uses = dict(uses or {})
        for c in self.serves:
            if not isinstance(c, Contract):
                raise AgentError(f"Mailbox(serves=[...]) takes contracts, got {c!r}")
        names = [c.name for c in self.serves]
        for peer, contracts in self.uses.items():
            if not isinstance(peer, Peer):
                raise AgentError(f"Mailbox(uses={{...}}) maps Peer('name') to contracts, got {peer!r}")
            if not isinstance(contracts, (list, tuple)) or not all(isinstance(c, Contract) for c in contracts):
                raise AgentError(f"Mailbox(uses={{{peer.name}: [...]}}) takes a list of contracts")
            names += [c.name for c in contracts]
        if len(set(names)) != len(names):
            raise AgentError("Mailbox: each contract once (it names the fields it fills)")
        if not self.serves and not self.uses:
            raise AgentError("Mailbox needs serves=[contracts] and/or uses={Peer: [contracts]}")

    def reply(self, contract, value: str) -> None:
        """Answer the request this run is handling (from a tool)."""
        from .net import _endpoint
        if contract not in self.serves:
            raise AgentError(f"mailbox.reply({contract.name}): add it to Mailbox(serves=[...])")
        ep = _endpoint()
        if ep is None:
            print(f"   mailbox > reply {contract.name}: {value} (not on a network)")
            return
        ep.reply(contract, value)

    def __repr__(self):
        return "prebuilt.Mailbox()"


class PrebuiltRoute:
    def __init__(self, kind: str, keys: tuple[str, ...]):
        self.kind, self.keys = kind, keys
        self.__name__ = kind

    def __repr__(self):
        return f"prebuilt.{self.kind}"


reason = Prebuilt("reason")
verify = Prebuilt("verify")
plan = Prebuilt("plan")
ask = Prebuilt("ask")

after_verify = PrebuiltRoute("after_verify", ("act", "ask", "plan"))
after_ask = PrebuiltRoute("after_ask", ("verify", "plan"))
