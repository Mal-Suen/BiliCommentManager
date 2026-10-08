# BiliCommentManager

<div align="center">

**Bulk-manage & delete your own Bilibili comments — your black-history cleaner**

**B 站个人评论管理器（黑历史管理器）：扫码登录、全量拉取、时间段筛选、低频删除**

[![Python](https://img.shields.io/badge/Python-3.9+-3776AB.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)](https://github.com/Mal-Suen/BiliCommentManager)
[![License](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](LICENSE)

**[在线介绍页 / Project page](https://mal-suen.github.io/BiliCommentManager/)**

</div>

---

> **首次运行必读 / First-run must-read**
>
> **中文**：exe 未购买代码签名证书，Windows 首次运行会弹蓝色警告「Windows 已保护你的电脑」——这不是检测到病毒，而是对所有未签名程序的默认提醒：点「**更多信息**」→「**仍要运行**」即可。国内安全软件（360、火绒、电脑管家等）可能误报拦截 PyInstaller 打包的单文件 exe——把 `BiliCommentManager.exe` 加入信任区即可正常使用。本工具开源、全部代码可审查，只访问 `api.bilibili.com` 与评论索引 `api.aicu.cc`，不上传任何数据到其他服务器。
>
> **English**: the exe is unsigned (no code-signing certificate), so Windows SmartScreen shows a blue "Windows protected your PC" warning on first run — this is the default prompt for all unsigned programs, not a virus detection: click **More info** → **Run anyway**. Chinese antivirus suites (360 / Huorong / PC Manager) frequently false-flag PyInstaller single-file exes — add `BiliCommentManager.exe` to the trust list. The tool is open source, only ever talks to `api.bilibili.com` and the comment index `api.aicu.cc`, and uploads nothing.

---

## Table of Contents / 目录

- [English](#english)
- [中文](#中文)

---

<a name="english"></a>
## English

### Overview

**BiliCommentManager** lists every comment you have ever posted on Bilibili (videos, dynamics, articles) and deletes them in bulk — using the same API the official web player uses when you click "delete". A GUI lets you filter by date range, mark comments to keep, and watch deletion progress in real time. Two deployment modes: a single self-contained exe, or readable source for those who prefer to audit what runs on their machine.

### Core Features

| Feature | What it does |
|---------|--------------|
| **QR login** | Official Bilibili QR-code login flow; the session cookie is written locally and only ever sent to `api.bilibili.com` |
| **Full history** | Lists all your comments via the third-party index AICU (public uid only, no cookies sent) |
| **Filter & review** | Date range / type / keyword / status filters; every comment links back to its source video or post |
| **Keep list** | Mark comments to preserve (e.g. keepsakes) before bulk deletion |
| **Paced deletion** | Random 5-12 s per comment, 30-60 s pause every 20 comments, automatic stop-and-save on risk-control signals; resumable |
| **Real-time progress** | The data file is rewritten after every single deletion, so the GUI progress bar reflects true state; background tasks survive closing the window |
| **Full snapshot view** | A read-only "view all comments" mode shows the complete inventory frozen at snapshot time — it never changes as deletion proceeds, so you can still review your full history afterwards |
| **Two deployment modes** | Single exe (zero setup) or from source (auditable) |

### How It Works & Honest Limitations

- **Listing** uses AICU (`api.aicu.cc`), a third-party index of public Bilibili data. It receives only your public uid — never your cookie. Limitations: the index lags (recent days may be missing), and it sits behind Cloudflare, so on some networks it is unreachable (direct DNS is poisoned in some regions; a proxy may be required). If listing fails, deletion of already-listed comments still works.
- **Deletion** calls `POST api.bilibili.com/x/v2/reply/del` — the same endpoint the web player uses — with your own cookie. It is an unofficial API usage: keep the default pacing, and expect the account to be rate-limited if you speed it up. The tool aborts and saves progress on HTTP 412 / code -412 risk-control responses.
- **Credentials** stay on your machine: `cookie.txt` is local, sent only to `api.bilibili.com`. Deleting comments is irreversible; the full inventory is archived to `my_comments.json` before any deletion.
- **Coverage**: comments deleted by others, or on deleted videos, may not be listed or deletable. Verify leftovers in the official app (创作中心 → 互动管理 → 发出的评论).

### Deployment

**Mode 1 — single exe (zero setup):** double-click `BiliCommentManager.exe`. No Python needed; the window is a native app (WebView2). Data files are created next to the exe; copy the folder to migrate. Unsigned exe: expect a SmartScreen warning on first run ("More info → Run anyway").

**Mode 2 — from source (auditable):**

```bash
git clone https://github.com/Mal-Suen/BiliCommentManager.git
cd BiliCommentManager
pip install -r requirements.txt
python bili_comment_gui.py          # GUI (or double-click 双击启动.bat)
python bili_comment_manager.py      # CLI: login / fetch / list / keep / delete
```

### Usage

1. Click **扫码登录** — a QR code pops up; scan with the Bilibili mobile app and confirm. The cookie is written automatically (re-scan when it expires).
2. Click **重新拉取** — full history is fetched in the background (slow due to third-party API rate limits: ~1 hour for ~1700 comments; progress shown live; you can close the window).
3. Filter by date range / type / keyword; mark keepers with the **保留** button.
4. Click **开始删除（当前筛选）** — confirm the dialog (shows count and ETA), watch the progress bar. Stop anytime; re-run to continue.
5. Click **查看全量评论** — switch to the read-only snapshot view: the complete inventory, frozen, unaffected by deletion — review your full history even after wiping it.

### Project Structure

```
BiliCommentManager/
├── bili_comment_manager.py   # Core: QR login, AICU listing, paced deletion (CLI)
├── bili_comment_gui.py       # Local Web UI: file-only GUI, spawns background tasks
├── requirements.txt          # requests / qrcode / pillow / pywebview
├── 双击启动.bat               # Double-click launcher (source mode)
├── 停止界面.bat               # Stop the GUI (background tasks unaffected)
└── README.md
```

---

<a name="中文"></a>
## 中文

### 概述

**BiliCommentManager** 列出你在 B 站发表过的全部评论（视频/动态/专栏），并支持批量删除——调用的是网页端点「删除」按钮的同款接口。图形界面可按时间段筛选、标记保留、实时查看删除进度。两种部署方式：免安装的单文件 exe，或可审查源码的源码部署（懂技术者更放心）。

### 核心功能

| 功能 | 说明 |
|------|------|
| **扫码登录** | B 站官方二维码登录；Cookie 只写入本地，且只发送给 `api.bilibili.com` |
| **全量拉取** | 经第三方索引 AICU 列出全部评论（只暴露公开 uid，不带 Cookie） |
| **筛选查看** | 日期范围/类型/关键词/状态筛选；每条评论可跳回原视频或原动态 |
| **保留名单** | 批量删除前标记想保留的评论（如纪念性评论） |
| **低频删除** | 每条随机 5-12 秒、每 20 条休息 30-60 秒；风控信号自动中止并保存进度；支持续跑 |
| **实时进度** | 每删一条即回写数据文件，进度条反映真实状态；关闭界面后台任务照常运行 |
| **全量快照视图** | 只读的「查看全量评论」模式：完整清单冻结于快照时刻，不随删除进度变化——删除后仍可回看全部历史 |
| **两种部署** | 单文件 exe（零门槛）或源码部署（可审查） |

### 工作原理

- **评论列表**来自第三方索引 AICU（`api.aicu.cc`），它只收到你的公开 uid，永远收不到 Cookie。局限：索引有滞后（可能缺最近几天的评论）；它在 Cloudflare 后面，部分网络环境不可达（部分地区 DNS 被污染，可能需要代理）。列表拉取失败不影响已列出评论的删除。
- **删除**调用 `POST api.bilibili.com/x/v2/reply/del`（网页端同款接口）＋你自己的 Cookie，属于非官方 API 用法：请保持默认节奏，调快可能触发账号限流。遇到风控信号（HTTP 412 / code -412）脚本会自动中止并保存进度。
- **凭证不出本机**：`cookie.txt` 只存在本地、只发给 `api.bilibili.com`。删除不可恢复；删除前全部评论内容已留档到 `my_comments.json`。
- **覆盖范围**：被别人删除的评论、已删除视频下的评论可能列不出也删不掉。清完后建议在官方 App（创作中心 → 互动管理 → 发出的评论）核对残留。

### 部署

**方式一——单文件 exe（零门槛）：** 双击 `BiliCommentManager.exe`。无需 Python；原生应用窗口（WebView2）。数据文件生成在 exe 旁边，整个文件夹拷走即迁移。未签名 exe 首次运行会有 SmartScreen 蓝色警告（「更多信息 → 仍要运行」）。

**方式二——源码部署（可审查）：**

```bash
git clone https://github.com/Mal-Suen/BiliCommentManager.git
cd BiliCommentManager
pip install -r requirements.txt
python bili_comment_gui.py          # 图形界面（或双击 双击启动.bat）
python bili_comment_manager.py      # 命令行：login / fetch / list / keep / delete
```

### 使用流程

1. 点**扫码登录**——二维码自动弹出，手机 B 站 App 扫码确认，Cookie 自动写入（过期重扫即可）
2. 点**重新拉取**——后台拉取全部评论（因接口限流速度较慢，约 1700 条需 1 小时左右，进度实时显示，期间可关闭窗口）
3. 按日期/类型/关键词筛选；用**保留**按钮标记想留的评论
4. 点**开始删除（当前筛选）**——确认弹窗（显示条数与预计耗时）后开始，进度条实时推进；随时可停，重开续跑
5. 点**查看全量评论**——切换到只读快照视图：完整清单不随删除变化，删除后仍可回看全部历史

### 项目结构

```
BiliCommentManager/
├── bili_comment_manager.py   # 核心：扫码登录、AICU 拉取、低频删除（命令行）
├── bili_comment_gui.py       # 本地 Web UI：只读写文件的界面，拉起后台任务
├── requirements.txt          # requests / qrcode / pillow / pywebview
├── 双击启动.bat               # 双击启动（源码模式）
├── 停止界面.bat               # 停止界面（不影响后台任务）
└── README.md
```
