"""
Agents on different boards (tiny_langgraph.net), on the PC: contracts and their fingerprints,
sharing contracts between PCs, the mailbox node, and the feeding station example end to end.

    python -m unittest discover -s tests -p "test_*.py"
"""
import importlib
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_python_api import load  # noqa: E402
from tiny_langgraph import GraphError, Network, drivers, net  # noqa: E402
from tiny_langgraph.build import BuildError, build  # noqa: E402
from tiny_langgraph.net import Contract, NetError  # noqa: E402

drivers.quiet()
STATION = os.path.join(ROOT, "examples", "feeding_station")


def station():
    """Fresh copies of the example's two agents (each test gets its own drivers and state)."""
    sys.path.insert(0, STATION)
    for m in ("contracts", "feeder", "watcher"):
        sys.modules.pop(m, None)
    feeder, watcher = importlib.import_module("feeder"), importlib.import_module("watcher")
    sys.path.remove(STATION)
    netw = Network()
    w = netw.board("watcher", watcher.app, watcher.Watcher())
    f = netw.board("feeder", feeder.app, feeder.Feeder())
    return netw, watcher, feeder, w.state, f.state


def at(mods, hour, minute=0, day=0):
    for m in mods:
        m.clock.set(hour=hour, minutes=day * 1440 + hour * 60 + minute)


class Contracts(unittest.TestCase):
    def test_validation(self):
        with self.assertRaisesRegex(NetError, "lower_snake_case"):
            Contract("Feed")
        with self.assertRaisesRegex(NetError, "2..16 different"):
            Contract("feed", params={"portions": ("one",)})
        with self.assertRaisesRegex(NetError, "not 'none'"):
            Contract("feed", replies=("done", "none"))
        with self.assertRaisesRegex(NetError, "at most 4 parameters"):
            Contract("feed", params={f"p{i}": ("a", "b") for i in range(5)})

    def test_fingerprint(self):
        a = Contract("feed", about="x", params={"portions": ("one", "two")}, replies=("done", "refused"))
        b = Contract("feed", about="other words", params={"portions": ("one", "two")}, replies=("done", "refused"))
        c = Contract("feed", params={"portions": ("one", "two", "three")}, replies=("done", "refused"))
        self.assertEqual(a.fingerprint, b.fingerprint)          # the description doesn't change the meaning
        self.assertNotEqual(a.fingerprint, c.fingerprint)       # a new value does

    def test_share_between_pcs(self):
        d = tempfile.mkdtemp()
        src, js, out = os.path.join(d, "mine.py"), os.path.join(d, "c.json"), os.path.join(d, "theirs.py")
        with open(src, "w") as f:
            f.write("from tiny_langgraph import Contract\n"
                    "feed = Contract('feed', about='Give the cat food', params={'portions': ('one', 'two')},"
                    " replies=('done', 'refused'))\n")
        net.main(["export", src, js])
        net.main(["import", js, out])
        theirs = net._contracts_in(out)
        self.assertEqual([(c.name, c.fingerprint, c.about) for c in theirs],
                         [(c.name, c.fingerprint, c.about) for c in net._contracts_in(src)])
        with open(js) as f:
            text = f.read().replace('"refused"', '"maybe"')        # edited by hand: the fingerprint won't match
        with open(js, "w") as f:
            f.write(text)
        with self.assertRaisesRegex(NetError, "fingerprint doesn't match"):
            net.import_contracts(js)


