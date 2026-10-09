#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站个人评论管理器（拉取、查看、保留、低频删除自己发表的评论）

原理
----
1. 列表：第三方索引服务 AICU（api.aicu.cc）按 uid 收录了用户发表过的评论，
   GET /api/v3/search/getreply?uid=&pn=&ps= 分页返回，其中 dyn.oid / dyn.type
   正是删除接口所需的参数。请求 AICU 时不携带任何 Cookie，只暴露公开的 uid。
2. 删除：B 站网页端删除自己评论走的就是 POST https://api.bilibili.com/x/v2/reply/del，
   表单参数 oid / type / rpid / csrf（csrf 即 Cookie 里的 bili_jct）。
   本脚本用你自己的登录 Cookie 调同一接口，逐条删除。

用法
----
方式一（推荐）：扫码登录，自动获得 Cookie
1. python bili_comment_manager.py login     手机 B 站 App 扫码，自动写入 cookie.txt
方式二：手动复制浏览器 Cookie
1. 浏览器登录 B 站，F12 打开开发者工具，网络标签里任选一个 bilibili.com 请求，
   在请求标头中复制整串 cookie 值，保存为本目录下的 cookie.txt
   （必须包含 SESSDATA 和 bili_jct 两项，建议复制完整串）。
2. python bili_comment_manager.py fetch          拉取全部评论，生成 my_comments.json
3. python bili_comment_manager.py list           查看统计与明细
4. python bili_comment_manager.py keep RPID,...  标记保留（删除时跳过）
5. python bili_comment_manager.py delete         确认后低频逐条删除，可 Ctrl+C 续跑

删除节奏（低频，防风控）
------------------------
  默认每条间隔随机 5-12 秒，每删 20 条额外休息 30-60 秒；
  100 条约 15 分钟。可用 --delay 调整，不建议低于默认值。

筛选参数（list 与 delete 通用，delete 只删命中的）
------------------------------------------------
  --type 1            类型：1=视频 11=带图动态 12=专栏 17=动态；all=全部
  --keyword 关键词     内容包含该子串（不分大小写）
  --before 2023-01-01  只看该日期之前发表的（可含 HH:MM）
  --after  2024-06-01  只看该日期之后发表的
  delete 另有：--limit 50 本轮最多删除条数；--exclude-oid av号,... 跳过指定 oid

产物（本目录）
--------------
  my_comments.json    评论清单与状态（内容留档、保留标记、删除进度，支持续跑）
  cleaner_log.txt     运行日志（UTF-8）

注意
----
- 删除不可恢复；脚本会先把全部评论内容留档到 my_comments.json。
- AICU 是第三方索引，可能缺少最近几天的评论；清理完建议在
  App「创作中心 → 互动管理 → 发出的评论」里核对残留。
- Cookie 等同账号控制权：只保存在本地 cookie.txt，只发给 api.bilibili.com，
  用完可删除。请求间隔已按低频设计，请勿调得过低。
- 网络：B 站接口直连；AICU 由系统 curl 访问（自动继承环境代理变量，
  失败时可用 --proxy 显式指定代理）。
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

try:
    import msvcrt
except ImportError:  # 非 Windows 平台无文件锁
    msvcrt = None

# noconsole 进程（GUI/worker）调用控制台程序（curl/tasklist）时，Windows 会为
# 子进程分配新控制台——拉取期间 cmd 窗口狂闪即由此而来；此标志抑制
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 冻结（PyInstaller exe）模式下数据文件放 exe 旁边；源码模式放脚本旁边
SCRIPT_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
              else Path(__file__).resolve().parent)
COOKIE_FILE = SCRIPT_DIR / "cookie.txt"
DATA_FILE = SCRIPT_DIR / "my_comments.json"
BACKUP_FILE = SCRIPT_DIR / "comments_backup.json"
LOG_FILE = SCRIPT_DIR / "cleaner_log.txt"

try:
    import requests
except ImportError:
    # GUI 拉起的 worker 无控制台且 stderr 被丢弃，退出原因必须落日志才能被界面看到
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 任务无法启动："
                    f"当前 Python（{sys.executable}）缺少 requests 库，"
                    "请给它执行 pip install requests，或改用 BiliCommentManager.exe\n")
    finally:
        sys.exit("缺少 requests 库，请先执行：pip install requests")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

AICU_ENDPOINTS = [
    "https://api.aicu.cc/api/v3/search/getreply",
    "https://apibackup2.aicu.cc:88/api/v3/search/getreply",
]
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
DEL_API = "https://api.bilibili.com/x/v2/reply/del"
QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

TYPE_NAMES = {1: "视频", 11: "带图动态", 12: "专栏", 17: "动态"}
AUTH_CODES = {-101, -111}
RISK_CODES = {-412}

# B 站接口走直连（忽略系统代理，国内直连即可且更稳）；
# 确需走代理时用 --proxy http://127.0.0.1:7890 显式指定
ACTIVE_PROXIES = None


def apply_network(session):
    session.trust_env = False
    if ACTIVE_PROXIES:
        session.proxies.update(ACTIVE_PROXIES)
    return session


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


# ---------- 任务互斥（防 GUI 存活误判导致同类任务双跑） ----------

TASK_LOCK_FDS = {}


