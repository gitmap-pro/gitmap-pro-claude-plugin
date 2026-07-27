"""Model resolution, meta re-registration, and presence payload.branch.

The transcript shapes here are the ones recorded in docs/probes.md (re-probed
2026-07-27): hook payloads carry `transcript_path` but no model, and the
transcript's assistant lines carry `message.model`.
"""
import json
import os
import subprocess
import time

import pytest
import report

from test_report_e2e import SESS, hook, posted_events, run_hook, wait_for


def assistant(model, sidechain=False, text="ok"):
    d = {"type": "assistant", "uuid": "u", "sessionId": SESS,
         "message": {"id": "msg_1", "role": "assistant", "model": model,
                     "type": "message", "content": [{"type": "text",
                                                     "text": text}]}}
    if sidechain:
        d["isSidechain"] = True
    return json.dumps(d)


def transcript(tmp_path, *lines, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("".join(ln + "\n" for ln in lines))
    return str(p)


def actor_posts(stub):
    return [r["body"] for r in stub.requests
            if r["method"] == "POST" and r["path"].endswith("/actors")]


def expire_git_ctx(env):
    """Force the next hook to re-read the branch (normally cached 60s)."""
    f = env / "gitctx.json"
    cache = json.load(open(f))
    for ent in cache.values():
        ent["at"] = 0
    f.write_text(json.dumps(cache))


def expire_model(env):
    try:
        os.remove(env / ("model.%s.json" % SESS[:8]))
    except OSError:
        pass


class TestTranscriptModel:
    def test_newest_assistant_model_wins(self, tmp_path):
        p = transcript(tmp_path, assistant("claude-opus-5"),
                       json.dumps({"type": "user", "message": {}}),
                       assistant("claude-sonnet-5"))
        assert report.transcript_model(p) == "claude-sonnet-5"

    def test_sidechain_lines_ignored(self, tmp_path):
        p = transcript(tmp_path, assistant("claude-fable-5"),
                       assistant("claude-haiku-4-5", sidechain=True))
        assert report.transcript_model(p) == "claude-fable-5"

    def test_torn_first_line_and_garbage_skipped(self, tmp_path):
        # a mid-record seek tears the first line in the window; also assert
        # non-JSON noise doesn't abort the scan
        p = tmp_path / "t.jsonl"
        p.write_text('e":"assistant"}\nnot json{\n' +
                     assistant("claude-sonnet-5") + "\n")
        assert report.transcript_model(str(p)) == "claude-sonnet-5"

    def test_window_widens_past_a_fat_tool_result(self, tmp_path):
        fat = json.dumps({"type": "user", "message": {"content": "x" * 90000}})
        p = transcript(tmp_path, assistant("claude-opus-5"), fat)
        assert report.transcript_model(p, window=1024) == "claude-opus-5"

    def test_missing_or_modelless_transcript_is_empty(self, tmp_path):
        assert report.transcript_model("") == ""
        assert report.transcript_model(str(tmp_path / "nope.jsonl")) == ""
        assert report.transcript_model(transcript(tmp_path, "{}")) == ""

    def test_model_value_capped(self, tmp_path):
        p = transcript(tmp_path, assistant("m" * 500))
        assert len(report.transcript_model(p)) == report.IDENT_MAX


class TestResolveModel:
    @pytest.fixture(autouse=True)
    def _cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(report, "CACHE_DIR", str(tmp_path / "c"))
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    def test_layering_hint_beats_env_beats_transcript(self, tmp_path,
                                                      monkeypatch):
        t = transcript(tmp_path, assistant("from-transcript"))
        monkeypatch.setenv("ANTHROPIC_MODEL", "from-env")
        assert report.resolve_model("s1", "from-hook", t) == "from-hook"
        assert report.resolve_model("s2", "", t) == "from-env"
        monkeypatch.delenv("ANTHROPIC_MODEL")
        assert report.resolve_model("s3", "", t) == "from-transcript"

    def test_cached_within_ttl_then_refreshed(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text(assistant("claude-opus-5") + "\n")
        assert report.resolve_model("s1", "", str(t)) == "claude-opus-5"
        t.write_text(assistant("claude-sonnet-5") + "\n")
        assert report.resolve_model("s1", "", str(t)) == "claude-opus-5"
        cache = os.path.join(report.CACHE_DIR, "model.s1.json")
        ent = json.load(open(cache))
        ent["at"] = time.time() - report.MODEL_TTL - 1
        open(cache, "w").write(json.dumps(ent))
        assert report.resolve_model("s1", "", str(t)) == "claude-sonnet-5"

    def test_unresolved_is_negative_cached_briefly(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("")
        assert report.resolve_model("s1", "", str(t)) == ""
        t.write_text(assistant("claude-opus-5") + "\n")
        assert report.resolve_model("s1", "", str(t)) == ""   # still negative
        cache = os.path.join(report.CACHE_DIR, "model.s1.json")
        ent = json.load(open(cache))
        ent["at"] = time.time() - report.MODEL_TTL_NEG - 1
        open(cache, "w").write(json.dumps(ent))
        assert report.resolve_model("s1", "", str(t)) == "claude-opus-5"


class TestPresenceBranch:
    def test_every_presence_event_carries_branch(self, env, repo,
                                                 stub_server):
        run_hook(hook("SessionStart", repo, {"source": "startup"}))
        run_hook(hook("PostToolUse", repo, {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(repo / "src" / "app.py")},
            "tool_response": {}}))
        run_hook(hook("Stop", repo))
        run_hook(hook("SessionEnd", repo, {"reason": "exit"}))
        assert wait_for(lambda: len([
            e for e in posted_events(stub_server)
            if e["type"] == "presence.working"]) >= 4)
        pres = [e for e in posted_events(stub_server)
                if e["type"] == "presence.working"]
        assert all(e.get("payload") == {"branch": "work"} for e in pres), pres
        assert {"session started", "editing src/app.py", "idle"} <= {
            e["value"] for e in pres}

    def test_branch_switch_shows_up_on_the_next_beat(self, env, repo,
                                                     stub_server):
        run_hook(hook("SessionStart", repo, {"source": "startup"}))
        assert wait_for(lambda: posted_events(stub_server))
        subprocess.run(["git", "checkout", "-q", "-b", "feature/x"],
                       cwd=repo, check=True)
        expire_git_ctx(env)
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: any(
            e.get("payload") == {"branch": "feature/x"}
            for e in posted_events(stub_server)))


class TestModelInMeta:
    def test_session_start_meta_carries_model(self, env, repo, stub_server,
                                              tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        t = transcript(tmp_path, assistant("claude-opus-5"))
        run_hook(hook("SessionStart", repo, {"source": "startup",
                                             "transcript_path": t}))
        assert wait_for(lambda: actor_posts(stub_server))
        meta = actor_posts(stub_server)[0]["meta"]
        assert meta["model"] == "claude-opus-5"
        assert meta["harness"] == "claude-code"
        assert meta["branch"] == "work"

    def test_subagent_meta_carries_harness_and_model(self, env, repo,
                                                     stub_server, tmp_path,
                                                     monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        t = transcript(tmp_path, assistant("claude-sonnet-5"))
        run_hook(hook("SubagentStart", repo, {"transcript_path": t},
                      agent="abcd1234efgh"))
        assert wait_for(lambda: any(
            b["id"].endswith("-sub-abcd1234") for b in actor_posts(
                stub_server)))
        sub = [b for b in actor_posts(stub_server)
               if b["id"].endswith("-sub-abcd1234")][0]
        assert sub["meta"]["harness"] == "claude-code"
        assert sub["meta"]["model"] == "claude-sonnet-5"
        assert sub["meta"]["subagent_type"] == "general-purpose"

    def test_model_absent_when_unresolvable(self, env, repo, stub_server,
                                            monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        run_hook(hook("SessionStart", repo, {"source": "startup"}))
        assert wait_for(lambda: actor_posts(stub_server))
        assert "model" not in actor_posts(stub_server)[0]["meta"]
        # a session that never resolves a model must not look like drift
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: any(e["value"] == "idle"
                                    for e in posted_events(stub_server)))
        time.sleep(0.4)
        assert len(actor_posts(stub_server)) == 1


class TestMetaRefresh:
    def test_steady_state_registers_once(self, env, repo, stub_server,
                                         tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        t = transcript(tmp_path, assistant("claude-opus-5"))
        run_hook(hook("SessionStart", repo, {"source": "startup",
                                             "transcript_path": t}))
        assert wait_for(lambda: actor_posts(stub_server))
        run_hook(hook("Stop", repo, {"transcript_path": t}))
        assert wait_for(lambda: any(e["value"] == "idle"
                                    for e in posted_events(stub_server)))
        time.sleep(0.4)
        assert len(actor_posts(stub_server)) == 1

    def test_branch_drift_reposts_full_meta(self, env, repo, stub_server,
                                            tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        t = transcript(tmp_path, assistant("claude-opus-5"))
        run_hook(hook("SessionStart", repo, {"source": "startup",
                                             "transcript_path": t}))
        assert wait_for(lambda: actor_posts(stub_server))
        subprocess.run(["git", "checkout", "-q", "-b", "feature/y"],
                       cwd=repo, check=True)
        expire_git_ctx(env)
        run_hook(hook("PostToolUse", repo, {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(repo / "src" / "app.py")},
            "tool_response": {}, "transcript_path": t}))
        assert wait_for(lambda: len(actor_posts(stub_server)) == 2)
        again = actor_posts(stub_server)[1]
        assert again["id"] == "cc-" + SESS[:8]
        assert again["name"] == "wt"
        # full object, not a patch: identity/worktree survive the refresh
        assert again["meta"] == {"harness": "claude-code",
                                 "branch": "feature/y", "worktree": "wt",
                                 "git_name": "Ada Lovelace",
                                 "git_email": "ada@example.com",
                                 "host": report.hostname(),
                                 "source": "startup",
                                 "model": "claude-opus-5"}

    def test_model_drift_reposts_and_then_settles(self, env, repo,
                                                  stub_server, tmp_path,
                                                  monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        t = tmp_path / "t.jsonl"
        t.write_text(assistant("claude-opus-5") + "\n")
        run_hook(hook("SessionStart", repo, {"source": "startup",
                                             "transcript_path": str(t)}))
        assert wait_for(lambda: actor_posts(stub_server))
        t.write_text(assistant("claude-haiku-4-5") + "\n")   # /model switch
        expire_model(env)
        run_hook(hook("Stop", repo, {"transcript_path": str(t)}))
        assert wait_for(lambda: len(actor_posts(stub_server)) == 2)
        assert actor_posts(stub_server)[1]["meta"]["model"] == \
            "claude-haiku-4-5"
        run_hook(hook("Stop", repo, {"transcript_path": str(t)}))
        time.sleep(0.4)
        assert len(actor_posts(stub_server)) == 2      # no drift, no re-POST

    def test_session_end_clears_per_session_caches(self, env, repo,
                                                   stub_server, tmp_path):
        t = transcript(tmp_path, assistant("claude-opus-5"))
        run_hook(hook("SessionStart", repo, {"source": "startup",
                                             "transcript_path": t}))
        assert wait_for(lambda: (env / ("model.%s.json" % SESS[:8])).exists())
        run_hook(hook("SessionEnd", repo, {"reason": "exit",
                                           "transcript_path": t}))
        assert wait_for(lambda: not (
            env / ("model.%s.json" % SESS[:8])).exists())
        assert wait_for(lambda: not (
            env / ("actor-meta.%s.json" % SESS[:8])).exists())