class Mailbox(unittest.TestCase):
    AGENT = '''
from tiny_langgraph import *

ping = Contract("ping", params={"level": ("low", "high")}, replies=("pong",))
mailbox = prebuilt.Mailbox(serves=[ping])

class S(State):
    ping_asked = Choice("no", "yes")
    ping_level = Choice("low", "high")

goals = Goals(logged=1)

@tool(cost=1, achieves=goals.logged)
def log(s: S):
    pass

@rules
def assess(s: S):
    want(goals.logged)

graph = StateGraph(S)
graph.add_node("mailbox", mailbox)
graph.add_node("assess", assess)
graph.add_node("plan", prebuilt.plan)
graph.add_node("verify", prebuilt.verify)
graph.add_node("ask", prebuilt.ask)
graph.add_node("act", prebuilt.Act([log]))
graph.add_edge(START, "mailbox")
graph.add_edge("mailbox", "assess")
graph.add_edge("assess", "plan")
graph.add_edge("plan", "verify")
graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])
graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])
graph.add_edge("act", END)
app = graph.compile(name="pinger")
'''

    def test_fields_are_checked(self):
        with self.assertRaisesRegex(GraphError, "add them to S:\n    ping_level = Choice\\('low', 'high'\\)"):
            load(self.AGENT.replace('    ping_level = Choice("low", "high")\n', ""))
        with self.assertRaisesRegex(GraphError, "declare it as  ping_level = Choice\\('low', 'high'\\)"):
            load(self.AGENT.replace('ping_level = Choice("low", "high")', 'ping_level = Choice("high", "low")'))

    def test_unanswered_requests_are_dropped(self):
        m = load(self.AGENT)                                      # serves ping but never replies
        netw = Network()
        ep = netw.board("pinger", m.app, m.S())
        netw.boards["pinger"].inbox["ping"] = net.deque([net.Request(m.ping, ("high",), "someone")])
        for _ in range(net.Network.KEEP_RUNS):
            netw.run()
            self.assertEqual(ep.state.ping_asked, "yes")
            self.assertEqual(ep.state.ping_level, "high")
        netw.run()
        self.assertEqual(ep.state.ping_asked, "no")

    def test_without_a_network(self):
        m = load(self.AGENT)
        s = m.app.invoke(m.S())
        self.assertEqual(s.ping_asked, "no")

    def test_builds_for_boards(self):
        m = load(self.AGENT)
        r = build(m.app, "nodemcu", log=lambda _: None)
        with open(os.path.join(r["out"], "tlg_build.h")) as f:
            self.assertIn("#define TLG_USE_NET 1", f.read())
        with open(os.path.join(r["out"], "pinger_agent.c")) as f:
            agent_c = f.read()
        self.assertIn("tlg_net_take_request(&NET, 0, v, from)", agent_c)
        self.assertIn(f"0x{m.ping.fingerprint:08x}u", agent_c)        # the contract's fingerprint, baked in
        self.assertIn("tlg_net.c", os.listdir(r["out"]))

    def test_converter_checks_calls(self):
        bad = self.AGENT.replace("def log(s: S):\n    pass", 'def log(s: S):\n    mailbox.reply(ping, "maybe")')
        m = load(bad)
        from tiny_langgraph import cagent
        from tiny_langgraph.transpile import SubsetError
        with self.assertRaisesRegex(SubsetError, "takes one of 'pong'"):
            cagent.generate(m.app, tempfile.mkdtemp(), "agent.py")


