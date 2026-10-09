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


# ---------- 拉取对账与状态合并（已删评论复活 bug 回归） ----------

def _reply(rpid, t=1700000000):
    """AICU 原始返回里的单条评论。"""
    return {"rpid": rpid, "message": f"c{rpid}", "time": t, "parent": 0,
            "dyn": {"oid": 1, "type": 1}}


def _page(rpids, is_end=False, total=None):
    cursor = {"is_end": is_end}
    if total is not None:
        cursor["all_count"] = total
    return {"data": {"cursor": cursor, "replies": [_reply(r) for r in rpids]}}


def test_merge_comments_preserves_local_state(mgr_env):
    old = {1: make_comment(1, deleted=True), 2: make_comment(2, keep=True),
           3: make_comment(3)}
    fresh = {1: make_comment(1), 2: make_comment(2), 4: make_comment(4)}
    merged = mgr.merge_comments(old, fresh)
    assert merged[1]["deleted"] is True       # AICU 索引滞后：已删状态保留
    assert merged[2]["keep"] is True
    assert merged[3] == old[3]                # 旧有新无：原样保留
    assert merged[4]["deleted"] is False      # 新评论默认待删


def test_reconcile_purges_deleted_and_skips_echoes(mgr_env):
    """重新拉取时清除：已删条目移出实时（归档），AICU 回声跳过，新评论进实时＋全量。"""
    old = {1: make_comment(1, deleted=True), 2: make_comment(2, deleted=True),
           3: make_comment(3), 4: make_comment(4, keep=True)}
    backup = {i: make_comment(i) for i in range(1, 5)}
    fresh = {1: make_comment(1), 3: make_comment(3),
             4: make_comment(4), 5: make_comment(5)}
    live, new_entries, archived, purged = mgr.reconcile_fetch(old, backup, fresh)
    assert 1 not in live and 2 not in live     # 已删条目被清除出实时
    assert purged == 2
    assert not archived                        # 两条都已在全量档案里
    assert live[3]["deleted"] is False         # 待删保留
    assert live[4]["keep"] is True             # 保留标记跟随
    assert set(new_entries) == {5}             # 新评论进实时＋全量


def test_reconcile_archives_purged_not_in_backup(mgr_env):
    """不在全量档案里的已删条目：清除前先归档，AICU 回声也不算新评论。"""
    old = {1: make_comment(1, deleted=True)}
    backup = {}
    fresh = {1: make_comment(1)}
    live, new_entries, archived, purged = mgr.reconcile_fetch(old, backup, fresh)
    assert live == {} and purged == 1
    assert set(archived) == {1}                 # 清除前先归档
    assert not new_entries                     # 回声不算新评论


def test_reconcile_new_comments_to_backup(mgr_env):
    old = {1: make_comment(1)}
    backup = {1: make_comment(1)}
    fresh = {1: make_comment(1), 5: make_comment(5)}
    live, new_entries, archived, purged = mgr.reconcile_fetch(old, backup, fresh)
    assert set(new_entries) == {5}             # 不在全量里 → 新评论
    assert live[5]["deleted"] is False
    assert purged == 0 and not archived


def test_save_data_complete_flag_roundtrip(mgr_env):
    comments = {1: make_comment(1)}
    mgr.save_data("u1", comments, complete=True)
    assert mgr.data_complete() is True
    mgr.save_data("u1", comments)              # 默认沿用现有标记
    assert mgr.data_complete() is True
    mgr.save_data("u1", comments, complete=False)
    assert mgr.data_complete() is False


def test_append_backup_appends_only_missing(mgr_env):
    mgr.append_backup("u1", {1: make_comment(1), 2: make_comment(2)})
    loaded = mgr.load_backup()
    assert set(loaded) == {1, 2}
    mgr.append_backup("u1", {2: make_comment(2, deleted=True), 3: make_comment(3)})
    loaded = mgr.load_backup()
    assert loaded[2]["deleted"] is False      # 追加式：已有条目原样保留
    assert set(loaded) == {1, 2, 3}


