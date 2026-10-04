<div align="center">

<img src="pics/b站弹幕提取姬图标_透明背景版.png" alt="B 站弹幕提取姬" width="160">

# bili-danmaku · B 站直播弹幕提取姬

实时抓取 B 站直播间**弹幕正文与 SC（Super Chat）**，落盘为 JSONL / CSV / TXT / SQLite，
并提供一个可实时查看、复盘、导出与分析弹幕的 Web 面板。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#安装)

</div>

---

## 简介

一个只做**弹幕数据面**的 B 站直播采集工具：连上直播间实时抓取弹幕与 SC，
一边落盘、一边在浏览器里实时看。历史弹幕可以翻页复盘、打标记切片，也能在本地做词频与异常分析。

- **协议自带**：自己实现 B 站直播协议栈（含压缩包递归拆解与风控签名），不依赖第三方弹幕库；
- **数据留在本机**：所有产物只写本地磁盘，不含任何上传逻辑，分析全部本地计算；
- **不做的事**：不发弹幕、不录制视频流、不做全量历史爬取。

## 功能

| 功能 | 说明 |
|------|------|
| 实时采集 | 单房间或多房间并发采集，断线自动重连，长时间运行稳定 |
| 四种落盘格式 | JSONL / CSV / TXT / SQLite 同时写出，内容一致 |
| Web 面板 | 浏览器实时看弹幕，支持类型与关键词过滤、高额 SC 提示 |
| 历史复盘 | 翻页浏览全部历史弹幕，底部时间轴可跳转到任意时刻 |
| 标记与切片 | 给弹幕打标记，按标记区间导出 |
| 本地分析 | 实时词频、热点突增、疑似刷屏与异常账号检测，全部本地计算 |
| 弹幕词云 | 输出终端榜单 / JSON / HTML / PNG 四种形式 |
| SC 汇总与告警 | 统计 SC 金额与贡献榜，可按门槛实时告警 |
| 扫码登录 | 二维码登录，凭据只保存在本机；不登录也能用 |
| 桌面形态 | 可选托盘常驻，也可打包为单个 exe 文件（Windows） |

## 采集范围

默认**只采集「有用户文字内容」的事件**：弹幕正文与 SC 正文。
进房、关注、上舰、礼物、点赞等不产生用户文字的事件不落盘。

被过滤的事件仍会被接收，并计入统计的「过滤」计数 —— 你能看到漏掉了多少，而不是无声丢弃。
协议调试时需要一并落盘，加 `--all-events` 即可。

## 安装

需要 **Python 3.10+**。

```bash
git clone https://github.com/OWNER/REPO.git
cd bili-danmaku

python -m venv .venv
# Windows:      .\.venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate

pip install -e .
```

### 构建 Web 面板（可选）

面板前端是 Vite + Vue 3 工程，构建需要 **Node 20+**：

```bash
cd web
npm install
npm run build     # 产物写入 web/dist，后端会自动托管
```

**不构建也能用** —— 后端找不到前端产物时会返回一个提示页，采集与接口不受影响。

## 使用

```bash
# 扫码登录（可选，但建议：登录后其他用户的昵称不再被打码）
bili-danmaku login

# 协议自检：房间解析 → token → 握手 → 心跳 → 采样，排障第一步
bili-danmaku probe <房间号> --seconds 30

# 抓取并落盘（Ctrl+C 可随时停止并正常收尾）
bili-danmaku grab <房间号> --duration 5m --out data

# 打开 Web 面板实时看弹幕
bili-danmaku web --rooms <房间号> --open
```

### 抓取

```bash
bili-danmaku grab <房间号> --format jsonl,csv,txt,sqlite        # 指定落盘格式
bili-danmaku grab <房间号> --until 23:00 --history              # 抓到指定时刻，并补种最近的历史弹幕
bili-danmaku grab <房间号> --duration 1h --anonymize            # 脱敏输出（uid 哈希、昵称掩码）

# 过滤
bili-danmaku grab <房间号> --types danmaku,gift                 # 按事件类型
bili-danmaku grab <房间号> --keyword 主播 --keyword "re:^\d+$"  # 关键词，re: 前缀走正则
bili-danmaku grab <房间号> --min-price 30                       # 只要 30 元以上的礼物 / SC / 上舰
bili-danmaku grab <房间号> --min-medal-level 10 --uid <uid>     # 粉丝牌等级 + uid 白名单
```

### Web 面板

```bash
bili-danmaku web --rooms <房间号> --open          # 前台运行，Ctrl+C 停止
bili-danmaku web --rooms <房间号>,<房间号>          # 同时监控多个房间

# 后台常驻：关掉终端也继续跑
bili-danmaku web start --rooms <房间号> --open
bili-danmaku web status                       # 查看地址 / pid / 已运行时间
bili-danmaku web logs --tail 50               # 查看后台日志
bili-danmaku web stop                         # 优雅停止（先把未落盘的数据写盘）
```

