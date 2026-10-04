<div align="center">

<img src="pics/b站弹幕提取姬图标_透明背景版.png" alt="B 站弹幕提取姬" width="180">

# bili-danmaku · B 站直播弹幕提取姬

**实时抓取 B 站直播间弹幕正文与 SC（Super Chat），落盘四种格式，配一个能复盘、能分析、能导出的 Web 面板。**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Frontend](https://img.shields.io/badge/frontend-Vue%203%20%2B%20Vite-42b883.svg)](web/index.html)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#环境要求)

</div>

---

## 这是什么

一个**只做弹幕数据面**的 B 站直播采集工具：从直播间 WebSocket 实时抓取弹幕与 SC，
一边落盘、一边在浏览器里实时看；历史可以翻页复盘、可以打标记切片、可以本地做词频与异常分析。

它最硬的部分是**自己实现的 B 站直播协议栈**（16 字节大端包头、zlib/brotli 递归拆包、
protobuf 子集解析、WBI 风控签名、`-352` 降级链），所有结论都来自真实抓包实测，
并已固化进代码 —— 不依赖任何第三方弹幕库。

**不做的事**：不发弹幕、不录视频流、不做全量历史爬取、不上传任何数据。

---

## 目录

- [特性](#特性)
- [采集范围（重要）](#采集范围重要)
- [界面预览](#界面预览)
- [快速开始](#快速开始)
- [使用](#使用)
  - [协议自检 `probe`](#协议自检-probe)
  - [抓取落盘 `grab`](#抓取落盘-grab)
  - [Web 面板 `web`](#web-面板-web)
  - [复盘器：历史浏览与标记切片](#复盘器历史浏览与标记切片)
  - [统计与导出 `stats` / `export` / `replay`](#统计与导出-stats--export--replay)
  - [词云 `wordcloud`](#词云-wordcloud)
  - [SC 汇总与告警 `sc`](#sc-汇总与告警-sc)
- [数据产物](#数据产物)
- [配置](#配置)
- [隐私与合规](#隐私与合规)
- [项目结构](#项目结构)
- [架构](#架构)
- [协议关键事实](#协议关键事实)
- [开发](#开发)
- [已知限制](#已知限制)
- [许可](#许可)

---

## 特性

| 能力 | 说明 |
|------|------|
| **实时采集** | asyncio 长连接 + 连接状态机、指数退避重连、flap guard、假死判定（按心跳回应，不按消息稀疏度） |
| **四格式落盘** | JSONL / CSV+TXT / SQLite 同时写出，各自独立有界队列与消费协程，**压测下四种产物条数严格相等** |
| **背压可控** | 队列满时**丢最旧并如实计数**，绝不 OOM；慢 sink 不拖累其他 sink，也不阻塞读循环 |
| **Web 面板** | FastAPI + Vue 3：实时弹幕流、类型/关键词过滤、高额 SC 横幅、类型分布图、发言 TOP、停止面板 |
| **复盘器** | 复合游标翻页（非 `OFFSET`）浏览全部历史、时间轴密度条跳转、按抓取会话浏览、**不受内存缓冲上限约束** |
| **标记与切片** | 时间轴打标记 / 给弹幕钉星，**按标记区间导出**；标注存独立库，采集库一张表都不动 |
| **本地感知分析** | 实时词频、热点突增词、疑似刷屏、疑似异常账号（附判定理由）。**纯本地计算，数据不出本机** |
| **词云** | `text` / `json` / `html`（零依赖）/ `png` 四种输出；表情占位符单独成榜，不刷屏词频 |
| **SC 汇总告警** | 金额合计 / 贡献榜 / 明细，`--follow` 实时告警；金额单位可切换 |
| **扫码登录** | `bili-danmaku login` 二维码登录，凭据只落本机且全程掩码；匿名模式依然可用 |
| **桌面形态** | 可选托盘常驻（`bili-danmaku tray`）与单文件 exe（`tools/build_exe.py`），Windows 侧零额外依赖 |
| **离线可用** | 除抓取本身外全部功能都不需要网络：重放、统计、导出、词云、分析都能读本地 JSONL / SQLite |

---

## 采集范围（重要）

**默认只采集「有用户文字内容」的事件**，包括正文里内嵌的表情占位符（如 `哈哈[dog]`）：

| ✅ 采集 | ❌ 不采集 |
|--------|----------|
| `danmaku` —— 有文字的弹幕（含表情占位符） | `interact` 进房/关注、`guard_buy` 上舰、`gift` 礼物 |
| `super_chat` —— SC 正文 | `like` / `room_stats` / `notice` / `dm_interaction` / `other` |
| | 任何**文本为空**的弹幕 |

被排除的事件**仍会被接收，并计入统计的「过滤」计数**，只是不落盘 ——
这样你能看到"漏掉了多少"，而不是无声丢弃。实测比例：70 秒内收包 216 → 入库 57 / 过滤 169。

需要连这些一起落盘时（协议调试场景）：

```bash
bili-danmaku grab 804456 --all-events          # 或在 config.toml 里设 general.content_only = false
```

> 礼物是否该保留是可争议点：它带金额但没有用户文字，当前按「无文字即排除」处理。
> 若要做礼物统计，把 `content_only` 关掉即可，过滤层代码无须改动。

---

## 界面预览

<div align="center">

<img src="pics/B站弹幕提取姬_团子形象2.png" alt="弹幕提取姬 · 形象" width="420">

</div>

> Web 面板运行在 `http://127.0.0.1:8787`：左栏实时弹幕流 + 底部时间轴密度条，
> 右栏直播间信息、类型分布、发言 TOP 与感知分析。跑起来比截图清楚 —— 见[快速开始](#快速开始)。

---

## 快速开始

### 环境要求

- **Python 3.10+**（开发环境为 3.11）
- Windows / Linux / macOS 均可（托盘与单文件 exe 为 Windows 专属）
- 前端面板需要 Node 20+ 才能构建 —— **但构建不是必须的**，见下文
  （`web/.npmrc` 默认指向 npmmirror 国内镜像并把 npm 缓存放在仓库内，按需修改）

### 安装

```bash
git clone https://github.com/<your-name>/bili-danmaku.git
cd bili-danmaku

python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate

pip install -e ".[dev]"
```

### 构建面板（可选）

```bash
cd web
npm install
npm run build     # 产物写入 web/dist，FastAPI 会自动托管
```

> **不构建也能用**：后端找不到 `web/dist/index.html` 时会返回一个内建提示页，
> REST 与 WebSocket 接口完全不受影响。面板是附属功能，不该因为缺 Node 而让整个工具不可用。

### 跑起来

```bash
bili-danmaku login                       # 扫码登录（可选，但建议：登录后昵称不再被打码）
bili-danmaku probe 7878 --seconds 30     # 协议自检：房间解析 → token → 握手 → 心跳 → 采样
bili-danmaku web --rooms 7878 --open     # 打开面板，浏览器实时看弹幕
```

---

## 使用

### 协议自检 `probe`

排障第一步。它会打印每一步的原始结果，任何一环失败都能立刻定位：

```bash
bili-danmaku probe 7878 --seconds 30 --messages 10
bili-danmaku probe 804456 --dump-raw out_capture   # 把原始帧与解析后的消息落盘，便于事后核对
```

退出码：`0` 正常 · `2` 配置错误 · `3` 房间/接口问题 · `4` 认证失败 · `5` 网络不可用

### 抓取落盘 `grab`

```bash
bili-danmaku grab 804456 --duration 5m --out data
bili-danmaku grab 804456 --format jsonl,csv,txt,sqlite     # 四格式同时落盘
bili-danmaku grab 804456 --until 23:00 --history           # 抓到指定时刻，并补种最近历史弹幕
bili-danmaku grab 804456 --duration 1h --anonymize         # 脱敏输出：uid 加盐哈希、昵称掩码

# 过滤
bili-danmaku grab 804456 --types danmaku,gift --exclude-masked
bili-danmaku grab 804456 --keyword 主播 --keyword "re:^\d+$"              # re: 前缀走正则
bili-danmaku grab 804456 --keyword 抽奖 --keyword 转发 --keyword-mode and # 需同时命中
bili-danmaku grab 804456 --min-price 30                                   # 只要 30 元以上的礼物/SC/上舰
bili-danmaku grab 804456 --min-medal-level 10 --uid 12345                 # 粉丝牌 + uid 白名单
```

`Ctrl+C` 可随时停止并正常收尾（flush 落盘 + 写会话结束时间 + 打印统计），退出码仍为 `0`。

> `--min-price` 只对**带金额的类型**（礼物 / SC / 上舰）生效，其余类型视为不通过 ——
> 用它就意味着"我只要高价值事件"。

### Web 面板 `web`

```bash
# 前台运行（Ctrl+C 停止，适合调试）
bili-danmaku web --rooms 7878 --open
bili-danmaku web --port 9000 --out data --rooms 7878,123456

# 后台常驻：关掉终端也继续跑
bili-danmaku web start --rooms 7878 --open    # 1~2 秒返回
bili-danmaku web status                       # 地址 / pid / 已运行时间 / 房间 / 落盘格式
bili-danmaku web logs --tail 50               # 后台日志
bili-danmaku web stop                         # 优雅停止（先 flush 落盘）
bili-danmaku web restart                      # 等价 stop + start
bili-danmaku web stop --force                 # 兜底强杀（会丢少量未落盘数据）
```

**端口被占用会自动向后找**，实际地址打印在启动日志与 `web status` 里。

**停止的三条路径**：① 面板右上角「停止面板」按钮；② `bili-danmaku web stop`；
③ 兜底 `taskkill /PID <pid> /F`（会丢队列里未落盘的批次）。

> **为什么不能靠信号停**：Windows 上 `CTRL_BREAK_EVENT` 对独立进程组的 uvicorn 无效，
> `taskkill`（不带 `/F`）会被系统拒绝。所以停止是"**让服务自己退出**"：
> CLI/按钮 → 仅回环的 `POST /api/admin/shutdown` → uvicorn 优雅收尾 →
> 每个房间 `bus.stop()` 把队列 flush 落盘。

后端接口清单：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 前端页面（未构建时返回内建提示页） |
| GET | `/api/health` | 状态、房间数、落盘格式、前端是否已构建、凭据/风控健康 |
| GET | `/api/rooms` | 监控中的房间与连接状态（含主播昵称） |
| GET | `/api/room-info` | `room=<房间号/短号/链接>` → 主播昵称/头像、标题、是否在监控中（**只查信息，不启动采集**） |
| POST | `/api/rooms` | `{"room": 804456}`；重复返回 409，失败返回 400 |
| DELETE | `/api/rooms/{id}` | 停止监控，不存在返回 404 |
| GET | `/api/events` | `room_id,type,q,since,until,limit,offset`（完整事件，含 raw） |
| GET | `/api/history` | 历史浏览：游标 `before_ts`+`before_id`（向更旧）或 `after_ts`+`after_id`（向更新），返回**瘦投影**、按时间升序 |
| GET | `/api/density` | 时间轴密度：`bucket_s` 按桶聚合的条数分布（缺省按数据跨度自适应步长） |
| GET | `/api/sessions` | 抓取会话列表（起止时间与事件数） |
| GET/POST | `/api/annotations` | 列出标记 / 打标记（`label`、`note`） |
| DELETE | `/api/annotations/{id}` | 删除一个标记 |
| GET | `/api/annotations/export` | 按标记区间导出：`bookmark_id`（必填）+ `to_bookmark_id?`；只给起点时终点自动取下一个标记 |
| GET | `/api/stats` | `window` 秒数；`window=0`（默认）= 自面板启动累计 |
| GET | `/api/analysis` | 本地感知分析：实时词频 / 热点突增 / 疑似刷屏 / 疑似异常账号 |
| GET | `/api/export` | 流式导出 `csv/jsonl/txt`（CSV 带 UTF-8 BOM，Excel 直接打开不乱码） |
| POST | `/api/admin/shutdown` | 停止面板（优雅退出，先 flush 落盘）。**只接受本机来源**，非回环一律 403 |
| WS | `/ws/live` | 实时事件推送，支持 `room_id` / `types` / `keyword` |

### 复盘器：历史浏览与标记切片

面板不是"只能看最近 2000 条"的窗口 —— 历史数据存在 SQLite 里，可以一直往上翻：

- **滚到顶部自动加载更早**，`(recv_ts, id)` 复合游标翻页，**不受内存缓冲上限约束**；
- **底部时间轴密度条**：点任意时刻跳转，当前窗口高亮；粒度可切 5 / 10 / 15 分钟；
- **按抓取会话浏览**：只看某一次采集；
- **标记**：时间轴下方打标记（可给标签），弹幕行 ★ 钉住某一条，标记列表可跳转；
- **按标记区间导出**：选一个标记作为导出起点，终点自动取下一个标记；
- 历史浏览中导出会自动限定在当前窗口的时间范围。

> 标记存**独立库** `data/annotations.db`（可配 `sinks.annotations_path`），
> **采集库一张表都不动** —— 想重置标注就删这一个文件，完全不必碰采集数据。

**直播间链接解析**：右栏顶部可直接粘 `https://live.bilibili.com/8432038?...`，
也支持纯房间号 / 短号 / `?room_id=`；识别不出就明确报错，不猜。

### 统计与导出 `stats` / `export` / `replay`

```bash
# 看板：类型分布、链路延迟分位、发言 TOP、会话列表
bili-danmaku stats --top 10
bili-danmaku stats --session 20260918-141801-804456 --json

# 导出：流式，不占内存
bili-danmaku export --session <id> --format csv --out out.csv
bili-danmaku export --keyword 主播 --format jsonl --out hits.jsonl
bili-danmaku export --types gift,super_chat --since "2026-09-18 14:00:00"

# 重放：把 JSONL 离线换成别的格式（不联网）
bili-danmaku replay data/804456_20260918-14.jsonl --to csv --out converted.csv

# 产出后校验四种格式是否互相吻合
python tools/verify_formats.py --dir data
```

### 词云 `wordcloud`

```bash
bili-danmaku wordcloud --session 20260927-232630-23438368 --top 30        # 终端榜单
bili-danmaku wordcloud --room 23438368 --since "2026-09-27 23:00" --format json --out wc.json
bili-danmaku wordcloud --format html --out cloud.html                     # 零依赖，浏览器打开
bili-danmaku wordcloud --format png --out cloud.png --top 120             # 需要 [wordcloud] 依赖
bili-danmaku wordcloud --source data/23438368_20260927-23.jsonl           # 离线：直接读 JSONL
bili-danmaku wordcloud --dict room.dict --stopwords-file stop.txt         # 补黑话 / 自定义停用词
bili-danmaku wordcloud --include-emotes                                   # 把 [dog] 也算进词榜
```

| 格式 | 依赖 | 说明 |
|------|------|------|
| `text`（默认） | 无 | 终端榜单 + 表情榜 |
| `json` | 无 | 带元信息（条数、词数、分词器、筛选条件），便于二次加工 |
| `html` | 无 | 自包含单文件，字号映射词频；**没有中文字体也能正常显示** |
| `png` | `[wordcloud]` + 系统中文字体 | 需要可用的中文字体，否则中文变方块（后端会自动探测系统字体） |

**表情占位符默认单独成榜**：弹幕里的表情是内嵌占位符（`哈哈[dog]`），
若当成普通词统计，词频榜会被表情刷屏。所以 `[dog]` 默认不计入词语榜，
而是列在「表情占位符」一节，并带上 `extra.emots` 里的图片链接；
要一起统计就加 `--include-emotes`。

分词：装了 `jieba` 就用它，否则退回**内置二元组兜底分词器**（功能仍可用，只是效果差些）。
房间黑话分不出来时用 `--dict` 补词（一行一个词）。

安装可选依赖：

```bash
pip install -e ".[wordcloud]"     # jieba + wordcloud（wordcloud 会带进 matplotlib/numpy/pillow）
```

### SC 汇总与告警 `sc`

```bash
bili-danmaku sc --room 23438368                  # 条数 / 金额合计 / 贡献榜 / 最近明细
bili-danmaku sc --room 23438368 --min-price 30   # 只看 30 元及以上
bili-danmaku sc --session <id> --json --top 5    # JSON，贡献榜取前 5
bili-danmaku sc 23438368 --follow --min-price 30 --duration 3600 --bell   # 实时告警（不落盘）
```

- **金额单位是元**。SC 的 `price` 字段本身就是元（30/50/100/500/1000/2000 这类档位）；
  **礼物/上舰才是金瓜子（1000 = 1 元）**，所以 `--min-price` 内部会把元门槛按类型换算，
  不会出现"3 分钱礼物当成 3 块钱"的错判。
- ⚠️ **SC 的金额单位尚未用真实样本验证**。如果拿到真实数据发现金额差了 1000 倍，
  用 `--price-unit milli` 覆盖即可，不必改代码。
- `--follow` 的告警**只打印、不落盘**：你要的是提醒，不是再存一份。
- 面板侧对应能力：顶栏筛选区有「SC 提示 ≥ N 元」，达到门槛的 SC 会在弹幕流上方弹横幅。

---

## 数据产物

默认输出到 `data/`（可 `--out` 覆盖）：

| 产物 | 说明 |
|------|------|
| `{room_id}_{YYYYmmdd-HH}.jsonl` | 每行一个事件，保留全部字段（含 `raw`），按小时 / 128 MB 轮转 |
| `{room_id}_{YYYYmmdd-HH}.csv` | `utf-8-sig`（Excel 双击不乱码），已做公式注入防护（`= + - @` 前缀） |
| `{room_id}_{YYYYmmdd-HH}.txt` | `[HH:MM:SS] 昵称: 内容`，仅弹幕 |
| `danmaku.db` | SQLite（WAL + `synchronous=NORMAL`），`event` 表 + `UNIQUE(dedupe_key)` |
| `annotations.db` | 面板标记 / 备注（**与采集库刻意分离**） |
| `.secrets/cookies.json` | 凭据，0600 权限，**已 gitignore** |

多房间时 JSONL/CSV/TXT 按 `room_id` 分文件，SQLite 共用一个库（靠 `room_id` / `session_id` 区分）。
单条含 `raw` 约 2 KB，`--no-raw` 可显著减小体积（约省 60%+）。

---

## 配置

优先级：**CLI 参数 > 环境变量（`BILI_*`）> `--config` 指定的 TOML > 内置默认值**。
复制 [`config.example.toml`](config.example.toml) 为 `config.toml` 即可。

```toml
[general]
log_level = "INFO"
max_rooms = 3
anonymize = false
content_only = true          # 只采有文字的弹幕与 SC（默认）

[api]
cookie_file = ".secrets/cookies.json"
min_request_interval = 1.0   # 两次 HTTP 调用最小间隔（秒），防风控

[client]
heartbeat_interval = 30.0    # 服务端 60 秒无心跳强制断开，不要超过 30
heartbeat_timeout = 90.0     # 超过则判定连接假死并重连
protover = 3                 # 3=brotli，2=zlib

[sinks]
types = ["jsonl", "csv", "sqlite"]
out_dir = "data"
keep_raw = true
rotate_hours = 1
queue_size = 20000

[web]
host = "127.0.0.1"           # 绑非回环地址时必须设置 token
port = 8787

[analysis]
rank_by = "users"            # 词频榜按"多少人说过"排序，把"一个人刷 20 遍"压下去
```

环境变量（也可只写进 `.env`，模板见 [`.env.example`](.env.example)）：

| 变量 | 说明 |
|------|------|
| `BILI_SESSDATA` / `BILI_BILI_JCT` / `BILI_DEDE_USER_ID` / `BILI_BUVUD3` | 凭据纯内存模式，**完全不落盘** |
| `BILI_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `BILI_OUT_DIR` | 数据输出目录 |
| `BILI_COOKIE_FILE` | 凭据文件路径 |

---

## 隐私与合规

**扫码登录得到的凭据只保存在本机，且项目不含任何上传逻辑。**

- **凭据位置**：`.secrets/cookies.json`（已 gitignore，权限收紧为仅当前用户）；
- **纯内存模式**：设置 `BILI_SESSDATA` 环境变量，凭据完全不落盘，适合临时使用或 CI；
- **凭据永不进入日志**：所有日志经统一脱敏过滤器（`RedactingFilter` + 已登记密钥表），
  任何位置出现凭据字面量都会被替换为 `SESSDATA=<redacted>`；
- **不保存账号资料**：只保留连接所需的 cookie 字段，昵称 / mid / 头像一律丢弃；
- **抓取到的第三方数据默认原样保留**（含平台打码结果），可用 `--exclude-masked` 丢弃打码记录，
  用 `--anonymize` 输出脱敏数据（uid 加盐哈希、昵称掩码），便于分享样本而不泄露隐私；
- 一键清除凭据：`bili-danmaku purge`。

**合规使用边界**：仅用于个人学习、研究或自有直播间运营分析等授权场景。
请遵守 B 站用户协议与相关法律法规，不要用于骚扰、人肉、批量爬取或商业转售。
本项目默认单房间单连接、限制并发房间数、强制 HTTP 调用最小间隔，从设计上避免给服务端造成压力。

**发现安全问题请不要开公开 issue**（尤其涉及凭据泄露、脱敏绕过、XSS 或任意文件读写时），
请通过仓库 **Security** 标签页的私密漏洞上报渠道联系维护者。
复现材料请用脱敏或伪造数据，**不要提供真实的 SESSDATA**。

---

## 项目结构

```
bili-danmaku/
├─ src/bili_danmaku/
│  ├─ protocol.py        封包 / 拆包 / zlib+brotli     ← 只认字节，与业务层解耦
│  ├─ protobuf_lite.py   通用 protobuf 解析             ← 零依赖，解 INTERACT_WORD_V2 的 data.pb
│  ├─ parser.py          cmd → Event 归一化             ← 结构化优先 + 下标兜底
│  ├─ client.py          WS 连接、认证、心跳、重连
│  ├─ collector.py       连接状态机 + 去重管道
│  ├─ api.py             HTTP 接口 + 限流 + token 降级链
│  ├─ wbi.py             WBI 风控签名（含官方签名向量）
│  ├─ credentials.py     扫码登录 / 掩码 / 清除
│  ├─ login.py           登录状态与扫码流程
│  ├─ filters.py         过滤规则（类型/关键词/uid/金额/粉丝牌）
│  ├─ rooms.py           房间号 / 直播间链接解析        ← 纯函数，识别不出就报错（不猜）
│  ├─ store.py           SQLite 只读查询层（统计 / 导出 / 游标分页）
│  ├─ annotations.py     标注存储                       ← 独立库，与采集库分离
│  ├─ text.py            分词 / 词频 / 词云渲染
│  ├─ analysis.py        本地感知分析                   ← 纯函数、无 IO / 无网络
│  ├─ export_stats.py    统计数据导出（CSV / 多 sheet xlsx）
│  ├─ runtime.py         面板托管运行时（pid / 日志 / 优雅停止）
│  ├─ launcher.py        托盘启动器与单实例管理
│  ├─ sinks/             jsonl / csv / txt / sqlite + 扇出总线
│  ├─ server/            hub.py（广播中枢）/ panel.py（多房间）/ app.py（FastAPI）
│  ├─ gui/               Windows 侧托盘与原生窗口（ctypes，零额外依赖）
│  └─ cli.py             命令行入口
├─ web/                  Vite + Vue 3 面板前端
│  └─ src/               App.vue + 16 个组件 + 2 个 composable（构建产物 dist/ 由后端托管）
├─ tools/                打包、压测、诊断脚本（手动运行，不参与运行时）
├─ assets/               app.ico（exe 与托盘共用，由 tools/make_icon.py 生成）
└─ pics/                 图标与形象源图
```

---

## 架构

```
                    ┌──────────────────────────────────────────┐
                    │              CLI  (click)                │
                    │ grab | web | replay | probe | wordcloud  │
                    └───────────────────┬──────────────────────┘
                                        │
                          ┌─────────────▼─────────────┐
                          │      Config (pydantic)    │
                          └─────────────┬─────────────┘
                                        │
   ┌────────────────────────────────────▼─────────────────────────────────────┐
   │                     RoomCollector  (每房间一个任务)                       │
   │                                                                          │
   │  ┌──────────┐   ┌────────────┐   ┌──────────┐   ┌─────────┐   ┌───────┐  │
   │  │ api.py   │──▶│ client.py  │──▶│parser.py │──▶│filters  │──▶│  bus  │  │
   │  │ HTTP 元数据│   │ WS 长连接  │   │ 原始→事件 │   │  规则   │   │ 扇出  │  │
   │  └──────────┘   └────────────┘   └──────────┘   └─────────┘   └───┬───┘  │
   │       ▲               ▲                                          │      │
   │       │          protocol.py (封包/解包/解压)                      │      │
   │       └── wbi.py (风控签名, 按需)                                  │      │
   └──────────────────────────────────────────────────────────────────┼──────┘
                                                                      │
        ┌──────────────┬──────────────┬──────────────┬────────────────┤
        ▼              ▼              ▼              ▼                ▼
   JsonlSink      CsvTxtSink     SqliteSink    WebHub          (更多 sink)
   逐行 append    轮转写文件      WAL 批量事务   广播给面板 WS
```

**四条关键设计原则**

1. **协议层与业务层解耦**：`protocol.py` 只认字节，`parser.py` 只认 JSON ——
   便于离线单测，协议变更时也能快速定位。
2. **原始消息永远保留**：归一化事件里带 `raw` 字段（可 `--no-raw` 关闭），
   协议字段位置变了也能事后重新解析。
3. **Sink 背压可控**：每个 sink 一个独立有界队列 + 独立消费协程，
   满了丢最旧并计数告警，绝不 OOM，慢 sink 也不拖累其他 sink。
4. **面板是消费者，不是依赖**：`WebHub` 只是 bus 的一个订阅者，
   每个 WS 订阅者独立有界队列、连续丢弃超阈值会被主动断开 ——
   **面板不该反向影响采集的数据完整性**。

---

## 协议关键事实

以下为对真实直播间的**只读实测结论**，是解析层所有字段映射的依据。
改动协议相关代码前请先读这一节。

- `room_init` 解析真实房间号、主播 uid、开播状态；
- **`getDanmuInfo` 匿名一律返回 `-352` 风控，登录态下叠加 WBI 签名才可用** ——
  这是 `wbi.py` 存在的原因，不是备选组件；失败降级到匿名可用的 `Danmu/getConf`；
- `getConf` 会返回重复节点（实测 6 条中只有 4 个唯一），必须去重；
- 认证包 `op=7`，建连后 5 秒内必须发出；`protover=3`（brotli）/ `2`（zlib）都实测可用；
- **解压后得到的仍是「带头部的包」，必须递归拆包**；
- 心跳 `op=2` 的负载是字面量 `[object Object]`，回应 `op=3` 前 4 字节为人气值；
- 心跳间隔 30 s、假死判定 90 s 无 `op=3` —— **用"多久没收到心跳回应"判假死，
  而不是"多久没收到消息"**（低热度房间消息稀疏，按消息判会疯狂重连）；
- 返回节点端口是 2245（登录态）或 443（匿名），**务必用响应里的 `wss_port`，不要硬编码**；
- 当前进场消息是 `INTERACT_WORD_V2`（不是 `INTERACT_WORD`），其 `data.pb` 是
  **base64 编码的 protobuf**，且**里面根本没有 uid**（登录后也没有）；
- `DANMU_MSG` 优先读 `info[0][15].user` 结构化对象；其中 `extra` 是 **JSON 字符串**，必须二次解析；
- 去重键首选 `info[0][15].extra.id_str`（全局唯一）；
- **礼物族事件（`COMBO_SEND`）字段是 snake_case**：`gift_name` / `combo_total_coin` / `total_num`，
  用户信息在嵌套的 `sender_uinfo`，粉丝牌在 `medal_info`；
- 用户信息分散在多种嵌套块：点赞用 `uinfo`、礼物用 `sender_uinfo`、SC 用 `user_info`；
- 表情占位符**已内嵌在 text**（`哈哈[dog]`），`extra.emots` 给出 `[dog]` → `{url, width, height, ...}` 映射；
- **登录后昵称完全可见**：`LOG_IN_NOTICE` 明确下发「为保护用户隐私，未登录无法查看他人昵称」，
  匿名下部分用户被打码（`宅***`）—— 这是登录态的主要价值；
- 实测链路延迟 **P50 ≈ 0.9–1.4 s / P95 ≈ 1.9–2.2 s**，这是 B 站本身的延迟，不是程序问题；
- 关键词检索**刻意不用 FTS5**：`unicode61` 把连续中文当单个 token（搜「主播」找不到「主播好」），
  `trigram` 又要求查询 ≥3 字符，而中文关键词常为 2 字 —— 统一走 `LIKE` 子串匹配
  （详见 `store.py` docstring）。

---

## 开发

### 目录约定

- **协议假设必须用实测样本验证**：先 `probe --dump-raw` 抓真实帧 → 再改 `parser.py`，
  不要凭记忆写 `DANMU_MSG` 的字段下标；
- 改异步/线程边界前注意：sink 的同步 IO 必须在工作线程里做（`asyncio.to_thread`），
  跨线程的 `sqlite3` 连接必须 `check_same_thread=False` ——
  否则会阻塞事件循环 → 心跳发不出 → 被判假死 → 无谓重连；
- **给表加列要同时管三处**：`SCHEMA`（新库）、`MIGRATIONS`（旧库升级）、
  只读层的自适应查询（缺列兜底），漏一处旧库用户就会撞上 `no such column`；
- **面板与分析不许进采集热路径**：`WebHub.publish()` 里不做排序/聚合，
  每个 WS 订阅者独立有界队列，慢客户端主动断开。

### 代码风格

`ruff` 风格的 100 列行宽、`from __future__ import annotations`、类型标注齐全；
中文注释与文档字符串保持原有密度 —— 这套代码的注释记录的是**决策理由**，不是语法复述。

### 提交信息

采用 [Conventional Commits](https://www.conventionalcommits.org/)：
`feat(web): …` / `fix(login): …` / `refactor(store): …` / `chore(tools): …`

---

## 已知限制

诚实列出，不藏：

| 限制 | 现状 |
|------|------|
| **SC 金额单位是推断值** | 蹲了 3 个高热度房间 4 分钟没抓到一条 SC。留了 `--price-unit milli` 逃生舱，拿到真实样本只需改配置 |
| **跨进程无单实例** | CLI 面板与托盘面板可同时写同一个 `danmaku.db`，两进程抓同一房间会重复入库。全局单例锁的代价更大，正确落点是数据库层唯一约束 |
| **磁盘治理缺失** | JSONL 按小时轮转但**永不清理**，库单调增长（实测单库可到 79 MB）。默认只归档不删除 —— 删数据是不可逆动作 |
| **`web.token` 默认空时本机任意进程可停面板** | 威胁模型是"仅本机"（绑定回环）。它防的是跨站请求，不防同机同用户进程；绑非回环地址时程序强制要求设置 token |
| **只读层的 `LIKE` 在大表上有性能隐忧** | FTS5 被中文分词否掉后暂无替代（中文字符串常为 2 字，`trigram` 要求 ≥3 字符）。先有真实慢查询样本再动手 |
| **热点突增只用合成数据验证过** | 真实直播间的突增分布还没拿到有说服力的样本，该指标可信度低于词频与刷屏判定 |

---

## 许可

[MIT](LICENSE) © 2026 QHaoooLG and bili-danmaku contributors

本项目与哔哩哔哩（Bilibili）官方无关，未获得其背书或赞助。
"哔哩哔哩"、"B 站" 等名称与标识归其各自所有者所有。
