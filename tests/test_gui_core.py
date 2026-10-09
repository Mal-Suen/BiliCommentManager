"""GUI 核心逻辑：进程存活探测（含 pid 复用防护）、文件读写重试、快照合并、进度解析。"""

import json
import subprocess
import sys
import threading
import time

import bili_comment_gui as gui
from util import make_comment


# ---------- win_proc_alive ----------

def test_win_proc_alive_states():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert gui.win_proc_alive(p.pid)                          # 活着
        assert gui.win_proc_alive(p.pid, not_before=time.time() - 5)   # 正常新拉起
        assert not gui.win_proc_alive(p.pid, not_before=time.time() - 3600)  # pid 复用防护
    finally:
        p.kill()
        p.wait(timeout=10)
    assert not gui.win_proc_alive(p.pid)                          # 死后判死
    assert not gui.win_proc_alive(99999999)                       # 不存在的 pid


# ---------- replace_with_retry ----------

def test_replace_with_retry_through_held_handle(tmp_path):
    dst = tmp_path / "dst.json"
    dst.write_text("old", encoding="utf-8")
    tmp = tmp_path / "tmp.json"
    tmp.write_text("new", encoding="utf-8")

    release = threading.Event()

    def holder():
        with open(dst, encoding="utf-8") as f:
            release.wait(timeout=5)

    hold_thread = threading.Thread(target=holder)
    hold_thread.start()
    time.sleep(0.1)

    result = {}

    def do_replace():
        t0 = time.time()
        try:
            gui.replace_with_retry(tmp, dst)
            result["ok"] = time.time() - t0
        except Exception as e:   # noqa: BLE001
            result["err"] = e

    replace_thread = threading.Thread(target=do_replace)
    replace_thread.start()
    time.sleep(0.3)
    release.set()
    replace_thread.join(timeout=15)
    hold_thread.join(timeout=5)

    assert "err" not in result, result["err"]
    assert result["ok"] > 0.2
    assert dst.read_text(encoding="utf-8") == "new"


# ---------- load_json ----------

def test_load_json_valid_missing_corrupt(tmp_path):
    p = tmp_path / "f.json"
    assert gui.load_json(p) is None                       # 文件不存在
    p.write_text(json.dumps({"comments": {"5": make_comment(5)}}), encoding="utf-8")
    assert gui.load_json(p) == {5: make_comment(5)}       # 键转 int
    p.write_text("{corrupt", encoding="utf-8")
    assert gui.load_json(p) is None                       # 损坏：重试后放弃


# ---------- make_snapshot：追加式合并 + 状态冻结 ----------

def test_make_snapshot_freezes_and_appends(gui_env):
    live = {"uid": "u", "comments": {
        "1": make_comment(1, deleted=True),
        "2": make_comment(2, deleted=False),
    }}
    (gui_env / "my_comments.json").write_text(json.dumps(live), encoding="utf-8")
    ok, err = gui.make_snapshot()
    assert ok and err is None

    # 快照后：2 被删除、新增 3
    live["comments"]["2"]["deleted"] = True
    live["comments"]["3"] = make_comment(3)
    (gui_env / "my_comments.json").write_text(json.dumps(live), encoding="utf-8")
    ok, _ = gui.make_snapshot()

    backup = json.loads((gui_env / "comments_backup.json").read_text(encoding="utf-8"))
    bc = backup["comments"]
    assert set(bc) == {"1", "2", "3"}          # 追加式：新评论并入、永不丢条目
    assert bc["1"]["deleted"] is True          # 快照时的状态保留
    assert bc["2"]["deleted"] is False         # 冻结：快照后的删除不改快照


def test_make_snapshot_missing_live(gui_env):
    ok, err = gui.make_snapshot()
    assert ok is False and "不存在" in err


# ---------- build_view：实时视图只含实时数据 ----------

def test_build_view_returns_live_only(gui_env):
    """实时视图只含实时数据条目（已删条目拉取时清除；全量历史看快照视图）。"""
    backup = {"comments": {"1": make_comment(1, deleted=False),
                           "2": make_comment(2, deleted=False)}}
    (gui_env / "comments_backup.json").write_text(json.dumps(backup), encoding="utf-8")
    live = {"uid": "u", "comments": {"1": make_comment(1, deleted=True),
                                     "3": make_comment(3)}}
    (gui_env / "my_comments.json").write_text(json.dumps(live), encoding="utf-8")

    items, live_out = gui.build_view()
    by_rpid = {c["rpid"]: c for c in items}
    assert set(by_rpid) == {1, 3}              # 快照独有的条目 2 不进实时视图
    assert by_rpid[1]["deleted"] is True       # 实时标记原样
    assert by_rpid[3]["deleted"] is False
    assert live_out is not None


def test_build_full_view_derives_status(gui_env):
    """全量视图派生：档案条目不在实时→已删；在实时→实时状态。"""
    live = {2: make_comment(2), 3: make_comment(3, keep=True)}
    backup = {1: make_comment(1), 2: make_comment(2), 3: make_comment(3)}
    items = gui.build_full_view(live, backup)
    by = {c["rpid"]: c for c in items}
    assert by[1]["deleted"] is True            # 不在实时 → 已删（清除语义）
    assert by[2]["deleted"] is False           # 在实时 → 实时状态
    assert by[3]["keep"] is True
    assert len(items) == 3


