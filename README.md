# BiliCommentManager

<div align="center">

**Bulk-manage & delete your own Bilibili comments — your black-history cleaner**

**B 站个人评论管理器（黑历史管理器）：扫码登录、增量拉取、低频删除、逐条核验**

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

**BiliCommentManager** lists every comment you have ever posted on Bilibili (videos, dynamics, articles) and deletes them in bulk — using the same API the official web player uses when you click "delete" — then verifies every deletion against Bilibili and automatically re-deletes any survivor. A GUI lets you filter by date range, mark comments to keep, and watch deletion progress in real time; routine pulls are incremental (minutes), with a full re-pull button for complete audits. Two deployment modes: a single self-contained exe, or readable source for those who prefer to audit what runs on their machine.

### Core Features

| Feature | What it does |
|---------|--------------|
| **QR login** | Official Bilibili QR-code login flow; the session cookie is written locally and only ever sent to `api.bilibili.com` |
| **Incremental pull** | Routine pulls scan only the head of the index and finish in minutes; a separate **full re-pull** button pages the entire archive to catch late-indexed comments |
| **Filter & review** | Date range / type / keyword / status filters; every comment links back to its source video or post |
| **Keep list** | Mark comments to preserve (e.g. keepsakes) before bulk deletion |
| **Paced deletion** | Random 5-12 s per comment, 30-60 s pause every 20 comments, automatic stop-and-save on risk-control signals; resumable |
| **Post-delete verification** | Every deletion is checked against Bilibili with a read-only lookup; any survivor is automatically re-deleted — a comment counts as deleted only when Bilibili itself says so |
| **Live ledger** | The stats bar totals your whole history (total / deleted / pending) while the working list shows only current comments; deleted entries are archived, not lost |
| **Full history view** | Read-only view of every comment ever seen, with current statuses — review your full history even after wiping it |
| **On-demand audit** | `verify` command re-checks all past deletions against Bilibili (`--fix` re-deletes survivors) |
| **Two deployment modes** | Single exe (zero setup) or from source (auditable) |

### How It Works

- **Listing** uses AICU (`api.aicu.cc`), a third-party index of public Bilibili data. It receives only your public uid — never your cookie. The index sits behind Cloudflare, so on some networks it is unreachable (a proxy may be required); if listing fails, deletion of already-listed comments still works. The index also keeps growing — their crawler back-fills old comments over time — so routine incremental pulls only find newly posted comments; the full re-pull button pages the whole archive and catches late-indexed ones.
- **Deletion** calls `POST api.bilibili.com/x/v2/reply/del` — the same endpoint the web player uses — with your own cookie. It is an unofficial API usage: keep the default pacing, and expect the account to be rate-limited if you speed it up. The tool aborts and saves progress on HTTP 412 / code -412 risk-control responses. After the delete loop, every deleted comment is verified with a read-only lookup (Bilibili answers "no such comment" for deleted ones); survivors are re-deleted automatically.
- **Credentials** stay on your machine: `cookie.txt` is local, sent only to `api.bilibili.com`. Deleting comments is irreversible; the full inventory is archived before any deletion.
- **Coverage**: comments deleted by others, or on deleted videos, may not be listed or deletable. Verify leftovers in the official app (创作中心 → 互动管理 → 发出的评论).

### Deployment

**Mode 1 — single exe (zero setup):** double-click `BiliCommentManager.exe`. No Python needed; the window is a native app (WebView2). Data files are created next to the exe; copy the folder to migrate. Unsigned exe: expect a SmartScreen warning on first run ("More info → Run anyway").

**Mode 2 — from source (auditable):**

```bash
git clone https://github.com/Mal-Suen/BiliCommentManager.git
cd BiliCommentManager
pip install -r requirements.txt
python bili_comment_gui.py          # GUI (or double-click 双击启动.bat)
python bili_comment_manager.py      # CLI: login / fetch / list / keep / delete / verify
```

### Usage

1. Click **扫码登录** — a QR code pops up; scan with the Bilibili mobile app and confirm. The cookie is written automatically (re-scan when it expires).
2. Click **重新拉取** — an incremental pull finds new comments in minutes. Click **完整重拉** occasionally for a full audit (~30-45 min for ~1700 comments; catches late-indexed entries the incremental pull cannot reach).
3. Filter by date range / type / keyword; mark keepers with the **保留** button.
4. Click **开始删除（当前筛选）** — confirm the dialog (shows count and ETA), watch the progress bar. Stop anytime; re-run to continue. After the loop, every deletion is verified against Bilibili and failures are re-deleted automatically.
5. Click **查看全量评论** — the full history view with current statuses; deleted entries stay reviewable.

### Project Structure

```
BiliCommentManager/
├── bili_comment_manager.py   # Core: QR login, incremental pull, paced deletion, verification (CLI)
├── bili_comment_gui.py       # Local Web UI: file-only GUI, spawns background tasks
├── tests/                    # 50 pytest cases (locks, pid handling, reconcile, progress parsing)
├── requirements.txt          # requests / qrcode / pillow / pywebview
├── 双击启动.bat               # Double-click launcher (source mode)
├── 停止界面.bat               # Stop the GUI (background tasks unaffected)
└── README.md
```

