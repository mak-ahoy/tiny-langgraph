# Your first agent

This page builds a small cat feeder for a NodeMCU. Press the button to ask for food; the
feeder feeds unless it fed in the last 3 hours. The rule is enforced by the verifier, on the
board as well as on your PC.

## The whole file

Save this as `feeder.py`:

```python
from tiny_langgraph import START, END, Choice, Int, State, StateGraph, Goal, Goals, prebuilt, rules, tool, want
from tiny_langgraph.drivers import Button, Clock, LED

button, clock, led = Button(pin="D3"), Clock(), LED(pin="D4")

class Feeder(State):
    asked        = Choice("no", "yes")
    fed_recently = Choice("no", "yes")
    fed_at       = Int(-1000)                       # memory between runs

goals = Goals(fed=Goal(5, only_if_wanted=True))

def may_feed(s: Feeder) -> bool:                    # a condition: checked on the device too
    return s.fed_recently == "no"

@tool(cost=3, achieves=goals.fed, needs=may_feed)
def dispense(s: Feeder):
    led.blink(3, 200)
    s.fed_at = clock.minutes()

def sense(s: Feeder):                               # a node: plain Python
    s.asked = "yes" if button.pressed() else "no"
    s.fed_recently = "yes" if clock.minutes() - s.fed_at < 180 else "no"

@rules
def assess(s: Feeder):                              # what the agent wants
    if s.asked == "yes":
        want(goals.fed)

graph = StateGraph(Feeder)
graph.add_node("sense", sense)
graph.add_node("assess", assess)
graph.add_node("plan", prebuilt.plan)               # cheapest plan for what's wanted
graph.add_node("verify", prebuilt.verify)           # checked before anything runs
graph.add_node("ask", prebuilt.ask)                 # human approval for risky tools
graph.add_node("act", prebuilt.Act([dispense]))
graph.add_edge(START, "sense")
graph.add_edge("sense", "assess")
graph.add_edge("assess", "plan")
graph.add_edge("plan", "verify")
graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])
graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])
graph.add_edge("act", END)
app = graph.compile(name="feeder")

if __name__ == "__main__":
    app.cli()                                       # python feeder.py check / build nodemcu
```

## What each part is

| Part | What it is | More |
|---|---|---|
| `Button`, `Clock`, `LED` | hardware, with your pins; simulated on the PC | [Drivers](../guides/drivers.md) |
| `class Feeder(State)` | what the agent knows: observations (`Choice`) and memory (`Int`) | [State and fields](../concepts/state.md) |
| `Goals` | what the agent can achieve, with weights | [Rules, planner, verifier](../concepts/deciding.md) |
| `@tool` | an action: its cost, the goal it achieves, the condition it needs | [Tools and approvals](../concepts/tools.md) |
| `sense` | a node that turns sensor readings into fields | [The graph](../concepts/graph.md) |
| `@rules` | decides what's wanted in this situation | [Rules, planner, verifier](../concepts/deciding.md) |
| `prebuilt.plan/verify/ask/Act` | the ready-made nodes that plan, check, ask and act | [The graph](../concepts/graph.md) |

## Run it on your PC

```python
import feeder
s = feeder.Feeder()
feeder.button.value = True          # simulate a press
feeder.app.invoke(s, verbose=True)
print(s.plan)                       # ['dispense']
```

Simulated outputs print what they would do (`led > blink 3x 200 ms`). Run it again straight
away and the plan is empty: `fed_recently` is now `"yes"`, so the condition `may_feed` blocks
`dispense`.

## Check the C

```sh
python feeder.py check
```

```text
check: the C decides exactly like your Python in all 4 situations
```

The build converts your nodes, tools, conditions and rules to C, compiles them, and runs every
combination of the `Choice` fields through both the C and the Python. If they disagree
anywhere, the build stops and shows the situation.

## Make the sketch

```sh
python feeder.py build nodemcu       # or: build esp32s3
```

This writes `build/nodemcu/feeder/feeder.ino`. Continue with [Flash a board](flash-a-board.md).