def acquire_task_lock(name):
    """文件锁：进程退出（含 taskkill 强杀）锁自动释放，无残留问题。"""
    if msvcrt is None:
        return True
    fd = os.open(str(SCRIPT_DIR / f"gui_{name}.lock"), os.O_CREAT | os.O_RDWR)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        return False
    TASK_LOCK_FDS[name] = fd  # fd 持有到进程退出，锁随之释放
    return True


def _pid_running(pid):
    if pid <= 0:
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
    except Exception:
        return False
    for line in r.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == str(pid):
            return True
    return False


def refuse_if_task_running(task, label):
    """worker 启动自查：pid 文件指向的进程还活着，或文件锁被占，则拒绝启动。"""
    pidfile = SCRIPT_DIR / f"gui_{task}.pid"
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except Exception:
        pid = 0
    own = {os.getpid(), os.getppid()}
    if pid and pid not in own and _pid_running(pid):
        sys.exit(f"检测到另一个{label}任务正在运行（pid {pid}）；"
                 f"如确认没有任务在跑，删除 {pidfile.name} 后重试")
    if not acquire_task_lock(task):
        sys.exit(f"已有另一个{label}任务在运行（文件锁占用中）")


def heal_pid_file(task):
    """把自身 pid 写回 GUI 的 pid 文件：被误判的重复 spawn 覆盖后 8 秒内自愈，
    也让 CLI 直接运行的任务在 GUI 里可见。"""
    try:
        (SCRIPT_DIR / f"gui_{task}.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def load_cookie():
    if not COOKIE_FILE.exists():
        sys.exit(
            f"未找到 {COOKIE_FILE}\n"
            "请先在浏览器登录 B 站，F12 → 网络 → 任选一个 bilibili.com 请求，\n"
            "复制请求标头里完整的 cookie 值，保存为 cookie.txt（须含 SESSDATA 与 bili_jct）"
        )
    raw = COOKIE_FILE.read_text(encoding="utf-8-sig").strip().replace("\n", "")
    m_jct = re.search(r"bili_jct=([^;\s]+)", raw)
    m_sess = re.search(r"SESSDATA=([^;\s]+)", raw)
    if not (m_jct and m_sess):
        sys.exit("cookie.txt 中找不到 SESSDATA / bili_jct，请复制完整 Cookie 字符串")
    return raw, m_jct.group(1)


def make_bili_session(cookie):
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Cookie": cookie,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    })
    return apply_network(s)


def get_self(session):
    r = session.get(NAV_API, timeout=15)
    data = r.json()
    if data.get("code") != 0 or not (data.get("data") or {}).get("isLogin"):
        sys.exit(f"登录已失效（code={data.get('code')} {data.get('message')}）｜"
                 "点「扫码登录」重新扫码；命令行用户：重新运行 login，"
                 "或从浏览器复制新 Cookie 更新 cookie.txt")
    return int(data["data"]["mid"]), data["data"].get("uname", "")


def cmd_login(args):
    """扫码登录：调 B 站官方二维码登录接口，成功后把 Cookie 写入 cookie.txt。"""
    session = apply_network(requests.Session())
    session.headers.update({"User-Agent": UA})
    try:
        r = session.get(QR_GENERATE_API, timeout=15)
        j = r.json()
    except Exception as e:
        sys.exit(f"请求二维码失败：{e}")
    if j.get("code") != 0:
        sys.exit(f"获取二维码失败：{j.get('code')} {j.get('message')}")
    qr_url = j["data"]["url"]
    qrcode_key = j["data"]["qrcode_key"]
    qr_png = SCRIPT_DIR / "login_qr.png"
    try:
        import qrcode
        qrcode.make(qr_url).save(qr_png)
        log(f"二维码已保存：{qr_png}")
        if not args.no_open:
            try:
                os.startfile(qr_png)
            except Exception:
                pass
    except Exception as e:
        log(f"二维码图片生成失败（{e}），可手动把以下内容生成二维码后扫码：\n  {qr_url}")
    log("等待手机扫码确认（二维码约 3 分钟有效）…")
    deadline = time.time() + 180
    last_state = None
    while time.time() < deadline:
        try:
            r = session.get(QR_POLL_API, params={"qrcode_key": qrcode_key}, timeout=15)
            j = r.json()
        except Exception as e:
            log(f"轮询异常：{e}")
            time.sleep(3)
            continue
        d = j.get("data") or {}
        state = d.get("code", j.get("code"))
        if state == 0:
            pairs = {}
            for k, v in parse_qs(urlparse(d.get("url", "")).query).items():
                if v:
                    pairs[k] = v[0]
            for c in session.cookies:
                pairs[c.name] = c.value
            for c in (d.get("cookie_info") or {}).get("cookies") or []:
                pairs[c["name"]] = c["value"]
            # 合并本机设备指纹 Cookie（若之前导出过），降低风控概率
            buvid_file = SCRIPT_DIR / "buvid_snippet.txt"
            if buvid_file.exists():
                for part in buvid_file.read_text(encoding="utf-8").strip().split(";"):
                    if "=" in part:
                        k, _, v = part.strip().partition("=")
                        pairs.setdefault(k, v)
            if "SESSDATA" not in pairs or "bili_jct" not in pairs:
                sys.exit(f"登录响应缺少 SESSDATA/bili_jct（拿到字段：{sorted(pairs)}）")
            cookie_str = "; ".join(f"{k}={v}" for k, v in pairs.items())
            COOKIE_FILE.write_text(cookie_str + "\n", encoding="utf-8")
            if d.get("refresh_token"):
                (SCRIPT_DIR / "refresh_token.txt").write_text(
                    str(d["refresh_token"]), encoding="utf-8")
            log(f"登录成功，Cookie 已写入 {COOKIE_FILE}（{len(pairs)} 项）")
            s2 = make_bili_session(cookie_str)
            uid, uname = get_self(s2)
            log(f"身份确认：{uname}（uid={uid}）")
            return
        if state != last_state:
            if state == 86090:
                log("已扫码，请在手机上确认登录")
            elif state == 86038:
                sys.exit("二维码已失效（约 3 分钟有效）｜再点一次「扫码登录」；"
                         "命令行用户：重新运行 login")
            else:
                log(f"状态 {state}：{d.get('message') or j.get('message')}")
            last_state = state
        time.sleep(2)
    sys.exit("超时未确认，请重新运行 login")


def aicu_get(params, max_tries=3):
    """通过系统 curl 请求 AICU（列出某 uid 发表过的评论）。

    AICU 在 Cloudflare 后面，本机 DNS 对其污染（解析到无关 IP），直连不通；
    Python 的 OpenSSL TLS 指纹即使走代理也会被 Cloudflare 掐断，而系统
    curl（Windows 自带，Schannel 指纹）实测可用。curl 会自动继承环境中的
    代理变量（如 HTTPS_PROXY），也可用 --proxy 显式指定。
    请求过猛（ps 偏大或频率偏高）会收到 Cloudflare「Just a moment」挑战页，
    等待后重试通常可恢复。
    """
    curl = shutil.which("curl")
    if not curl:
        log("未找到系统 curl（Windows 10/11 自带），无法访问 AICU")
        return None
    qs = urlencode(params)
    for attempt in range(1, max_tries + 1):
        for endpoint in AICU_ENDPOINTS:
            cmd = [curl, "-s", "--max-time", "25",
                   "-H", f"User-Agent: {UA}", f"{endpoint}?{qs}"]
            if ACTIVE_PROXIES:
                cmd += ["-x", ACTIVE_PROXIES["https"]]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=40,
                                   creationflags=CREATE_NO_WINDOW)
                body = r.stdout.decode("utf-8", errors="replace").strip()
                if body.startswith("{"):
                    data = json.loads(body)
                    if data.get("code") == 0:
                        return data
                    log(f"AICU {endpoint} 返回 code={data.get('code')} {data.get('message')}")
                elif "<!DOCTYPE" in body[:100] or "Just a moment" in body:
                    log(f"AICU {endpoint} 触发 Cloudflare 人机验证")
                else:
                    log(f"AICU {endpoint} 返回异常（curl exit={r.returncode}）")
            except Exception as e:
                log(f"AICU {endpoint} 请求异常：{e}")
        if attempt < max_tries:
            wait = random.uniform(15, 30)
            log(f"AICU 第 {attempt}/{max_tries} 轮未成功，等待 {wait:.0f} 秒后重试；"
                "持续失败请检查代理（如 Clash）是否在运行，或用 --proxy 显式指定"
                "（如果你开了梯子或代理软件，试试换节点或暂时关掉）")
            time.sleep(wait)
    return None


