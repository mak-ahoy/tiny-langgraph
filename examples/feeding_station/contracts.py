"""
What the feeding station's boards say to each other: one contract, shared by both agents.
On two PCs: `python -m tiny_langgraph.net export contracts.py feeder.contracts.json` on one,
`python -m tiny_langgraph.net import feeder.contracts.json contracts.py` on the other.
"""
from tiny_langgraph import Contract

feed = Contract("feed", about="Give the cat food",
                params={"portions": ("one", "two")},
                replies=("done", "refused", "busy"))
