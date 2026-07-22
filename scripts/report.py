#!/usr/bin/env python3
"""gitmap agent reporter: Claude Code hook entrypoint.

Parent mode (no argv flag): read the hook payload from stdin, append an
attention touch and/or decide presence, then — only when there is network
work — write a job file and spawn a detached child (`--child <jobfile>`)
before exiting 0. The parent never touches the network; hook latency stays
in single-digit milliseconds for spool-only events.

Child mode (--child): owns all HTTP. Resolves the repo to a map, registers
actors, posts presence and flushed attention batches. Short timeouts,
swallows every error; the Claude session must never feel the server.

Config: userConfig via CLAUDE_PLUGIN_OPTION_SERVER / _TOKEN, overridable by
GITMAP_SERVER / GITMAP_TOKEN. Optional GITMAP_MAP skips repo resolution
(escape hatch until /api/resolve-repo is deployed, and handy in tests).
Optional GITMAP_NAME / CLAUDE_PLUGIN_OPTION_NAME overrides the contributor
display name otherwise taken from git config user.name.
Debug: GITMAP_HOOK_DEBUG=1 logs to ~/.cache/gitmap/agent-hook.log.

Field contract: docs/probes.md (recorded from live payloads, 2026-07-21).
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import attention  # noqa: E402

CACHE_DIR = os.path.expanduser(os.environ.get("GITMAP_HOOK_CACHE",
                                              "~/.cache/gitmap"))
SPOOL_DIR = os.path.join(CACHE_DIR, "attention")
RESOLVE_TTL_OK = 3600
RESOLVE_TTL_NEG = 900
CONNECT_TIMEOUT = 3.0
PRESENCE_TTL = 300
PRESENCE_TTL_STOP = 120

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SEARCH_TOOLS = ("Grep", "Glob")


def dlog(msg):
    if os.environ.get("GITMAP_HOOK_DEBUG") != "1":
        return
    try:
        with open(os.path.join(CACHE_DIR, "agent-hook.log"), "a") as f:
            f.write("%.3f %d %s\n" % (time.time(), os.getpid(), msg))
    except OSError:
        pass


def config():
    server = (os.environ.get("GITMAP_SERVER")
              or os.environ.get("CLAUDE_PLUGIN_OPTION_SERVER") or "")
    token = (os.environ.get("GITMAP_TOKEN")
             or os.environ.get("CLAUDE_PLUGIN_OPTION_TOKEN") or "")
    return server.rstrip("/"), token


# ---------------------------------------------------------------- git ctx

def _git(cwd, *args):
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             timeout=3, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_ctx(cwd):
    """Cached per-cwd: {toplevel, branch, origin, git_name, git_email}.
    Branch is refreshed when stale (>60s) since it changes mid-session;
    toplevel/origin/identity don't."""
    cache_file = os.path.join(CACHE_DIR, "gitctx.json")
    try:
        cache = json.load(open(cache_file))
    except (OSError, ValueError):
        cache = {}
    ent = cache.get(cwd)
    now = time.time()
    if not ent or now - ent.get("at", 0) > 60:
        toplevel = (ent or {}).get("toplevel") or _git(
            cwd, "rev-parse", "--show-toplevel")
        if not toplevel:
            cache[cwd] = {"toplevel": "", "at": now}
            _write_json(cache_file, cache)
            return cache[cwd]
        ent = {
            "toplevel": toplevel,
            "origin": (ent or {}).get("origin") or _git(
                cwd, "remote", "get-url", "origin"),
            "branch": _git(cwd, "branch", "--show-current"),
            "git_name": (ent or {}).get("git_name") or _git(
                cwd, "config", "user.name"),
            "git_email": (ent or {}).get("git_email") or _git(
                cwd, "config", "user.email"),
            "at": now,
        }
        cache[cwd] = ent
        _write_json(cache_file, cache)
    return ent


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except OSError:
        pass


def rel_path(file_path, toplevel):
    """Repo-relative path, or None when outside the repo (skip rule)."""
    if not file_path or not toplevel:
        return None
    fp = os.path.abspath(file_path)
    top = os.path.abspath(toplevel)
    if fp == top:
        return ""
    if not fp.startswith(top + os.sep):
        return None
    return fp[len(top) + 1:].replace(os.sep, "/")


