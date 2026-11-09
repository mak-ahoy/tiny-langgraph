#!/usr/bin/env python3
"""
A stand-in for the board's web API, for working on the viewer without hardware.

  ./build/door_guard --trace > build/door_guard.log
  python tools/fake_board.py build/door_guard.log        -> http://127.0.0.1:8080/

Replays the trace slowly and pauses at every approval until the page answers
(PIN 4721), exactly like the board's /tlg/events, /tlg/pending and /tlg/answer.
POST /tlg/reboot simulates a restart. Development only: never expose it.
"""
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN = "4721"
SLOTS = 256


class Board:
    def __init__(self, lines, delay):
        self.lines, self.delay = lines, delay
        self.lock = threading.Lock()
        self.answer_event = threading.Event()
        self.reset()

    def reset(self):
        with self.lock:
            self.boot = f"{random.getrandbits(32):08x}"
            self.sticky, self.ring, self.next_seq = [], [], 1
            self.pending, self.answer, self.failures = None, None, 0
            self.generation = getattr(self, "generation", 0) + 1
        self.answer_event.set()

    def put(self, line):
        with self.lock:
            kind = json.loads(line[5:])["k"]
            if kind == "graph":
                self.sticky = []
            if kind in ("graph", "node"):
                self.sticky.append(line)
                return
            self.ring.append((self.next_seq, line))
            self.next_seq += 1
            del self.ring[:-SLOTS]

    def get(self, since, with_graph):
        with self.lock:
            out = list(self.sticky) if with_graph else []
            last = since
            for seq, line in self.ring:
                if seq > since:
                    out.append(line)
                    last = seq
            return "".join(l + "\n" for l in out), last

    def play(self):
        while True:
            gen = self.generation
            run = 0
            for line in self.lines:
                if gen != self.generation:
                    break
                self.put(line)
                ev = json.loads(line[5:])
                if ev["k"] == "run":
                    run = ev["run"]
                if ev["k"] == "end" and ev["status"] == "interrupted":
                    with self.lock:
                        self.pending, self.answer = {"run": run, "question": ev["reason"]}, None
                        self.answer_event.clear()
                    print(f"[fake board] waiting for an answer: {ev['reason']}", flush=True)
                    self.answer_event.wait(120)
                    with self.lock:
                        print(f"[fake board] answer: {self.answer or 'timeout'}", flush=True)
                        self.pending = None
                time.sleep(self.delay)
            if gen == self.generation:
                time.sleep(3)
                self.reset()


def make_handler(board):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json", headers=()):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                self.send(200, open(os.path.join(ROOT, "viewer", "index.html"), "rb").read(), "text/html")
            elif u.path == "/tlg/info":
                self.send(200, json.dumps({"board": "door_guard", "boot": board.boot, "approval": True}))
            elif u.path == "/tlg/events":
                text, last = board.get(int(q.get("since", ["0"])[0]), "graph" in q)
                self.send(200, text, "text/plain", [("X-Last", str(last)), ("X-More", "0"), ("X-Boot", board.boot)])
            elif u.path == "/tlg/pending":
                p = board.pending
                self.send(200, json.dumps({"pending": True, **p} if p else {"pending": False}))
            else:
                self.send(404, "not found", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(n).decode())
            if u.path == "/tlg/reboot":
                board.reset()
                return self.send(200, '{"ok":true}')
            if u.path != "/tlg/answer":
                return self.send(404, "not found", "text/plain")
            if form.get("pin", [""])[0] != PIN:
                return self.send(403, '{"error":"wrong pin"}')
            with board.lock:
                if not board.pending or board.answer:
                    return self.send(409, '{"error":"nothing is waiting for an answer"}')
                board.answer = form.get("answer", [""])[0]
            board.answer_event.set()
            self.send(200, '{"ok":true}')
    return H


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    lines = [l.rstrip("\n") for l in open(sys.argv[1]) if l.startswith("@tlg ")]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.12
    board = Board(lines, delay)
    threading.Thread(target=board.play, daemon=True).start()
    print(f"fake board on http://127.0.0.1:{port}/  (PIN {PIN})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(board)).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
