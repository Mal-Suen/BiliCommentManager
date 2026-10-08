"""GUI HTTP 层：红条警告字段、缺库前置拦截、任务互斥守卫、停止与保留。"""

import json
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import bili_comment_gui as gui
from util import make_comment


@pytest.fixture
def server(gui_env):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.read().decode("utf-8")


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def test_html_contains_env_banner(server):
    _, base = server
    assert 'id="env-banner"' in _get(base, "/")


def test_api_data_carries_worker_warning(server):
    _, base = server
    data = json.loads(_get(base, "/api/data"))
    assert "worker_warning" in data
    assert "worker_warning" in json.loads(_get(base, "/api/progress"))


def test_delete_blocked_upfront_when_worker_warning(server, monkeypatch):
    monkeypatch.setattr(gui, "WORKER_WARNING", "测试警告：缺 requests")
    _, base = server
    j = _post(base, "/api/delete", {"from": None, "to": None,
                                    "type": "all", "keyword": None})
    assert j["ok"] is False
    assert "测试警告" in j["error"]


def test_delete_refused_while_fetch_running(server):
    _, base = server
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (gui.FETCH_PID).write_text(str(sleeper.pid), encoding="utf-8")
        j = _post(base, "/api/delete", {"from": None, "to": None,
                                        "type": "all", "keyword": None})
        assert j["ok"] is False
        assert "拉取任务进行中" in j["error"]
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)


def test_stop_with_no_tasks(server):
    _, base = server
    j = _post(base, "/api/stop", {})
    assert j["ok"] is False
    assert "没有运行中的任务" in j["error"]


def test_keep_toggle_persists(server, gui_env):
    _, base = server
    live = {"uid": "u", "comments": {"1": make_comment(1, keep=False)}}
    (gui_env / "my_comments.json").write_text(json.dumps(live), encoding="utf-8")
    j = _post(base, "/api/keep", {"rpid": 1, "value": True})
    assert j["ok"] is True
    saved = json.loads((gui_env / "my_comments.json").read_text(encoding="utf-8"))
    assert saved["comments"]["1"]["keep"] is True