# ------------------------------------------------------------- resolution

def resolve_map(server, token, origin):
    """origin url -> map name via cache, then GET /api/resolve-repo.
    Returns map name, "" (known-negative), or None (can't resolve now)."""
    if os.environ.get("GITMAP_MAP"):
        return os.environ["GITMAP_MAP"]
    if not origin:
        return None
    cache_file = os.path.join(CACHE_DIR, "resolve.json")
    try:
        cache = json.load(open(cache_file))
    except (OSError, ValueError):
        cache = {}
    ent = cache.get(origin)
    now = time.time()
    if ent:
        ttl = RESOLVE_TTL_OK if ent.get("map") else RESOLVE_TTL_NEG
        if now - ent.get("at", 0) < ttl:
            return ent.get("map") or ""
    body = http(server, token, "GET",
                "/api/resolve-repo?url=" + urllib.parse.quote(origin, ""))
    if body is None:
        return None                    # network trouble: don't cache
    mapname = body.get("map") or ""
    cache[origin] = {"map": mapname, "at": now}
    _write_json(cache_file, cache)
    return mapname


def cached_negative(origin):
    """Parent-side fast check: is this repo known-unmapped? (skip spooling)"""
    try:
        ent = json.load(open(os.path.join(CACHE_DIR, "resolve.json"))
                        ).get(origin)
    except (OSError, ValueError, AttributeError):
        return False
    return bool(ent) and not ent.get("map") and (
        time.time() - ent.get("at", 0) < RESOLVE_TTL_NEG)


# ------------------------------------------------------------------ http

