"""
The watcher (an ESP32-S3): at meal times it asks the feeder board to feed the cat, and tells
you if the feeder refuses or can't be reached. It never feeds by itself: it only asks.

    python run.py              the feeder and the watcher together, on a simulated network
    python watcher.py build esp32s3   the sketch for the ESP32-S3
"""
from contracts import feed

from tiny_langgraph import END, START, Choice, Goal, Goals, Int, Peer, State, StateGraph, prebuilt, rules, tool, want
from tiny_langgraph.drivers import Button, Clock, Phone

feeder = Peer("feeder")                              # the other board, found by its agent name
clock  = Clock()
phone  = Phone()
test_button = Button(0)                       # BOOT: hold it during a run to ask now (bench test)
mailbox = prebuilt.Mailbox(uses={feeder: [feed]})    # fills feeder_online and feed_reply


class Watcher(State):
    """asks the feeder at meal times"""
    meal          = Choice("not_due", "due")
    waiting       = Choice("no", "yes")              # asked within the last hour
    test          = Choice("no", "yes")              # BOOT held: a meal right now, for testing
    feeder_online = Choice("no", "yes")              # filled in by the mailbox (discovery)
    feed_reply    = Choice("none", "done", "refused", "busy")   # the feeder's answer, a run later
    asked_at      = Int(-1000)
    boot_held     = Int(0)                           # BOOT was held last run: one ask per press


goals = Goals(
    fed=Goal(5, only_if_wanted=True),
    told=Goal(3, only_if_wanted=True),
)


def feeder_up(s: Watcher) -> bool:
    return s.feeder_online == "yes"


@tool(cost=2, achieves=goals.fed, needs=feeder_up)
def ask_feeder(s: Watcher):
    feeder.call(feed, "one")
    s.asked_at = clock.minutes()


@tool(cost=1, achieves=goals.told)
def tell_owner(s: Watcher):
    phone.send("the cat's meal didn't happen: the feeder refused or can't be reached")


def sense(s: Watcher):
    hour = clock.hour()
    s.meal = "due" if hour == 7 or hour == 19 else "not_due"
    s.waiting = "yes" if clock.minutes() - s.asked_at < 60 else "no"
    pressed = test_button.pressed()
    s.test = "yes" if pressed and s.boot_held == 0 else "no"
    s.boot_held = 1 if pressed else 0


@rules
def assess(s: Watcher):
    if (s.meal == "due" and s.waiting == "no") or s.test == "yes":
        if s.feeder_online == "yes":
            want(goals.fed)
        else:
            want(goals.told)                         # the feeder isn't there: tell the owner
    if s.feed_reply == "refused" or s.feed_reply == "busy":
        want(goals.told)


graph = StateGraph(Watcher)
graph.add_node("sense", sense)
graph.add_node("mailbox", mailbox)
graph.add_node("assess", assess)
graph.add_node("plan", prebuilt.plan)
graph.add_node("verify", prebuilt.verify)
graph.add_node("ask", prebuilt.ask)
graph.add_node("act", prebuilt.Act([ask_feeder, tell_owner]))
graph.add_edge(START, "sense")
graph.add_edge("sense", "mailbox")
graph.add_edge("mailbox", "assess")
graph.add_edge("assess", "plan")
graph.add_edge("plan", "verify")
graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])
graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])
graph.add_edge("act", END)

app = graph.compile(name="watcher")


if __name__ == "__main__":
    app.cli()            # python watcher.py build esp32s3
