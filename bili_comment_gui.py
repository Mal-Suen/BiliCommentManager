#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站评论管理器 - 图形界面（本地 Web UI）

架构
----
- GUI 只读写本地文件，不直接做网络操作：
  · my_comments.json     源文件，删除进程每删一条实时回写（进度来源）
  · comments_backup.json 全量快照，展示基准（不随删除变化）
  · cleaner_log.txt      任务日志，解析「[i/N] 已删除」取删除进度
- 长任务（delete / fetch）由 GUI 拉起为独立后台进程（DETACHED），
  关闭 GUI 或浏览器不影响任务；GUI 轮询文件实时显示进度。
- 删除进程由 bili_comment_manager.py 执行（低频 5-12 秒/条 + 风控中止）。

启动
----
python bili_comment_gui.py
（自动打开浏览器 http://127.0.0.1:8765/ ，Ctrl+C 退出 GUI）
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 冻结（PyInstaller exe）模式下数据文件放 exe 旁边；源码模式放脚本旁边
FROZEN = bool(getattr(sys, "frozen", False))
SCRIPT_DIR = (Path(sys.executable).resolve().parent if FROZEN
              else Path(__file__).resolve().parent)
PY = sys.executable
MANAGER = SCRIPT_DIR / "bili_comment_manager.py"
# 冻结模式下没有独立 python 可拉起：让 exe 以 --worker 模式自调用执行任务
TASK_PREFIX = [PY, "--worker"] if FROZEN else [PY, str(MANAGER)]
LIVE = SCRIPT_DIR / "my_comments.json"
BACKUP = SCRIPT_DIR / "comments_backup.json"
LOG = SCRIPT_DIR / "cleaner_log.txt"
DEL_PID = SCRIPT_DIR / "gui_delete.pid"
FETCH_PID = SCRIPT_DIR / "gui_fetch.pid"
LOGIN_PID = SCRIPT_DIR / "gui_login.pid"
GUI_PID = SCRIPT_DIR / "gui.pid"

TYPE_NAMES = {1: "视频", 11: "带图动态", 12: "专栏", 17: "动态"}
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 源码模式下 worker 与 GUI 用同一个解释器：requests 缺失时界面一切正常，
# 但「登录/拉取/删除」任务会秒死。启动时探测一次，页面顶部红条警示
# （exe 自带依赖，无需检查）
WORKER_WARNING = None
if not FROZEN:
    try:
        import importlib.util
        if importlib.util.find_spec("requests") is None:
            WORKER_WARNING = (f"当前 Python（{sys.executable}）缺少 requests 库："
                              "界面可用，但扫码登录 / 重新拉取 / 开始删除会立即失败。"
                              "请改用 BiliCommentManager.exe，"
                              "或给该 Python 执行 pip install requests")
    except Exception:
        pass

file_lock = threading.Lock()
delete_proc = None
fetch_proc = None
login_proc = None


# ---------- 文件读写 ----------

def load_json(path):
    # worker 每删一条就原子替换数据文件，读取撞上替换的瞬间会共享冲突，短暂重试即可
    for attempt in range(4):
        if not path.exists():
            return None
        try:
            with file_lock:
                raw = json.loads(path.read_text(encoding="utf-8"))
            return {int(k): v for k, v in raw.get("comments", {}).items()}
        except Exception:
            if attempt == 3:
                return None
            time.sleep(0.05)
    return None


def replace_with_retry(tmp, dst, tries=10):
    """Windows：并发读 dst 时 replace 报 PermissionError（共享冲突），退避重试。"""
    for attempt in range(tries):
        try:
            tmp.replace(dst)
            return
        except PermissionError:
            time.sleep(0.05 * (2 ** min(attempt, 5)))
    tmp.replace(dst)


def save_live(comments, uid):
    with file_lock:
        complete = None
        try:
            complete = json.loads(LIVE.read_text(encoding="utf-8")).get("complete")
        except Exception:
            pass
        # tmp 带 pid：防止与 worker 或其他 GUI 进程的写盘共用同一 tmp 互相覆盖
        tmp = LIVE.with_name(f"{LIVE.stem}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(
            {"uid": uid, "fetched_at": datetime.now().isoformat(timespec="seconds"),
             "complete": bool(complete),
             "comments": {str(k): v for k, v in comments.items()}},
            ensure_ascii=False, indent=1), encoding="utf-8")
        replace_with_retry(tmp, LIVE)


def live_complete():
    try:
        return bool(json.loads(LIVE.read_text(encoding="utf-8")).get("complete"))
    except Exception:
        return False


def make_snapshot():
    """追加式快照：已有条目状态冻结（不随删除变化），只并入新抓到的评论。"""
    with file_lock:
        if not LIVE.exists():
            return False, "my_comments.json 不存在，请先运行 fetch"
        live = None
        for attempt in range(4):  # 删除任务进行中会周期性替换该文件，撞上就重试
            try:
                live = json.loads(LIVE.read_text(encoding="utf-8"))
                break
            except Exception:
                if attempt == 3:
                    return False, "读取 my_comments.json 失败"
                time.sleep(0.05)
        live_comments = live.get("comments", {})
        backup = {}
        if BACKUP.exists():
            try:
                backup = json.loads(BACKUP.read_text(encoding="utf-8")).get("comments", {})
            except Exception:
                backup = {}
        merged = dict(backup)
        for k, v in live_comments.items():
            if k not in merged:
                merged[k] = v
        tmp = BACKUP.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"uid": live.get("uid"), "comments": merged},
            ensure_ascii=False, indent=1), encoding="utf-8")
        replace_with_retry(tmp, BACKUP)
        return True, None


