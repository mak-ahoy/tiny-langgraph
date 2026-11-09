# Multi-agent

Boards advertise what they can do and ask each other for it, over ESP-NOW: no router needed,
ESP32 and ESP8266 together, messages up to 250 bytes.

**A call is a request, never a command.** It arrives in the receiver's state as input fields.
The receiver's own rules decide, its verifier checks the plan, and a tool replies.

## A contract

Both boards import the same contract:

```python
# contracts.py
from tiny_langgraph import Contract

feed = Contract("feed", about="Give the cat food",
                params={"portions": ("one", "two")},
                replies=("done", "refused", "busy"))
```

Parameters and replies are small sets of values, like `Choice` fields, so both boards' builds
can check every call. Each contract has a **fingerprint** (a hash of its name, parameters and
replies) baked into both builds. A board with a different version of the contract counts as
offline for it, and the trace says why.

## The board that serves it

```python
mailbox = prebuilt.Mailbox(serves=[feed])

class Feeder(State):
    feed_asked    = Choice("no", "yes")        # filled by the mailbox
    feed_portions = Choice("one", "two")
    fed_recently  = Choice("no", "yes")

@tool(cost=3, achieves=[goals.fed, goals.answered], needs=may_feed)
def dispense(s: Feeder):
    feeder_motor.turn(90)
    mailbox.reply(feed, "done")

@tool(cost=1, achieves=goals.answered, needs=was_asked)
def refuse(s: Feeder):
    mailbox.reply(feed, "refused")

graph.add_node("mailbox", mailbox)             # right after sense
```

## The board that uses it

```python
feeder  = Peer("feeder")                       # the other agent's name
mailbox = prebuilt.Mailbox(uses={feeder: [feed]})

class Watcher(State):
    feeder_online = Choice("no", "yes")        # filled by the mailbox
    feed_reply    = Choice("none", "done", "refused", "busy")

def feeder_up(s: Watcher) -> bool:
    return s.feeder_online == "yes"

@tool(cost=2, achieves=goals.fed, needs=feeder_up)
def ask_feeder(s: Watcher):
    feeder.call(feed, "one")
```

Sending is a tool like any other, so the planner chooses it and the verifier checks it. Here
it's only allowed while the feeder is online. A board can serve and use contracts at the same
time.

## Timing

| | |
|---|---|
| online | the peer announced itself (every 5 s) within the last 15 s, with matching fingerprints |
| a request | handled on the receiver's next run; dropped after 3 runs with no answer |
| a reply | arrives on the caller's **next** run, in `<contract>_reply` |
| queues | 4 requests per served contract, 2 replies per used contract |

Only the reply to the latest call counts: if you ask twice, the first reply is dropped as stale.

## Try it on your PC

```python
from tiny_langgraph import Network
net = Network()
net.board("watcher", watcher.app, watcher.Watcher())
net.board("feeder", feeder.app, feeder.Feeder())
net.run(verbose=True)          # one round: every board runs once, messages delivered
net.unplug("feeder")           # and net.plug("feeder")
```

`examples/feeding_station/run.py` plays a whole story. The tests also link both boards'
generated C into one program and check it tells the same story byte for byte.

## On the boards

Both boards need the **same `NET_KEY`** in `config.h` and the **same Wi-Fi channel**: join the
same router, or no router at all. The boot line shows the channel:

```text
network: ESP-NOW as "watcher" on Wi-Fi channel 13 (boards must share the channel and NET_KEY)
```

## Boards built on different PCs

```sh
python -m tiny_langgraph.net export contracts.py feeder.contracts.json    # on the feeder's PC
python -m tiny_langgraph.net import feeder.contracts.json contracts.py    # on the other PC
python -m tiny_langgraph.net import http://feeder.local/mcp contracts.py --key <MCP_KEY>   # from a live board
```

Fingerprints are checked on import.

!!! warning "Not encrypted yet"
    Messages are ESP-NOW broadcasts filtered by a hash of `NET_KEY`. Anyone in radio range who
    knows the key can send requests. The receiving board's rules still decide what it does.
    Per-pair encryption is planned.
