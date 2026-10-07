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
    import requests
except ImportError:
    sys.exit("缺少 requests 库，请先执行：pip install requests")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

# 冻结（PyInstaller exe）模式下数据文件放 exe 旁边；源码模式放脚本旁边
SCRIPT_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
              else Path(__file__).resolve().parent)
COOKIE_FILE = SCRIPT_DIR / "cookie.txt"
DATA_FILE = SCRIPT_DIR / "my_comments.json"
LOG_FILE = SCRIPT_DIR / "cleaner_log.txt"

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
        sys.exit(f"Cookie 校验失败（code={data.get('code')} {data.get('message')}），"
                 "Cookie 可能已过期，请重新从浏览器复制")
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
                sys.exit("二维码已失效，请重新运行 login")
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
                r = subprocess.run(cmd, capture_output=True, timeout=40)
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
                "持续失败请检查代理（如 Clash）是否在运行，或用 --proxy 显式指定")
            time.sleep(wait)
    return None


def fetch_all_comments(uid, ps=5, page_delay=(3.0, 6.0), max_pages=0):
    # AICU 走系统 curl（见 aicu_get 注释），不携带任何 Cookie，只暴露公开 uid
    comments = {}
    page, total, skipped = 1, None, 0
    while True:
        params = {"uid": uid, "pn": page, "ps": ps, "mode": 0, "keyword": ""}
        data = aicu_get(params)
        if data is None:
            log("AICU 多轮尝试均失败，提前结束抓取（已抓到的数据不受影响）")
            break
        d = data.get("data") or {}
        if total is None:
            total = (d.get("cursor") or {}).get("all_count", 0)
            log(f"AICU 索引到你的评论共 {total} 条，开始分页抓取…")
        replies = d.get("replies") or []
        if not replies:
            break
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
            except (KeyError, ValueError, TypeError):
                skipped += 1
        log(f"第 {page} 页：{len(replies)} 条，累计 {len(comments)} 条")
        if page % 10 == 0:
            save_data(uid, comments)  # 中途落盘：进程崩溃不丢已抓数据，list 可中途查看
        if (d.get("cursor") or {}).get("is_end"):
            break
        if max_pages and page >= max_pages:
            log(f"已达 --max-pages {max_pages} 上限，停止抓取")
            break
        page += 1
        time.sleep(random.uniform(*page_delay))
    log(f"抓取完成：共 {len(comments)} 条（跳过缺 dyn 字段 {skipped} 条）")
    return comments


def load_data():
    if DATA_FILE.exists():
        try:
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in raw.get("comments", {}).items()}, raw.get("uid")
        except Exception as e:
            sys.exit(f"读取 {DATA_FILE} 失败：{e}\n可删除该文件后重新 fetch")
    return {}, None


def save_data(uid, comments):
    payload = {
        "uid": uid,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "comments": {str(k): v for k, v in comments.items()},
    }
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
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


def cmd_fetch(args):
    cookie, _ = load_cookie()
    session = make_bili_session(cookie)
    uid, uname = get_self(session)
    log(f"登录校验通过：{uname}（uid={uid}）")
    fresh = fetch_all_comments(
        uid, ps=args.ps,
        page_delay=parse_pair(args.page_delay, (3.0, 6.0)),
        max_pages=args.max_pages,
    )
    old, _ = load_data()
    merged = {}
    for rpid, c in fresh.items():
        oc = old.get(rpid)
        if oc:
            if oc.get("deleted"):
                c["deleted"] = True  # AICU 索引有滞后，保留已删状态避免重复请求
            if oc.get("keep"):
                c["keep"] = True
        merged[rpid] = c
    for rpid, c in old.items():
        if rpid not in merged:
            merged[rpid] = c
    save_data(uid, merged)
    by_type = Counter(c["type"] for c in merged.values())
    stat = "，".join(f"{type_name(t)} {n} 条" for t, n in sorted(by_type.items()))
    pending = sum(1 for c in merged.values() if not c.get("deleted"))
    log(f"已保存 {len(merged)} 条到 {DATA_FILE}（{stat}）")
    log(f"待处理 {pending} 条；用 list 查看，keep 标记保留，delete 删除")


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


def cmd_delete(args):
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
            form = {"oid": c["oid"], "type": c["type"], "rpid": c["rpid"], "csrf": csrf}
            url = DEL_API
            if c["type"] == 11:
                url = f"{DEL_API}?csrf={csrf}"  # 与网页端行为保持一致
            try:
                r = session.post(url, data=form, timeout=20)
                if r.status_code == 412:
                    save_data(uid, comments)
                    sys.exit("B 站返回 HTTP 412 风控挑战：请调大 --delay 放慢速度、"
                             "确认 cookie.txt 是完整 Cookie（含 buvid3），稍后再试；进度已保存")
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
                save_data(uid, comments)
                sys.exit(f"Cookie/CSRF 失效（code={code} {res.get('message')}），"
                         "请重新复制 Cookie 后再跑；进度已保存")
            elif code in RISK_CODES:
                save_data(uid, comments)
                sys.exit(f"触发 B 站风控（code={code} {res.get('message')}）："
                         "请调大 --delay 放慢速度，过几小时再跑；进度已保存")
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
    save_data(uid, comments)
    log(f"本轮结束：成功删除 {ok} 条，失败 {fail} 条"
        + (f"；失败明细见 {DATA_FILE} 中 error 字段" if fail else ""))


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

    args = parser.parse_args()
    if args.proxy:
        global ACTIVE_PROXIES
        ACTIVE_PROXIES = {"http": args.proxy, "https": args.proxy}
    args.func(args)


if __name__ == "__main__":
    main()