class FeedingStation(unittest.TestCase):
    def test_ask_feed_reply(self):
        netw, watcher, feeder, ws, fs = station()
        at((watcher, feeder), 7)
        netw.run()
        self.assertEqual(ws.plan, ["ask_feeder"])
        self.assertEqual(fs.plan, ["dispense"])                   # the feeder's own rules allowed it
        at((watcher, feeder), 7, 1)
        netw.run()
        self.assertEqual(ws.feed_reply, "done")
        self.assertEqual(ws.plan, [])                             # no asking again, no message

    def test_the_feeder_decides(self):
        netw, watcher, feeder, ws, fs = station()
        fs.fed_at = 18 * 60 + 30                                  # fed by hand at 18:30
        at((watcher, feeder), 19)
        netw.run()
        self.assertEqual(fs.plan, ["refuse"])
        at((watcher, feeder), 19, 1)
        netw.run()
        self.assertEqual(ws.feed_reply, "refused")
        self.assertEqual(ws.plan, ["tell_owner"])

    def test_feeder_offline(self):
        netw, watcher, feeder, ws, fs = station()
        netw.unplug("feeder")
        at((watcher, feeder), 7)
        netw.run()
        self.assertEqual(ws.feeder_online, "no")
        self.assertEqual(ws.plan, ["tell_owner"])                 # can't call it: the verifier wouldn't allow it

    def test_contracts_must_match(self):
        """Boards built from different contracts don't talk: the peer counts as offline."""
        netw, watcher, feeder, ws, fs = station()
        old = feeder.mailbox.serves[0]
        feeder.mailbox.serves[0] = Contract("feed", params={"portions": ("one", "two", "three")},
                                            replies=("done", "refused", "busy"))
        try:
            ok, why = netw.online("feeder", [watcher.feed])
            self.assertFalse(ok)
            self.assertIn("doesn't match ours", why)
            at((watcher, feeder), 7)
            netw.run()
            self.assertEqual(ws.feeder_online, "no")
        finally:
            feeder.mailbox.serves[0] = old

    def test_calls_are_checked(self):
        netw, watcher, feeder, ws, fs = station()
        ep = netw.boards["watcher"]
        with self.assertRaisesRegex(NetError, "takes 1 value"):
            ep.call(watcher.feeder, watcher.feed, ())
        with self.assertRaisesRegex(NetError, "portions can't be 'three'"):
            ep.call(watcher.feeder, watcher.feed, ("three",))
        other = Contract("play", replies=("ok",))
        with self.assertRaisesRegex(NetError, "add it to the mailbox"):
            ep.call(watcher.feeder, other, ())


CC = __import__("shutil").which(os.environ.get("CC", "cc")) or __import__("shutil").which("gcc")

# the story of examples/feeding_station/run.py: (hour, minute, day, what changes before the round)
STORY = [(7, 0, 0, ""), (7, 1, 0, ""), (19, 0, 0, "fed by hand"), (19, 1, 0, ""), (7, 0, 1, "unplug feeder")]


def python_story():
    netw, watcher, feeder, ws, fs = station()
    lines = []
    for hour, minute, day, change in STORY:
        if change == "fed by hand":
            fs.fed_at = 18 * 60 + 30
        if change == "unplug feeder":
            netw.unplug("feeder")
        at((watcher, feeder), hour, minute, day)
        netw.run()
        lines.append(f"w|{' '.join(ws.plan)}|{ws.feed_reply}|{ws.feeder_online}")
        if "feeder" not in netw.unplugged:
            lines.append(f"f|{' '.join(fs.plan)}|{fs.feed_asked}")
    return lines, watcher, feeder


