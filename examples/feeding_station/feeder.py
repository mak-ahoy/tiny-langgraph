"""
The feeder (a NodeMCU): offers `feed` to the other boards. A call is only a request: the
feeder's own rules decide. Here, a MINIMAL stand-in for testing the network: it feeds if it
hasn't fed in the last 3 hours, and refuses otherwise. The real feeder's rules are yours.

    python run.py              the feeder and the watcher together, on a simulated network
    python feeder.py build nodemcu    the sketch for the NodeMCU
"""
from contracts import feed

from tiny_langgraph import END, START, Choice, Goal, Goals, Int, State, StateGraph, prebuilt, rules, tool, want
from tiny_langgraph.drivers import LED, Button, Clock

led    = LED(pin="D4", active_low=True)      # stands in for the servo: blinks = one portion
button = Button(pin="D3")                    # the FLASH button: "I fed the cat by hand"
clock  = Clock()
mailbox = prebuilt.Mailbox(serves=[feed])    # advertises `feed`; requests fill feed_asked, feed_portions


class Feeder(State):
    """feeds the cat when another board asks, unless it was fed recently"""
    feed_asked    = Choice("no", "yes")      # filled in by the mailbox
    feed_portions = Choice("one", "two")
    fed_recently  = Choice("no", "yes")      # its own memory: fed in the last 3 hours
    fed_at        = Int(-1000)


goals = Goals(
    fed=Goal(5, only_if_wanted=True),
    answered=Goal(2, only_if_wanted=True),
)


def may_feed(s: Feeder) -> bool:
    return s.feed_asked == "yes" and s.fed_recently == "no"


def was_asked(s: Feeder) -> bool:
    return s.feed_asked == "yes"


@tool(cost=3, achieves=[goals.fed, goals.answered], needs=may_feed)
def dispense(s: Feeder):
    led.blink(3, 200)
    s.fed_at = clock.minutes()
    mailbox.reply(feed, "done")


@tool(cost=1, achieves=goals.answered, needs=was_asked)
def refuse(s: Feeder):
    mailbox.reply(feed, "refused")


def sense(s: Feeder):
    if button.pressed():
        s.fed_at = clock.minutes()
    s.fed_recently = "yes" if clock.minutes() - s.fed_at < 180 else "no"


@rules
def assess(s: Feeder):
    """Its own rules: whoever asks, it never feeds twice within 3 hours."""
    if s.feed_asked == "yes":
        want(goals.answered)
        if s.fed_recently == "no":
            want(goals.fed)


graph = StateGraph(Feeder)
graph.add_node("sense", sense)
graph.add_node("mailbox", mailbox)
graph.add_node("assess", assess)
graph.add_node("plan", prebuilt.plan)
graph.add_node("verify", prebuilt.verify)
graph.add_node("ask", prebuilt.ask)
graph.add_node("act", prebuilt.Act([dispense, refuse]))
graph.add_edge(START, "sense")
graph.add_edge("sense", "mailbox")
graph.add_edge("mailbox", "assess")
graph.add_edge("assess", "plan")
graph.add_edge("plan", "verify")
graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])
graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])
graph.add_edge("act", END)

app = graph.compile(name="feeder")


if __name__ == "__main__":
    app.cli()            # python feeder.py build nodemcu
