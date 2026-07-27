"""content.delta must never be able to sink an attention flush.

The server 400s a whole batch when it doesn't recognise one event type in it,
and rejected attention events get re-spooled — so a reporter that mixes
opt-in content deltas into the attention batches would fail every flush, for
ever, against a server that doesn't know content.delta yet. These tests pin
the isolation that makes such a server cost only the (droppable) deltas.
"""
import os
import time

import pytest

import attention
from test_report_e2e import SESS, hook, run_hook, wait_for

CONTENT = "content.delta"


@pytest.fixture()
def live_content(monkeypatch):
    monkeypatch.setenv("GITMAP_LIVE_CONTENT", "1")


def edit_hook(repo, path="src/app.py", start=3, added=("x = 1", "y = 2")):
    return hook("PostToolUse", repo, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / path),
                       "old_string": "a", "new_string": "b"},
        "tool_response": {"filePath": str(repo / path),
                          "structuredPatch": [
                              {"oldStart": start, "oldLines": 2,
                               "newStart": start, "newLines": len(added),
                               "lines": ["+" + ln for ln in added]}]}})


def event_posts(stub):
    """Every /events POST, in order, as lists of event types."""
    return [[e.get("type") for e in (r["body"] or {}).get("events", [])]
            for r in stub.requests
            if r["method"] == "POST" and r["path"].endswith("/events")]


def spool_of(cache):
    return attention.spool_path(str(cache / "attention"), SESS[:8])


def content_spool_of(cache):
    return attention.content_spool_path(str(cache / "attention"), SESS[:8])


def stamp_of(cache):
    """The successful-flush stamp. Written *after* the POST it belongs to, so
    tests must gate on the stamp itself rather than on the request landing."""
    return str(cache / "attention" / ("%s.last-flush" % SESS[:8]))


class TestBatchIsolation:
    def test_content_never_shares_a_request_with_attention(
            self, env, repo, stub_server, live_content):
        run_hook(edit_hook(repo))
        assert os.path.exists(content_spool_of(env))
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: any(CONTENT in p for p in
                                    event_posts(stub_server)))
        posts = event_posts(stub_server)
        for types in posts:
            assert set(types) == {CONTENT} or CONTENT not in types, posts
        last_other = max(i for i, t in enumerate(posts) if CONTENT not in t)
        first_content = min(i for i, t in enumerate(posts) if CONTENT in t)
        assert first_content > last_other      # content goes last

    def test_flush_request_budget_still_holds(self, env, repo, stub_server,
                                              live_content):
        spool_dir = str(env / "attention")
        for i in range(130):
            attention.append_touch(spool_dir, SESS[:8],
                                   attention.encode_touch(
                                       "read", "f%03d.py" % i, None,
                                       "Read", ""))
        for i in range(30):
            attention.append_content(spool_dir, SESS[:8],
                                     attention.encode_content(
                                         "c%03d.py" % i, 1, ["x"]))
        run_hook(hook("SessionEnd", repo, {"reason": "exit"}))
        assert wait_for(lambda: len(event_posts(stub_server)) >=
                        attention.POSTS_PER_FLUSH)
        time.sleep(0.5)
        assert len(event_posts(stub_server)) == attention.POSTS_PER_FLUSH


class TestRejectingServer:
    """A server that predates content.delta support."""

    def test_attention_lands_and_spool_is_not_poisoned(
            self, env, repo, stub_server, live_content):
        stub_server.reject_types = {CONTENT}
        run_hook(edit_hook(repo))
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: any("attention.edit" in p
                                    for p in event_posts(stub_server)))
        # the rejected batch held nothing but content
        rejected = [p for p in event_posts(stub_server) if CONTENT in p]
        assert rejected and all(set(p) == {CONTENT} for p in rejected)
        # attention flushed clean: spool consumed, not re-spooled for retry
        assert wait_for(lambda: os.path.exists(stamp_of(env)))
        assert not os.path.exists(spool_of(env))
        assert not os.path.exists(content_spool_of(env))

    def test_second_flush_is_not_a_replay_of_the_first(
            self, env, repo, stub_server, live_content):
        stub_server.reject_types = {CONTENT}
        run_hook(edit_hook(repo))
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: os.path.exists(stamp_of(env)))
        n_edits = sum(p.count("attention.edit")
                      for p in event_posts(stub_server))
        assert n_edits == 1
        # a later flush carries only what was spooled since — nothing is
        # stuck in an endless re-spool loop
        os.remove(stamp_of(env))           # skip the 5-min Stop throttle
        run_hook(edit_hook(repo, path="src/other.py"))
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: sum(p.count("attention.edit")
                                    for p in event_posts(stub_server)) == 2)
        time.sleep(0.4)
        assert sum(p.count("attention.edit")
                   for p in event_posts(stub_server)) == 2

    def test_auth_rejection_still_drops_everything(self, env, repo,
                                                   stub_server,
                                                   live_content):
        # 402 is the plan/auth path: it must keep short-circuiting the whole
        # flush, content included, rather than falling through to a content
        # request the negative cache would refuse anyway
        stub_server.post_status = 402
        run_hook(edit_hook(repo))
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: not os.path.exists(spool_of(env)))
        assert not any(CONTENT in p for p in event_posts(stub_server))


class TestOptIn:
    def test_no_content_spool_without_the_env_flag(self, env, repo,
                                                   stub_server):
        run_hook(edit_hook(repo))
        assert os.path.exists(spool_of(env))
        assert not os.path.exists(content_spool_of(env))
        run_hook(hook("Stop", repo))
        assert wait_for(lambda: any("attention.edit" in p
                                    for p in event_posts(stub_server)))
        time.sleep(0.3)
        assert not any(CONTENT in p for p in event_posts(stub_server))