C_STORY = r"""
#include "watcher_agent.h"
#include "feeder_agent.h"
#include "tlg_drivers.h"
#include <stdio.h>

static watcher_state_t W;
static feeder_state_t F;
static tlg_graph_t gw, gf;
static uint32_t now_ms;
static int feeder_plugged = 1;

/* the radio: every announcement and queued message reaches the other board (if it's plugged in) */
static void radio(void)
{
    uint8_t buf[TLG_NET_MSG];
    int n;
    if (feeder_plugged) {
        n = tlg_net_announce(feeder_net(), buf, sizeof buf);
        tlg_net_receive(watcher_net(), buf, (size_t)n, now_ms);
    }
    n = tlg_net_announce(watcher_net(), buf, sizeof buf);
    if (feeder_plugged) tlg_net_receive(feeder_net(), buf, (size_t)n, now_ms);
    while ((n = tlg_net_next_out(watcher_net(), buf, sizeof buf)) > 0)
        if (feeder_plugged) tlg_net_receive(feeder_net(), buf, (size_t)n, now_ms);
    while ((n = tlg_net_next_out(feeder_net(), buf, sizeof buf)) > 0)
        if (feeder_plugged) tlg_net_receive(watcher_net(), buf, (size_t)n, now_ms);
}

static void plan_names(const tlg_domain_t *d, const tlg_agent_t *a, char *out)
{
    out[0] = '\0';
    if (!a->acted_ok) return;
    for (int i = 0; i < a->plan_len; i++) {
        strcat(out, i ? " " : "");
        strcat(out, d->actions[a->plan[i]].name);
    }
}

static void round_at(int hour, int minute, int day)
{
    char names[200];
    tlg_run_t r;
    tlg_host_clock(hour, day * 1440 + hour * 60 + minute);
    now_ms = (uint32_t)(day * 1440 + hour * 60 + minute) * 60000u;
    tlg_net_tick(watcher_net(), now_ms);
    tlg_net_tick(feeder_net(), now_ms);
    radio();
    watcher_begin(&W);
    tlg_run_init(&r, &gw);
    tlg_invoke(&gw, &r, &W);
    plan_names(&watcher_domain, &W.agent, names);
    radio();
    if (feeder_plugged) {
        char fnames[200];
        feeder_begin(&F);
        tlg_run_init(&r, &gf);
        tlg_invoke(&gf, &r, &F);
        plan_names(&feeder_domain, &F.agent, fnames);
        radio();
        printf("w|%s|%s|%s\n", names, (const char *[]){ "none", "done", "refused", "busy" }[W.in.feed_reply],
               W.in.feeder_online ? "yes" : "no");
        printf("f|%s|%s\n", fnames, F.in.feed_asked ? "yes" : "no");
    } else {
        printf("w|%s|%s|%s\n", names, (const char *[]){ "none", "done", "refused", "busy" }[W.in.feed_reply],
               W.in.feeder_online ? "yes" : "no");
    }
}

int main(void)
{
    watcher_state_init(&W);
    feeder_state_init(&F);
    watcher_graph(&gw, &W);
    feeder_graph(&gf, &F);
    round_at(7, 0, 0);
    round_at(7, 1, 0);
    F.fed_at = 18 * 60 + 30;
    round_at(19, 0, 0);
    round_at(19, 1, 0);
    feeder_plugged = 0;
    round_at(7, 0, 1);
    return 0;
}
"""


class MessageFormat(unittest.TestCase):
    def test_round_trip(self):
        nid = net.net_id("my house")
        a = net.encode(net.ANNOUNCE, nid, "feeder", values=[0x12345678, 0xdeadbeef])
        self.assertEqual(net.decode(a), {"kind": 1, "net": nid, "sender": "feeder", "fingerprints": [0x12345678, 0xdeadbeef]})
        c = net.encode(net.CALL, nid, "watcher", "feeder", 7, 0xabcdef01, [1, 0])
        self.assertEqual(net.decode(c)["values"], [1, 0])
        r = net.encode(net.REPLY, nid, "feeder", "watcher", 7, 0xabcdef01, [2])
        self.assertEqual((net.decode(r)["reply"], net.decode(r)["seq"]), (2, 7))
        self.assertIsNone(net.decode(b"hello"))

    @unittest.skipUnless(CC, "no C compiler")
    def test_c_writes_the_same_bytes(self):
        nid = net.net_id("my house")
        cases = [(net.ANNOUNCE, "feeder", "", 0, 0, [0x12345678, 0xdeadbeef]),
                 (net.CALL, "watcher", "feeder", 7, 0xabcdef01, [1, 0]),
                 (net.REPLY, "feeder", "watcher", 65535, 0x01020304, [2])]
        lines = []
        for kind, sender, target, seq, fp, values in cases:
            if kind == net.ANNOUNCE:
                vals = "".join(f"0x{b:02x}, " for v in values for b in v.to_bytes(4, "little"))
            else:
                vals = ", ".join(map(str, values))
            lines.append(f'    {{ const uint8_t v[] = {{ {vals} }}; int n = tlg_net_encode(buf, sizeof buf, {kind}, '
                         f'tlg_net_id("my house"), "{sender}", "{target}", {seq}, {fp}u, v, {len(values)});'
                         f' for (int i = 0; i < n; i++) printf("%02x", buf[i]); printf("\\n"); }}')
        src = ('#include "tlg_net.h"\n#include <stdio.h>\nint main(void) {\n    uint8_t buf[TLG_NET_MSG];\n'
               + "\n".join(lines) + "\n    return 0;\n}\n")
        work = tempfile.mkdtemp(prefix="tlg_netfmt_")
        path, exe = os.path.join(work, "fmt.c"), os.path.join(work, "fmt")
        with open(path, "w") as f:
            f.write(src)
        __import__("subprocess").run([CC, "-std=c11", "-Wall", "-Werror", "-I", os.path.join(ROOT, "core"), "-o", exe,
                                      path, os.path.join(ROOT, "core", "tlg_net.c")], check=True)
        out = __import__("subprocess").run([exe], check=True, capture_output=True, text=True).stdout.split()
        for (kind, sender, target, seq, fp, values), c in zip(cases, out):
            self.assertEqual(c, net.encode(kind, nid, sender, target, seq, fp, values).hex())


