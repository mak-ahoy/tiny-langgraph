"""
Pet watcher: keeps an eye on a cat while you're away.

    python agent.py                 run a few situations on your PC
    python agent.py build           write the C code, make the ESP32-S3 sketch (+ train the model if USE_MODEL)
    python agent.py mermaid         draw the graph

Everything below is plain Python. app.build() turns it into C for the board and
checks that the C decides exactly like this file in every situation.
"""
import os

from tiny_langgraph import END, START, Choice, Goal, Goals, Int, State, StateGraph, Think, prebuilt, rules, tool, want
from tiny_langgraph.drivers import LED, Buzzer, Camera, Clock, DigitalIn, Phone, Servo, Wifi

# False: @rules + the planner decide (default; fast, right by construction).
# True:  the tiny model proposes plans and the verifier checks them, for comparing the two.
# Or without editing:  $env:USE_MODEL=1  (PowerShell)  /  USE_MODEL=1 python agent.py build
USE_MODEL = os.environ.get("USE_MODEL", "0") == "1"

# ---------------------------------------------------------------- hardware
room     = DigitalIn(pin=1, pulldown=True)    # PIR sensor watching the room (LOW if unplugged)
counter  = DigitalIn(pin=2, pulldown=True)    # PIR sensor on the kitchen counter (no cats allowed)
home     = DigitalIn(pin=3, pullup=True, active_low=True)   # switch by the door: on = I'm home
light    = LED(pin=21)            # soft night light
buzzer   = Buzzer(pin=14)
feeder   = Servo(pin=47)          # turns to drop one portion of food
camera   = Camera()
phone    = Phone()                # messages appear on the board's web page
clock    = Clock()
wifi     = Wifi()


# ---------------------------------------------------------------- state
class Pet(State):
    """keeps an eye on a cat and decides how to respond"""
    # what the agent observes: the model reads these
    cat          = Choice("none", "seen", "in_zone")
    activity     = Choice("resting", "normal", "restless")
    last_seen    = Choice("recent", "hours", "long")
    meal         = Choice("not_due", "due", "overdue")
    fed_recently = Choice("no", "yes")
    tod          = Choice("day", "night")
    owner        = Choice("home", "away")
    wifi         = Choice("up", "down")
    recent       = Choice("none", "few", "many")      # alerts lately
    # what the agent concludes: the model predicts these, the rules decide them
    wellbeing    = Think("ok", "watch", "concern")
    mischief     = Think("no", "yes")
    # memory, kept between runs (minutes since the board started)
    seen_at      = Int(0)
    fed_at       = Int(-1000)
    moves        = Int(0)
    window_at    = Int(0)
    alerts       = Int(0)
    alerts_at    = Int(0)


goals = Goals(
    recorded=1,
    evidence=2,
    owner_informed=Goal(3, only_if_wanted=True),   # no surprise messages
    deterred=Goal(4, only_if_wanted=True),         # no surprise beeps
    comforted=Goal(3, only_if_wanted=True),
    fed=Goal(5, only_if_wanted=True),              # never feed unless it's meal time
)


# ---------------------------------------------------------------- conditions
# Checked on the device with its own reading of the situation, so the model can
# never unlock a tool by getting things wrong.
def cat_visible(s: Pet) -> bool:
    return s.cat != "none"

def wifi_up(s: Pet) -> bool:
    return s.wifi == "up"

def restless_night(s: Pet) -> bool:
    return s.activity == "restless" and s.tod == "night"

def can_chirp(s: Pet) -> bool:          # beep only at a cat on the counter, and stop if it isn't working
    return s.cat == "in_zone" and s.recent != "many"

def can_feed(s: Pet) -> bool:           # never overfeed
    return s.meal != "not_due" and s.fed_recently == "no"


# ---------------------------------------------------------------- tools
@tool(cost=1, achieves=goals.recorded)
def log(s: Pet):
    pass                                 # every run is in the trace already

@tool(cost=2, achieves=goals.evidence, needs=cat_visible)
def snapshot(s: Pet):
    return camera.snapshot()

@tool(cost=3, achieves=goals.owner_informed, needs=wifi_up)
def notify(s: Pet):
    phone.send("your cat needs attention")
    s.alerts += 1
    s.alerts_at = clock.minutes()

@tool(cost=3, achieves=goals.deterred, needs=can_chirp)
def chirp(s: Pet):
    buzzer.tone(2000, 150)
    s.alerts += 1
    s.alerts_at = clock.minutes()

@tool(cost=2, achieves=goals.comforted, needs=restless_night)
def night_light(s: Pet):
    light.on()

@tool(cost=3, achieves=goals.fed, needs=can_feed, approval=True)
def dispense(s: Pet):
    feeder.turn(90)
    feeder.turn(0)
    s.fed_at = clock.minutes()