def test_fetch_midway_save_keeps_deleted_marks(mgr_env, monkeypatch):
    """中途落盘并入旧状态：已删标记不被 fresh 覆盖（复活 bug 回归测试）。"""
    old = {i: make_comment(i, deleted=True) for i in range(1, 6)}
    mgr.save_data("u1", old)
    seq = [_page([1, 2, 3, 4, 5], total=5) for _ in range(10)] \
        + [_page([], is_end=True)]
    state = {"i": 0}

    def fake_aicu(params, max_tries=3):
        d = seq[state["i"]]
        state["i"] += 1
        return d

    monkeypatch.setattr(mgr, "aicu_get", fake_aicu)
    fresh, completed = mgr.fetch_all_comments(
        "u1", page_delay=(0, 0), old=old, incremental=False)
    assert completed is True
    loaded, uid = mgr.load_data()
    assert uid == "u1" and set(loaded) == {1, 2, 3, 4, 5}
    assert all(c["deleted"] for c in loaded.values())   # 第 10 页落盘后标记仍在


def test_fetch_incremental_stops_at_known_pages(mgr_env, monkeypatch):
    """增量模式：AICU 按时间倒序、新评论在前，连续整页已知即提前停。"""
    old = {i: make_comment(i) for i in range(1, 11)}
    mgr.save_data("u1", old, complete=True)
    seq = [
        _page([11, 12, 1, 2, 3], total=12),   # 2 新 + 3 已知
        _page([4, 5, 6, 7, 8]),               # 全已知 → 连续第 1 页
        _page([6, 7, 8, 9, 10]),              # 全已知 → 连续第 2 页，停止
        _page([9, 10]),                       # 不应被请求
    ]
    state = {"i": 0}

    def fake_aicu(params, max_tries=3):
        d = seq[state["i"]]
        state["i"] += 1
        return d

    monkeypatch.setattr(mgr, "aicu_get", fake_aicu)
    fresh, completed = mgr.fetch_all_comments(
        "u1", page_delay=(0, 0), old=old, incremental=True)
    assert completed is True
    assert state["i"] == 3                    # 第 4 页没有请求
    assert 11 in fresh and 12 in fresh


def test_fetch_full_mode_pages_everything(mgr_env, monkeypatch):
    """全量模式翻完所有页，不提前停。"""
    old = {i: make_comment(i) for i in range(1, 6)}
    seq = [
        _page([1, 2, 3, 4, 5], total=5),
        _page([1, 2, 3, 4, 5]),
        _page([], is_end=True),
    ]
    state = {"i": 0}

    def fake_aicu(params, max_tries=3):
        d = seq[state["i"]]
        state["i"] += 1
        return d

    monkeypatch.setattr(mgr, "aicu_get", fake_aicu)
    fresh, completed = mgr.fetch_all_comments(
        "u1", page_delay=(0, 0), old=old, incremental=False)
    assert completed is True and state["i"] == 3


# ---------- 删除核验 ----------

class _FakeReply:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, status=200, payload=None, exc=None):
        self.status, self.payload, self.exc = status, payload, exc

    def get(self, url, timeout=20):
        if self.exc:
            raise self.exc
        return _FakeReply(self.status, self.payload)


def test_comment_exists_by_12006(mgr_env):
    gone = _FakeSession(payload={"code": 12006, "message": "没有该评论"})
    present = _FakeSession(payload={"code": 0, "data": {"root": {}}})
    broken = _FakeSession(exc=RuntimeError("net"))
    c = make_comment(1)
    assert mgr.comment_exists(gone, c) is False      # 12006 → 已删
    assert mgr.comment_exists(present, c) is True    # code=0 → 仍存在
    assert mgr.comment_exists(broken, c) is True     # 网络异常保守当作存在


def test_verify_targets_selection(mgr_env):
    live = {1: make_comment(1, deleted=True), 2: make_comment(2),
            3: make_comment(3, keep=True)}
    backup = {1: make_comment(1), 2: make_comment(2), 3: make_comment(3),
              4: make_comment(4)}                     # 4 已清除出实时
    targets = mgr.verify_targets(live, backup)
    assert set(targets) == {1, 4}                    # 已删的＋仅档案里的
    assert 2 not in targets and 3 not in targets     # 现存的不核验
