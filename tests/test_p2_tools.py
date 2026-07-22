import os
import subprocess
import sys

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")


def run(script, *args, cwd, env_extra=None):
    env = os.environ.copy()
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, os.path.join(SCRIPTS, script),
                       *args], capture_output=True, text=True, timeout=20,
                       cwd=cwd, env=env)
    return p


class TestStatus:
    def test_reports_config_repo_probe_and_spool(self, env, repo,
                                                 stub_server):
        import attention
        attention.append_touch(str(env / "attention"), "sess1234",
                               attention.encode_touch("read", "a.py", None,
                                                      "Read", ""))
        p = run("status.py", cwd=repo)
        assert p.returncode == 0
        assert "server: http://127.0.0.1" in p.stdout
        assert "token:  set (gm_tes...)" in p.stdout
        assert "branch work" in p.stdout
        assert "map 'myrepo'" in p.stdout
        assert "sess1234.jsonl: 1 touches" in p.stdout

    def test_unconfigured_and_outside_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITMAP_HOOK_CACHE", str(tmp_path / "c"))
        for k in ("GITMAP_SERVER", "GITMAP_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        p = run("status.py", cwd=tmp_path)
        assert p.returncode == 0
        assert "NOT CONFIGURED" in p.stdout
        assert "not a git repository" in p.stdout


class TestFocus:
    def test_posts_note_and_registers_type_once(self, env, repo,
                                                stub_server):
        p = run("focus.py", "--path", "src/app.py", "--note",
                "testing the focus path", cwd=repo,
                env_extra={"CLAUDE_SESSION_ID": "sessabcd-full"})
        assert p.returncode == 0
        assert "focus note recorded on src/app.py" in p.stdout
        types = [r for r in stub_server.requests
                 if r["path"].endswith("/event-types")]
        assert types[0]["body"]["name"] == "attention.focus"
        evs = [e for r in stub_server.requests
               if r["method"] == "POST" and r["path"].endswith("/events")
               for e in r["body"]["events"]]
        assert evs[0]["type"] == "attention.focus"
        assert evs[0]["value"] == "testing the focus path"
        assert evs[0]["anchor"] == {"path": "src/app.py"}
        assert evs[0]["actor"] == "cc-sessabcd"

    def test_unconfigured_prints_and_exits_zero(self, repo, monkeypatch,
                                                tmp_path):
        env_extra = {"GITMAP_HOOK_CACHE": str(tmp_path / "c3")}
        for k in ("GITMAP_SERVER", "GITMAP_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        p = run("focus.py", "--note", "x", cwd=repo, env_extra=env_extra)
        assert p.returncode == 0
        assert "not configured" in p.stdout