def fetch_all_comments(uid, ps=5, page_delay=(3.0, 6.0), max_pages=0,
                       old=None, incremental=True, known=None):
    # AICU 走系统 curl（见 aicu_get 注释），不携带任何 Cookie，只暴露公开 uid
    old = old or {}
    known = set(old) if known is None else known
    comments = {}
    page, total, skipped = 1, None, 0
    consecutive_known = 0
    completed = False
    while True:
        params = {"uid": uid, "pn": page, "ps": ps, "mode": 0, "keyword": ""}
        data = aicu_get(params)
        if data is None:
            log("AICU 多轮尝试均失败，提前结束抓取（已抓到的数据不受影响）｜"
                "稍后点「重新拉取」重试，已抓到的不会丢")
            break
        d = data.get("data") or {}
        if total is None:
            total = (d.get("cursor") or {}).get("all_count", 0)
            if incremental and known:
                log("增量拉取：只找新评论，翻到已知区域即停")
            else:
                log(f"AICU 索引到你的评论共 {total} 条，开始分页抓取…")
        replies = d.get("replies") or []
        if not replies:
            completed = True
            break
        new_in_page = 0
        for item in replies:
            try:
                rpid = int(item["rpid"])
                dyn = item.get("dyn") or {}
                if "oid" not in dyn or "type" not in dyn:
                    skipped += 1
                    continue
                comments[rpid] = {
                    "rpid": rpid,
                    "oid": int(dyn["oid"]),
                    "type": int(dyn["type"]),
                    "message": item.get("message", ""),
                    "time": int(item.get("time", 0)),
                    "is_reply": bool(item.get("parent")),
                    "keep": False,
                    "deleted": False,
                    "error": None,
                }
                if rpid not in known:
                    new_in_page += 1
            except (KeyError, ValueError, TypeError):
                skipped += 1
        log(f"第 {page} 页：{len(replies)} 条，累计 {len(comments)} 条"
            + (f"，新 {new_in_page} 条" if known else ""))
        heal_pid_file("fetch")  # pid 被覆盖后自愈，GUI 进度显示不中断
        if incremental and known and new_in_page == 0:
            consecutive_known += 1
            if consecutive_known >= 2:
                log(f"连续 {consecutive_known} 页均为已知评论，提前结束抓取"
                    "（新评论已找齐；完整重拉可用 fetch --full）")
                completed = True
                break
        else:
            consecutive_known = 0
        if page % 10 == 0:
            # 中途落盘必须并入本地状态：只写 fresh 会把已删/保留标记整个清掉
            save_data(uid, merge_comments(old, comments), complete=False)
        if (d.get("cursor") or {}).get("is_end"):
            completed = True
            break
        if max_pages and page >= max_pages:
            log(f"已达 --max-pages {max_pages} 上限，停止抓取")
            break
        page += 1
        time.sleep(random.uniform(*page_delay))
    log(f"抓取完成：共 {len(comments)} 条（跳过缺 dyn 字段 {skipped} 条）")
    return comments, completed


