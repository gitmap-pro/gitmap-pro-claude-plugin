"""End-to-end for the intent/outcome emissions (codemap#267): hook stdin
fixtures through report.py as a real subprocess, exact requests at the stub
events API."""
import json
import os
import subprocess
import sys
import time

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts",
                      "report.py")
SESS = "out26767-full-session-id"


def hook(event, repo, extra=None):
    d = {"hook_event_name": event, "session_id": SESS, "cwd": str(repo)}
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


def posted_events(stub):
    return [e for r in stub.requests
            if r["method"] == "POST" and r["path"].endswith("/events")
            for e in (r["body"] or {}).get("events", [])]


def by_type(stub, type_):
    return [e for e in posted_events(stub) if e["type"] == type_]


def bash(repo, command, stdout="", stderr=""):
    return hook("PostToolUse", repo, {
        "tool_name": "Bash", "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": stderr}})


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True)


class TestIntent:
    def test_first_prompt_posts_focus_and_started(self, env, repo,
                                                  stub_server):
        run_hook(hook("UserPromptSubmit", repo,
                      {"prompt": "fix the flaky flush race\ndetails..."}))
        assert wait_for(lambda: len(by_type(stub_server, "task.status")))
        foci = by_type(stub_server, "attention.focus")
        assert foci == [{"type": "attention.focus", "actor": "cc-out26767",
                         "anchor": {}, "value": "fix the flaky flush race",
                         "corr": SESS}]
        st = by_type(stub_server, "task.status")
        assert st[0]["value"] == "started"
        assert st[0]["payload"]["subtype"] == "fix the flaky flush race"
        # the focus type was registered before the batch that used it
        reg = [r for r in stub_server.requests
               if r["path"].endswith("/event-types")]
        assert reg and reg[0]["body"]["name"] == "attention.focus"

        # a second prompt says nothing new: no fork, no new requests
        n = len(stub_server.requests)
        run_hook(hook("UserPromptSubmit", repo,
                      {"prompt": "now do the other half of it"}))
        time.sleep(0.4)
        assert len(stub_server.requests) == n

    def test_intent_gate_keeps_prompt_text_off_the_map(self, env, repo,
                                                       stub_server,
                                                       monkeypatch):
        monkeypatch.setenv("GITMAP_INTENT", "0")
        run_hook(hook("UserPromptSubmit", repo,
                      {"prompt": "secret project details here"}))
        assert wait_for(lambda: len(by_type(stub_server, "task.status")))
        assert by_type(stub_server, "attention.focus") == []
        st = by_type(stub_server, "task.status")
        assert st[0]["value"] == "started" and "payload" not in st[0]

    def test_trivial_prompt_starts_but_carries_no_goal(self, env, repo,
                                                       stub_server):
        run_hook(hook("UserPromptSubmit", repo, {"prompt": "continue"}))
        assert wait_for(lambda: len(by_type(stub_server, "task.status")))
        assert by_type(stub_server, "attention.focus") == []


