import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bili_comment_gui as gui       # noqa: E402
import bili_comment_manager as mgr   # noqa: E402

SOURCES = ("bili_comment_manager.py", "bili_comment_gui.py")


@pytest.fixture
def mgr_env(tmp_path, monkeypatch):
    """manager 的文件路径全部指向 tmp：测试不碰真实数据/日志/锁。"""
    monkeypatch.setattr(mgr, "SCRIPT_DIR", tmp_path)
    monkeypatch.setattr(mgr, "LOG_FILE", tmp_path / "cleaner_log.txt")
    monkeypatch.setattr(mgr, "DATA_FILE", tmp_path / "my_comments.json")
    monkeypatch.setattr(mgr, "BACKUP_FILE", tmp_path / "comments_backup.json")
    monkeypatch.setattr(mgr, "COOKIE_FILE", tmp_path / "cookie.txt")
    return tmp_path


@pytest.fixture
def gui_env(tmp_path, monkeypatch):
    """GUI 的文件路径全部指向 tmp。"""
    monkeypatch.setattr(gui, "SCRIPT_DIR", tmp_path)
    monkeypatch.setattr(gui, "LIVE", tmp_path / "my_comments.json")
    monkeypatch.setattr(gui, "BACKUP", tmp_path / "comments_backup.json")
    monkeypatch.setattr(gui, "LOG", tmp_path / "cleaner_log.txt")
    monkeypatch.setattr(gui, "DEL_PID", tmp_path / "gui_delete.pid")
    monkeypatch.setattr(gui, "FETCH_PID", tmp_path / "gui_fetch.pid")
    monkeypatch.setattr(gui, "LOGIN_PID", tmp_path / "gui_login.pid")
    return tmp_path


@pytest.fixture
def app_dir(tmp_path):
    """独立源码副本：子进程的 SCRIPT_DIR 落在 tmp，与真实文件完全隔离。"""
    app = tmp_path / "app"
    app.mkdir()
    for name in SOURCES:
        shutil.copy2(PROJECT_ROOT / name, app / name)
    return app


def _read_log(app):
    log = app / "cleaner_log.txt"
    return log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.fixture
def run_app(app_dir):
    """以命令行方式运行独立副本的 manager，返回 (CompletedProcess, 日志文本)。"""
    def _run(args, timeout=90):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run(
            [sys.executable, str(app_dir / "bili_comment_manager.py"), *args],
            capture_output=True, text=True, timeout=timeout, cwd=str(app_dir),
            encoding="utf-8", errors="replace", env=env)
        return r, _read_log(app_dir)
    return _run


@pytest.fixture
def run_snippet(app_dir):
    """在独立副本目录里执行任意代码片段，返回 (CompletedProcess, 日志文本)。"""
    def _run(code, timeout=60):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout, cwd=str(app_dir),
            encoding="utf-8", errors="replace", env=env)
        return r, _read_log(app_dir)
    return _run
