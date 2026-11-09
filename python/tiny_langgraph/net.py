"""
Agents on different boards working together, MCP-style: a board advertises CONTRACTS (what it
can do), other boards discover it and call them. A call is a request, never a command: it
arrives as input fields on the receiving board, whose own rules and verifier decide.

    # contracts.py, shared by both agents (or exported/imported between PCs, see below)
    feed = Contract("feed", about="Give the cat food",
                    params={"portions": ("one", "two")}, replies=("done", "refused", "busy"))

    # the board that offers it
    mailbox = prebuilt.Mailbox(serves=[feed])       # fields feed_asked, feed_portions
    ... a tool:  mailbox.reply(feed, "done")

    # the board that uses it
    feeder = Peer("feeder")
    mailbox = prebuilt.Mailbox(uses={feeder: [feed]})   # fields feeder_online, feed_reply
    ... a tool:  feeder.call(feed, "one")

On the PC, Network() connects several agents in one program (see examples/feeding_station).
On boards (step M2/M3): ESP-NOW and an MCP endpoint over HTTP.

Boards built on different PCs: every contract has a fingerprint (its name, params and replies);
a board only talks to a peer whose contracts match. Share contracts as a file:

    python -m tiny_langgraph.net export contracts.py feeder.contracts.json
    python -m tiny_langgraph.net import feeder.contracts.json contracts_feeder.py
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field

from .drivers import Driver
from .state import NAME_RE

MAX_PARAMS = 4            # small, so a request fits one ESP-NOW message
MAX_VALUES = 16


class NetError(Exception):
    pass


class Contract:
    """Something a board offers: a name, a few typed parameters (each one of a few named
    values), the replies it can give, and a description for people and AI clients."""

    def __init__(self, name: str, about: str = "", params: dict | None = None, replies=("done",)):
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise NetError(f"contract name {name!r} must be lower_snake_case")
        params = dict(params or {})
        if len(params) > MAX_PARAMS:
            raise NetError(f"contract {name}: at most {MAX_PARAMS} parameters (a request must fit one radio message)")
        for p, values in params.items():
            if not isinstance(p, str) or not NAME_RE.match(p):
                raise NetError(f"contract {name}: parameter name {p!r} must be lower_snake_case")
            values = tuple(values) if not isinstance(values, str) else (values,)
            if not 2 <= len(values) <= MAX_VALUES or len(set(values)) != len(values) \
                    or not all(isinstance(v, str) and NAME_RE.match(v) for v in values):
                raise NetError(f"contract {name}: parameter {p} takes 2..{MAX_VALUES} different "
                               f"lower_snake_case values, e.g. ('one', 'two')")
            params[p] = values
        replies = tuple(replies)
        if not 1 <= len(replies) <= MAX_VALUES or len(set(replies)) != len(replies) \
                or not all(isinstance(r, str) and NAME_RE.match(r) for r in replies) or "none" in replies:
            raise NetError(f"contract {name}: replies are 1..{MAX_VALUES} different lower_snake_case "
                           "values (not 'none': that means no reply yet)")
        self.name, self.about, self.params, self.replies = name, about, params, replies

    def __repr__(self):
        return f"Contract({self.name!r})"

    def to_dict(self) -> dict:
        return {"name": self.name, "about": self.about,
                "params": {p: list(v) for p, v in self.params.items()}, "replies": list(self.replies)}

    @classmethod
    def from_dict(cls, d) -> "Contract":
        return cls(d["name"], d.get("about", ""), {p: tuple(v) for p, v in d.get("params", {}).items()},
                   tuple(d["replies"]))

    @property
    def fingerprint(self) -> int:
        """32 bits from the name, params and replies (not the description): two builds of the
        same contract agree; any change to what it means gives a different number."""
        canon = json.dumps([self.name, [[p, list(v)] for p, v in self.params.items()], list(self.replies)],
                           separators=(",", ":"))
        return int.from_bytes(hashlib.sha256(canon.encode()).digest()[:4], "little")

    def check_call(self, values):
        if len(values) != len(self.params):
            raise NetError(f"{self.name} takes {len(self.params)} value(s) "
                           f"({', '.join(self.params) or 'none'}), got {len(values)}")
        for (p, allowed), v in zip(self.params.items(), values):
            if v not in allowed:
                raise NetError(f"{self.name}: {p} can't be {v!r}: use one of {', '.join(allowed)}")


class Peer(Driver):
    """Another board, found by its name on the network. Its contracts are called from tools:
    feeder.call(feed, "one"). The answer arrives in <contract>_reply on a later run."""
    c_prefix = "tlg_peer"

    def __init__(self, name: str):
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise NetError(f"peer name {name!r} must be lower_snake_case (the other board's agent name)")
        super().__init__(name)
        self.name = name

    def __repr__(self):
        return f"Peer({self.name!r})"

    def call(self, contract, *values) -> None:
        ep = _endpoint()
        if ep is None:
            self._echo(f"{contract.name}{tuple(values)}: not on a network (dropped)")
            return
        ep.call(self, contract, values)


def _endpoint():
    from . import agent as _agent
    ctx = _agent._context.get()
    return getattr(ctx.state, "_net", None) if ctx is not None else None


# ---------------------------------------------------------------- the network, on the PC
@dataclass
class Request:
    contract: Contract
    values: tuple
    sender: str                        # a board's name, or "@mcp" for a call over HTTP
    runs: int = 0                      # runs of the receiver that saw it without replying
    seq: int = 0


@dataclass
class Endpoint:
    """One board on the network: its inbox of requests, the replies waiting for it, and the
    request each served contract is answering in the current run."""
    network: "Network"
    name: str
    app: object
    state: object
    inbox: dict = field(default_factory=dict)        # contract name -> deque of Request
    replies: dict = field(default_factory=dict)      # contract name -> deque of (value, from)
    current: dict = field(default_factory=dict)      # contract name -> Request being answered
    log: list = field(default_factory=list)
    local: dict = field(default_factory=dict)        # contract name -> (seq, reply) of the last "@mcp" call
    seq: int = 0

    def local_call(self, contract, values) -> int | None:
        """A call over HTTP (MCP): into the same inbox, the same rules. Its seq, or None if full."""
        queue = self.inbox.setdefault(contract.name, deque())
        if len(queue) >= QUEUE:
            return None
        self.seq += 1
        queue.append(Request(contract, tuple(values), "@mcp", seq=self.seq))
        return self.seq

    def local_result(self, contract, seq):
        s, value = self.local.get(contract.name, (None, None))
        return value if s == seq else None

    def note(self, text):
        from . import agent as _agent
        ctx = _agent._context.get()
        if ctx is not None:
            ctx.say(text)
        self.log.append(text)

    def call(self, peer, contract, values):
        mb = self.app.mailbox
        if mb is None or contract not in mb.uses.get(peer, []):
            raise NetError(f"{peer.name}.call({contract.name}): add it to the mailbox: "
                           f"prebuilt.Mailbox(uses={{{peer.name}: [{contract.name}]}})")
        contract.check_call(values)
        ok, why = self.network.deliver(self.name, peer.name, Request(contract, tuple(values), self.name))
        self.note(f"call {peer.name}.{contract.name}({', '.join(values)}): {'sent' if ok else why}")

    def reply(self, contract, value):
        if value not in contract.replies:
            raise NetError(f"{contract.name} replies are {', '.join(contract.replies)}, not {value!r}")
        req = self.current.pop(contract.name, None)
        if req is None:
            self.note(f"reply {contract.name}: {value}: nothing was asked (ignored)")
            return
        self.inbox[contract.name].remove(req)
        if req.sender.startswith("@"):                # asked over HTTP: the answer stays here
            self.local[contract.name] = (req.seq, value)
        else:
            self.network.answer(req.sender, contract, value, self.name)
        self.note(f"reply to {req.sender}.{contract.name}: {value}")


class Network:
    """Several agents in one program on the PC, as if their boards were in radio range:

        net = Network()
        feeder = net.board("feeder", feeder_app, Feeder())
        watcher = net.board("watcher", watcher_app, Watcher())
        net.run()                    # every board runs once, in the order they were added

    net.unplug("feeder") / net.plug("feeder") take a board off the network and back."""

    KEEP_RUNS = 3              # a request nobody replies to is dropped after this many runs

    def __init__(self):
        self.boards: dict[str, Endpoint] = {}
        self.unplugged: set[str] = set()

    def board(self, name, app, state) -> Endpoint:
        if name in self.boards:
            raise NetError(f"there's already a board called {name!r}")
        if not NAME_RE.match(name):
            raise NetError(f"board name {name!r} must be lower_snake_case")
        ep = Endpoint(self, name, app, state)
        object.__setattr__(state, "_net", ep)
        self.boards[name] = ep
        return ep

    def unplug(self, name):
        self.unplugged.add(name)

    def plug(self, name):
        self.unplugged.discard(name)

    def offers(self, name) -> dict[str, int]:
        """What a board advertises: contract name -> fingerprint (discovery)."""
        ep = self.boards.get(name)
        if ep is None or name in self.unplugged or ep.app.mailbox is None:
            return {}
        return {c.name: c.fingerprint for c in ep.app.mailbox.serves}

    def online(self, peer: str, contracts) -> tuple[bool, str]:
        """Can this board's contracts be used on peer right now? (seen, and fingerprints match)"""
        if peer not in self.boards or peer in self.unplugged:
            return False, f"{peer} isn't on the network"
        offers = self.offers(peer)
        for c in contracts:
            if c.name not in offers:
                return False, f"{peer} doesn't offer {c.name}"
            if offers[c.name] != c.fingerprint:
                return False, (f"{peer}'s contract {c.name} doesn't match ours "
                               f"({offers[c.name]:08x} vs {c.fingerprint:08x}): rebuild both with the same contracts")
        return True, "ok"

    def deliver(self, sender, to, req) -> tuple[bool, str]:
        ok, why = self.online(to, [req.contract])
        if not ok:
            return False, f"not sent: {why}"
        queue = self.boards[to].inbox.setdefault(req.contract.name, deque())
        if len(queue) >= QUEUE:                     # like the board: TLG_NET_QUEUE requests per contract
            return False, f"not taken: {to}'s inbox for {req.contract.name} is full"
        queue.append(req)
        return True, "sent"

    def answer(self, to, contract, value, sender):
        ep = self.boards.get(to)
        if ep is not None and to not in self.unplugged:
            ep.replies.setdefault(contract.name, deque()).append((value, sender))

    def run(self, rounds: int = 1, verbose: bool = False, label: str = ""):
        for _ in range(rounds):
            for name, ep in self.boards.items():
                if name in self.unplugged:
                    continue
                if verbose:
                    print(f"[{name}]{' ' + label if label else ''}")
                ep.app.invoke(ep.state, verbose=verbose)


