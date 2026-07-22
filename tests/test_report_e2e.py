"""End-to-end: feed hook stdin fixtures (shapes recorded in docs/probes.md)
through report.py as a real subprocess, assert exact requests at the stub
events API and exact spool state on disk."""
import json
import os
import subprocess
import sys
import time

import attention

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts",
                      "report.py")
SESS = "sess1234-full-session-id"


def hook(event, repo, extra=None, agent=None):
    d = {"hook_event_name": event, "session_id": SESS, "cwd": str(repo)}
    if agent:
        d["agent_id"] = agent
        d["agent_type"] = "general-purpose"
    d.update(extra or {})
    return d


def run_hook(payload, timeout=15):
    p = subprocess.run([sys.executable, SCRIPT],
                       input=json.dumps(payload).encode(),
                       capture_output=True, timeout=timeout,
                       env=os.environ.copy())
    assert p.returncode == 0, p.stderr.decode()
    return p


def wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def posted_events(stub, path_suffix="/events"):
    return [e for r in stub.requests
            if r["method"] == "POST" and r["path"].endswith(path_suffix)
            for e in (r["body"] or {}).get("events", [])]


def spool_of(cache):
    return attention.spool_path(str(cache / "attention"), SESS[:8])


def read_fixture(repo, offset=None, limit=None):
    ti = {"file_path": str(repo / "src" / "app.py")}
    if offset is not None:
        ti["offset"] = offset
    if limit is not None:
        ti["limit"] = limit
    return {"tool_name": "Read", "tool_input": ti,
            "tool_response": {"type": "text", "file": {}}}


class TestSpooling:
    def test_read_spools_no_fork_no_network(self, env, repo, stub_server):
        run_hook(hook("PostToolUse", repo, read_fixture(repo, 5, 8)))
        out = attention.decode_lines(open(spool_of(env)).read())
        assert out[0] == {"t": out[0]["t"], "k": "read", "p": "src/app.py",
                          "l": [5, 12], "tool": "Read", "agent": ""}
        time.sleep(0.4)
        assert stub_server.requests == []        # spool-only: zero network
        assert not [f for f in os.listdir(env / "attention")
                    if f.startswith("job.")]

    def test_whole_file_read_and_subagent_attribution(self, env, repo,
                                                      stub_server):
        run_hook(hook("PostToolUse", repo, read_fixture(repo),
                      agent="abcd1234efgh"))
        out = attention.decode_lines(open(spool_of(env)).read())
        assert out[0]["l"] is None
        assert out[0]["agent"] == "abcd1234efgh"

    def test_grep_spools_root_and_pattern(self, env, repo, stub_server):
        run_hook(hook("PostToolUse", repo, {
            "tool_name": "Grep",
            "tool_input": {"pattern": "def ", "path": str(repo / "src")},
            "tool_response": {"numFiles": 1}}))
        out = attention.decode_lines(open(spool_of(env)).read())
        assert out[0]["k"] == "search" and out[0]["p"] == "src"
        assert out[0]["pattern"] == "def "

    def test_edit_spools_structured_patch_ranges(self, env, repo,
                                                 stub_server):
        run_hook(hook("PostToolUse", repo, {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(repo / "src" / "app.py"),
                           "old_string": "x", "new_string": "y"},
            "tool_response": {"filePath": str(repo / "src" / "app.py"),
                              "structuredPatch": [
                                  {"oldStart": 3, "oldLines": 4,
                                   "newStart": 3, "newLines": 5,
                                   "lines": []}]}}))
        out = attention.decode_lines(open(spool_of(env)).read())
        assert out[0]["k"] == "edit" and out[0]["l"] == [3, 6]

    def test_outside_repo_path_skipped(self, env, repo, stub_server,
                                       tmp_path):
        run_hook(hook("PostToolUse", repo, {
            "tool_name": "Read",
            "tool_input": {"file_path": str(tmp_path / "elsewhere.txt")},
            "tool_response": {}}))
        assert not os.path.exists(spool_of(env))