def http(server, token, method, path, payload=None):
    """One request. dict on 2xx, {} on empty 2xx, None on any failure.
    401/402/403 negative-cache the whole config (auth_bad stamp)."""
    if not server:
        return None
    if auth_bad():
        return None
    req = urllib.request.Request(
        server + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token} if token else
                {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
            body = resp.read()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        dlog("HTTP %s %s -> %d" % (method, path, e.code))
        if e.code in (401, 402, 403):
            _write_json(os.path.join(CACHE_DIR, "auth-bad.json"),
                        {"at": time.time(), "code": e.code})
        return None
    except Exception as e:  # noqa: BLE001 — fire-and-forget by design
        dlog("HTTP %s %s -> %r" % (method, path, e))
        return None


def auth_bad():
    try:
        ent = json.load(open(os.path.join(CACHE_DIR, "auth-bad.json")))
        return time.time() - ent.get("at", 0) < RESOLVE_TTL_NEG
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------ hook parent

def sess8(payload):
    return (payload.get("session_id") or "unknown")[:8]


def actor_id(payload):
    s = "cc-" + sess8(payload)
    if payload.get("agent_id"):
        s += "-sub-" + payload["agent_id"][:8]
    return s


def spool_touch(payload, ctx):
    name = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    agent = payload.get("agent_id") or ""
    if name == "Read":
        p = rel_path(ti.get("file_path"), ctx["toplevel"])
        if p is None:
            return
        lines = None
        if isinstance(ti.get("offset"), int):
            start = max(ti["offset"], 1)
            limit = ti.get("limit") if isinstance(ti.get("limit"), int) \
                else 2000
            lines = [start, start + max(limit, 1) - 1]
        attention.append_touch(SPOOL_DIR, sess8(payload),
                               attention.encode_touch(
                                   "read", p, lines, name, agent))
    elif name in SEARCH_TOOLS:
        root = ti.get("path") or payload.get("cwd") or ""
        p = rel_path(root, ctx["toplevel"])
        if p is None:
            return
        attention.append_touch(SPOOL_DIR, sess8(payload),
                               attention.encode_touch(
                                   "search", p, None, name, agent,
                                   pattern=ti.get("pattern")))
    elif name in EDIT_TOOLS:
        p = rel_path(ti.get("file_path") or ti.get("notebook_path"),
                     ctx["toplevel"])
        if p is None:
            return
        lines = None
        patch = (payload.get("tool_response") or {}) if isinstance(
            payload.get("tool_response"), dict) else {}
        hunks = patch.get("structuredPatch")
        if isinstance(hunks, list):
            for h in hunks:
                try:
                    a = int(h["oldStart"])
                    b = a + max(int(h["oldLines"]) - 1, 0)
                except (KeyError, TypeError, ValueError):
                    continue
                attention.append_touch(SPOOL_DIR, sess8(payload),
                                       attention.encode_touch(
                                           "edit", p, [a, b], name, agent))
            if hunks:
                return
        attention.append_touch(SPOOL_DIR, sess8(payload),
                               attention.encode_touch(
                                   "edit", p, lines, name, agent))


def contributor_name(ctx):
    """Display name for the human behind this session; config beats git."""
    return (os.environ.get("GITMAP_NAME")
            or os.environ.get("CLAUDE_PLUGIN_OPTION_NAME")
            or ctx.get("git_name", ""))


def hostname():
    try:
        return socket.gethostname()
    except OSError:
        return ""


def presence_job(payload, ctx, event):
    """Build the presence part of a child job, or None."""
    aid = actor_id(payload)
    base = {"actor": aid, "session": sess8(payload),
            "corr": (payload.get("session_id") or "")[:120]}
    worktree = os.path.basename(ctx["toplevel"]) if ctx["toplevel"] else ""
    if event == "SessionStart":
        return dict(base, kind="start", ttl=PRESENCE_TTL, path="",
                    vstr="session started",
                    actor_name=worktree or "claude-code",
                    meta={"harness": "claude-code",
                          "branch": ctx.get("branch", ""),
                          "worktree": worktree,
                          "git_name": contributor_name(ctx),
                          "git_email": ctx.get("git_email", ""),
                          "host": hostname(),
                          "source": payload.get("source") or ""})
    if event == "SubagentStart":
        return dict(base, kind="start", ttl=PRESENCE_TTL, path="",
                    vstr="subagent: " + (payload.get("agent_type") or "?"),
                    actor_name=payload.get("agent_type") or "subagent",
                    parent="cc-" + sess8(payload),
                    meta={"harness": "claude-code",
                          "subagent_type": payload.get("agent_type") or "",
                          "host": hostname()})
    if event == "PostToolUse":
        ti = payload.get("tool_input") or {}
        p = rel_path(ti.get("file_path") or ti.get("notebook_path"),
                     ctx["toplevel"])
        if p is None:
            return None
        return dict(base, kind="beat", ttl=PRESENCE_TTL, path=p,
                    vstr="editing " + p)
    if event == "Stop":
        return dict(base, kind="beat", ttl=PRESENCE_TTL_STOP, path="",
                    vstr="idle")
    if event == "SessionEnd":
        reason = payload.get("reason") or ""
        return dict(base, kind="clear", ttl=0, path="",
                    vstr=("ended: " + reason) if reason else "")
    if event == "SubagentStop":
        return dict(base, kind="clear", ttl=0, path="", vstr="")
    return None


def spawn_child(job):
    """Detached fire-and-forget child; parent returns immediately."""
    os.makedirs(SPOOL_DIR, exist_ok=True)
    jobfile = os.path.join(SPOOL_DIR, "job.%d.%d.json"
                           % (os.getpid(), int(time.time() * 1000)))
    with open(jobfile, "w") as f:
        json.dump(job, f)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--child", jobfile],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
        env=os.environ.copy())


def parent():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return
    event = payload.get("hook_event_name") or ""
    server, token = config()
    if not server:
        return
    cwd = payload.get("cwd") or os.getcwd()
    ctx = git_ctx(cwd)
    if not ctx.get("toplevel"):
        return                                   # not a git repo: skip
    if not os.environ.get("GITMAP_MAP") and cached_negative(
            ctx.get("origin", "")):
        return                                   # known-unmapped repo
    s8 = sess8(payload)

    if event == "PostToolUse":
        spool_touch(payload, ctx)

    pres = presence_job(payload, ctx, event) \
        if event != "PostToolUse" or \
        (payload.get("tool_name") in EDIT_TOOLS) else None
    do_flush = attention.flush_due(SPOOL_DIR, s8, event)
    if not pres and not do_flush:
        return                                   # spool-only: no fork
    spawn_child({"origin": ctx.get("origin", ""), "sess8": s8,
                 "corr": (payload.get("session_id") or "")[:120],
                 "event": event, "presence": pres,
                 "flush": bool(do_flush),
                 "final": event == "SessionEnd"})