# ---------------------------------------------------------------- the mailbox node (Python side)
def fields_for(mailbox) -> dict[str, tuple[str, ...]]:
    """The state fields a mailbox fills in, with the values each must have (in this order)."""
    out = {}
    for c in mailbox.serves:
        out[f"{c.name}_asked"] = ("no", "yes")
        for p, values in c.params.items():
            out[f"{c.name}_{p}"] = values
    for peer, contracts in mailbox.uses.items():
        out[f"{peer.name}_online"] = ("no", "yes")
        for c in contracts:
            out[f"{c.name}_reply"] = ("none",) + c.replies
    return out


def run_mailbox(mailbox, s, ctx):
    """Requests in, discovery and replies in: fill the mailbox's fields for this run."""
    ep = getattr(s, "_net", None)
    for c in mailbox.serves:
        req = None
        if ep is not None:
            queue = ep.inbox.get(c.name)
            while queue and queue[0].runs >= Network.KEEP_RUNS:      # nobody answered: give up on it
                old = queue.popleft()
                ctx.say(f"{c.name} from {old.sender}: dropped after {Network.KEEP_RUNS} runs without a reply")
            req = queue[0] if queue else None
            if req is not None:
                req.runs += 1
                ep.current[c.name] = req
            else:
                ep.current.pop(c.name, None)
        setattr(s, f"{c.name}_asked", "yes" if req else "no")
        if req:
            for p, v in zip(c.params, req.values):
                setattr(s, f"{c.name}_{p}", v)
            ctx.say(f"asked by {req.sender}: {c.name}({', '.join(req.values)})")
    for peer, contracts in mailbox.uses.items():
        ok, why = ep.network.online(peer.name, contracts) if ep is not None else (False, "not on a network")
        setattr(s, f"{peer.name}_online", "yes" if ok else "no")
        if not ok:
            ctx.say(f"{peer.name}: offline ({why})")
        for c in contracts:
            queue = ep.replies.get(c.name) if ep is not None else None
            value, sender = queue.popleft() if queue else ("none", None)
            setattr(s, f"{c.name}_reply", value)
            if sender:
                ctx.say(f"reply from {sender}: {c.name} -> {value}")


