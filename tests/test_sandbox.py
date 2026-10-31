"""Sandbox mode: the generated Docker setup, and the allowlist proxy (tested for real, no Docker needed)."""

import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from covenant import sandbox, sandbox_proxy
from covenant.ir import Clause, ClauseKind, Covenant, Goal


def test_prepare_writes_an_isolated_setup(mylib_repo, tmp_path):
    rules = Covenant(instruction="x", goal=Goal(action="x"), clauses=[Clause(
        id="C1", kind=ClauseKind.EFFECT_PROHIBITION, source_span="only reach pypi.org", checker="command_guard",
        params={"deny": ["network"], "allow_hosts": ["pypi.org"]})])
    plan = sandbox.prepare(mylib_repo, rules, ["github.com"], tmp_path / "state")
    assert plan["allow"][:1] == ["api.anthropic.com"] and "pypi.org" in plan["allow"] and "github.com" in plan["allow"]
    spec = yaml.safe_load(plan["compose"].read_text(encoding="utf-8"))
    agent, hooks, proxy = spec["services"]["agent"], spec["services"]["hooks"], spec["services"]["proxy"]
    assert spec["networks"]["sandbox"]["internal"] is True          # no route out...
    assert agent["networks"] == ["sandbox"]                         # ...for the agent,
    assert set(proxy["networks"]) == {"sandbox", "outside"}         # only the proxy reaches outside
    assert agent["environment"]["HTTPS_PROXY"] == "http://proxy:3128"
    assert not any(v.get("target") == "/state" for v in agent["volumes"])  # Covenant's state isn't in the agent's reach
    assert any(v.get("target") == "/state" for v in hooks["volumes"])
    assert agent["environment"]["GIT_CONFIG_VALUE_0"] == "/covenant-git-hooks"
    assert "--allow" in proxy["command"] and "pypi.org" in proxy["command"]
    settings = (plan["dir"] / "claude-settings.json").read_text(encoding="utf-8")
    assert "http://hooks:8787/hook" in settings
    assert (plan["dir"] / "proxy" / "sandbox_proxy.py").is_file()
    assert (plan["dir"] / "covenant-src" / "pyproject.toml").is_file()
    assert "curl" in (plan["dir"] / "git-hooks" / "pre-commit").read_text(encoding="utf-8")
    # the host repo's own git hooks are left alone
    assert not (mylib_repo / ".git" / "hooks" / "pre-commit").exists() or "hooks:8787" not in \
        (mylib_repo / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")


def test_commands():
    up, run, down = sandbox.commands(sandbox.Path("c.yml"), ["claude", "-p", "hi"])
    assert up[-3:] == ["--build", "hooks", "proxy"] and run[-3:] == ["claude", "-p", "hi"] and down[-1] == "down"


@pytest.fixture
def upstream():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello from " + self.path.encode())

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def proxy():
    ready, stop = threading.Event(), threading.Event()
    threading.Thread(target=sandbox_proxy.serve, args=(0, ["localhost"], "127.0.0.1", ready, stop), daemon=True).start()
    ready.wait(5)
    yield ready.port
    stop.set()


def test_proxy_allows_listed_hosts_and_refuses_the_rest(upstream, proxy):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy}"}))
    assert opener.open(f"http://localhost:{upstream}/ok", timeout=10).read() == b"hello from /ok"
    with pytest.raises(urllib.error.HTTPError) as err:
        opener.open(f"http://127.0.0.1:{upstream}/no", timeout=10)  # an IP isn't on the list
    assert err.value.code == 403


def test_proxy_connect_tunnel(upstream, proxy):
    s = socket.create_connection(("127.0.0.1", proxy), timeout=10)
    s.sendall(f"CONNECT localhost:{upstream} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    assert s.recv(100).startswith(b"HTTP/1.1 200")
    s.sendall(b"GET /tunnel HTTP/1.0\r\nHost: localhost\r\n\r\n")
    data = b""
    while chunk := s.recv(4096):
        data += chunk
    assert data.endswith(b"hello from /tunnel")
    s.close()
    t = socket.create_connection(("127.0.0.1", proxy), timeout=10)
    t.sendall(b"CONNECT evil.example.com:443 HTTP/1.1\r\n\r\n")
    assert t.recv(100).startswith(b"HTTP/1.1 403")


def test_allowed_matching():
    assert sandbox_proxy.allowed("api.anthropic.com", ["anthropic.com"])
    assert not sandbox_proxy.allowed("anthropic.com.evil.io", ["anthropic.com"])
