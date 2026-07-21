"""Pure attention-spool logic: line codec, aggregation, batching, lifecycle.

No HTTP and no hook parsing here — report.py owns those. Everything in this
module is deterministic and unit-testable without a server or a Claude
session. Stdlib only.

Spool: one JSONL file per Claude session at <dir>/<sess8>.jsonl. A line is
one touch: {"t": epoch, "k": "read"|"edit"|"search", "p": repo-relative path
("" = repo-wide), "l": [start, end] or null, "tool": name, "agent": agent_id
or ""}. Appends are single O_APPEND writes so concurrent hook processes
interleave whole lines, not bytes.
"""
import json
import os
import time

SPOOL_MAX_LINES = 10000
SPOOL_MAX_BYTES = 2 * 1024 * 1024
RANGES_CAP = 20
PATTERNS_CAP = 10
BATCH_MAX = 20          # server GITMAP_EVENTS_BATCH_MAX
POSTS_PER_FLUSH = 5
FLUSH_MIN_INTERVAL = 300        # Stop-triggered flushes at most every 5 min
FLUSH_LINE_THRESHOLD = 500
ORPHAN_FLUSH_AGE = 24 * 3600
ORPHAN_DELETE_AGE = 7 * 24 * 3600
STALE_CLAIM_AGE = 3600

KIND_TO_TYPE = {"read": "attention.read", "edit": "attention.edit",
                "search": "attention.search"}


def spool_path(spool_dir, sess8):
    return os.path.join(spool_dir, "%s.jsonl" % sess8)


def encode_touch(kind, path, lines, tool, agent, pattern=None):
    d = {"t": round(time.time(), 3), "k": kind, "p": path,
         "l": lines, "tool": tool, "agent": agent or ""}
    if pattern:
        d["pattern"] = str(pattern)[:200]
    return json.dumps(d, separators=(",", ":"))