@unittest.skipUnless(CC, "no C compiler")
class OnTheBoards(unittest.TestCase):
    def test_c_tells_the_same_story_as_python(self):
        """Both agents' generated C in one program, the radio replaced by passing the bytes:
        every run's plan and fields match the Python simulation."""
        import subprocess
        from tiny_langgraph import cagent
        py, watcher, feeder = python_story()
        work = tempfile.mkdtemp(prefix="tlg_station_")
        srcs, incs = [], []
        for mod, name in ((watcher, "watcher"), (feeder, "feeder")):
            gen = os.path.join(work, name)
            files, _ = cagent.generate(mod.app, gen, f"{name}.py")
            srcs += [f for f in files if f.endswith(".c")]
            incs += ["-I", gen]
        main = os.path.join(work, "main.c")
        with open(main, "w") as f:
            f.write(C_STORY.replace("#include <stdio.h>", "#include <stdio.h>\n#include <string.h>"))
        exe = os.path.join(work, "story")
        core = [os.path.join(ROOT, "core", f) for f in ("tlg.c", "tlg_plan.c", "tlg_trace.c", "tlg_agent.c",
                                                         "tlg_count.c", "tlg_net.c")]
        subprocess.run([CC, "-std=c11", "-Wall", "-Wextra", "-Werror", *incs, "-I", os.path.join(ROOT, "core"),
                        "-I", os.path.join(ROOT, "drivers"), "-o", exe, main, *srcs, *core,
                        os.path.join(ROOT, "drivers", "tlg_drivers_host.c")], check=True)
        out = subprocess.run([exe], check=True, capture_output=True, text=True).stdout.splitlines()
        c = [x for x in out if x.startswith(("w|", "f|"))]
        self.assertEqual(c, py)


# ---------------------------------------------------------------- MCP: outside clients call the feeder
KEY = "k3y"
ABOUT = "feeds the cat when another board asks"


def rpc(method, mid=1, params=None):
    import json
    return json.dumps({"jsonrpc": "2.0", "method": method, **({} if mid is None else {"id": mid}),
                       **({} if params is None else {"params": params})})


MCP_SESSION = [                                   # (authorization, body), sent in this order
    (None, rpc("ping")),
    ("Bearer wrong", rpc("ping")),
    (f"Bearer {KEY}", rpc("initialize", 1, {"protocolVersion": "2025-03-26", "capabilities": {}})),
    (f"Bearer {KEY}", rpc("initialize", 2, {"protocolVersion": "1999-01-01"})),
    (f"Bearer {KEY}", rpc("notifications/initialized", None)),
    (f"Bearer {KEY}", rpc("ping", "a1")),
    (f"Bearer {KEY}", rpc("tools/list", 3)),
    (f"Bearer {KEY}", rpc("tools/call", 4, {"name": "feeder_status", "arguments": {}})),
    (f"Bearer {KEY}", rpc("tools/call", 5, {"name": "feed", "arguments": {"portions": "two"}})),
    (f"Bearer {KEY}", rpc("tools/call", 6, {"name": "feeder_status"})),
    (f"Bearer {KEY}", rpc("tools/call", 7, {"name": "feed", "arguments": {"portions": "one"}})),
    (f"Bearer {KEY}", rpc("tools/call", 8, {"name": "feed", "arguments": {"portions": "three"}})),
    (f"Bearer {KEY}", rpc("tools/call", 9, {"name": "feed"})),
    (f"Bearer {KEY}", rpc("tools/call", 10, {"name": "dance", "arguments": {}})),
    (f"Bearer {KEY}", rpc("tools/call", 11, {"arguments": {}})),
    (f"Bearer {KEY}", rpc("resources/list", 12)),
    (f"Bearer {KEY}", "hello"),
]

