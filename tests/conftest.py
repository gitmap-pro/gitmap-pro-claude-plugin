import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts",
                      "report.py")


def run_hook(payload, timeout=15):
    """Feed one hook payload through report.py as a real subprocess."""
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


class StubEventsAPI(http.server.BaseHTTPRequestHandler):
    """Minimal stand-in for gitmap's events API: records every request so
    tests assert exact shapes. Behavior switches via server attributes."""

    def log_message(self, *a):
        pass

    def _record(self, body=None):
        self.server.requests.append(
            {"method": self.command, "path": self.path, "body": body})

    def do_GET(self):
        self._record()
        if self.path.startswith("/api/resolve-repo"):
            self._json({"map": self.server.map_name})
        else:
            self._json({"error": "nope"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        self._record(body)
        code = self.server.post_status
        types = {e.get("type") for e in body.get("events", [])}
        if types & self.server.reject_types:
            # how the real server answers a batch holding an event type it
            # doesn't know: the whole batch 400s, known types included
            self._json({"error": "unknown event type"}, 400)
            return
        if code >= 400:
            self._json({"error": "stub says %d" % code}, code)
        else:
            self._json({"ok": True,
                        "seqs": list(range(len(body.get("events", [])))) or
                        [1]})

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def stub_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubEventsAPI)
    srv.requests = []
    srv.map_name = "myrepo"
    srv.post_status = 200
    srv.reject_types = set()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture()
def env(tmp_path, monkeypatch, stub_server):
    """Isolated cache dir + config pointed at the stub."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("GITMAP_HOOK_CACHE", str(cache))
    monkeypatch.setenv("GITMAP_SERVER",
                       "http://127.0.0.1:%d" % stub_server.server_address[1])
    monkeypatch.setenv("GITMAP_TOKEN", "gm_test")
    monkeypatch.delenv("GITMAP_MAP", raising=False)
    monkeypatch.delenv("GITMAP_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_NAME", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    return cache


@pytest.fixture()
def repo(tmp_path):
    """A real git repo with an origin remote, for path relativization."""
    import subprocess
    d = tmp_path / "wt"
    d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "work"], cwd=d, check=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "https://github.com/acme/myrepo.git"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "Ada Lovelace"],
                   cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "ada@example.com"],
                   cwd=d, check=True)
    (d / "src").mkdir()
    (d / "src" / "app.py").write_text("print('hi')\n")
    return d