端口被占用时会自动向后寻找，实际地址打印在启动日志与 `web status` 里。

面板支持：实时弹幕流、类型与关键词过滤、高额 SC 横幅、位置分布统计、
历史翻页复盘、时间轴跳转、打标记与按区间导出、本地词频与异常分析。

### 统计、导出与重放

```bash
bili-danmaku stats --top 10                                   # 类型分布、发言榜、会话列表
bili-danmaku export --session <id> --format csv --out out.csv
bili-danmaku export --keyword 主播 --format jsonl --out hits.jsonl
bili-danmaku replay data/<房间号>_20260918-14.jsonl --to csv --out converted.csv
```

### 词云

```bash
bili-danmaku wordcloud --session <id> --top 30               # 终端榜单
bili-danmaku wordcloud --format html --out cloud.html        # 自包含单文件，零额外依赖
bili-danmaku wordcloud --format png --out cloud.png          # 需要可选依赖，见下
bili-danmaku wordcloud --source data/xxx.jsonl               # 离线：直接读 JSONL
```

输出 PNG 需要额外安装可选依赖：

```bash
pip install -e ".[wordcloud]"    # jieba（中文分词）+ wordcloud
```

### SC 汇总与告警

```bash
bili-danmaku sc --room <房间号>                  # 条数 / 金额合计 / 贡献榜 / 最近明细
bili-danmaku sc --room <房间号> --min-price 30   # 只看 30 元及以上
bili-danmaku sc <房间号> --follow --min-price 30 --duration 3600 --bell   # 实时告警
```

## 数据产物

默认输出到 `data/` 目录（可用 `--out` 修改）：

| 文件 | 说明 |
|------|------|
| `{房间号}_{时间}.jsonl` | 每行一条事件，保留全部字段，按小时轮转 |
| `{房间号}_{时间}.csv` | 带 UTF-8 BOM，Excel 可直接打开 |
| `{房间号}_{时间}.txt` | `[HH:MM:SS] 昵称: 内容`，仅弹幕 |
| `danmaku.db` | SQLite 数据库，面板与统计的数据来源 |
| `annotations.db` | 面板的标记与备注 |
| `.secrets/cookies.json` | 登录凭据（已 gitignore） |

## 配置

优先级：**命令行参数 > 环境变量 > 配置文件 > 内置默认值**。

复制 [`config.example.toml`](config.example.toml) 为 `config.toml` 后按需修改，常用项：

```toml
[general]
max_rooms = 3                # 最多并发监控的房间数
content_only = true          # 只采有文字的弹幕与 SC
anonymize = false            # 脱敏输出

[sinks]
types = ["jsonl", "csv", "sqlite"]
out_dir = "data"

[web]
host = "127.0.0.1"           # 绑定非回环地址时必须设置 token
port = 8787
```

也可以完全用环境变量（模板见 [`.env.example`](.env.example)）：

| 变量 | 说明 |
|------|------|
| `BILI_SESSDATA` 等 | 凭据纯内存模式，完全不落盘 |
| `BILI_LOG_LEVEL` | 日志级别 |
| `BILI_OUT_DIR` | 数据输出目录 |
| `BILI_COOKIE_FILE` | 凭据文件路径 |

## 隐私与合规

- **凭据只保存在本机**：默认落在 `.secrets/cookies.json`（已 gitignore、权限收紧）；
  设置 `BILI_SESSDATA` 环境变量则完全不落盘；`bili-danmaku purge` 可一键清除；
- **凭据不进入日志**：所有日志经过统一脱敏处理；
- 项目**不含任何上传逻辑**，抓取与分析全部在本机完成；
- 弹幕数据包含其他观众的 uid 与昵称，请自行妥善保管，不要公开分发原始数据集；
  可用 `--anonymize` 输出脱敏数据，或用 `--exclude-masked` 丢弃被平台打码的记录。

**使用边界**：仅用于个人学习、研究或自有直播间的运营分析等授权场景。
请遵守 B 站用户协议与相关法律法规，不要用于骚扰、人肉、批量爬取或商业转售。
本项目默认单房间单连接、限制并发房间数、限制请求频率，从设计上避免给服务端造成压力。

## 许可

[MIT](LICENSE) © 2026 QHaoooLG and bili-danmaku contributors

本项目与哔哩哔哩（Bilibili）官方无关，未获得其背书或赞助。
「哔哩哔哩」「B 站」等名称与标识归其各自所有者所有。