class TestFlush:
    def seed(self, env, repo):
        run_hook(hook("PostToolUse", repo, read_fixture(repo, 5, 8)))
        run_hook(hook("PostToolUse", repo, read_fixture(repo, 13, 8)))

    def test_stop_flushes_rollup_and_presence(self, env, repo, stub_server):
        self.seed(env, repo)
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: len(posted_events(stub_server)) >= 2)
        evs = posted_events(stub_server)
        att = [e for e in evs if e["type"] == "attention.read"]
        pres = [e for e in evs if e["type"] == "presence.working"]
        assert att[0]["value"] == 2
        assert att[0]["anchor"] == {"path": "src/app.py",
                                    "lines": [5, 20]}   # coalesced
        assert att[0]["payload"]["ranges"] == [[5, 20]]
        assert att[0]["corr"] == SESS[:120]
        assert att[0]["actor"] == "cc-" + SESS[:8]
        assert pres and pres[0]["ttl"] == 120
        resolves = [r for r in stub_server.requests
                    if r["path"].startswith("/api/resolve-repo")]
        assert len(resolves) == 1
        assert wait_for(lambda: not os.path.exists(spool_of(env)))

    def test_stop_throttled_within_window(self, env, repo, stub_server):
        self.seed(env, repo)
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: len(posted_events(stub_server)) >= 2)
        n = len(stub_server.requests)
        self.seed(env, repo)
        run_hook(hook("Stop", repo))         # throttled: presence only
        assert wait_for(lambda: len(stub_server.requests) > n)
        time.sleep(0.5)
        assert not any(e["type"].startswith("attention.")
                       for e in posted_events(stub_server)[2:])
        assert os.path.exists(spool_of(env))  # touches retained

    def test_session_end_final_flush_deletes_spool(self, env, repo,
                                                   stub_server):
        self.seed(env, repo)
        run_hook(hook("SessionEnd", repo, {"reason": "exit"}))
        assert wait_for(lambda: any(
            e["type"] == "attention.read"
            for e in posted_events(stub_server)))
        assert wait_for(lambda: not os.path.exists(spool_of(env)))
        assert wait_for(lambda: not os.path.exists(
            spool_of(env) + ".last-flush"))

    def test_server_down_retains_spool_then_drains(self, env, repo,
                                                   stub_server,
                                                   monkeypatch):
        self.seed(env, repo)
        monkeypatch.setenv("GITMAP_SERVER", "http://127.0.0.1:1")
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: os.path.exists(spool_of(env)))
        time.sleep(0.5)
        assert os.path.exists(spool_of(env))
        monkeypatch.setenv(
            "GITMAP_SERVER",
            "http://127.0.0.1:%d" % stub_server.server_address[1])
        stamp = env / "attention" / ("%s.last-flush" % SESS[:8])
        if stamp.exists():          # failed flush leaves no stamp
            stamp.unlink()
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: any(
            e["type"] == "attention.read"
            for e in posted_events(stub_server)))

    def test_auth_rejection_drops_batch_no_retry(self, env, repo,
                                                 stub_server):
        stub_server.post_status = 402
        self.seed(env, repo)
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: not os.path.exists(spool_of(env)))
        n_posts = len([r for r in stub_server.requests
                       if r["method"] == "POST"])
        self.seed(env, repo)                 # negative-cached now
        run_hook(hook("Stop", repo))
        time.sleep(0.6)
        assert len([r for r in stub_server.requests
                    if r["method"] == "POST"]) == n_posts

    def test_flush_capped_at_five_posts(self, env, repo, stub_server):
        spool_dir = str(env / "attention")
        for i in range(130):
            attention.append_touch(spool_dir, SESS[:8],
                                   attention.encode_touch(
                                       "read", "f%03d.py" % i, None,
                                       "Read", ""))
        run_hook(hook("SessionEnd", repo, {"reason": "exit"}))
        assert wait_for(lambda: len(posted_events(stub_server)) >= 100)
        time.sleep(0.5)
        posts = [r for r in stub_server.requests
                 if r["method"] == "POST" and r["path"].endswith("/events")]
        assert len(posts) == attention.POSTS_PER_FLUSH
        assert all(len(r["body"]["events"]) <= attention.BATCH_MAX
                   for r in posts)


class TestPresenceActors:
    def test_session_start_registers_actor_with_meta(self, env, repo,
                                                     stub_server):
        run_hook(hook("SessionStart", repo, {"source": "startup"}))
        assert wait_for(lambda: any(
            r["path"].endswith("/actors") for r in stub_server.requests))
        actor = [r for r in stub_server.requests
                 if r["path"].endswith("/actors")][0]["body"]
        assert actor["id"] == "cc-" + SESS[:8]
        assert actor["kind"] == "agent"
        assert actor["meta"]["harness"] == "claude-code"
        assert actor["meta"]["branch"] == "work"
        assert actor["meta"]["worktree"] == "wt"

    def test_subagent_lifecycle(self, env, repo, stub_server):
        run_hook(hook("SubagentStart", repo, agent="abcd1234efgh"))
        assert wait_for(lambda: any(
            r["path"].endswith("/actors") for r in stub_server.requests))
        actor = [r for r in stub_server.requests
                 if r["path"].endswith("/actors")][0]["body"]
        assert actor["id"] == "cc-%s-sub-abcd1234" % SESS[:8]
        assert actor["parent"] == "cc-" + SESS[:8]
        run_hook(hook("SubagentStop", repo, agent="abcd1234efgh"))
        assert wait_for(lambda: any(
            e["ttl"] == 0 and e["actor"].endswith("-sub-abcd1234")
            for e in posted_events(stub_server)))

    def test_unmapped_repo_negative_cache_stops_spooling(self, env, repo,
                                                         stub_server):
        stub_server.map_name = None
        run_hook(hook("Stop", repo))         # forces one resolve
        assert wait_for(lambda: any(
            r["path"].startswith("/api/resolve-repo")
            for r in stub_server.requests))
        assert wait_for(lambda: (env / "resolve.json").exists())
        run_hook(hook("PostToolUse", repo, read_fixture(repo)))
        assert not os.path.exists(spool_of(env))


class TestTiming:
    def test_parent_fast_even_with_server_down(self, env, repo,
                                               monkeypatch):
        monkeypatch.setenv("GITMAP_SERVER", "http://127.0.0.1:1")
        run_hook(hook("PostToolUse", repo, read_fixture(repo)))  # warm git
        t0 = time.time()
        run_hook(hook("Stop", repo))
        assert time.time() - t0 < 1.0

    def test_unknown_event_and_garbage_exit_zero(self, env, repo):
        run_hook(hook("SomeFutureEvent", repo))
        p = subprocess.run([sys.executable, SCRIPT], input=b"not json{",
                           capture_output=True, timeout=10,
                           env=os.environ.copy())
        assert p.returncode == 0

    def test_no_server_configured_is_inert(self, repo, monkeypatch,
                                           tmp_path):
        monkeypatch.setenv("GITMAP_HOOK_CACHE", str(tmp_path / "c2"))
        monkeypatch.delenv("GITMAP_SERVER", raising=False)
        monkeypatch.delenv("GITMAP_TOKEN", raising=False)
        run_hook(hook("PostToolUse", repo, read_fixture(repo)))
        assert not (tmp_path / "c2" / "attention").exists()