def append_touch(spool_dir, sess8, encoded_line):
    """One O_APPEND write; creates the dir/file on first touch."""
    os.makedirs(spool_dir, exist_ok=True)
    fd = os.open(spool_path(spool_dir, sess8),
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (encoded_line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def decode_lines(text):
    out = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue        # torn/corrupt line: drop it, keep the rest
        if isinstance(d, dict) and d.get("k") in KIND_TO_TYPE:
            out.append(d)
    return out


def merge_ranges(ranges, cap=RANGES_CAP):
    """Sort, coalesce overlapping/adjacent 1-based [a, b] ranges, cap.

    Returns (merged, truncated). Invalid entries are skipped.
    """
    clean = []
    for r in ranges:
        if (isinstance(r, (list, tuple)) and len(r) == 2
                and all(isinstance(v, (int, float)) for v in r)):
            a, b = int(r[0]), int(r[1])
            a = max(a, 1)
            if b >= a:
                clean.append([a, b])
    clean.sort()
    merged = []
    for a, b in clean:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged[:cap], len(merged) > cap


def aggregate(touches, corr):
    """Group decoded touches into attention events, biggest groups first.

    One event per (kind, path). Anchor gets `lines` only when the merged
    ranges coalesce to a single range — a hull over disjoint reads would lie.
    """
    groups = {}
    for t in touches:
        groups.setdefault((t["k"], t["p"]), []).append(t)
    events = []
    for (kind, path), items in groups.items():
        ranges, truncated = merge_ranges(
            [t["l"] for t in items if t.get("l")])
        anchor = {}
        if path:
            anchor["path"] = path
        if len(ranges) == 1 and path:
            anchor["lines"] = list(ranges[0])
        payload = {
            "t0": round(min(t["t"] for t in items), 3),
            "t1": round(max(t["t"] for t in items), 3),
        }
        if ranges:
            payload["ranges"] = ranges
        if truncated:
            payload["ranges_truncated"] = True
        tools = {}
        agents = {}
        patterns = []
        for t in items:
            tools[t["tool"]] = tools.get(t["tool"], 0) + 1
            if t.get("agent"):
                agents[t["agent"]] = agents.get(t["agent"], 0) + 1
            if kind == "search" and t.get("pattern"):
                if t["pattern"] not in patterns:
                    patterns.append(t["pattern"])
        payload["tools"] = tools
        if agents:
            payload["agents"] = agents
        if patterns:
            payload["patterns"] = patterns[:PATTERNS_CAP]
        events.append({"type": KIND_TO_TYPE[kind], "anchor": anchor,
                       "value": len(items), "corr": corr,
                       "payload": payload, "_count": len(items)})
    events.sort(key=lambda e: -e["_count"])
    for e in events:
        del e["_count"]
    return events


def split_batches(events, batch_max=BATCH_MAX, post_max=POSTS_PER_FLUSH):
    """(batches, dropped_events). Events beyond post_max*batch_max don't fit
    this flush; the caller re-spools their raw lines rather than losing them."""
    fit = events[:batch_max * post_max]
    batches = [fit[i:i + batch_max] for i in range(0, len(fit), batch_max)]
    return batches, events[len(fit):]


def claim(path):
    """Atomically claim a spool for flushing. Returns the claim path, or
    None if another flusher won the rename race (or nothing to flush)."""
    claimed = "%s.flushing.%d" % (path, os.getpid())
    try:
        os.rename(path, claimed)
    except OSError:
        return None
    return claimed


def unclaim(claim_path, leftover_lines=None):
    """Release a claim: re-append any unposted raw lines to the live spool
    (server down, or overflow past the per-flush cap), then drop the claim."""
    if leftover_lines:
        spool = claim_path.rsplit(".flushing.", 1)[0]
        fd = os.open(spool, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            body = "".join(json.dumps(ln, separators=(",", ":")) + "\n"
                           for ln in leftover_lines)
            os.write(fd, body.encode("utf-8"))
        finally:
            os.close(fd)
    try:
        os.remove(claim_path)
    except OSError:
        pass


def enforce_bounds(path):
    """Cap the live spool; drop oldest lines beyond the caps."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size <= SPOOL_MAX_BYTES:
        with open(path, "rb") as f:
            n = sum(1 for _ in f)
        if n <= SPOOL_MAX_LINES:
            return False
    with open(path, "rb") as f:
        lines = f.read().splitlines(True)
    kept = lines[-SPOOL_MAX_LINES:]
    while sum(len(ln) for ln in kept) > SPOOL_MAX_BYTES and kept:
        kept = kept[len(kept) // 10 + 1:]
    tmp = path + ".trim"
    with open(tmp, "wb") as f:
        f.writelines(kept)
    os.replace(tmp, path)
    return True


def flush_due(spool_dir, sess8, event_name):
    """Should this hook invocation trigger a flush?"""
    if event_name in ("SubagentStop", "SessionEnd"):
        return True
    path = spool_path(spool_dir, sess8)
    if event_name == "Stop":
        stamp = os.path.join(spool_dir, "%s.last-flush" % sess8)
        try:
            if time.time() - os.stat(stamp).st_mtime < FLUSH_MIN_INTERVAL:
                return False
        except OSError:
            pass
        return os.path.exists(path)
    # any spooling hook: cheap size guard first, count lines only near it
    try:
        size = os.stat(path).st_size
    except OSError:
        return False
    if size < FLUSH_LINE_THRESHOLD * 40:        # well under ~500 avg lines
        return False
    if size > FLUSH_LINE_THRESHOLD * 200:
        return True
    with open(path, "rb") as f:
        return sum(1 for _ in f) >= FLUSH_LINE_THRESHOLD


def touch_stamp(spool_dir, sess8):
    stamp = os.path.join(spool_dir, "%s.last-flush" % sess8)
    with open(stamp, "w"):
        pass
    os.utime(stamp, None)


def sweep(spool_dir, active_sess8):
    """Housekeeping after a flush: recover stale claims, surface orphaned
    spools from dead sessions, hard-delete ancient files.

    Returns sess8 names of orphaned spools the caller should flush too."""
    orphans = []
    now = time.time()
    try:
        names = os.listdir(spool_dir)
    except OSError:
        return orphans
    for name in names:
        full = os.path.join(spool_dir, name)
        try:
            age = now - os.stat(full).st_mtime
        except OSError:
            continue
        if ".flushing." in name:
            if age > STALE_CLAIM_AGE:        # crashed flusher: un-claim
                back = os.path.join(spool_dir,
                                    name.split(".flushing.")[0])
                try:
                    if os.path.exists(back):
                        with open(full, "rb") as src, \
                                open(back, "ab") as dst:
                            dst.write(src.read())
                        os.remove(full)
                    else:
                        os.rename(full, back)
                except OSError:
                    pass
        elif name.endswith(".jsonl"):
            sess = name[:-len(".jsonl")]
            if sess == active_sess8:
                continue
            if age > ORPHAN_DELETE_AGE:
                try:
                    os.remove(full)
                except OSError:
                    pass
            elif age > ORPHAN_FLUSH_AGE:
                orphans.append(sess)
        elif name.endswith(".last-flush") and age > ORPHAN_DELETE_AGE:
            try:
                os.remove(full)
            except OSError:
                pass
    return orphans