---

<a name="中文"></a>
## 中文

### 概述

**BiliCommentManager** 列出你在 B 站发表过的全部评论（视频/动态/专栏），并支持批量删除——调用的是网页端「删除」按钮的同款接口——**删完逐条向 B 站核验，没删掉的自动重删**。图形界面可按时间段筛选、标记保留、实时查看删除进度；日常拉取走增量模式（几分钟），另有「完整重拉」按钮做全档案审计。两种部署方式：免安装的单文件 exe，或可审查源码的源码部署（懂技术者更放心）。

### 核心功能

| 功能 | 说明 |
|------|------|
| **扫码登录** | B 站官方二维码登录；Cookie 只写入本地，且只发送给 `api.bilibili.com` |
| **增量拉取** | 日常拉取只扫索引头部、几分钟完成；「**完整重拉**」按钮翻全档案，捞出晚收录的旧评论 |
| **筛选查看** | 日期范围/类型/关键词/状态筛选；每条评论可跳回原视频或原动态 |
| **保留名单** | 批量删除前标记想保留的评论（如纪念性评论） |
| **低频删除** | 每条随机 5-12 秒、每 20 条休息 30-60 秒；风控信号自动中止并保存进度；支持续跑 |
| **删除核验** | 每条删除后向 B 站只读查询确认生效，未生效的自动重删——B 站亲口确认才算删完 |
| **台账统计** | 统计条显示完整历史（总数/已删/待删）；工作列表只显示现存评论，已删条目归档可回看 |
| **全量历史视图** | 只读的「查看全量评论」：见过的每条评论、状态为当前已知——删完仍可回看全部历史 |
| **按需审计** | `verify` 命令随时全量核验历史删除（`--fix` 重删漏网之鱼） |
| **两种部署** | 单文件 exe（零门槛）或源码部署（可审查） |

### 工作原理

- **评论列表**来自第三方索引 AICU（`api.aicu.cc`），它只收到你的公开 uid，永远收不到 Cookie。它在 Cloudflare 后面，部分网络环境不可达（可能需要代理）；列表拉取失败不影响已列出评论的删除。索引还会持续生长（爬虫不断回补旧评论）——日常增量拉取只能发现你新发的评论，「完整重拉」翻全档案才能捞出晚收录的旧评论，建议隔一阵子做一次。
- **删除**调用 `POST api.bilibili.com/x/v2/reply/del`（网页端同款接口）＋你自己的 Cookie，属于非官方 API 用法：请保持默认节奏，调快可能触发账号限流。遇到风控信号（HTTP 412 / code -412）脚本会自动中止并保存进度。删除循环结束后，每条已删评论都会用只读查询向 B 站核验（已删的返回「没有该评论」），未生效的自动重删。
- **凭证不出本机**：`cookie.txt` 只存在本地、只发给 `api.bilibili.com`。删除不可恢复；删除前全部评论内容已留档。
- **覆盖范围**：被别人删除的评论、已删除视频下的评论可能列不出也删不掉。清完后建议在官方 App（创作中心 → 互动管理 → 发出的评论）核对残留。

### 部署

**方式一——单文件 exe（零门槛）：** 双击 `BiliCommentManager.exe`。无需 Python；原生应用窗口（WebView2）。数据文件生成在 exe 旁边，整个文件夹拷走即迁移。未签名 exe 首次运行会有 SmartScreen 蓝色警告（「更多信息 → 仍要运行」）。

**方式二——源码部署（可审查）：**

```bash
git clone https://github.com/Mal-Suen/BiliCommentManager.git
cd BiliCommentManager
pip install -r requirements.txt
python bili_comment_gui.py          # 图形界面（或双击 双击启动.bat）
python bili_comment_manager.py      # 命令行：login / fetch / list / keep / delete / verify
```

### 使用流程

1. 点**扫码登录**——二维码自动弹出，手机 B 站 App 扫码确认，Cookie 自动写入（过期重扫即可）
2. 点**重新拉取**——增量模式几分钟找齐新评论；隔一阵子点**完整重拉**做全档案审计（约 1700 条需 30-45 分钟，能捞出增量够不到的晚收录评论）
3. 按日期/类型/关键词筛选；用**保留**按钮标记想留的评论
4. 点**开始删除（当前筛选）**——确认弹窗（显示条数与预计耗时）后开始，进度条实时推进；随时可停，重开续跑。删完自动逐条核验，未生效的自动重删
5. 点**查看全量评论**——全量历史视图、状态为当前已知；已删条目仍可回看

### 项目结构

```
BiliCommentManager/
├── bili_comment_manager.py   # 核心：扫码登录、增量拉取、低频删除、删除核验（命令行）
├── bili_comment_gui.py       # 本地 Web UI：只读写文件的界面，拉起后台任务
├── tests/                    # 50 个 pytest 用例（锁/pid/对账/进度解析）
├── requirements.txt          # requests / qrcode / pillow / pywebview
├── 双击启动.bat               # 双击启动（源码模式）
├── 停止界面.bat               # 停止界面（不影响后台任务）
└── README.md
```