# ---------------------------------------------------------------- the radio message format
# Must match core/tlg_net.c byte for byte (tests/test_net.py checks): little-endian,
# 'T' 'L' version type, network id, sender (16 bytes), then per type (see tlg_net.h).
ANNOUNCE, CALL, REPLY = 1, 2, 3
NAME_BYTES, MSG_BYTES = 16, 250
QUEUE = 4                  # TLG_NET_QUEUE: requests waiting per served contract


def net_id(key: str) -> int:
    """FNV-1a of the network key (NET_KEY in config.h), like tlg_net_id()."""
    h = 2166136261
    for b in key.encode():
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def _name(s) -> bytes:
    b = (s or "").encode()[:NAME_BYTES - 1]
    return b + bytes(NAME_BYTES - len(b))


def encode(kind: int, nid: int, sender: str, target: str = "", seq: int = 0, fp: int = 0, values=()) -> bytes:
    """One radio message. announce: values = fingerprints; call: values = value indices;
    reply: values = (reply index,)."""
    head = b"TL" + bytes([1, kind]) + nid.to_bytes(4, "little") + _name(sender)
    if kind == ANNOUNCE:
        out = head + bytes([len(values)]) + b"".join(v.to_bytes(4, "little") for v in values)
    else:
        out = head + _name(target) + seq.to_bytes(2, "little") + fp.to_bytes(4, "little")
        out += bytes([len(values)]) + bytes(values) if kind == CALL else bytes([values[0]])
    if len(out) > MSG_BYTES:
        raise NetError(f"message too long for one radio packet ({len(out)} > {MSG_BYTES} bytes)")
    return out