def build_view():
    # 实时数据只留现存评论（拉取时清除已删条目）；全量历史看 comments_backup.json
    live = load_json(LIVE)
    items = [dict(c) for c in (live or {}).values()]
    items.sort(key=lambda c: c.get("time", 0))
    return items, live


# ---------- 进程管理 ----------

# 进程存活探测的两个坑：
# 1) OpenProcess 默认 restype=c_int 会截断 64 位句柄——必须显式 c_void_p；
# 2) WaitForSingleObject 需要 SYNCHRONIZE 权限，只开 QUERY 会 ERROR_ACCESS_DENIED
#    （Wait 返回 WAIT_FAILED，活进程被误判为已退出——进度不显示、重复拉起任务的根因）
_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
_kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint),
                ("dwHighDateTime", ctypes.c_uint)]


def win_proc_alive(pid, not_before=None):
    """not_before 传 pid 文件的写入时间：进程创建时间晚于它 5 秒以上，
    说明 pid 已被无关进程复用（原 worker 已死）——避免幻影任务与误杀。"""
    try:
        h = _kernel32.OpenProcess(0x00100000 | 0x1000, False, int(pid))  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            if _kernel32.WaitForSingleObject(h, 0) != 0x102:  # WAIT_TIMEOUT = 仍在运行
                return False
            if not_before is not None:
                creation, exit_t, kernel, user = (_FILETIME(), _FILETIME(),
                                                  _FILETIME(), _FILETIME())
                if _kernel32.GetProcessTimes(h, ctypes.byref(creation),
                                             ctypes.byref(exit_t),
                                             ctypes.byref(kernel),
                                             ctypes.byref(user)):
                    ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                    started = ft / 10_000_000 - 11644473600  # Unix 秒
                    if started > not_before + 5:
                        return False
            return True
        finally:
            _kernel32.CloseHandle(h)
    except Exception:
        return False


def read_pid(path):
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pidfile_mtime(path):
    try:
        return path.stat().st_mtime
    except Exception:
        return None


def task_alive(handle, pidfile):
    global delete_proc, fetch_proc
    if handle is not None and handle.poll() is None:
        return True
    pid = read_pid(pidfile)
    return pid is not None and win_proc_alive(pid, not_before=_pidfile_mtime(pidfile))


def tail_log(n=800):
    # 800 行：全量抓取约 344 页，窗口太小会把开头的「索引到共 N 条」滚出去导致百分比消失
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        return lines[-n:]
    except Exception:
        return []


def parse_progress():
    """从日志尾部解析删除/拉取/登录进度。"""
    d_run, d_i, d_n, d_done = False, None, None, None
    f_run, f_page, f_count, f_total, f_done = False, None, None, None, False
    l_run = False
    last_line = ""
    import re
    for line in tail_log():
        last_line = line if line else last_line
        m = re.search(r"\[(\d+)/(\d+)\] 已删除", line)
        if m:
            d_i, d_n = int(m.group(1)), int(m.group(2))
        m = re.search(r"本轮结束：成功删除 (\d+) 条", line)
        if m:
            d_done = int(m.group(1))
        m = re.search(r"第 (\d+) 页：\d+ 条，累计 (\d+) 条", line)
        if m:
            f_page, f_count = int(m.group(1)), int(m.group(2))
            f_done = False  # 新一页出现则此前的完成标记作废
        m = re.search(r"索引到你的评论共 (\d+) 条", line)
        if m:
            f_total = int(m.group(1))
        if re.search(r"抓取完成|提前结束抓取", line):
            f_done = True
    d_run = task_alive(delete_proc, DEL_PID)
    f_run = task_alive(fetch_proc, FETCH_PID)
    l_run = task_alive(login_proc, LOGIN_PID)
    return {
        "delete": {"running": d_run, "i": d_i, "n": d_n, "done": d_done},
        "fetch": {"running": f_run, "page": f_page, "count": f_count,
                  "total": f_total, "done": f_done},
        "login": {"running": l_run},
        "last_line": last_line,
    }


def spawn(cmd, pidfile):
    global delete_proc, fetch_proc
    # 剥离 _MEIPASS2：exe 自调用时不再复用 GUI 的临时解包目录，
    # 关闭 GUI 窗口不会连带清掉 worker 正在使用的文件
    env = {k: v for k, v in os.environ.items() if k != "_MEIPASS2"}
    env["PYTHONIOENCODING"] = "utf-8"  # worker 的 stderr 将并入 UTF-8 日志
    # worker 无控制台：stderr 并入任务日志，退出消息/堆栈才能被界面看到
    err = open(LOG, "ab")
    try:
        proc = subprocess.Popen(
            cmd, creationflags=DETACHED, env=env,
            stdout=subprocess.DEVNULL, stderr=err,
            stdin=subprocess.DEVNULL, cwd=str(SCRIPT_DIR))
    finally:
        err.close()
    pidfile.write_text(str(proc.pid), encoding="utf-8")
    return proc


# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/api/data"):
            if "view=full" in self.path:
                backup = load_json(BACKUP) if BACKUP.exists() else None
                items = [dict(c) for c in (backup or {}).values()]
                items.sort(key=lambda c: c.get("time", 0))
                deleted = sum(1 for c in items if c.get("deleted"))
                kept = sum(1 for c in items if c.get("keep") and not c.get("deleted"))
                self._json({
                    "comments": items, "full": True,
                    "worker_warning": WORKER_WARNING,
                    "stats": {
                        "total": len(items), "deleted": deleted, "kept": kept,
                        "pending": len(items) - deleted - kept,
                        "oldest": datetime.fromtimestamp(items[0]["time"]).strftime("%Y-%m-%d") if items and items[0].get("time") else None,
                        "newest": datetime.fromtimestamp(items[-1]["time"]).strftime("%Y-%m-%d") if items and items[-1].get("time") else None,
                        "backup_at": datetime.fromtimestamp(BACKUP.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if BACKUP.exists() else None,
                        "has_live": LIVE.exists(),
                    },
                })
                return
            items, live = build_view()
            deleted = sum(1 for c in items if c.get("deleted"))
            kept = sum(1 for c in items if c.get("keep") and not c.get("deleted"))
            failed = sum(1 for c in items if c.get("error") and not c.get("deleted"))
            stats = {
                "total": len(items),
                "deleted": deleted,
                "kept": kept,
                "failed": failed,
                "pending": len(items) - deleted - kept,
                "oldest": datetime.fromtimestamp(items[0]["time"]).strftime("%Y-%m-%d") if items and items[0].get("time") else None,
                "newest": datetime.fromtimestamp(items[-1]["time"]).strftime("%Y-%m-%d") if items and items[-1].get("time") else None,
                "backup_at": datetime.fromtimestamp(BACKUP.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if BACKUP.exists() else None,
                "has_live": live is not None,
            }
            self._json({"comments": items, "stats": stats,
                        "worker_warning": WORKER_WARNING})
        elif self.path == "/api/progress":
            items, _ = build_view()
            deleted = sum(1 for c in items if c.get("deleted"))
            kept = sum(1 for c in items if c.get("keep") and not c.get("deleted"))
            prog = parse_progress()
            self._json({
                "deleted": deleted, "kept": kept,
                "pending": len(items) - deleted - kept,
                "delete": prog["delete"], "fetch": prog["fetch"],
                "login": prog["login"],
                "last_line": prog["last_line"],
                "worker_warning": WORKER_WARNING,
            })
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        global delete_proc, fetch_proc, login_proc
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}

        if self.path == "/api/keep":
            if task_alive(delete_proc, DEL_PID):
                self._json({"ok": False, "error": "删除任务进行中，暂不能修改保留标记"})
                return
            if task_alive(fetch_proc, FETCH_PID):
                self._json({"ok": False, "error": "拉取任务进行中，暂不能修改保留标记"})
                return
            live = load_json(LIVE)
            if live is None:
                self._json({"ok": False, "error": "数据文件不存在"})
                return
            rpid, value = int(body.get("rpid", 0)), bool(body.get("value", True))
            c = live.get(rpid)
            if c is None:
                self._json({"ok": False, "error": f"未找到 rpid={rpid}"})
                return
            c["keep"] = value
            uid = None
            if LIVE.exists():
                try:
                    uid = json.loads(LIVE.read_text(encoding="utf-8")).get("uid")
                except Exception:
                    pass
            save_live(live, uid)
            self._json({"ok": True})

        elif self.path == "/api/snapshot":
            ok, err = make_snapshot()
            self._json({"ok": ok, "error": err})

        elif self.path == "/api/delete":
            if task_alive(delete_proc, DEL_PID):
                self._json({"ok": False, "error": "已有删除任务在运行"})
                return
            if task_alive(fetch_proc, FETCH_PID):
                self._json({"ok": False, "error": "拉取任务进行中，请等它完成后再删除"
                                                 "（两者同时写数据文件会互相冲突）"})
                return
            if WORKER_WARNING:
                self._json({"ok": False, "error": WORKER_WARNING})
                return
            if not LIVE.exists():
                self._json({"ok": False, "error": "数据文件不存在，请先拉取"})
                return
            args = TASK_PREFIX + ["delete", "--yes"]
            if body.get("from"):
                args += ["--after", str(body["from"])]
            if body.get("to"):
                try:
                    day_after = (datetime.strptime(str(body["to"]), "%Y-%m-%d")
                                 + timedelta(days=1)).strftime("%Y-%m-%d")
                    args += ["--before", day_after]
                except ValueError:
                    self._json({"ok": False, "error": "日期格式无效"})
                    return
            if body.get("type") and body["type"] != "all":
                args += ["--type", str(body["type"])]
            if body.get("keyword"):
                args += ["--keyword", str(body["keyword"])]
            delete_proc = spawn(args, DEL_PID)
            self._json({"ok": True, "pid": delete_proc.pid})

        elif self.path == "/api/stop":
            stopped = []
            pid = read_pid(DEL_PID)
            if delete_proc is not None and delete_proc.poll() is None:
                pid = delete_proc.pid
            if pid is not None and win_proc_alive(pid, not_before=_pidfile_mtime(DEL_PID)):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
                stopped.append("删除")
            pid = read_pid(FETCH_PID)
            if fetch_proc is not None and fetch_proc.poll() is None:
                pid = fetch_proc.pid
            if pid is not None and win_proc_alive(pid, not_before=_pidfile_mtime(FETCH_PID)):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
                stopped.append("拉取")
            if stopped:
                self._json({"ok": True, "stopped": "、".join(stopped)})
            else:
                self._json({"ok": False, "error": "没有运行中的任务"})

        elif self.path == "/api/fetch":
            if task_alive(fetch_proc, FETCH_PID):
                self._json({"ok": False, "error": "已有拉取任务在运行"})
                return
            if task_alive(delete_proc, DEL_PID):
                self._json({"ok": False, "error": "删除任务进行中，请等它完成后再拉取"
                                                 "（两者同时写数据文件会互相冲突）"})
                return
            if WORKER_WARNING:
                self._json({"ok": False, "error": WORKER_WARNING})
                return
            args = TASK_PREFIX + ["fetch"]
            fetch_proc = spawn(args, FETCH_PID)
            self._json({"ok": True, "pid": fetch_proc.pid,
                        "incremental": LIVE.exists() and live_complete()})

        elif self.path == "/api/login":
            if task_alive(login_proc, LOGIN_PID):
                self._json({"ok": False, "error": "已有登录任务在运行"})
                return
            if WORKER_WARNING:
                self._json({"ok": False, "error": WORKER_WARNING})
                return
            args = TASK_PREFIX + ["login"]  # 默认自动弹出二维码图片
            login_proc = spawn(args, LOGIN_PID)
            self._json({"ok": True, "pid": login_proc.pid})

        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, *a):
        pass


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>B 站评论管理器</title>
<style>
:root{--bg:#0f1115;--panel:#161a22;--card:#1c212c;--border:#2a303c;
--text:#e8eaf0;--muted:#8b93a3;--accent:#4f8cff;--green:#3fb96f;
--red:#e5484d;--yellow:#e0b93e;--radius:14px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif;padding-bottom:88px}
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
padding:18px 24px;border-bottom:1px solid var(--border)}
header h1{font-size:18px;font-weight:600}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{background:var(--card);border:1px solid var(--border);border-radius:999px;
padding:3px 12px;font-size:12.5px;color:var(--muted)}
.chip b{color:var(--text);font-weight:600;margin-left:4px}
.hbtns{margin-left:auto;display:flex;gap:8px}
button{background:var(--card);color:var(--text);border:1px solid var(--border);
border-radius:8px;padding:6px 14px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--accent)}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{background:transparent;border-color:var(--red);color:var(--red)}
.filters{display:flex;gap:14px;flex-wrap:wrap;align-items:center;
padding:14px 24px;border-bottom:1px solid var(--border)}
.filters label{font-size:12.5px;color:var(--muted);display:flex;
gap:6px;align-items:center}
input,select{background:var(--card);border:1px solid var(--border);
border-radius:8px;color:var(--text);padding:5px 10px;font-size:13px}
input[type=date]{color-scheme:dark}
main{padding:16px 24px}
table{width:100%;border-collapse:collapse}
th{font-size:12px;color:var(--muted);text-align:left;font-weight:500;
padding:8px 10px;border-bottom:1px solid var(--border);position:sticky;top:0;
background:var(--bg)}
td{padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top}
tr:hover td{background:rgba(79,140,255,.05)}
.t-time{white-space:nowrap;color:var(--muted);font-size:12.5px}
.t-msg{max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td .del{color:var(--muted);text-decoration:line-through}
.badge{display:inline-block;border-radius:6px;padding:1px 8px;font-size:11.5px}
.b-type{background:rgba(79,140,255,.12);color:var(--accent)}
.b-pending{background:rgba(139,147,163,.15);color:var(--muted)}
.b-kept{background:rgba(224,185,62,.15);color:var(--yellow)}
.b-deleted{background:rgba(63,185,111,.15);color:var(--green)}
.b-failed{background:rgba(229,72,77,.15);color:var(--red)}
.rowact{white-space:nowrap;display:flex;gap:8px;align-items:center}
.rowact a{color:var(--accent);text-decoration:none;font-size:12.5px}
.kbtn{padding:2px 10px;font-size:12px}
.pager{display:flex;gap:10px;align-items:center;justify-content:center;
padding:16px 0;color:var(--muted);font-size:13px}
#bar{position:fixed;left:0;right:0;bottom:0;background:var(--panel);
border-top:1px solid var(--border);padding:12px 24px;display:flex;
gap:14px;align-items:center;flex-wrap:wrap;z-index:10}
#bar-info{color:var(--muted);font-size:13px}
#bar-info b{color:var(--text)}
#prog{flex:1;min-width:180px;height:8px;background:var(--card);
border-radius:99px;overflow:hidden}
#prog-fill{height:100%;width:0;background:var(--accent);transition:width .5s}
#prog-text{font-size:12.5px;color:var(--muted);max-width:46%;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.modal-mask{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:20;
display:flex;align-items:center;justify-content:center}
.modal{background:var(--panel);border:1px solid var(--border);
border-radius:var(--radius);padding:24px;width:420px}
.modal h3{font-size:16px;margin-bottom:12px}
.modal p{color:var(--muted);font-size:13px;margin-bottom:8px}
.modal p b{color:var(--text)}
.modal .acts{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
#toast{position:fixed;top:18px;left:50%;transform:translateX(-50%);
background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:9px 18px;font-size:13px;z-index:30;display:none}
#view-banner{margin:0 24px;padding:9px 14px;border:1px solid rgba(224,185,62,.35);
background:rgba(224,185,62,.08);border-radius:8px;color:var(--yellow);font-size:12.5px}
#env-banner{margin:10px 24px 0;padding:9px 14px;border:1px solid rgba(229,72,77,.4);
background:rgba(229,72,77,.08);border-radius:8px;color:var(--red);font-size:12.5px}
#fail-banner{margin:10px 24px 0;padding:9px 14px;border:1px solid rgba(229,72,77,.4);
background:rgba(229,72,77,.08);border-radius:8px;color:var(--red);font-size:12.5px;
display:flex;gap:10px;align-items:flex-start}
#fail-text{flex:1;word-break:break-all}
#fail-close{cursor:pointer;font-size:16px;line-height:1.2;flex-shrink:0}
.empty{padding:60px 24px;text-align:center;color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>B 站评论管理器</h1>
  <div class="chips" id="stats"></div>
  <div class="hbtns">
    <button id="btn-login" title="手机 B 站 App 扫码，二维码图片会自动弹出">扫码登录</button>
    <button id="btn-fetch" title="重新拉取：本地清单完整时增量补新评论（几分钟）；首次或上次中断则全量拉档案。已删条目会从实时数据清除（历史在全量快照可回看）">重新拉取</button>
    <button id="btn-full" title="查看全量评论快照：完整清单，不随删除进度变化——删除后仍可回看全部历史评论">查看全量评论</button>
  </div>
</header>

<div id="env-banner" style="display:none"></div>
<div id="fail-banner" style="display:none"><span id="fail-text"></span><span id="fail-close" title="关闭">×</span></div>

<section class="filters">
  <label>从 <input type="date" id="f-from"></label>
  <label>到 <input type="date" id="f-to"></label>
  <label>类型
    <select id="f-type">
      <option value="all">全部</option><option value="1">视频</option>
      <option value="17">动态</option><option value="11">带图动态</option>
      <option value="12">专栏</option>
    </select></label>
  <label>关键词 <input id="f-kw" placeholder="内容包含" size="12"></label>
  <label>状态
    <select id="f-status">
      <option value="all">全部</option><option value="pending">待删</option>
      <option value="kept">保留</option><option value="deleted">已删</option>
      <option value="failed">失败</option>
    </select></label>
  <label>排序
    <select id="f-sort"><option value="asc">旧→新</option>
    <option value="desc">新→旧</option></select></label>
</section>

<main><div id="view-banner" style="display:none"></div><div id="table-wrap"></div><div class="pager" id="pager"></div></main>

<div id="bar">
  <span id="bar-info">加载中…</span>
  <button class="primary" id="btn-del">开始删除（当前筛选）</button>
  <button class="danger" id="btn-stop" style="display:none">停止删除</button>
  <div id="prog" style="display:none"><div id="prog-fill"></div></div>
  <span id="prog-text"></span>
</div>

<div id="toast"></div>
<div id="modal-root"></div>

<script>
const PER = 100;
let DATA = [], page = 1, lastSig = '', deleting = false, fetching = false, fullView = false;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ftime = ts => ts ? new Date(ts * 1000).toLocaleString('zh-CN',
  {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',
   minute:'2-digit',hour12:false}) : '时间未知';
const TYPEN = {1:'视频',11:'带图动态',12:'专栏',17:'动态'};
function linkOf(c){
  if(c.type===1) return 'https://www.bilibili.com/video/av'+c.oid;
  if(c.type===12) return 'https://www.bilibili.com/read/cv'+c.oid;
  if(c.type===11) return 'https://h.bilibili.com/ywh/'+c.oid;
  return 'https://t.bilibili.com/'+c.oid;
}
function statusOf(c){
  if(c.deleted) return 'deleted';
  if(c.keep) return 'kept';
  return c.error ? 'failed' : 'pending';
}

function toast(msg, ms=2600){
  const t = $('toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(()=>t.style.display='none', ms);
}

function showEnv(w){
  const b = $('env-banner');
  if(w){ b.style.display = ''; b.textContent = w; }
  else b.style.display = 'none';
}

// 任务失败原因常驻红条（6 秒 toast 会消失，这里保留到用户关闭或新任务开始）
const FAIL_RE = /任务终止|任务无法启动|未捕获异常|文件锁占用|检测到另一个任务/;
function showFail(line){
  const b = $('fail-banner');
  if(line && FAIL_RE.test(line)){
    $('fail-text').textContent = line;
    b.style.display = 'flex';
  } else {
    b.style.display = 'none';
  }
}
$('fail-close').onclick = ()=>{ $('fail-banner').style.display = 'none'; };

function filtered(){
  const from = $('f-from').value, to = $('f-to').value;
  const type = $('f-type').value, kw = $('f-kw').value.trim().toLowerCase();
  const st = $('f-status').value, sort = $('f-sort').value;
  // 带 T00:00:00 的日期时间串按本地时区解析；纯日期串会被当 UTC（差 8 小时）
  const fts = from ? new Date(from + 'T00:00:00').getTime()/1000 : null;
  const tts = to ? new Date(to + 'T23:59:59').getTime()/1000 : null;
  let out = DATA.filter(c => {
    if(fts!==null && (!c.time || c.time < fts)) return false;
    if(tts!==null && (!c.time || c.time > tts)) return false;
    if(type!=='all' && String(c.type)!==type) return false;
    if(kw && !String(c.message||'').toLowerCase().includes(kw)) return false;
    if(st!=='all' && statusOf(c)!==st) return false;
    return true;
  });
  out.sort((a,b)=> sort==='asc' ? a.time-b.time : b.time-a.time);
  return out;
}

function renderStats(s){
  $('stats').innerHTML =
    `<span class="chip">总数<b>${s.total}</b></span>`+
    `<span class="chip">待删<b>${s.pending}</b></span>`+
    `<span class="chip">已删<b class"">${s.deleted}</b></span>`+
    `<span class="chip">保留<b>${s.kept}</b></span>`+
    (s.failed?`<span class="chip">失败<b>${s.failed}</b></span>`:'')+
    (s.oldest?`<span class="chip">${s.oldest} ~ ${s.newest}</span>`:'')+
    (s.backup_at?`<span class="chip">快照 ${s.backup_at}</span>`:'');
}

function renderTable(){
  const list = filtered();
  const pages = Math.max(1, Math.ceil(list.length/PER));
  if(page>pages) page = pages;
  const slice = list.slice((page-1)*PER, page*PER);
  if(!DATA.length){
    $('table-wrap').innerHTML =
      '<div class="empty">暂无数据：点右上角「扫码登录」（手机确认后 Cookie 自动写入），' +
      '再点「重新拉取」即可开始</div>';
    $('pager').innerHTML = '';
  } else if(!list.length){
    $('table-wrap').innerHTML = '<div class="empty">当前筛选没有匹配的评论</div>';
    $('pager').innerHTML = '';
  } else {
    let rows = '';
    for(const c of slice){
      const st = statusOf(c);
      const badge = {pending:'<span class="badge b-pending">待删</span>',
        kept:'<span class="badge b-kept">保留</span>',
        deleted:'<span class="badge b-deleted">已删</span>',
        failed:'<span class="badge b-failed">失败</span>'}[st];
      const reply = c.is_reply ? '（回复）' : '';
      const cls = st==='deleted' ? 'del' : '';
      const kbtn = c.keep
        ? `<button class="kbtn" onclick="toggleKeep(${c.rpid},false)">取消保留</button>`
        : `<button class="kbtn" onclick="toggleKeep(${c.rpid},true)">保留</button>`;
      rows += `<tr><td class="t-time">${ftime(c.time)}</td>`+
        `<td><span class="badge b-type">${TYPEN[c.type]||('type'+c.type)}</span></td>`+
        `<td class="t-msg ${cls}" title="${esc(c.message)}">${esc(reply+c.message)}</td>`+
        `<td>${badge}</td><td><div class="rowact">${kbtn}`+
        `<a href="${linkOf(c)}" target="_blank">打开</a></div></td></tr>`;
    }
    $('table-wrap').innerHTML =
      `<table><thead><tr><th style="width:130px">时间</th><th style="width:80px">类型`+
      `</th><th>内容</th><th style="width:64px">状态</th><th style="width:150px">操作`+
      `</th></tr></thead><tbody>${rows}</tbody></table>`;
    $('pager').innerHTML =
      `<button onclick="goPage(${page-1})" ${page<=1?'disabled':''}>上一页</button>`+
      `<span>第 ${page} / ${pages} 页（${list.length} 条）</span>`+
      `<button onclick="goPage(${page+1})" ${page>=pages?'disabled':''}>下一页</button>`;
  }
  const pend = list.filter(c=>statusOf(c)==='pending').length;
  const fr = $('f-from').value, to = $('f-to').value;
  const range = (fr||to) ? `（${fr||'最早'} ~ ${to||'最新'}）` : '';
  $('bar-info').innerHTML = `当前筛选待删 <b>${pend}</b> 条${range}`;
}

function goPage(p){ page = p; renderTable(); }

async function loadData(){
  const r = await fetch('/api/data' + (fullView ? '?view=full' : ''));
  const j = await r.json();
  showEnv(j.worker_warning);
  DATA = j.comments;
  renderStats(j.stats);
  renderTable();
  const banner = $('view-banner');
  if(fullView){
    banner.style.display = '';
    banner.textContent = '全量快照视图：完整评论清单，不随删除进度变化'
      + (j.stats.backup_at ? `（快照时间 ${j.stats.backup_at}）` : '')
      + '——点右上角「返回实时视图」查看删除进度';
  } else {
    banner.style.display = 'none';
  }
  if(!j.stats.backup_at && j.stats.total && !fullView){
    fetch('/api/snapshot', {method:'POST'});  // 首次自动快照
    toast('已自动创建全量快照');
  }
}

async function toggleKeep(rpid, value){
  if(fullView){ toast('全量视图为只读快照，请先点「返回实时视图」再操作'); return; }
  if(deleting){ toast('删除进行中，暂不能修改保留标记'); return; }
  const r = await fetch('/api/keep', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({rpid, value})});
  const j = await r.json();
  if(!j.ok){ toast(j.error||'操作失败'); return; }
  const c = DATA.find(x=>x.rpid===rpid);
  if(c){ c.keep = value; }
  renderTable();
}

$('f-from').onchange = $('f-to').onchange = $('f-type').onchange =
$('f-status').onchange = $('f-sort').onchange = ()=>{ page=1; renderTable(); };
let kwTimer;
$('f-kw').oninput = ()=>{ clearTimeout(kwTimer);
  kwTimer = setTimeout(()=>{ page=1; renderTable(); }, 300); };

$('btn-login').onclick = async ()=>{
  const r = await fetch('/api/login', {method:'POST'});
  const j = await r.json();
  toast(j.ok ? '二维码图片已弹出，用手机 B 站 App 扫码并确认登录'
             : (j.error||'失败'), 4000);
};

$('btn-full').onclick = async ()=>{
  fullView = !fullView;
  if(fullView){
    $('btn-full').textContent = '返回实时视图';
    toast('全量快照视图：完整评论清单，不随删除进度变化，删除后仍可回看全部历史', 4500);
  } else {
    $('btn-full').textContent = '查看全量评论';
    toast('已返回实时视图');
  }
  page = 1;
  await loadData();
};

$('btn-fetch').onclick = async ()=>{
  const r = await fetch('/api/fetch', {method:'POST'});
  const j = await r.json();
  if(j.ok){
    fetching = true;
    const msg = (j.incremental
      ? '拉取已开始：本地清单完整，增量补新评论（通常几分钟）——已删条目同时从实时数据清除'
      : '拉取已开始：全量拉取 AICU 档案（约每 1700 条需 1 小时）——完成后实时数据只留现存评论，已删条目清除（历史在全量快照）')
      + '。进度在底部显示，可随时停止；关闭窗口甚至关机都没关系——已抓到的不会丢';
    toast(msg, 8000);
  } else {
    toast(j.error||'失败', 6500);
  }
};

$('btn-del').onclick = ()=>{
  if(fullView){ toast('全量视图为只读快照，请先点「返回实时视图」再删除'); return; }
  const list = filtered().filter(c=>statusOf(c)==='pending');
  if(!list.length){ toast('当前筛选没有待删评论'); return; }
  if(deleting){ toast('已有删除任务在运行'); return; }
  const fr = $('f-from').value, to = $('f-to').value;
  const kw = $('f-kw').value.trim();
  const tp = $('f-type').value;
  const range = (fr||to) ? `${fr||'最早'} ~ ${to||'最新'}` : '全部时间';
  const root = $('modal-root');
  root.innerHTML = `<div class="modal-mask"><div class="modal">`+
    `<h3>确认删除</h3>`+
    `<p>时间范围：<b>${range}</b>${kw?`｜关键词：<b>${esc(kw)}</b>`:''}`+
    `${tp!=='all'?`｜类型：<b>${TYPEN[tp]||tp}</b>`:''}</p>`+
    `<p>本轮将删除 <b>${list.length}</b> 条评论，<b style="color:var(--red)">不可恢复</b>。</p>`+
    `<p>因 B 站接口限制采用低频删除（每条随机 5-12 秒、每 20 条休息 30-60 秒），`+
    `预计耗时约 <b>${Math.round(list.length*12/60)} 分钟</b>。</p>`+
    `<p>删除完成后自动逐条向 B 站核验（约每条 1 秒），未生效的会自动重新删除；核验通过的已删条目自动移出列表。</p>`+
    `<p>任务在后台独立运行：<b>关闭窗口、甚至关机都没关系</b>——已删除的不会丢，`+
    `下次点「开始删除」会自动从剩余的继续；也可以随时点「停止删除」。</p>`+
    `<div class="acts"><button onclick="closeModal()">取消</button>`+
    `<button class="danger" onclick="doDelete()">确认删除</button></div>`+
    `</div></div>`;
};

function closeModal(){ $('modal-root').innerHTML = ''; }

async function doDelete(){
  closeModal();
  const body = {from: $('f-from').value || null, to: $('f-to').value || null,
    type: $('f-type').value, keyword: $('f-kw').value.trim() || null};
  const r = await fetch('/api/delete', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)});
  const j = await r.json();
  if(!j.ok){ toast(j.error||'启动失败'); return; }
  deleting = true;
  toast('删除任务已启动');
}

