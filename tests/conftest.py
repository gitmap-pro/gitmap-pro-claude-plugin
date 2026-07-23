import http.server
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


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