def decode(msg: bytes) -> dict | None:
    """The fields of a radio message, or None if it isn't one of ours."""
    if len(msg) < 25 or msg[:2] != b"TL" or msg[2] != 1:
        return None
    out = {"kind": msg[3], "net": int.from_bytes(msg[4:8], "little"),
           "sender": msg[8:24].split(b"\0")[0].decode(errors="replace")}
    if out["kind"] == ANNOUNCE:
        n = msg[24]
        out["fingerprints"] = [int.from_bytes(msg[25 + 4 * i:29 + 4 * i], "little") for i in range(n)]
        return out
    if len(msg) < 47:
        return None
    out.update(target=msg[24:40].split(b"\0")[0].decode(errors="replace"),
               seq=int.from_bytes(msg[40:42], "little"), fingerprint=int.from_bytes(msg[42:46], "little"))
    if out["kind"] == CALL:
        out["values"] = list(msg[47:47 + msg[46]])
    else:
        out["reply"] = msg[46]
    return out


# ---------------------------------------------------------------- MCP: outside AI clients call the board
MCP_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class McpServer:
    """The board's MCP endpoint (core/tlg_mcp.c), in Python: the same answers, so the PC can
    stand in for a board. serve(port) puts a simulated board on real HTTP for MCP clients.

        mcp = McpServer(net.boards["feeder"], key="secret", about="Feeds the cat")
        mcp.serve(8765)     # http://localhost:8765/mcp, header Authorization: Bearer secret"""

    def __init__(self, endpoint: Endpoint, key: str = "", about: str = "", wait_ms: int = 10000):
        self.ep, self.key, self.about, self.wait_ms = endpoint, key, about, wait_ms
        mb = endpoint.app.mailbox
        self.contracts = list(mb.serves) if mb else []

    def _status(self) -> str:
        s = self.ep.state
        vals = {f.name: getattr(s, f.name) for f in s.inputs()}
        vals["plan"] = " ".join(s.plan) if s._agent.acted_ok and s.plan else ("nothing" if s._agent.acted_ok else "-")
        return json.dumps(vals, separators=(",", ":"))

    def _run_now(self) -> bool:
        self.ep.app.invoke(self.ep.state)
        return True

    def handle(self, body: str, authorization: str | None = None) -> tuple[int, str]:
        """One POST to /mcp: (HTTP status, body)."""
        if self.key and authorization != f"Bearer {self.key}":
            return 401, json.dumps({"error": "send the board's MCP key: Authorization: Bearer <MCP_KEY from config.h>"})
        try:
            msg = json.loads(body)
            method = msg["method"]
            if not isinstance(method, str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return 400, json.dumps({"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32600, "message": "not a JSON-RPC request"}})
        if "id" not in msg:
            return 202, ""                                # a notification
        mid, params = msg["id"], msg.get("params") or {}

        def ok(result):
            return 200, json.dumps({"jsonrpc": "2.0", "id": mid, "result": result})

        def err(code, text):
            return 200, json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}})

        def tool(text, reply=None, is_error=False):
            r = {"content": [{"type": "text", "text": text}]}
            if reply is not None:
                r["structuredContent"] = {"reply": reply}
            r["isError"] = is_error
            return ok(r)

        board = self.ep.name
        if method == "initialize":
            asked = params.get("protocolVersion")
            return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": board, "version": "tiny-langgraph 0.4"},
                       "instructions": f"{self.about or board}. Each tool is a request: the board's own rules "
                                       "decide, and its answer is the result."})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            tools = [{"name": c.name, "description": f"{c.about or c.name} (a request: {board}'s own rules decide)",
                      "inputSchema": {"type": "object",
                                      "properties": {p: {"type": "string", "enum": list(v)} for p, v in c.params.items()},
                                      "required": list(c.params), "additionalProperties": False},
                      "outputSchema": {"type": "object",
                                       "properties": {"reply": {"type": "string", "enum": list(c.replies)}},
                                       "required": ["reply"]},
                      "_meta": {"tiny_langgraph": {"fingerprint": f"{c.fingerprint:08x}", "replies": list(c.replies)}}}
                     for c in self.contracts]
            tools.append({"name": f"{board}_status",
                          "description": f"What {board} observes right now, and its last plan (read-only)",
                          "inputSchema": {"type": "object", "properties": {}}})
            return ok({"tools": tools})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return err(-32602, "tools/call needs params.name")
            if name == f"{board}_status":
                return tool(self._status())
            c = next((k for k in self.contracts if k.name == name), None)
            if c is None:
                return err(-32602, f"unknown tool {name}")
            args = params.get("arguments") or {}
            values = []
            for p, allowed in c.params.items():
                if args.get(p) not in allowed:
                    return err(-32602, f"{p} must be one of {', '.join(allowed)}")
                values.append(args[p])
            seq = self.ep.local_call(c, values)
            if seq is None:
                return tool("busy: too many requests are waiting; try again later", is_error=True)
            if not self._run_now():
                return tool("busy: the board is in the middle of something; it will still decide on its next run",
                            is_error=True)
            reply = self.ep.local_result(c, seq)
            if reply is None:
                return tool(f"no answer within {self.wait_ms} ms (waiting for a human's approval?); "
                            "it may still decide later", is_error=True)
            return tool(reply, reply)
        return err(-32601, f"method not found: {method}")

    def serve(self, port: int = 8765, host: str = "127.0.0.1"):
        """Serve /mcp over HTTP until Ctrl-C (one request at a time, like the board)."""
        server = self.http_server(port, host)
        print(f"MCP: http://{host}:{server.server_port}/mcp  (board {self.ep.name}"
              f"{', key ' + self.key if self.key else ''})")
        try:
            server.serve_forever()
        finally:
            server.server_close()

    def http_server(self, port: int = 8765, host: str = "127.0.0.1"):
        """An http.server for /mcp, not started yet (port 0 = any free port)."""
        from http.server import BaseHTTPRequestHandler, HTTPServer
        mcp = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path.split("?")[0] != "/mcp":
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                status, text = mcp.handle(body, self.headers.get("Authorization"))
                self.send_response(status)
                if text:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(text.encode())))
                self.end_headers()
                self.wfile.write(text.encode())

            def do_GET(self):
                self.send_response(405)                   # no server-sent events stream
                self.send_header("Allow", "POST")
                self.end_headers()

            def log_message(self, *a):
                pass

        return HTTPServer((host, port), Handler)