class TestBashOutcomes:
    def test_test_run_posts_sweep_completed(self, env, repo, stub_server):
        run_hook(bash(repo, "python -m pytest tests/ -q",
                      stdout="== 2 failed, 10 passed in 1.2s =="))
        assert wait_for(lambda: len(by_type(stub_server, "sweep.completed")))
        ev = by_type(stub_server, "sweep.completed")[0]
        assert ev["value"] == "fail" and ev["anchor"] == {}
        assert ev["payload"]["subtype"] == "tests"
        assert ev["payload"]["passed"] == 10
        assert ev["payload"]["failed"] == 2
        assert ev["corr"] == SESS

    def test_unrecognized_output_posts_nothing(self, env, repo,
                                               stub_server):
        run_hook(bash(repo, "pytest --collect-only",
                      stdout="collected 12 items"))
        time.sleep(0.4)
        assert stub_server.requests == []

    def test_commit_posts_vcs_commit_isolated(self, env, repo, stub_server):
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")
        run_hook(hook("SessionStart", repo, {"source": "startup"}))
        assert wait_for(lambda: any(
            r["path"].endswith("/actors") for r in stub_server.requests))
        (repo / "src" / "app.py").write_text("print('v2')\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "add the thing")
        run_hook(bash(repo, "git commit -q -m 'add the thing'",
                      stdout="[work abc] add the thing"))
        assert wait_for(lambda: len(by_type(stub_server, "vcs.commit")))
        ev = by_type(stub_server, "vcs.commit")[0]
        assert ev["value"] == "add the thing"
        assert ev["payload"]["branch"] == "work"
        assert ev["payload"]["files"] == 1
        assert len(ev["payload"]["sha"]) == 40
        # isolated request: nothing else shares the vcs.commit batch
        req = [r for r in stub_server.requests
               if r["method"] == "POST" and r["path"].endswith("/events")
               and any(e["type"] == "vcs.commit"
                       for e in r["body"].get("events", []))]
        assert len(req) == 1
        assert {e["type"] for e in req[0]["body"]["events"]} \
            == {"vcs.commit"}

        # no new commit since: the same command posts nothing more
        n = len(by_type(stub_server, "vcs.commit"))
        run_hook(bash(repo, "git commit -q -m 'nothing'", stderr="error"))
        time.sleep(0.4)
        assert len(by_type(stub_server, "vcs.commit")) == n

    def test_rejected_commit_type_costs_only_the_commit(self, env, repo,
                                                        stub_server):
        stub_server.reject_types = {"vcs.commit"}
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")
        run_hook(hook("SessionStart", repo, {"source": "startup"}))
        assert wait_for(lambda: any(
            r["path"].endswith("/actors") for r in stub_server.requests))
        (repo / "src" / "app.py").write_text("print('v3')\n")
        _git(repo, "commit", "-q", "-am", "won't land")
        run_hook(bash(repo, "git commit -q -am x", stdout="ok"))
        assert wait_for(lambda: len(by_type(stub_server, "vcs.commit")))
        # the rejected batch held nothing but vcs.commit — its 400 cost no
        # presence or attention events, and nothing retries it
        for r in stub_server.requests:
            if r["method"] == "POST" and r["path"].endswith("/events"):
                types = {e["type"] for e in r["body"].get("events", [])}
                if "vcs.commit" in types:
                    assert types == {"vcs.commit"}
        n = len(by_type(stub_server, "vcs.commit"))
        time.sleep(0.4)
        assert len(by_type(stub_server, "vcs.commit")) == n   # no retry

    def test_pr_create_posts_review(self, env, repo, stub_server):
        run_hook(hook("UserPromptSubmit", repo,
                      {"prompt": "open a PR for the flush fix"}))
        assert wait_for(lambda: len(by_type(stub_server, "task.status")))
        run_hook(bash(repo, 'gh pr create --title "flush fix"',
                      stdout="https://github.com/acme/myrepo/pull/42\n"))
        assert wait_for(lambda: len(by_type(stub_server, "task.status")) >= 2)
        st = by_type(stub_server, "task.status")
        assert [e["value"] for e in st] == ["started", "review"]
        assert st[1]["payload"]["subtype"] == "open a PR for the flush fix"

    def test_failed_pr_create_posts_nothing(self, env, repo, stub_server):
        run_hook(bash(repo, "gh pr create", stderr="a branch is required"))
        time.sleep(0.4)
        assert by_type(stub_server, "task.status") == []


class TestLifecycle:
    def test_blocked_then_resumed_then_done(self, env, repo, stub_server):
        run_hook(hook("UserPromptSubmit", repo,
                      {"prompt": "wire the sessions endpoint"}))
        assert wait_for(lambda: len(by_type(stub_server, "task.status")))
        run_hook(hook("Notification", repo,
                      {"message": "Claude needs your permission"}))
        assert wait_for(
            lambda: len(by_type(stub_server, "task.status")) >= 2)
        # a second notification dedupes: still blocked, nothing new forked
        run_hook(hook("Notification", repo, {"message": "waiting"}))
        run_hook(hook("UserPromptSubmit", repo, {"prompt": "yes go ahead"}))
        assert wait_for(
            lambda: len(by_type(stub_server, "task.status")) >= 3)
        run_hook(hook("SessionEnd", repo, {"reason": "logout"}))
        assert wait_for(
            lambda: len(by_type(stub_server, "task.status")) >= 4)
        vals = [e["value"] for e in by_type(stub_server, "task.status")]
        assert vals == ["started", "blocked", "started", "done"]

    def test_session_end_without_a_task_posts_no_done(self, env, repo,
                                                      stub_server):
        run_hook(hook("SessionEnd", repo, {"reason": "clear"}))
        assert wait_for(lambda: any(          # the presence clear went out
            e["type"] == "presence.working"
            for e in posted_events(stub_server)))
        assert by_type(stub_server, "task.status") == []
