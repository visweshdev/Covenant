import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def session(repo, state, messages):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    stdin = "\n".join(json.dumps(m) for m in messages) + "\n"
    r = subprocess.run(
        [sys.executable, "-m", "covenant", "--repo", str(repo), "--state-dir", str(state), "mcp"],
        input=stdin, capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


def call(n, name, **arguments):
    return {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def text(reply):
    return reply["result"]["content"][0]["text"]


def test_mcp_session(mylib_repo, tmp_path):
    replies = session(mylib_repo, tmp_path / "s", [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        call(3, "covenant_rules"),
        call(4, "covenant_record_edit", path=str(mylib_repo / "mylib" / "core.py")),
        call(5, "covenant_preflight", action="commit"),
        call(6, "covenant_run_tests"),
        call(7, "covenant_preflight", action="commit"),
        call(8, "nope"),
        {"jsonrpc": "2.0", "id": 9, "method": "resources/list"},
    ])
    by_id = {r["id"]: r for r in replies}
    assert len(replies) == 9  # the notification gets no reply
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in by_id[2]["result"]["tools"]} >= {"covenant_rules", "covenant_run_tests", "covenant_preflight"}
    assert "don't change public APIs" in text(by_id[3])
    assert by_id[5]["result"]["isError"] and "C3" in text(by_id[5])
    assert text(by_id[6]).startswith("PASSED")
    assert not by_id[7]["result"]["isError"]
    assert by_id[8]["error"]["code"] == -32602
    assert by_id[9]["error"]["code"] == -32601