def data_complete():
    """上次 fetch 是否完整跑完——决定本次能否增量拉取（中断过则全量补齐）。"""
    try:
        return bool(json.loads(DATA_FILE.read_text(encoding="utf-8")).get("complete"))
    except Exception:
        return False


def load_backup():
    """全量快照（comments_backup.json）：追加式历史档案，缺失或损坏视为空。"""
    if BACKUP_FILE.exists():
        try:
            raw = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in raw.get("comments", {}).items()}
        except Exception:
            return {}
    return {}


def append_backup(uid, new_entries):
    """新评论并入全量快照（追加式：已有条目原样保留）。"""
    if not new_entries:
        return
    backup = load_backup()
    for rpid, c in new_entries.items():
        backup.setdefault(rpid, c)
    tmp = BACKUP_FILE.with_name(f"{BACKUP_FILE.stem}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(
        {"uid": uid, "comments": {str(k): v for k, v in backup.items()}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(10):
        try:
            tmp.replace(BACKUP_FILE)
            return
        except PermissionError:
            time.sleep(0.05 * (2 ** min(attempt, 5)))
    tmp.replace(BACKUP_FILE)


def merge_comments(old, fresh):
    """新抓数据并入本地状态：已删/保留标记跟随旧数据；旧有新无的条目原样保留。"""
    merged = {}
    for rpid, c in fresh.items():
        oc = old.get(rpid)
        if oc:
            if oc.get("deleted"):
                c["deleted"] = True  # AICU 索引不反映删除，已删状态以本地为准
            if oc.get("keep"):
                c["keep"] = True
        merged[rpid] = c
    for rpid, c in old.items():
        if rpid not in merged:
            merged[rpid] = c
    return merged


def load_data():
    if DATA_FILE.exists():
        try:
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in raw.get("comments", {}).items()}, raw.get("uid")
        except Exception as e:
            sys.exit(f"读取 {DATA_FILE} 失败：{e}\n可删除该文件后重新 fetch")
    return {}, None


def save_data(uid, comments, complete=None):
    if complete is None:
        complete = data_complete()  # 删除/保留等操作不改变清单完整性，沿用现有标记
    payload = {
        "uid": uid,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "complete": bool(complete),
        "comments": {str(k): v for k, v in comments.items()},
    }
    # tmp 带 pid：防止并发任务（CLI+GUI、fetch+delete）共用同一 tmp 互相覆盖
    tmp = DATA_FILE.with_name(f"{DATA_FILE.stem}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    # Windows：界面进程正在读 DATA_FILE 时 replace 会报 PermissionError（共享冲突），
    # 退避重试等读方松手，避免删除任务无声中断
    for attempt in range(10):
        try:
            tmp.replace(DATA_FILE)
            return
        except PermissionError:
            time.sleep(0.05 * (2 ** min(attempt, 5)))
    tmp.replace(DATA_FILE)


def type_name(t):
    return TYPE_NAMES.get(t, f"type{t}")


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "时间未知"


def preview(msg, n=40):
    return re.sub(r"\s+", " ", msg)[:n]


def parse_types(s):
    if s.lower() in ("all", "a", "*"):
        return None
    try:
        return {int(x) for x in s.split(",") if x.strip()}
    except ValueError:
        sys.exit(f"--type 参数无效：{s}（应为 all 或逗号分隔数字，如 1 或 1,12）")


def parse_oids(s):
    if not s:
        return set()
    try:
        return {int(x) for x in s.split(",") if x.strip()}
    except ValueError:
        sys.exit(f"--exclude-oid 参数无效：{s}（应为逗号分隔的数字）")


def parse_rpids(s):
    try:
        return [int(x) for x in re.split(r"[,\s]+", s.strip()) if x]
    except ValueError:
        sys.exit(f"rpid 参数无效：{s}（应为逗号分隔的数字，见 list 输出）")


def parse_date(s):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    sys.exit(f"日期格式无效：{s}（应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）")


def parse_pair(s, default):
    try:
        a, b = (float(x) for x in s.split(","))
        if a <= 0 or b < a:
            raise ValueError
        return a, b
    except ValueError:
        log(f"--delay 参数无效：{s}，使用默认 {default}")
        return default


def apply_filters(comments, args):
    """按 list/delete 通用筛选条件过滤，返回按时间升序的列表。"""
    types = parse_types(args.type)
    kw = args.keyword.lower() if args.keyword else None
    before = parse_date(args.before) if args.before else None
    after = parse_date(args.after) if args.after else None
    out = []
    for c in comments.values():
        if types is not None and c["type"] not in types:
            continue
        if kw and kw not in c.get("message", "").lower():
            continue
        if before is not None and c["time"] >= before:
            continue
        if after is not None and c["time"] < after:
            continue
        out.append(c)
    out.sort(key=lambda c: c["time"])
    return out


def reconcile_fetch(old, backup, fresh):
    """拉取对账＋清除（实时数据只留现存评论）：
    - 已删条目移出实时数据——不在全量档案的先归档，档案可回看；
    - AICU 返回、在全量档案或本次清除清单里的 → 已删条目的索引回声，跳过
      （AICU 索引不反映删除）；
    - 全量档案里没有的 → 新评论，进实时＋全量。
    返回 (新的实时数据, 新评论, 待归档的已删条目, 清除条数)。"""
    purged = {rpid: c for rpid, c in old.items() if c.get("deleted")}
    live = {rpid: dict(c) for rpid, c in old.items() if not c.get("deleted")}
    new_entries = {}
    for rpid, c in fresh.items():
        oc = live.get(rpid)
        if oc is not None:
            if oc.get("keep"):
                c["keep"] = True
            live[rpid] = c
        elif rpid in backup or rpid in purged:
            pass  # 已删条目的 AICU 回声：索引不反映删除，跳过
        else:
            live[rpid] = c
            new_entries[rpid] = c
    archived = {rpid: c for rpid, c in purged.items() if rpid not in backup}
    return live, new_entries, archived, len(purged)


def cmd_fetch(args):
    refuse_if_task_running("fetch", "拉取")
    cookie, _ = load_cookie()
    session = make_bili_session(cookie)
    uid, uname = get_self(session)
    log(f"登录校验通过：{uname}（uid={uid}）")
    old, _ = load_data()
    backup = load_backup()
    known = set(backup) | set(old)  # 已知＝实时＋全量档案（清除过的也算已知）
    incremental = bool(known) and not getattr(args, "full", False) and data_complete()
    if incremental:
        log("本地清单完整，增量拉取：只补新评论")
    fresh, completed = fetch_all_comments(
        uid, ps=args.ps,
        page_delay=parse_pair(args.page_delay, (3.0, 6.0)),
        max_pages=args.max_pages,
        old=old, incremental=incremental, known=known,
    )
    live, new_entries, archived, purged = reconcile_fetch(old, backup, fresh)
    # 一条都没抓到的失败（网络抖动）不降级完整标记：本地覆盖没变，
    # 降级会让下次拉取误回退全量模式（用户会再看到档案总数）
    save_data(uid, live, complete=completed if fresh else None)
    append_backup(uid, {**archived, **new_entries})
    by_type = Counter(c["type"] for c in live.values())
    stat = "，".join(f"{type_name(t)} {n} 条" for t, n in sorted(by_type.items()))
    pending = sum(1 for c in live.values() if not c.get("deleted"))
    if purged:
        log(f"已清除 {purged} 条已删评论（实时数据只留现存；历史在全量快照可回看）")
    log(f"已保存 {len(live)} 条到 {DATA_FILE}（{stat}）")
    log(f"本次新发现 {len(new_entries)} 条（已并入全量快照），待处理 {pending} 条；"
        "用 list 查看，keep 标记保留，delete 删除")


def cmd_list(args):
    comments, _ = load_data()
    if not comments:
        sys.exit("清单为空，请先运行 fetch")
    items = apply_filters(comments, args)
    deleted = sum(1 for c in items if c.get("deleted"))
    kept = sum(1 for c in items if c.get("keep"))
    by_type = Counter(c["type"] for c in items)
    if items and items[0]["time"]:
        span = f"{fmt_time(items[0]['time'])} ~ {fmt_time(items[-1]['time'])}"
    else:
        span = "无时间信息"
    print(f"共 {len(items)} 条（已删 {deleted}，保留 {kept}，待删 {len(items) - deleted - kept}）"
          f"｜{'，'.join(f'{type_name(t)} {n}' for t, n in sorted(by_type.items()))}｜{span}")
    print("视频评论可用 https://www.bilibili.com/video/av{oid} 打开核对；"
          "keep 命令按 rpid 标记保留")
    show = items if args.all else items[:args.head]
    for c in show:
        mark = "已删" if c.get("deleted") else ("保留" if c.get("keep") else "待删")
        reply = "（回复）" if c.get("is_reply") else ""
        print(f"[{fmt_time(c['time'])}] {type_name(c['type'])} rpid={c['rpid']} "
              f"oid={c['oid']} {reply}{mark}｜{preview(c['message'])}")
    if not args.all and len(items) > args.head:
        print(f"（仅显示最早 {args.head} 条，共 {len(items)} 条；--all 显示全部）")


def cmd_keep(args):
    value = getattr(args, "value", True)
    comments, uid = load_data()
    if not comments:
        sys.exit("清单为空，请先运行 fetch")
    rpids = parse_rpids(args.rpids)
    hit, miss = 0, 0
    for rpid in rpids:
        c = comments.get(rpid)
        if c is None:
            log(f"未找到 rpid={rpid}")
            miss += 1
        else:
            c["keep"] = value
            hit += 1
    save_data(uid, comments)
    action = "标记保留" if value else "取消保留"
    log(f"已{action} {hit} 条" + (f"，未找到 {miss} 条" if miss else ""))


def comment_exists(session, c):
    """只读核验：B 站 reply/reply 接口查评论是否仍存在。

    已删返回 code=12006「没有该评论」，现存返回 code=0（根评论与楼中楼、
    视频/动态/专栏全类型实测一致）；网络异常时保守当作仍存在，交给重删兜底。"""
    url = (f"https://api.bilibili.com/x/v2/reply/reply"
           f"?type={c['type']}&oid={c['oid']}&root={c['rpid']}&pn=1&ps=1")
    try:
        r = session.get(url, timeout=20)
        return r.status_code == 200 and r.json().get("code") == 0
    except Exception:
        return True


def _terminate(uid, comments, kind, detail):
    """删除/重删遇到登录失效或风控：存盘、落终止消息、退出。"""
    save_data(uid, comments)
    if kind == "auth":
        log(f"任务终止：登录已失效（{detail}）｜"
            "点界面右上角「扫码登录」重新扫码，再点「开始删除」会接着删｜"
            "命令行用户：重新运行 login｜进度已保存")
    elif kind == "risk_http":
        log("任务终止：B 站风控拦截（HTTP 412，请求太频繁被暂时拦下）｜"
            "等几个小时再点一次「开始删除」，会从剩余的继续，已删的不受影响｜"
            "调大 --delay、补全 cookie.txt（含 buvid3）可减少复发｜"
            "进度已保存")
    else:
        log(f"任务终止：触发 B 站风控（{detail}）｜"
            "过几个小时再点一次「开始删除」，会从剩余的继续｜"
            "调大 --delay 放慢速度可减少复发｜进度已保存")
    sys.exit(1)


def cmd_delete(args):
    refuse_if_task_running("delete", "删除")
    comments, _ = load_data()
    if not comments:
        sys.exit("清单为空，请先运行 fetch")
    cookie, csrf = load_cookie()
    session = make_bili_session(cookie)
    uid, uname = get_self(session)
    log(f"登录校验通过：{uname}（uid={uid}）")
    exclude = parse_oids(args.exclude_oid)
    pending = [c for c in apply_filters(comments, args)
               if not c.get("deleted") and not c.get("keep")
               and c["oid"] not in exclude]
    if args.limit > 0:
        pending = pending[:args.limit]
    if not pending:
        sys.exit("没有待删除的评论（可能都已删除、被标记保留或被筛选条件排除）")
    by_type = Counter(c["type"] for c in pending)
    stat = "，".join(f"{type_name(t)} {n} 条" for t, n in sorted(by_type.items()))
    log(f"本轮待删除 {len(pending)} 条（{stat}），"
        f"时间范围 {fmt_time(pending[0]['time'])} ~ {fmt_time(pending[-1]['time'])}")
    for c in pending[:5]:
        print(f"  例：[{fmt_time(c['time'])}] {type_name(c['type'])} "
              f"oid={c['oid']}｜{preview(c['message'])}")
    if not args.yes:
        ans = input(f"删除不可恢复，将删除 {len(pending)} 条评论。输入 yes 回车确认：")
        if ans.strip().lower() != "yes":
            sys.exit("已取消")
    dmin, dmax = parse_pair(args.delay, (5.0, 12.0))
    ok = fail = 0
    try:
        for i, c in enumerate(pending, 1):
            heal_pid_file("delete")  # pid 被误判的重复 spawn 覆盖后自愈
            form = {"oid": c["oid"], "type": c["type"], "rpid": c["rpid"], "csrf": csrf}
            url = DEL_API
            if c["type"] == 11:
                url = f"{DEL_API}?csrf={csrf}"  # 与网页端行为保持一致
            try:
                r = session.post(url, data=form, timeout=20)
                if r.status_code == 412:
                    _terminate(uid, comments, "risk_http", "HTTP 412")
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                res = r.json()
            except Exception as e:
                c["error"] = f"网络异常: {e}"
                fail += 1
                log(f"[{i}/{len(pending)}] 异常 rpid={c['rpid']} {e}")
                save_data(uid, comments)
                continue
            code = res.get("code")
            if code == 0:
                c["deleted"] = True
                c["error"] = None
                ok += 1
                log(f"[{i}/{len(pending)}] 已删除 rpid={c['rpid']} "
                    f"{type_name(c['type'])} oid={c['oid']}｜{preview(c['message'])}")
            elif code in AUTH_CODES:
                _terminate(uid, comments, "auth", f"code={code} {res.get('message')}")
            elif code in RISK_CODES:
                _terminate(uid, comments, "risk", f"code={code} {res.get('message')}")
            else:
                c["error"] = f"code={code} {res.get('message')}"
                fail += 1
                log(f"[{i}/{len(pending)}] 失败 rpid={c['rpid']} "
                    f"code={code} {res.get('message')}")
            save_data(uid, comments)
            if i < len(pending):
                time.sleep(random.uniform(dmin, dmax))
                if i % 20 == 0:  # 低频节奏：批量中途长停顿，降低风控概率
                    pause = random.uniform(30, 60)
                    log(f"已处理 {i} 条，休息 {pause:.0f} 秒…")
                    time.sleep(pause)
    except KeyboardInterrupt:
        save_data(uid, comments)
        log(f"收到中断，进度已保存（成功 {ok}，失败 {fail}）；重跑 delete 会继续")
        return
    # ---------- 核验：逐条向 B 站确认删除生效，未生效的自动重删 ----------
    round_deleted = [c for c in pending if c.get("deleted")]
    verified = retry_ok = retry_fail = 0
    failed_verify = []
    if round_deleted:
        log(f"开始核验：向 B 站逐条确认本轮删除的 {len(round_deleted)} 条已生效"
            "（只读查询，约每条 1 秒）")
        for i, c in enumerate(round_deleted, 1):
            heal_pid_file("delete")
            if comment_exists(session, c):
                failed_verify.append(c)
                log(f"[核验 {i}/{len(round_deleted)}] 未生效 rpid={c['rpid']}"
                    f"｜{preview(c['message'])}")
            else:
                verified += 1
            if i % 50 == 0:
                log(f"核验进度 {i}/{len(round_deleted)}")
            time.sleep(random.uniform(0.8, 1.5))
        if failed_verify:
            log(f"核验完成：{verified} 条确认删除，{len(failed_verify)} 条未生效，"
                "开始重新删除（低频节奏同删除）")
            for i, c in enumerate(failed_verify, 1):
                heal_pid_file("delete")
                form = {"oid": c["oid"], "type": c["type"],
                        "rpid": c["rpid"], "csrf": csrf}
                url = DEL_API
                if c["type"] == 11:
                    url = f"{DEL_API}?csrf={csrf}"
                try:
                    r = session.post(url, data=form, timeout=20)
                    if r.status_code == 412:
                        _terminate(uid, comments, "risk_http", "HTTP 412")
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    res = r.json()
                except Exception as e:
                    c["deleted"] = False
                    c["error"] = f"重删网络异常: {e}"
                    retry_fail += 1
                    log(f"[重删 {i}/{len(failed_verify)}] 异常 rpid={c['rpid']} {e}")
                    save_data(uid, comments)
                    continue
                code = res.get("code")
                if code == 0:
                    c["deleted"] = True
                    c["error"] = None
                    retry_ok += 1
                    log(f"[重删 {i}/{len(failed_verify)}] 已删除 rpid={c['rpid']}")
                elif code in AUTH_CODES:
                    _terminate(uid, comments, "auth",
                               f"code={code} {res.get('message')}")
                elif code in RISK_CODES:
                    _terminate(uid, comments, "risk",
                               f"code={code} {res.get('message')}")
                else:
                    c["deleted"] = False
                    c["error"] = f"重删失败 code={code} {res.get('message')}"
                    retry_fail += 1
                    log(f"[重删 {i}/{len(failed_verify)}] 失败 rpid={c['rpid']} "
                        f"code={code} {res.get('message')}")
                save_data(uid, comments)
                if i < len(failed_verify):
                    time.sleep(random.uniform(dmin, dmax))
        else:
            log(f"核验完成：本轮 {len(round_deleted)} 条删除全部生效")
    # ---------- 清除：已删条目移出实时数据（归档进全量快照） ----------
    purged_all = {rpid: c for rpid, c in comments.items() if c.get("deleted")}
    if purged_all:
        backup = load_backup()
        to_archive = {rpid: c for rpid, c in purged_all.items()
                      if rpid not in backup}
        comments = {rpid: c for rpid, c in comments.items()
                    if not c.get("deleted")}
        append_backup(uid, to_archive)
        log(f"已清除 {len(purged_all)} 条已删评论"
            "（实时数据只留现存；历史在全量快照可回看）")
    save_data(uid, comments)
    log(f"本轮结束：成功删除 {ok} 条，失败 {fail} 条；"
        f"核验确认 {verified} 条，未生效重删成功 {retry_ok} 条、仍失败 {retry_fail} 条"
        + (f"；失败明细见 {DATA_FILE} 中 error 字段" if fail or retry_fail else ""))


def verify_targets(live, backup):
    """核验对象：实时数据里标记已删的＋已清除出实时（仅存全量档案）的。"""
    universe = dict(backup)
    universe.update(live)
    targets = {}
    for rpid, c in universe.items():
        lc = live.get(rpid)
        if lc is not None and not lc.get("deleted"):
            continue  # 现存评论（待删/保留/失败）不核验
        targets[rpid] = lc if lc is not None else c
    return targets


def cmd_verify(args):
    """核验历史删除：逐条向 B 站确认已删评论真实生效；--fix 重删仍存在的。"""
    refuse_if_task_running("delete", "核验")
    cookie, csrf = load_cookie()
    session = make_bili_session(cookie)
    uid, uname = get_self(session)
    log(f"登录校验通过：{uname}（uid={uid}）")
    live, _ = load_data()
    backup = load_backup()
    targets = verify_targets(live, backup)
    if not targets:
        log("没有可核验的已删评论（实时数据无已删条目、全量档案无清除记录）")
        return
    log(f"开始核验 {len(targets)} 条已删评论（只读查询，约每条 1 秒）…")
    survivors = []
    confirmed = 0
    for i, (rpid, c) in enumerate(
            sorted(targets.items(), key=lambda kv: kv[1].get("time", 0)), 1):
        heal_pid_file("delete")
        if comment_exists(session, c):
            survivors.append(c)
            log(f"[核验 {i}/{len(targets)}] 仍存在 rpid={rpid}"
                f"｜{preview(c['message'])}")
        else:
            confirmed += 1
        if i % 50 == 0:
            log(f"核验进度 {i}/{len(targets)}")
        time.sleep(random.uniform(0.8, 1.5))
    log(f"核验完成：{confirmed} 条确认已删，{len(survivors)} 条仍存在")
    if not survivors:
        return
    if not args.fix:
        log("仍存在的评论：加 --fix 直接重新删除，或先 fetch 拉取后用界面删除")
        return
    log(f"开始重新删除 {len(survivors)} 条（低频节奏同删除）…")
    dmin, dmax = parse_pair(args.delay, (5.0, 12.0))
    fixed = still = 0
    for i, c in enumerate(survivors, 1):
        heal_pid_file("delete")
        form = {"oid": c["oid"], "type": c["type"], "rpid": c["rpid"], "csrf": csrf}
        url = DEL_API
        if c["type"] == 11:
            url = f"{DEL_API}?csrf={csrf}"
        try:
            r = session.post(url, data=form, timeout=20)
            if r.status_code == 412:
                _terminate(uid, live, "risk_http", "HTTP 412")
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            res = r.json()
        except Exception as e:
            c["deleted"] = False
            c["error"] = f"重删网络异常: {e}"
            still += 1
            live[c["rpid"]] = c  # 放回实时数据，界面可见可重试
            log(f"[重删 {i}/{len(survivors)}] 异常 rpid={c['rpid']} {e}")
            save_data(uid, live)
            continue
        code = res.get("code")
        if code == 0:
            fixed += 1
            log(f"[重删 {i}/{len(survivors)}] 已删除 rpid={c['rpid']}")
        elif code in AUTH_CODES:
            _terminate(uid, live, "auth", f"code={code} {res.get('message')}")
        elif code in RISK_CODES:
            _terminate(uid, live, "risk", f"code={code} {res.get('message')}")
        else:
            c["deleted"] = False
            c["error"] = f"重删失败 code={code} {res.get('message')}"
            still += 1
            live[c["rpid"]] = c
            log(f"[重删 {i}/{len(survivors)}] 失败 rpid={c['rpid']} "
                f"code={code} {res.get('message')}")
        save_data(uid, live)
        if i < len(survivors):
            time.sleep(random.uniform(dmin, dmax))
    log(f"重删结束：成功 {fixed} 条，仍失败 {still} 条（已放回实时数据，界面可见）")


def add_filter_args(p):
    p.add_argument("--type", default="all", help="筛选类型：all 或逗号分隔（如 1）")
    p.add_argument("--keyword", default=None, help="内容包含该子串（不分大小写）")
    p.add_argument("--before", default=None, help="只看该日期前发表的（YYYY-MM-DD[ HH:MM]）")
    p.add_argument("--after", default=None, help="只看该日期后发表的（YYYY-MM-DD[ HH:MM]）")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--proxy", default=None,
                        help="经指定代理访问（如 http://127.0.0.1:7890），默认直连")
    parser = argparse.ArgumentParser(
        description="B 站个人评论管理器（fetch 拉清单 → list 查看 → keep 保留 → delete 低频删除）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", parents=[common], help="扫码登录，自动写入 cookie.txt")
    p.add_argument("--no-open", action="store_true", help="不自动打开二维码图片")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("fetch", parents=[common], help="拉取自己发表过的全部评论")
    p.add_argument("--ps", type=int, default=5,
                   help="每页条数（默认 5；实测更大易触发 Cloudflare 挑战）")
    p.add_argument("--page-delay", default="3,6", help="翻页随机间隔秒（默认 3,6）")
    p.add_argument("--max-pages", type=int, default=0, help="最多抓取页数（0=不限）")
    p.add_argument("--full", action="store_true",
                   help="完整重拉全部档案页（默认：本地清单完整时增量补新，几分钟即完）")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("list", parents=[common], help="查看评论清单")
    add_filter_args(p)
    p.add_argument("--head", type=int, default=30, help="只显示最早 N 条（默认 30）")
    p.add_argument("--all", action="store_true", help="显示全部")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("keep", parents=[common], help="标记保留（delete 会跳过）")
    p.add_argument("rpids", help="评论 rpid，逗号分隔（见 list 输出）")
    p.set_defaults(func=cmd_keep, value=True)

    p = sub.add_parser("unkeep", parents=[common], help="取消保留标记")
    p.add_argument("rpids", help="评论 rpid，逗号分隔")
    p.set_defaults(func=cmd_keep, value=False)

    p = sub.add_parser("delete", parents=[common], help="低频逐条删除自己的评论")
    add_filter_args(p)
    p.add_argument("--limit", type=int, default=0, help="本轮最多删除条数（0=不限）")
    p.add_argument("--delay", default="5,12", help="删除随机间隔秒（默认 5,12，低频防风控）")
    p.add_argument("--exclude-oid", default="", help="跳过的 oid 列表，逗号分隔")
    p.add_argument("--yes", action="store_true", help="跳过确认")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("verify", parents=[common],
                       help="核验历史删除：逐条向 B 站确认已删评论真实生效")
    p.add_argument("--fix", action="store_true",
                   help="仍存在的评论直接重新删除（默认只报告）")
    p.add_argument("--delay", default="5,12", help="重删随机间隔秒（默认 5,12）")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    if args.proxy:
        global ACTIVE_PROXIES
        ACTIVE_PROXIES = {"http": args.proxy, "https": args.proxy}
    args.func(args)


def run_main():
    # GUI 拉起的 worker 无控制台：任何退出路径都必须先落日志，否则界面只看到任务无声消失
    # （源码模式由 __main__ 调用；冻结 exe 由 bili_comment_gui 的 --worker 分支调用）
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str) and e.code.strip():
            log(f"任务退出：{e.code}")
            sys.exit(1)
        sys.exit(e.code)
    except KeyboardInterrupt:
        log("收到中断，退出")
        sys.exit(130)
    except Exception:
        import traceback
        log("任务发生未捕获异常：\n" + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    run_main()