# ---------------------------------------------------------------- nodes
def sense(s: Pet):
    """Turn sensor readings and memory into the observed fields."""
    now = clock.minutes()
    hour = clock.hour()

    if counter.read():
        s.cat = "in_zone"
    elif room.read():
        s.cat = "seen"
    else:
        s.cat = "none"

    if now - s.window_at >= 10:          # count movements over 10 minutes
        s.window_at = now
        s.moves = 0
    if s.cat != "none":
        s.seen_at = now
        s.moves += 1
    if s.moves >= 6:
        s.activity = "restless"
    elif s.moves >= 2:
        s.activity = "normal"
    else:
        s.activity = "resting"

    if now - s.seen_at < 60:
        s.last_seen = "recent"
    elif now - s.seen_at < 240:
        s.last_seen = "hours"
    else:
        s.last_seen = "long"

    if hour == 7 or hour == 19:          # meals at 7:00 and 19:00
        s.meal = "due"
    elif hour in (8, 9, 20, 21):
        s.meal = "overdue"
    else:
        s.meal = "not_due"
    s.fed_recently = "yes" if now - s.fed_at < 180 else "no"

    s.tod = "night" if hour >= 22 or hour < 6 else "day"
    s.owner = "home" if home.read() else "away"
    s.wifi = "up" if wifi.connected() else "down"

    if now - s.alerts_at >= 60:          # alerts in the last hour
        s.alerts = 0
    if s.alerts == 0:
        s.recent = "none"
    elif s.alerts < 3:
        s.recent = "few"
    else:
        s.recent = "many"


@rules
def assess(s: Pet):
    """What the model learns, and what the device checks the model against."""
    s.mischief = "yes" if s.cat == "in_zone" else "no"

    if s.last_seen == "long":
        s.wellbeing = "concern"                    # nobody has seen the cat for a long time
    elif s.meal == "overdue" and not cat_visible(s):
        s.wellbeing = "concern"                    # missed a meal and is nowhere to be seen
    elif s.last_seen == "hours":
        s.wellbeing = "watch"
    elif cat_visible(s) and s.activity == "restless":
        s.wellbeing = "watch"
    elif s.meal == "overdue":
        s.wellbeing = "watch"                      # visible, but hasn't eaten on time
    else:
        s.wellbeing = "ok"

    want(goals.recorded)
    if cat_visible(s) and (s.mischief == "yes" or s.wellbeing != "ok"):
        want(goals.evidence)                       # a photo worth looking at later
    if s.mischief == "yes":
        want(goals.deterred)                       # off the counter, please
        if s.recent == "many":
            want(goals.owner_informed)             # beeping isn't working: tell the owner
    if s.wellbeing == "concern":
        want(goals.owner_informed)
    elif s.wellbeing == "watch" and s.owner == "away" and s.recent == "none":
        want(goals.owner_informed)
    if s.meal != "not_due":
        want(goals.fed)                            # can_feed still stops overfeeding
    if cat_visible(s) and restless_night(s):
        want(goals.comforted)                      # a soft light for a restless cat at night


# ---------------------------------------------------------------- the graph
graph = StateGraph(Pet)
graph.add_node("sense", sense)
graph.add_node("assess", assess)
if USE_MODEL:
    graph.add_node("reason", prebuilt.reason)      # the tiny model proposes a plan
graph.add_node("verify", prebuilt.verify)          # checked against the rules and conditions
graph.add_node("plan", prebuilt.plan)              # the planner finds the cheapest plan (or repairs the model's)
graph.add_node("ask", prebuilt.ask)                # a human approves tools marked approval=True
graph.add_node("act", prebuilt.Act([log, snapshot, notify, chirp, night_light, dispense]))

graph.add_edge(START, "sense")
graph.add_edge("sense", "assess")
if USE_MODEL:
    graph.add_edge("assess", "reason")
    graph.add_edge("reason", "verify")
else:
    graph.add_edge("assess", "plan")
graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])
graph.add_edge("plan", "verify")
graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])
graph.add_edge("act", END)

app = graph.compile(name="pet_watcher")


# ---------------------------------------------------------------- try it on your PC
def ask_human(question: str) -> bool:
    """Approval questions are answered here on the PC (on the board: web page, BOOT button or serial)."""
    return input(f"  >>> {question} [y/n] ").strip().lower().startswith("y")


def show(s: Pet):
    done = "done" if s._agent.acted_ok else "nothing done"
    print(f"plan: {' -> '.join(s.plan) or '(nothing)'}  ({done})\n")


def demo():
    clock.set(hour=14, minutes=600)
    counter.value = True                           # the cat jumps on the counter
    s = app.invoke(Pet(), approve=ask_human, verbose=True)
    show(s)

    counter.value = room.value = False             # evening meal, cat out of sight for hours
    clock.set(hour=20, minutes=900)
    s = app.invoke(s, approve=ask_human, verbose=True)
    show(s)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        app.cli()
    else:
        demo()