C_MCP = r"""
#include "feeder_agent.h"
#include "tlg_drivers.h"
#include <stdio.h>
#include <string.h>

static feeder_state_t F;
static tlg_graph_t g;

static bool run_now(void *ctx, uint32_t wait_ms)
{
    (void)ctx; (void)wait_ms;
    tlg_run_t r;
    feeder_begin(&F);
    tlg_run_init(&r, &g);
    tlg_invoke(&g, &r, &F);
    return true;
}

static int status(void *ctx, char *buf, size_t size) { (void)ctx; return feeder_status_json(&F, buf, size); }

int main(void)
{
    static char line[4096], out[8192];
    tlg_mcp_t m = { 0 };
    feeder_state_init(&F);
    feeder_graph(&g, &F);
    tlg_host_clock(7, 7 * 60);
    m.board = "feeder"; m.about = "@ABOUT@"; m.key = "@KEY@"; m.wait_ms = 10000;
    m.contracts = feeder_mcp_list(&m.n_contracts);
    m.net = feeder_net();
    m.run_now = run_now; m.status = status;
    while (fgets(line, sizeof line, stdin)) {          /* authorization TAB body; "-" = no header */
        line[strcspn(line, "\r\n")] = '\0';
        char *body = strchr(line, '\t');
        *body++ = '\0';
        int http = 0;
        tlg_mcp_handle(&m, body, strlen(body), strcmp(line, "-") ? line : NULL, out, sizeof out, &http);
        printf("%d\t%s\n", http, out);
    }
    return 0;
}
"""


def feeder_mcp():
    netw, watcher, feeder, ws, fs = station()
    feeder.clock.set(hour=7, minutes=7 * 60)
    return net.McpServer(netw.boards["feeder"], key=KEY, about=ABOUT), feeder