def test_archived_deleted_count(gui_env):
    """台账已删数＝档案中不在实时里的条目数。"""
    live = {2: make_comment(2)}
    backup = {1: make_comment(1), 2: make_comment(2), 4: make_comment(4)}
    assert gui.archived_deleted_count(live, backup) == 2    # 1 和 4
    assert gui.archived_deleted_count(None, None) == 0


# ---------- parse_progress ----------

def test_parse_progress_delete_and_fetch(gui_env):
    lines = [
        "[2026-10-09 00:00:00] 登录校验通过：测试（uid=1）",
        "[2026-10-09 00:00:01] AICU 索引到你的评论共 100 条，开始分页抓取…",
        "[2026-10-09 00:00:02] 第 3 页：5 条，累计 15 条",
        "[2026-10-09 00:00:03] [7/50] 已删除 rpid=1 视频 oid=1｜内容",
    ]
    (gui_env / "cleaner_log.txt").write_text("\n".join(lines), encoding="utf-8")
    prog = gui.parse_progress()
    assert prog["delete"]["i"] == 7 and prog["delete"]["n"] == 50
    assert prog["delete"]["running"] is False          # pid 文件不存在 → 不在运行
    assert prog["fetch"]["page"] == 3
    assert prog["fetch"]["count"] == 15
    assert prog["fetch"]["total"] == 100
    assert prog["fetch"]["running"] is False
    assert prog["last_line"].endswith("内容")


def test_parse_progress_ignores_stale_task_lines(gui_env):
    """旧任务的日志行不污染当前进度：登录校验行重置全部进度状态。"""
    lines = [
        "[2026-10-09 00:00:00] 登录校验通过：旧任务（uid=1）",
        "[2026-10-09 00:00:01] AICU 索引到你的评论共 1722 条，开始分页抓取…",
        "[2026-10-09 00:00:02] 第 100 页：5 条，累计 500 条",
        "[2026-10-09 00:00:03] 抓取完成：共 500 条",
        "[2026-10-09 00:00:04] [7/50] 已删除 rpid=1 视频 oid=1｜内容",
        "[2026-10-09 00:00:05] 登录校验通过：新任务（uid=1）",
        "[2026-10-09 00:00:06] 增量拉取：只找新评论，翻到已知区域即停",
        "[2026-10-09 00:00:07] 第 1 页：5 条，累计 5 条",
    ]
    (gui_env / "cleaner_log.txt").write_text("\n".join(lines), encoding="utf-8")
    prog = gui.parse_progress()
    f = prog["fetch"]
    assert f["total"] is None           # 旧全量的 1722 不再当进度分母
    assert f["page"] == 1 and f["count"] == 5
    assert f["done"] is False           # 旧的完成标记也被重置
    assert prog["delete"]["i"] is None  # 旧删除进度不残留


def test_parse_progress_fetch_done_and_reset(gui_env):
    base = [
        "[2026-10-09 00:00:00] AICU 索引到你的评论共 10 条，开始分页抓取…",
        "[2026-10-09 00:00:02] 抓取完成：共 10 条（跳过缺 dyn 字段 0 条）",
        "[2026-10-09 00:01:00] 已保存 10 条到 my_comments.json",
    ]
    (gui_env / "cleaner_log.txt").write_text("\n".join(base), encoding="utf-8")
    prog = gui.parse_progress()
    assert prog["fetch"]["done"] is True              # 完成标记
    assert prog["fetch"]["total"] == 10

    # 新一轮抓取开始 → 完成标记作废
    lines = base + [
        "[2026-10-09 00:05:00] AICU 索引到你的评论共 12 条，开始分页抓取…",
        "[2026-10-09 00:05:02] 第 1 页：5 条，累计 5 条",
    ]
    (gui_env / "cleaner_log.txt").write_text("\n".join(lines), encoding="utf-8")
    prog = gui.parse_progress()
    assert prog["fetch"]["done"] is False
    assert prog["fetch"]["total"] == 12
    assert prog["fetch"]["count"] == 5


def test_parse_progress_aborted_fetch_counts_as_done(gui_env):
    lines = [
        "[2026-10-09 00:00:00] AICU 索引到你的评论共 10 条，开始分页抓取…",
        "[2026-10-09 00:00:30] AICU 多轮尝试均失败，提前结束抓取（已抓到的数据不受影响）",
    ]
    (gui_env / "cleaner_log.txt").write_text("\n".join(lines), encoding="utf-8")
    prog = gui.parse_progress()
    assert prog["fetch"]["done"] is True


# ---------- spawn：stderr 并入日志 ----------

def test_spawn_redirects_stderr_to_log(gui_env):
    cmd = [sys.executable, "-c", "import sys; sys.exit('退出原因标记XYZ')"]
    proc = gui.spawn(cmd, gui.DEL_PID)
    proc.wait(timeout=30)
    log = (gui_env / "cleaner_log.txt").read_text(encoding="utf-8")
    assert "退出原因标记XYZ" in log
    assert (gui_env / "gui_delete.pid").read_text(encoding="utf-8").strip() \
        == str(proc.pid)
