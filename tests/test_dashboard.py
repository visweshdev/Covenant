import json
import threading
import urllib.error
import urllib.request

import pytest

from covenant import dashboard
from covenant.events import Commit, Edit
from covenant.runtime import Runtime


@pytest.fixture
def server(mylib_repo):
    rt = Runtime.open(mylib_repo)  # default state folder, under the test's COVENANT_HOME
    rt.start()
    rt.record(Edit(path="mylib/core.py"))
    rt.record(Commit())  # blocked by C3
    srv = dashboard.serve(0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    TOKENS[url] = srv.token
    yield url
    srv.shutdown()
    srv.server_close()


TOKENS: dict = {}


def post(server, path, body, token=None, origin=None):
    headers = {"Content-Type": "application/json", "X-Covenant-Token": TOKENS[server] if token is None else token}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(server + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def get(url, host=None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_lists_and_details(server, mylib_repo):
    status, ctype, body = get(server + "/")
    assert status == 200 and ctype.startswith("text/html") and b"<title>Covenant Dashboard</title>" in body
    _, _, body = get(server + "/api/workspaces")
    [ws] = json.loads(body)
    assert ws["name"] == "mylib" and ws["blocks"] == {"C3": 1} and ws["events"] == 1
    _, _, body = get(server + f"/api/workspaces/{ws['id']}")
    detail = json.loads(body)
    assert [c["id"] for c in detail["clauses"]] == ["C1", "C2", "C3"]
    assert detail["recent_reports"][-1]["reports"][0]["violation"] == "C3"


def test_read_only_and_local_only(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(urllib.request.Request(server + "/api/workspaces", data=b"x", method="POST"), timeout=30)
    assert err.value.code == 403  # no token
    with pytest.raises(urllib.error.HTTPError) as err:
        get(server + "/api/workspaces", host="evil.example.com")
    assert err.value.code == 403
    for bad in ("/api/workspaces/..%2F..", "/api/workspaces/nope", "/etc/passwd"):
        with pytest.raises(urllib.error.HTTPError) as err:
            get(server + bad)
        assert err.value.code == 404


def workspace(server):
    _, _, body = get(server + "/api/workspaces")
    return json.loads(body)[0]["id"]


def test_changes_need_the_token_and_a_local_origin(server):
    ws = workspace(server)
    assert post(server, f"/api/workspaces/{ws}/check", {"text": ""}, token="wrong")[0] == 403
    assert post(server, f"/api/workspaces/{ws}/check", {"text": ""}, origin="https://evil.example.com")[0] == 403
    assert post(server, f"/api/workspaces/{ws}/check", {"text": "covenant: {"})[1]["ok"] is False


def test_add_a_rule_from_plain_english_and_approve_it(server, mylib_repo):
    ws = workspace(server)
    status, rules = 200, json.loads(get(server + f"/api/workspaces/{ws}/rules")[2])
    assert "C3" in rules["text"]
    status, draft = post(server, f"/api/workspaces/{ws}/draft", {"instruction": "Never force push", "offline": True})
    assert status == 200 and draft["added"] == ["C4"] and draft["summary"][0]["checker"] == "command_guard"
    status, checked = post(server, f"/api/workspaces/{ws}/check", {"text": draft["text"]})
    assert checked["ok"] and checked["added"] == ["C4"] and not checked["changed"]
    status, saved = post(server, f"/api/workspaces/{ws}/save", {"text": draft["text"]})
    assert status == 200 and saved["approved"] == ["C4"]
    detail = json.loads(get(server + f"/api/workspaces/{ws}")[2])
    assert [c["id"] for c in detail["clauses"]] == ["C1", "C2", "C3", "C4"]
    assert detail["trace_length"] == 1  # the task's history is kept


def test_changing_a_rule_needs_a_restart(server, mylib_repo):
    ws = workspace(server)
    text = json.loads(get(server + f"/api/workspaces/{ws}/rules")[2])["text"]
    loosened = text.replace("run tests before committing", "run tests sometimes")
    assert loosened != text
    status, out = post(server, f"/api/workspaces/{ws}/save", {"text": loosened})
    assert status == 409 and out["changed"] == ["C3"]
    assert "sometimes" not in (mylib_repo / "covenant.yaml").read_text()  # nothing written
    status, out = post(server, f"/api/workspaces/{ws}/save", {"text": loosened, "restart": True})
    assert status == 200 and out["approved"] == "all"
    assert json.loads(get(server + f"/api/workspaces/{ws}")[2])["trace_length"] == 0


def test_read_only_dashboard_refuses_changes(mylib_repo):
    Runtime.open(mylib_repo).start()
    srv = dashboard.serve(0, editable=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        TOKENS[url] = ""
        assert post(url, f"/api/workspaces/{workspace(url)}/check", {"text": ""})[0] == 403
        assert json.loads(get(url + "/api/capabilities")[2]) == {"editable": False}
    finally:
        srv.shutdown()
        srv.server_close()


def test_history_keeps_past_sessions_and_replays_them(mylib_repo):
    from covenant import dashboard
    from covenant.events import Commit, Edit
    from covenant.ir import Covenant
    from covenant.runtime import Runtime

    cov = Covenant.load(mylib_repo / "covenant.yaml")
    rt = Runtime(cov, mylib_repo)
    rt.start()
    rt.record(Edit(path="mylib/core.py"))
    assert not rt.record(Commit()).allowed  # blocked: no test run
    rt.start()  # a new task: the old one is archived, not lost
    ws = rt.state_dir.name
    listed = dashboard.sessions(ws)
    assert [s["id"] for s in listed][0] == "current" and len(listed) == 2
    old = listed[1]
    assert old["events"] == 1 and old["refused"] == 1
    replay = dashboard.timeline(ws, old["id"])
    kinds = [(s["kind"], s["type"]) for s in replay["steps"]]
    assert kinds == [("event", "edit"), ("decision", "commit")]
    blocked = replay["steps"][1]
    assert blocked["allowed"] is False and blocked["rules"][0]["id"] == "C3"
    assert "tests haven't passed" in blocked["rules"][0]["why"]
    assert {r["id"] for r in replay["rules"]} >= {"C1", "C2", "C3"}
    assert dashboard.timeline(ws, "../../etc") is None
    assert dashboard.timeline(ws, "current")["steps"] == []
