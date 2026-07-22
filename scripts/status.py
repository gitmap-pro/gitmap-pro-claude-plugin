#!/usr/bin/env python3
"""Human-readable reporter diagnostics for the /gitmap-status command.
Read-only except for one live resolve probe; always exits 0."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report  # noqa: E402


def age(ts):
    d = time.time() - ts
    if d < 120:
        return "%ds ago" % d
    if d < 7200:
        return "%dm ago" % (d / 60)
    return "%.1fh ago" % (d / 3600)


def main():
    server, token = report.config()
    print("gitmap reporter status")
    print("  server: %s" % (server or "NOT CONFIGURED"))
    print("  token:  %s" % ("set (%s...)" % token[:6] if token
                            else "NOT CONFIGURED"))
    if os.environ.get("GITMAP_MAP"):
        print("  map override: %s" % os.environ["GITMAP_MAP"])

    cwd = os.getcwd()
    ctx = report.git_ctx(cwd)
    if not ctx.get("toplevel"):
        print("  repo: cwd is not a git repository — reporter is inert here")
    else:
        print("  repo: %s (branch %s)" % (ctx["toplevel"],
                                          ctx.get("branch") or "?"))
        print("  origin: %s" % (ctx.get("origin") or "none"))

    if report.auth_bad():
        print("  auth: NEGATIVE-CACHED (recent 401/402/403 — check token "
              "scope/plan; clears in <15 min)")

    try:
        cache = json.load(open(os.path.join(report.CACHE_DIR,
                                            "resolve.json")))
    except (OSError, ValueError):
        cache = {}
    ent = cache.get(ctx.get("origin", ""))
    if ent:
        print("  resolve cache: map=%s (%s)"
              % (ent.get("map") or "<none>", age(ent.get("at", 0))))

    if server and ctx.get("origin"):
        m = report.resolve_map(server, token, ctx["origin"])
        if m is None:
            print("  server probe: UNREACHABLE (or auth rejected)")
        elif m == "":
            print("  server probe: reachable, but no map for this repo")
        else:
            print("  server probe: ok — map '%s'" % m)

    spool_dir = report.SPOOL_DIR
    try:
        names = sorted(n for n in os.listdir(spool_dir)
                       if n.endswith(".jsonl") or ".flushing." in n)
    except OSError:
        names = []
    if not names:
        print("  spool: empty")
    for n in names:
        full = os.path.join(spool_dir, n)
        try:
            st = os.stat(full)
            with open(full, "rb") as f:
                lines = sum(1 for _ in f)
        except OSError:
            continue
        flag = " [STALE CLAIM]" if ".flushing." in n else ""
        print("  spool %s: %d touches, %db, updated %s%s"
              % (n, lines, st.st_size, age(st.st_mtime), flag))
    for n in (os.listdir(spool_dir) if os.path.isdir(spool_dir) else []):
        if n.endswith(".last-flush"):
            print("  last flush (%s): %s"
                  % (n.split(".")[0],
                     age(os.stat(os.path.join(spool_dir, n)).st_mtime)))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("status error: %r" % e)
    sys.exit(0)