# ------------------------------------------------------------- hook child

def child(jobfile):
    try:
        job = json.load(open(jobfile))
    except (OSError, ValueError):
        return
    finally:
        try:
            os.remove(jobfile)
        except OSError:
            pass
    server, token = config()
    mapname = resolve_map(server, token, job.get("origin", ""))
    s8 = job.get("sess8", "unknown")
    spool = attention.spool_path(SPOOL_DIR, s8)
    if not mapname:
        if mapname == "" and job.get("final"):
            for p in (spool, spool + ".last-flush"):
                try:
                    os.remove(p)
                except OSError:
                    pass
        dlog("no map for %s" % job.get("origin", ""))
        return
    base = "/maps/%s" % mapname

    pres = job.get("presence")
    events = []
    if pres:
        if pres.get("kind") == "start":
            actor = {"id": pres["actor"], "name": pres.get("actor_name", ""),
                     "kind": "agent", "emoji": "🤖"}
            if pres.get("parent"):
                actor["parent"] = pres["parent"]
            if pres.get("meta"):
                actor["meta"] = pres["meta"]
            if http(server, token, "POST", base + "/actors", actor) is None:
                # pre-D2 server may 400 on parent/meta: retry bare once
                http(server, token, "POST", base + "/actors",
                     {k: actor[k] for k in ("id", "name", "kind", "emoji")})
        events.append({"type": "presence.working", "actor": pres["actor"],
                       "anchor": {"path": pres["path"]} if pres["path"]
                       else {}, "value": pres.get("vstr", ""),
                       "corr": pres.get("corr", ""), "ttl": pres["ttl"]})

    leftover = []
    claim_path = None
    agg = []
    touches = []
    if job.get("flush"):
        claim_path = attention.claim(spool)
    if claim_path:
        try:
            with open(claim_path) as f:
                touches = attention.decode_lines(f.read())
        except OSError:
            touches = []
        agg = attention.aggregate(touches,
                                  corr=job.get("corr") or s8)
        for e in agg:
            e["actor"] = "cc-" + s8

    # presence goes first, then attention rollups biggest-first; one budget
    # (≤ POSTS_PER_FLUSH requests) covers both so a fat flush can't stack
    # an extra presence request on top.
    batches, dropped = attention.split_batches(events + agg)
    if dropped:
        dropped_keys = {(e["type"], e["anchor"].get("path", ""))
                        for e in dropped}
        leftover = [t for t in touches
                    if (attention.KIND_TO_TYPE[t["k"]], t["p"])
                    in dropped_keys]
        dlog("flush overflow: %d events re-spooled" % len(dropped))
    ok = True
    for body in batches:
        if http(server, token, "POST", base + "/events",
                {"events": body}) is None:
            ok = False
            break

    if claim_path:
        if ok:
            attention.unclaim(claim_path, leftover)
            attention.touch_stamp(SPOOL_DIR, s8)
        else:
            try:
                with open(claim_path) as f:
                    all_lines = attention.decode_lines(f.read())
            except OSError:
                all_lines = []
            if auth_bad():
                attention.unclaim(claim_path, None)   # rejected: drop
                dlog("flush dropped (auth/plan)")
            else:
                attention.unclaim(claim_path, all_lines)  # retry later

    if job.get("final"):
        for p in (spool, spool + ".last-flush"):
            try:
                os.remove(p)
            except OSError:
                pass
    else:
        attention.enforce_bounds(spool)
        for orphan in attention.sweep(SPOOL_DIR, s8):
            oc = attention.claim(attention.spool_path(SPOOL_DIR, orphan))
            if not oc:
                continue
            with open(oc) as f:
                agg = attention.aggregate(attention.decode_lines(f.read()),
                                          corr=orphan)
            batches, _ = attention.split_batches(agg)
            o_ok = all(http(server, token, "POST", base + "/events",
                            {"events": b}) is not None for b in batches)
            attention.unclaim(oc, None)
            if o_ok:
                try:
                    os.remove(attention.spool_path(SPOOL_DIR, orphan))
                except OSError:
                    pass


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        parent()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — never break the Claude session
        dlog("fatal: %r" % e)
    sys.exit(0)
