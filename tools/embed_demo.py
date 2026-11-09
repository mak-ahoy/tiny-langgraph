#!/usr/bin/env python3
"""Put a trace log inside viewer/index.html, so "Load demo" works offline.

  ./build/door_guard --trace > demo.log
  python tools/embed_demo.py demo.log viewer/index.html
"""
import re
import sys


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    trace = open(sys.argv[1], encoding="utf-8").read().strip("\n")
    if "</script" in trace.lower():
        print("trace contains '</script', refusing to embed")
        return 1
    html = open(sys.argv[2], encoding="utf-8").read()
    pattern = re.compile(r'(<script type="text/plain" id="demo-trace">\n)(.*?)(\n</script>)', re.S)
    if not pattern.search(html):
        print("demo-trace block not found")
        return 1
    html = pattern.sub(lambda m: m.group(1) + trace + m.group(3), html, count=1)
    open(sys.argv[2], "w", encoding="utf-8").write(html)
    print(f"embedded {trace.count(chr(10)) + 1} lines into {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
