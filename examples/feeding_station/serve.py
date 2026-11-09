"""
The feeder as an MCP server on your PC, so an AI client (e.g. Claude Code) can call it like the
real board: same tools, same answers (tests/test_net.py checks the board's C answers alike).
Its clock follows the PC's, so "not twice within 3 hours" is real time.

    python serve.py                      # http://127.0.0.1:8765/mcp, key "feeder-demo"
    claude mcp add --transport http feeder http://127.0.0.1:8765/mcp --header "Authorization: Bearer feeder-demo"
"""
import argparse
import datetime

import feeder

from tiny_langgraph import Network
from tiny_langgraph.net import McpServer


class PcFeeder(McpServer):
    def _run_now(self):
        now = datetime.datetime.now()
        feeder.clock.set(hour=now.hour, minutes=int(now.timestamp() // 60))
        return super()._run_now()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--key", default="feeder-demo")
    args = ap.parse_args()
    net = Network()
    board = net.board("feeder", feeder.app, feeder.Feeder())
    PcFeeder(board, key=args.key, about=(feeder.Feeder.__doc__ or "").strip()).serve(args.port)


if __name__ == "__main__":
    main()