class Mcp(unittest.TestCase):
    def session(self, mcp):
        import json
        out = []
        for auth, body in MCP_SESSION:
            status, text = mcp.handle(body, auth)
            out.append((status, json.loads(text) if text else None))
        return out

    def test_a_client_session(self):
        import json
        mcp, _ = feeder_mcp()
        r = self.session(mcp)
        self.assertEqual([s for s, _ in r], [401, 401, 200, 200, 202, 200, 200, 200, 200, 200, 200, 200, 200,
                                             200, 200, 200, 400])
        self.assertEqual(r[2][1]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(r[3][1]["result"]["protocolVersion"], "2025-06-18")     # unknown: offers its newest
        self.assertEqual(r[5][1], {"jsonrpc": "2.0", "id": "a1", "result": {}})
        tools = {t["name"]: t for t in r[6][1]["result"]["tools"]}
        self.assertEqual(set(tools), {"feed", "feeder_status"})
        self.assertEqual(tools["feed"]["inputSchema"]["properties"]["portions"]["enum"], ["one", "two"])
        self.assertEqual(json.loads(r[7][1]["result"]["content"][0]["text"])["plan"], "-")    # no run yet
        self.assertEqual(r[8][1]["result"]["structuredContent"], {"reply": "done"})          # the feeder decided
        status = json.loads(r[9][1]["result"]["content"][0]["text"])
        self.assertEqual((status["feed_portions"], status["plan"]), ("two", "dispense"))
        self.assertEqual(r[10][1]["result"]["structuredContent"], {"reply": "refused"})      # its rules: not twice
        self.assertEqual(r[11][1]["error"]["message"], "portions must be one of one, two")
        self.assertEqual(r[12][1]["error"]["code"], -32602)
        self.assertEqual(r[13][1]["error"]["message"], "unknown tool dance")
        self.assertEqual(r[15][1]["error"]["code"], -32601)

    def test_no_answer_and_busy(self):
        mcp, _ = feeder_mcp()
        mcp._run_now = lambda: True                      # the board doesn't get to run in time
        status, text = mcp.handle(rpc("tools/call", 1, {"name": "feed", "arguments": {"portions": "one"}}),
                                  f"Bearer {KEY}")
        self.assertIn("no answer within 10000 ms", text)
        for _ in range(4):                               # the inbox holds 4 per contract
            mcp.handle(rpc("tools/call", 1, {"name": "feed", "arguments": {"portions": "one"}}), f"Bearer {KEY}")
        status, text = mcp.handle(rpc("tools/call", 1, {"name": "feed", "arguments": {"portions": "one"}}),
                                  f"Bearer {KEY}")
        self.assertIn("busy: too many requests", text)

    def test_import_from_a_live_board(self):
        """Over real HTTP, the way another PC reads a board's contracts (fingerprints checked)."""
        import threading
        mcp, _ = feeder_mcp()
        server = mcp.http_server(0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/mcp"
            cs = net.import_from_board(url, KEY)
            sys.path.insert(0, STATION)
            from contracts import feed
            sys.path.remove(STATION)
            self.assertEqual([(c.name, c.fingerprint) for c in cs], [("feed", feed.fingerprint)])
            with self.assertRaises(Exception):
                net.import_from_board(url, "wrong")      # 401
            out = os.path.join(tempfile.mkdtemp(), "theirs.py")
            net.main(["import", url, out, "--key", KEY])
            with open(out) as f:
                self.assertIn("feed = Contract('feed'", f.read())
        finally:
            server.shutdown()
            server.server_close()

    @unittest.skipUnless(CC, "no C compiler")
    def test_c_answers_like_python(self):
        """The board's tlg_mcp.c, fed the same session, answers exactly like McpServer."""
        import json
        import subprocess
        from tiny_langgraph import cagent
        mcp, feeder = feeder_mcp()
        py = self.session(mcp)
        work = tempfile.mkdtemp(prefix="tlg_mcp_")
        gen = os.path.join(work, "feeder")
        files, _ = cagent.generate(feeder.app, gen, "feeder.py")
        main = os.path.join(work, "main.c")
        with open(main, "w") as f:
            f.write(C_MCP.replace("@ABOUT@", ABOUT).replace("@KEY@", KEY))
        exe = os.path.join(work, "mcp")
        core = [os.path.join(ROOT, "core", f) for f in ("tlg.c", "tlg_plan.c", "tlg_trace.c", "tlg_agent.c",
                                                         "tlg_count.c", "tlg_net.c", "tlg_mcp.c")]
        subprocess.run([CC, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", gen, "-I", os.path.join(ROOT, "core"),
                        "-I", os.path.join(ROOT, "drivers"), "-o", exe, main,
                        *[x for x in files if x.endswith(".c")], *core,
                        os.path.join(ROOT, "drivers", "tlg_drivers_host.c")], check=True)
        stdin = "".join(f"{a or '-'}\t{b}\n" for a, b in MCP_SESSION)
        out = subprocess.run([exe], input=stdin, check=True, capture_output=True, text=True).stdout
        c = []
        for line in out.splitlines():
            if "\t" not in line or not line.split("\t")[0].isdigit():
                continue                                 # driver prints
            status, text = line.split("\t", 1)
            c.append((int(status), json.loads(text) if text else None))
        self.assertEqual(c, py)


if __name__ == "__main__":
    unittest.main()