def import_from_board(url: str, key: str = "") -> list[Contract]:
    """The contracts a live board serves, read from its MCP endpoint (tools/list), fingerprints checked."""
    import urllib.request

    def rpc(method, mid, params=None):
        body = {"jsonrpc": "2.0", "method": method, **({"id": mid} if mid is not None else {}), "params": params or {}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream",
                                              **({"Authorization": f"Bearer {key}"} if key else {})})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode()
        return json.loads(text) if text else None

    init = rpc("initialize", 1, {"protocolVersion": MCP_VERSIONS[0], "capabilities": {},
                                 "clientInfo": {"name": "tiny-langgraph", "version": "0.4"}})
    if "result" not in init:
        raise NetError(f"{url}: initialize failed: {init.get('error')}")
    rpc("notifications/initialized", None)
    tools = rpc("tools/list", 2)["result"]["tools"]
    out = []
    for t in tools:
        meta = (t.get("_meta") or {}).get("tiny_langgraph")
        if not meta:
            continue                                      # e.g. <board>_status: not a contract
        about = t.get("description", "").split(" (a request:")[0]
        params = {p: tuple(s["enum"]) for p, s in t["inputSchema"].get("properties", {}).items()}
        c = Contract(t["name"], about if about != t["name"] else "", params, tuple(meta["replies"]))
        if f"{c.fingerprint:08x}" != meta["fingerprint"]:
            raise NetError(f"{url}: contract {c.name} doesn't rebuild to the board's fingerprint "
                           f"({c.fingerprint:08x} vs {meta['fingerprint']})")
        out.append(c)
    return out


