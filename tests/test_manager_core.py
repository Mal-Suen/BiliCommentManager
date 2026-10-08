"""manager 核心逻辑：任务锁互斥、启动自查、pid 自愈、存盘重试、筛选。"""

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import pytest

import bili_comment_manager as mgr
from util import make_comment


# ---------- 文件锁 ----------

def test_lock_exclusive_within_process(mgr_env):
    assert mgr.acquire_task_lock("t1")
    assert not mgr.acquire_task_lock("t1")      # 同名二次加锁必须失败
    assert mgr.acquire_task_lock("t2")          # 不同任务互不干扰


def test_lock_cross_process(app_dir):
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import bili_comment_manager as m, time; "
         "assert m.acquire_task_lock('cx'); "
         "print('locked', flush=True); time.sleep(20)"],
        cwd=str(app_dir), stdout=subprocess.PIPE,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    try:
        assert child.stdout.readline().decode().strip() == "locked"
        r = subprocess.run(
            [sys.executable, "-c",
             "import bili_comment_manager as m; print(m.acquire_task_lock('cx'))"],
            cwd=str(app_dir), capture_output=True, timeout=30,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        assert r.stdout.decode().strip() == "False"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_lock_released_after_process_exit(app_dir):
    r = subprocess.run(
        [sys.executable, "-c",
         "import bili_comment_manager as m; "
         "assert m.acquire_task_lock('gone'); print('ok')"],
        cwd=str(app_dir), capture_output=True, timeout=30,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0
    r2 = subprocess.run(
        [sys.executable, "-c",
         "import bili_comment_manager as m; print(m.acquire_task_lock('gone'))"],
        cwd=str(app_dir), capture_output=True, timeout=30,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r2.stdout.decode().strip() == "True"   # 进程退出后锁自动释放


# ---------- 启动自查 refuse_if_task_running ----------

def test_refuse_when_other_worker_alive(mgr_env):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (mgr_env / "gui_delete.pid").write_text(str(sleeper.pid), encoding="utf-8")
        with pytest.raises(SystemExit) as ei:
            mgr.refuse_if_task_running("delete", "删除")
        assert "另一个删除任务" in str(ei.value.code)
        assert str(sleeper.pid) in str(ei.value.code)
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)


def test_refuse_passes_when_pid_dead(mgr_env):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=30)
    (mgr_env / "gui_delete.pid").write_text(str(p.pid), encoding="utf-8")
    mgr.refuse_if_task_running("delete", "删除")   # 不抛即通过


def test_refuse_excludes_own_and_parent_pid(mgr_env):
    (mgr_env / "gui_delete.pid").write_text(str(os.getpid()), encoding="utf-8")
    mgr.refuse_if_task_running("delete", "删除")
    (mgr_env / "gui_fetch.pid").write_text(str(os.getppid()), encoding="utf-8")
    mgr.refuse_if_task_running("fetch", "拉取")


def test_refuse_passes_when_pidfile_missing(mgr_env):
    mgr.refuse_if_task_running("delete", "删除")


# ---------- pid 自愈 ----------

def test_heal_pid_file_writes_own_pid(mgr_env):
    (mgr_env / "gui_delete.pid").write_text("12345", encoding="utf-8")
    mgr.heal_pid_file("delete")
    assert (mgr_env / "gui_delete.pid").read_text(encoding="utf-8").strip() \
        == str(os.getpid())


# ---------- 存盘：共享冲突退避重试 ----------

def test_save_data_retries_through_held_handle(mgr_env):
    comments = {i: make_comment(i) for i in range(1, 6)}
    mgr.save_data("u1", comments)

    release = threading.Event()

    def holder():
        with open(mgr.DATA_FILE, encoding="utf-8") as f:   # 持续占用读句柄
            release.wait(timeout=5)

    hold_thread = threading.Thread(target=holder)
    hold_thread.start()
    time.sleep(0.1)

    result = {}

    def do_save():
        t0 = time.time()
        try:
            mgr.save_data("u1", comments)
            result["ok"] = time.time() - t0
        except Exception as e:      # noqa: BLE001
            result["err"] = e

    save_thread = threading.Thread(target=do_save)
    save_thread.start()
    time.sleep(0.3)                # 期间 save 应一直 PermissionError 重试
    release.set()
    save_thread.join(timeout=15)
    hold_thread.join(timeout=5)

    assert "err" not in result, result["err"]
    assert result["ok"] > 0.2      # 确实经历了退避等待而非瞬间成功
    loaded, uid = mgr.load_data()
    assert uid == "u1" and set(loaded) == set(comments)


def test_two_savers_interleave_cleanly(app_dir):
    """pid 后缀 tmp：两个进程交错写盘不崩溃、最终文件完整可读。"""
    code = (
        "import bili_comment_manager as m, time\n"
        "c = {i: {'rpid': i, 'oid': 1, 'type': 1, 'message': 'x', 'time': 1,\n"
        "         'is_reply': False, 'keep': False, 'deleted': False,\n"
        "         'error': None} for i in range(1, 51)}\n"
        "for _ in range(10):\n"
        "    m.save_data('u', c)\n"
        "    time.sleep(0.02)\n"
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    procs = [subprocess.Popen([sys.executable, "-c", code], cwd=str(app_dir), env=env)
             for _ in range(2)]
    for p in procs:
        assert p.wait(timeout=60) == 0
    r = subprocess.run(
        [sys.executable, "-c",
         "import bili_comment_manager as m; d, u = m.load_data(); print(len(d), u)"],
        cwd=str(app_dir), capture_output=True, timeout=30, env=env)
    assert r.stdout.decode().strip() == "50 u"


# ---------- 筛选与参数解析 ----------

def _ns(**kw):
    base = {"type": "all", "keyword": None, "before": None, "after": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


def test_apply_filters_dates_type_keyword():
    comments = {
        1: make_comment(1, t=_ts("2023-01-01 10:00"), type=1, msg="Hello World"),
        2: make_comment(2, t=_ts("2023-06-01 10:00"), type=17, msg="hello"),
        3: make_comment(3, t=_ts("2024-01-01 10:00"), type=1, msg="WORLD"),
    }
    out = mgr.apply_filters(comments, _ns(before="2024-01-01"))
    assert [c["rpid"] for c in out] == [1, 2]        # before 边界：当日不含
    out = mgr.apply_filters(comments, _ns(after="2023-06-01"))
    assert [c["rpid"] for c in out] == [2, 3]
    out = mgr.apply_filters(comments, _ns(type="1", keyword="hello"))
    assert [c["rpid"] for c in out] == [1]           # 关键词不分大小写
    out = mgr.apply_filters(comments, _ns())
    assert [c["rpid"] for c in out] == [1, 2, 3]     # 按时间升序


def test_parse_helpers(mgr_env):
    assert mgr.parse_types("all") is None
    assert mgr.parse_types("1,12") == {1, 12}
    with pytest.raises(SystemExit):
        mgr.parse_types("x")
    assert mgr.parse_pair("bad", (5.0, 12.0)) == (5.0, 12.0)   # 非法回退默认
    assert mgr.parse_pair("3,6", (5.0, 12.0)) == (3.0, 6.0)
    assert mgr.parse_oids("") == set()
    assert mgr.parse_oids("1, 2") == {1, 2}
    assert mgr.parse_rpids("1 2,3") == [1, 2, 3]


def test_pid_running(mgr_env):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert mgr._pid_running(sleeper.pid)
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)
    assert not mgr._pid_running(0)
    assert not mgr._pid_running(-5)
