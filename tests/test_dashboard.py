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
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


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
    assert err.value.code == 405
    with pytest.raises(urllib.error.HTTPError) as err:
        get(server + "/api/workspaces", host="evil.example.com")
    assert err.value.code == 403
    for bad in ("/api/workspaces/..%2F..", "/api/workspaces/nope", "/etc/passwd"):
        with pytest.raises(urllib.error.HTTPError) as err:
            get(server + bad)
        assert err.value.code == 404