# ---------------------------------------------------------------- sharing contracts between PCs
def export_contracts(contracts, path):
    """Write contracts to a JSON file to give to whoever builds the other board."""
    data = {"tiny_langgraph": "contracts", "contracts": [dict(c.to_dict(), fingerprint=f"{c.fingerprint:08x}")
                                                         for c in contracts]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def import_contracts(path) -> list[Contract]:
    """Read a contracts JSON file (from export_contracts); checks every fingerprint."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("tiny_langgraph") != "contracts":
        raise NetError(f"{path} isn't a tiny-langgraph contracts file")
    out = []
    for d in data["contracts"]:
        c = Contract.from_dict(d)
        if "fingerprint" in d and int(d["fingerprint"], 16) != c.fingerprint:
            raise NetError(f"{path}: contract {c.name}'s fingerprint doesn't match its content (edited by hand?)")
        out.append(c)
    return out


def write_module(contracts, path, source=""):
    """A Python module defining the contracts, to import in your agent."""
    import os
    if source and "://" not in source:
        source = os.path.basename(source)                                 # a name, not a path: no backslashes
    origin = f" from {source}" if source else ""
    lines = [f'"""Contracts{origin!s}, generated by tiny-langgraph: don\'t edit, re-import instead."""',
             "from tiny_langgraph import Contract", ""]
    for c in contracts:
        params = "{" + ", ".join(f"{p!r}: {tuple(v)!r}" for p, v in c.params.items()) + "}"
        lines.append(f"{c.name} = Contract({c.name!r}, about={c.about!r},\n"
                     f"    {' ' * len(c.name)}params={params}, replies={c.replies!r})   "
                     f"# fingerprint {c.fingerprint:08x}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _contracts_in(path) -> list[Contract]:
    import importlib.util
    spec = importlib.util.spec_from_file_location("_tlg_contracts", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return [v for v in vars(mod).values() if isinstance(v, Contract)]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m tiny_langgraph.net", description="share contracts between PCs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="contracts in a .py file -> a JSON file to share")
    e.add_argument("module")
    e.add_argument("json")
    i = sub.add_parser("import", help="a shared JSON file, or a live board's MCP URL -> a .py module "
                                      "to import in your agent")
    i.add_argument("json", metavar="json_or_url", help="contracts.json, or http://<board>/mcp")
    i.add_argument("module")
    i.add_argument("--key", default="", help="the board's MCP_KEY (from its config.h), for a URL")
    args = ap.parse_args(argv)
    if args.cmd == "export":
        cs = _contracts_in(args.module)
        export_contracts(cs, args.json)
        print(f"wrote {args.json}: " + ", ".join(f"{c.name} ({c.fingerprint:08x})" for c in cs))
    else:
        live = args.json.startswith(("http://", "https://"))
        cs = import_from_board(args.json, args.key) if live else import_contracts(args.json)
        write_module(cs, args.module, source=args.json)
        print(f"wrote {args.module}: " + ", ".join(f"{c.name} ({c.fingerprint:08x})" for c in cs))


if __name__ == "__main__":
    main()
