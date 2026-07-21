#!/usr/bin/env python3
"""P0 payload probe: append every hook invocation's raw stdin JSON to a local
log so the reporter's parsing can be written against real payloads instead of
docs. Replaced by report.py in P1. Never blocks the session: any failure exits
0, and large tool_response bodies are truncated rather than skipped so the
field inventory stays complete."""
import json
import os
import sys
import time

LOG_DIR = os.path.expanduser("~/.cache/gitmap/probe")
TRUNCATE_AT = 4000


def _truncate(obj, depth=0):
    if isinstance(obj, str) and len(obj) > TRUNCATE_AT:
        return obj[:TRUNCATE_AT] + f"...<truncated {len(obj)} chars>"
    if isinstance(obj, dict) and depth < 6:
        return {k: _truncate(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list) and depth < 6:
        return [_truncate(v, depth + 1) for v in obj[:50]]
    return obj


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"unparseable": raw[:TRUNCATE_AT]}
    line = json.dumps(
        {"probe_ts": time.time(),
         "env": {k: v for k, v in os.environ.items()
                 if k.startswith(("CLAUDE_PLUGIN", "CLAUDE_"))
                 and "TOKEN" not in k and "KEY" not in k},
         "payload": _truncate(payload)},
        separators=(",", ":"))
    os.makedirs(LOG_DIR, exist_ok=True)
    fd = os.open(os.path.join(LOG_DIR, "payloads.jsonl"),
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
