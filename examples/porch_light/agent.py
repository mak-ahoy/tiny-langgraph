"""
Porch light: a small agent for a NodeMCU (ESP8266).

    python agent.py                 run a few situations on your PC
    python agent.py build nodemcu   write the C code, check it, make the NodeMCU sketch

Lights the porch when someone moves there at night, and tells you (on the board's web page)
when the door has been left open. Rules + planner, no model: the ESP8266 has no room for one.

Wiring (NodeMCU pin names):
    D0  PIR motion sensor output      (D0 is the only ESP8266 pin with a pull-down)
    D2  door reed switch to GND       (closed door = switch closed = LOW)
    D4  the built-in LED as the light (on when LOW)
    D5  passive buzzer (optional)
"""
from tiny_langgraph import END, START, Choice, Goal, Goals, Int, State, StateGraph, prebuilt, rules, tool, want
from tiny_langgraph.drivers import LED, Buzzer, Clock, DigitalIn, Phone

motion = DigitalIn(pin="D0", pulldown=True)
door   = DigitalIn(pin="D2", pullup=True, active_low=True)    # read() is True when the door is closed
light  = LED(pin="D4", active_low=True)
buzzer = Buzzer(pin="D5")
clock  = Clock()
phone  = Phone()


class Porch(State):
    """lights the porch at night and minds the door"""
    motion    = Choice("none", "seen")
    door      = Choice("closed", "open")
    tod       = Choice("day", "night")
    open_long = Choice("no", "yes")          # open for 5 minutes or more
    opened_at = Int(-1)                      # memory: minute the door opened (-1 = closed)
    told      = Int(0)                       # memory: already told about this opening


goals = Goals(
    logged=1,
    lit=Goal(3, only_if_wanted=True),
    dark=Goal(2, only_if_wanted=True),
    warned=Goal(4, only_if_wanted=True),
)


def is_night(s: Porch) -> bool:
    return s.tod == "night"


def door_open(s: Porch) -> bool:
    return s.door == "open"


@tool(cost=1, achieves=goals.logged)
def log(s: Porch):
    pass


@tool(cost=1, achieves=goals.lit, needs=is_night)
def light_on(s: Porch):
    light.on()


@tool(cost=1, achieves=goals.dark)
def light_off(s: Porch):
    light.off()


@tool(cost=2, achieves=goals.warned, needs=door_open)
def tell_owner(s: Porch):
    phone.send("the front door has been open for 5 minutes")
    buzzer.beep(200)
    s.told = 1


def sense(s: Porch):
    now = clock.minutes()
    hour = clock.hour()
    s.motion = "seen" if motion.read() else "none"
    s.tod = "night" if hour >= 20 or hour < 6 else "day"
    if door.read():
        s.door = "closed"
        s.opened_at = -1
        s.told = 0
    else:
        s.door = "open"
        if s.opened_at < 0:
            s.opened_at = now
    s.open_long = "yes" if s.opened_at >= 0 and now - s.opened_at >= 5 and s.told == 0 else "no"


@rules
def assess(s: Porch):
    want(goals.logged)
    if s.motion == "seen" and s.tod == "night":
        want(goals.lit)
    else:
        want(goals.dark)
    if s.open_long == "yes":
        want(goals.warned)


graph = StateGraph(Porch)
graph.add_node("sense", sense)
graph.add_node("assess", assess)
graph.add_node("plan", prebuilt.plan)
graph.add_node("verify", prebuilt.verify)
graph.add_node("ask", prebuilt.ask)
graph.add_node("act", prebuilt.Act([log, light_on, light_off, tell_owner]))
graph.add_edge(START, "sense")
graph.add_edge("sense", "assess")
graph.add_edge("assess", "plan")
graph.add_edge("plan", "verify")
graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])
graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])
graph.add_edge("act", END)

app = graph.compile(name="porch_light")


def demo():
    s = Porch()
    clock.set(hour=22, minutes=0)
    motion.value, door.value = True, True                 # someone on the porch at night, door closed
    app.invoke(s, verbose=True)
    print(f"plan: {' -> '.join(s.plan)}\n")
    motion.value, door.value = False, False               # they went in and left the door open
    for minute in (1, 3, 6):
        clock.set(minutes=minute)
        app.invoke(s, verbose=minute == 6)
    print(f"plan: {' -> '.join(s.plan)}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        app.cli()
    else:
        demo()
