#!/usr/bin/env python3
"""Post a semantic attention.focus note to the map ("working on X because
Y"). Used by the attention-focus skill; safe to run by hand.

Usage: focus.py --note "refactoring the flush path to be claim-safe" \
                [--path src/flush.py]

attention.focus is a per-map custom event type (not built-in), so the first
post on a map registers it; registration failures for "already exists" are
fine. Unlike the hook reporter this runs in the foreground and prints what
happened — it's an explicit user/agent action, not a hot path.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report  # noqa: E402

TYPE_DEF = {"name": "attention.focus", "kind": "durable",
            "value_type": "string",
            "descr": "an agent's stated focus: what it is working on at the "
                     "anchor and why (the value is the note text)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--note", required=True,
                    help="what you're working on and why")
    ap.add_argument("--path", default="",
                    help="repo-relative file/dir the focus is on")
    args = ap.parse_args()

    server, token = report.config()
    if not server or not token:
        print("gitmap not configured (server/token); note not posted")
        return
    ctx = report.git_ctx(os.getcwd())
    if not ctx.get("toplevel"):
        print("not inside a git repository; note not posted")
        return
    mapname = report.resolve_map(server, token, ctx.get("origin", ""))
    if not mapname:
        print("no gitmap map for this repo (or server unreachable); "
              "note not posted")
        return
    base = "/maps/%s" % mapname
    report.http(server, token, "POST", base + "/event-types", TYPE_DEF)
    sess = os.environ.get("CLAUDE_SESSION_ID", "")[:8] or "manual"
    ev = {"type": "attention.focus", "actor": "cc-" + sess,
          "anchor": {"path": args.path} if args.path else {},
          "value": args.note[:500], "corr": sess}
    if report.http(server, token, "POST", base + "/events",
                   {"events": [ev]}) is None:
        print("post failed (server/auth); note not recorded")
    else:
        where = args.path or "the whole map"
        print("focus note recorded on %s (map '%s')" % (where, mapname))


if __name__ == "__main__":
    main()
    sys.exit(0)