$('btn-stop').onclick = async ()=>{
  const label = $('btn-stop').textContent;
  if(!confirm(`确定${label}？已完成的部分不受影响。`)) return;
  const r = await fetch('/api/stop', {method:'POST'});
  const j = await r.json();
  toast(j.ok ? `已停止${j.stopped || '任务'}` : (j.error||'失败'));
};

async function poll(){
  try{
    const r = await fetch('/api/progress');
    const j = await r.json();
    showEnv(j.worker_warning);
    const sig = j.deleted+'/'+j.kept+'/'+j.pending;
    const d = j.delete, f = j.fetch;
    if(d.running || f.running){ showFail(null); }   // 新任务开始，清掉旧失败提示
    const texts = [];
    let pct = null;
    if(d.running){
      deleting = true;
      $('btn-del').disabled = true;
      if(d.i && d.n){ pct = Math.round(d.i/d.n*100); }
      texts.push(d.i ? `删除中 ${d.i}/${d.n}（${pct}%）｜${j.last_line||''}`
                     : (j.last_line || '删除任务启动中…'));
    } else {
      if(deleting){
        deleting = false;
        toast('删除任务已结束' + (j.last_line ? '｜' + j.last_line : ''), 6000);
        showFail(j.last_line);
        loadData();
      }
      $('btn-del').disabled = false;
    }
    if(f.running){
      fetching = true;
      if(f.count && f.total && pct === null){ pct = Math.round(f.count/f.total*100); }
      let eta = null;
      if(f.count && f.total && f.count < f.total){
        eta = Math.max(1, Math.round((f.total - f.count) / 5 * 10 / 60));
      }
      texts.push(f.count ? `拉取中 ${f.count}/${f.total || '?'} 条（第 ${f.page} 页`
                         + (eta ? `，预计还需约 ${eta} 分钟` : '') + '）'
                         : (j.last_line || '拉取任务启动中…'));
    } else if(fetching){
      fetching = false;
      if(f.done){
        fetch('/api/snapshot', {method:'POST'});  // 拉取完成，新评论并入全量快照
        toast('拉取完成：新评论已并入全量快照');
        showFail(null);
      } else {
        toast('拉取已停止或未启动成功' + (j.last_line ? '｜' + j.last_line : ''), 6000);
        showFail(j.last_line);
      }
      loadData();
    }
    if(j.login && j.login.running){
      texts.push('扫码登录中…' + (j.last_line ? '｜' + j.last_line : ''));
    }
    const anyTask = d.running || f.running;
    $('btn-stop').style.display = anyTask ? '' : 'none';
    if(anyTask){
      $('btn-stop').textContent = (d.running && f.running) ? '停止全部'
        : (d.running ? '停止删除' : '停止拉取');
    }
    if(pct !== null){
      $('prog').style.display = '';
      $('prog-fill').style.width = pct+'%';
    } else {
      $('prog').style.display = 'none';
    }
    $('prog-text').textContent = texts.join('｜');
    if(sig !== lastSig && !d.running && !f.running){
      lastSig = sig;
      loadData();
    } else { lastSig = sig; }
  } catch(e){}
}

