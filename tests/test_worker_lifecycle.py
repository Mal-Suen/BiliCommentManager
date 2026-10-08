"""worker 生命周期：run_main 退出兜底（消息落日志）、缺 requests 落日志。"""

import os
import subprocess
import sys
from pathlib import Path


def test_run_main_logs_string_exit(run_app):
    r, log = run_app(["list"])          # 无数据文件 → SystemExit("清单为空…")
    assert r.returncode == 1
    assert "任务退出：清单为空，请先运行 fetch" in log


def test_run_main_preserves_int_exit_code(run_app):
    r, _ = run_app(["list", "--head", "notanumber"])   # argparse 错误 → exit 2
    assert r.returncode == 2


def test_run_main_logs_uncaught_exception(run_snippet):
    code = (
        "import bili_comment_manager as m\n"
        "def boom():\n"
        "    raise ValueError('boom-marker')\n"
        "m.main = boom\n"
        "m.run_main()\n"
    )
    r, log = run_snippet(code)
    assert r.returncode == 1
    assert "任务发生未捕获异常" in log
    assert "boom-marker" in log


def test_run_main_logs_keyboard_interrupt(run_snippet):
    code = (
        "import bili_comment_manager as m\n"
        "def interrupted():\n"
        "    raise KeyboardInterrupt\n"
        "m.main = interrupted\n"
        "m.run_main()\n"
    )
    r, log = run_snippet(code)
    assert r.returncode == 130
    assert "收到中断" in log


def test_worker_refuses_when_lock_held(app_dir, run_snippet):
    """GUI 误判双拉起时，第二个 worker 被文件锁拦下且原因经 run_main 落日志。"""
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import bili_comment_manager as m, time; "
         "assert m.acquire_task_lock('delete'); "
         "print('locked', flush=True); time.sleep(20)"],
        cwd=str(app_dir), stdout=subprocess.PIPE,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    try:
        assert child.stdout.readline().decode().strip() == "locked"
        r, log = run_snippet(
            "import bili_comment_manager as m\n"
            "def fake_main():\n"
            "    m.refuse_if_task_running('delete', '删除')\n"
            "m.main = fake_main\n"
            "m.run_main()\n")
        assert r.returncode == 1
        assert "文件锁占用" in log
    finally:
        child.kill()
        child.wait(timeout=10)


def test_missing_requests_logs_reason(app_dir):
    """缺 requests 的解释器跑 worker：退出原因必须落日志（无声死亡修复）。"""
    candidates = [Path(r"C:\Program Files\Python313\python.exe")]
    py = next((c for c in candidates if c.exists()), None)
    if py is None:
        import pytest
        pytest.skip("本机找不到缺 requests 的解释器，跳过")
    probe = subprocess.run([str(py), "-c", "import requests"], capture_output=True)
    if probe.returncode == 0:
        import pytest
        pytest.skip("候选解释器装有 requests，无法复现缺库场景")
    r = subprocess.run([str(py), str(app_dir / "bili_comment_manager.py"), "list"],
                       capture_output=True, cwd=str(app_dir), timeout=60)
    log = (app_dir / "cleaner_log.txt").read_text(encoding="utf-8")
    assert r.returncode != 0
    assert "任务无法启动" in log
    assert "缺少 requests" in log
    assert "Python313" in log