loadData();
setInterval(poll, 2000);
poll();
</script>
</body>
</html>"""


def main():
    if not FROZEN and not MANAGER.exists():
        sys.exit(f"未找到 {MANAGER}")
    if WORKER_WARNING:
        try:
            with LOG.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 警告：{WORKER_WARNING}\n")
        except Exception:
            pass
    port = 8765
    for _ in range(10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("8765-8774 端口均被占用")
    GUI_PID.write_text(str(__import__("os").getpid()), encoding="utf-8")
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def _say(msg):
        if sys.stdout is not None:  # pythonw / --noconsole 下 stdout 为 None
            print(msg)

    # 优先原生应用窗口（pywebview + Windows 自带 WebView2），失败回退浏览器
    if "--browser" not in sys.argv:
        try:
            import webview
            webview.create_window("B 站评论管理器", url,
                                  width=1180, height=800, min_size=(900, 600))
            webview.start()
            _say("窗口已关闭，GUI 退出（后台任务继续运行）")
            return
        except Exception as e:
            _say(f"原生窗口不可用（{e}），回退浏览器模式")

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    _say(f"B 站评论管理器 GUI：{url}")
    _say("Ctrl+C 退出 GUI（后台删除/拉取任务不受影响）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _say("GUI 已退出（后台任务继续运行）")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        # 冻结模式下 exe 自调用：以命令行模式执行管理器任务
        # （走 run_main：任何退出原因先落日志再退出，与源码模式一致）
        import bili_comment_manager as mgr
        sys.argv = ["bili_comment_manager.py"] + sys.argv[2:]
        mgr.run_main()
    else:
        main()
