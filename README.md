# bili-danmaku · B 站直播弹幕提取器

实时抓取 B 站直播间**弹幕正文与 SC（Super Chat）内容**，落盘为
**JSONL / CSV+TXT / SQLite**，并提供 **Web 面板**实时查看与导出。

- 设计文档（已评审）：[`DESIGN.md`](DESIGN.md)
- 技术文档（实现级）：[`TECH_DESIGN.md`](TECH_DESIGN.md)
- 隐私说明：**[`docs/PRIVACY.md`](docs/PRIVACY.md)** ← 使用前请先读
- 交接文档：**[`HANDOVER.md`](HANDOVER.md)** ← 新会话/新同学从这里开始

---

## 采集范围（重要）

**只采集「有用户文字内容」的事件**，包括正文里内嵌的表情占位符（如 `哈哈[dog]`）：

| 采集 | 不采集 |
|------|--------|
| `danmaku` —— 有文字的弹幕（含表情占位符） | `interact` —— 某用户进入直播间 / 关注 |
| `super_chat` —— SC 正文 | `guard_buy` —— 上舰信息 |
| | `gift` —— 礼物（有金额但无用户文字） |
| | `like` / `room_stats` / `notice` / `dm_interaction` / `other` |
| | 任何**文本为空**的弹幕 |

被排除的事件**仍会被接收并计入统计的「过滤」计数**，只是不落盘 ——
这样你能看到"漏掉了多少"，而不是无声丢弃。

需要连这些一起落盘时（协议调试场景）：

```powershell
bili-danmaku grab 804456 --all-events        # 或在 config.toml 里设 content_only = false
```

> 礼物是否该保留是个可争议点：它带金额但没有用户文字，当前按「无文字即排除」处理。
> 若你要做礼物统计，把 `content_only` 关掉即可，过滤器代码无须改动。

---

## 当前状态

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M0 | 工程骨架、协议层、接口层、凭据与扫码登录、`probe` 自检 | ✅ 完成 |
| M1 | 抓取核心（连接状态机、自动重连、JSONL 落盘） | ✅ 完成 |
| M2 | CSV/TXT/SQLite 落盘、过滤、重放、统计 | ✅ 完成 |
| M3 | Web 面板（Vite + Vue 3 组件化前端） | ✅ 完成（REST/WS 端到端已验证） |
| M4 | 多房间并发、文档、测试覆盖率、打包 | ✅ 完成（真实双房间并发 + 热门房间压测已验收） |
| V2-a | 弹幕词云（词频 / PNG / HTML，表情占位符单独处理） | ✅ 完成 |
| V2-b | SC 汇总 / 告警 | ✅ 完成 |
| V2-c | 面板常驻化（start/stop/status/logs）+ 面板交互优化 | ✅ 完成 |

测试：**568 项全绿**（全部离线，不依赖真实直播）；核心模块覆盖率 **90.00%**（门禁 85%）。

---

## 环境要求

- Python 3.10+（开发环境为 3.11.3）
- Windows / Linux / macOS 均可

---

## 安装

### 常规方式

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

### 本机特殊方式（pip 网络不可用时）

本开发机上 pip 的网络栈异常（访问 pypi.org 报 `SSLEOFError`，国内各镜像挂死到超时），
但 httpx 访问 PyPI 完全正常。因此提供了一套离线获取方案：

```powershell
# 1. 用系统 Python（需 httpx + packaging）解析并下载 wheel
python tools/wheel_fetch.py --out wheels

# 2. 用 lock 文件里的清单离线安装
$pkgs = (Get-Content wheels\requirements.lock.txt) -replace '==.*$',''
.\.venv\Scripts\python.exe -m pip install --no-input --no-index --find-links wheels @pkgs

# 3. 安装本项目
.\.venv\Scripts\python.exe -m pip install --no-input --no-index --no-deps --no-build-isolation -e .
```

解析出的精确版本会写入 `wheels/requirements.lock.txt`。
`tools/wheel_fetch.py` 是**约束感知**的（会读 `requires_dist` 求版本交集并逐版本回溯），
否则会出现 pydantic 2.13.5 要求 `pydantic-core==2.46.5` 却装了 2.49.0 这类冲突。

### 前端构建（Web 面板用，可选）

面板前端是 Vite + Vue 3 工程。**不构建也能用**——后端会返回一个内建提示页，
REST 与 WebSocket 接口不受影响。

```powershell
cd web

# 本机 registry.npmjs.org 不可达，web/.npmrc 已指向国内镜像，
# 并把 npm 缓存指向工作区内（全局缓存位于沙箱外会 EPERM）
npm install
npm run build          # 产物写入 web/dist，FastAPI 会自动托管
```

开发模式（热更新，自动把 `/api`、`/ws` 代理到 8787 的 Python 后端）：

```powershell
cd web && npm run dev   # 打开 http://127.0.0.1:5173
```

> Vite 在 Windows 上会执行 `net use` 探测网络驱动器；若运行在受限沙箱中，
> 带管道的子进程会被拒绝并报 `spawn EPERM`，此时需要放宽沙箱再构建。

### 打包（wheel）

```powershell
# 1. 先构建前端（想随包分发面板的话；跳过则面板显示降级页）
cd web && npm run build && cd ..

# 2. 构建 wheel：会把 web/dist 暂存进 src/bili_danmaku/web_dist/，构建后清理
.\.venv\Scripts\python.exe tools\build_wheel.py --out dist

# 3. 离线安装（本机 pip 网络不可用）
.\.venv\Scripts\python.exe -m pip install --no-input --no-index --find-links wheels `
  dist\bili_danmaku-0.1.0-py3-none-any.whl
```

- 后端按「包内 `web_dist/` → `./web/dist` → 仓库根 `web/dist`」的顺序找前端，
  所以装好的 wheel **开箱带面板**，不依赖工作区目录结构；
- `pip install .`（不经过该脚本）依然可用，只是面板走降级提示页 —— 这是有意的降级路径；
- `--skip-frontend` 可显式构建"纯 Python 包"。

---

## 使用

```powershell
# 扫码登录（凭据只落在本机 .secrets/，不进日志、不进版本库）
bili-danmaku login

# 校验凭据
bili-danmaku check-auth

# 连通性与协议自检：房间解析 → token → 握手 → 心跳 → 采样
bili-danmaku probe 7878 --seconds 30 --messages 10

# Web 面板：浏览器实时看弹幕、过滤、导出
bili-danmaku web --rooms 7878 --open          # 前台运行（Ctrl+C 停止，适合调试）
bili-danmaku web --port 9000 --out data_web   # 指定端口与输出目录

# 面板常驻：关掉终端也继续跑
bili-danmaku web start --rooms 7878 --open    # 后台启动（1~2 秒返回）
bili-danmaku web status                       # 地址 / pid / 已运行时间
bili-danmaku web logs --tail 50               # 后台日志
bili-danmaku web stop                         # 优雅停止（先 flush 落盘）
bili-danmaku web restart                      # 等价 stop + start
bili-danmaku web stop --force                 # 兜底强杀（会丢少量未落盘数据）

# 抓取并落盘（Ctrl+C 可随时停止并正常收尾）
bili-danmaku grab 804456 --duration 5m --out data
bili-danmaku grab 804456 --format jsonl,csv,txt,sqlite      # 四格式同时落盘
bili-danmaku grab 804456 --until 23:00 --history            # 抓到指定时刻，并补种最近历史弹幕
bili-danmaku grab 804456 --duration 1h --anonymize          # 脱敏输出

# 过滤：只要弹幕与礼物，且丢弃被平台打码的身份
bili-danmaku grab 804456 --types danmaku,gift --exclude-masked

# 采集范围开关（默认只采有文字的弹幕与 SC）
bili-danmaku grab 804456 --all-events                        # 连进房/上舰/点赞等一并落盘
bili-danmaku grab 804456 --keyword 主播 --keyword "re:^\d+$"  # 关键词（re: 前缀走正则）
bili-danmaku grab 804456 --keyword 抽奖 --keyword 转发 --keyword-mode and  # 需同时命中
bili-danmaku grab 804456 --min-price 30                     # 只要 30 以上的礼物/SC/上舰
bili-danmaku grab 804456 --min-medal-level 10 --uid 12345   # 粉丝牌 + uid 白名单

# 看板：从 SQLite 读统计（类型分布、链路延迟分位、发言 TOP、会话列表）
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

# 把原始帧与消息落盘为测试夹具
bili-danmaku probe 804456 --dump-raw tests/fixtures/capture

# 词云（V2）：统计词频，可出终端榜单 / JSON / PNG / HTML
bili-danmaku wordcloud --session 20260927-232630-23438368 --top 30
bili-danmaku wordcloud --room 23438368 --since "2026-09-27 23:00" --format json --out wc.json
bili-danmaku wordcloud --format png --out cloud.png --top 120       # 需要 [wordcloud] 依赖
bili-danmaku wordcloud --format html --out cloud.html               # 零依赖，浏览器打开
bili-danmaku wordcloud --source data/23438368_20260927-23.jsonl    # 离线：直接读 JSONL
bili-danmaku wordcloud --dict room.dict --stopwords-file stop.txt   # 补黑话 / 自定义停用词
bili-danmaku wordcloud --include-emotes                             # 把 [dog] 也算进词榜

# SC 汇总（V2）：条数 / 金额合计 / 贡献榜 / 最近明细
bili-danmaku sc --room 23438368                 # 全部 SC
bili-danmaku sc --room 23438368 --min-price 30  # 只看 30 元及以上
bili-danmaku sc --session <id> --json --top 5   # JSON，贡献榜取前 5

# SC 实时告警：跟一个房间，达到门槛就打成醒目告警行（不落盘）
bili-danmaku sc 23438368 --follow --min-price 30 --duration 3600 --bell

# 删除本机保存的凭据
bili-danmaku purge
```

`probe` 退出码：`0` 正常 · `2` 配置错误 · `3` 房间/接口问题 · `4` 认证失败 · `5` 网络不可用
`grab` 退出码：同上，`0` 含被 Ctrl+C 正常中断的情况

> 注意 `--min-price` 只对**带金额的类型**（礼物 / SC / 上舰）生效，其余类型视为不通过 ——
> 即用它就意味着"我只要高价值事件"。

### 词云说明（V2）

**表情占位符默认单独成榜**：弹幕里的表情是内嵌占位符（`哈哈[dog]`），
若当成普通词统计，词频榜会被表情刷屏。所以 `[dog]` 默认不计入词语榜，
而是列在「表情占位符」一节，并带上 `extra.emots` 里的图片链接；
要一起统计就加 `--include-emotes`（此时以 `[dog]` 原形入榜，便于和正文词区分）。

输出格式：

| 格式 | 依赖 | 说明 |
|------|------|------|
| `text`（默认） | 无 | 终端榜单 + 表情榜 |
| `json` | 无 | 带元信息（条数、词数、分词器、筛选条件），便于二次加工 |
| `html` | 无 | 自包含单文件，字号映射词频；**没有中文字体也能正常显示**（用浏览器字体） |
| `png` | `[wordcloud]` + 系统中文思源/雅黑等字体 | 需要显式字体路径，否则中文变方块；后端会自动探测系统字体 |

分词：装了 `jieba` 就用它，否则退回**内置二元组兜底分词器**（功能仍可用，只是效果差些）。
房间黑话分不出来时用 `--dict` 补词（一行一个词）。

### SC 说明（V2）

```powershell
bili-danmaku sc --room 23438368 --min-price 30   # 汇总
bili-danmaku sc 23438368 --follow --min-price 30 # 实时告警（不落盘）
```

- **金额单位是元**。SC 的 `price` 字段本身就是元（30/50/100/500/1000/2000 这类档位）；
  **礼物/上舰才是金瓜子（1000 = 1 元）**，所以 `--min-price` 内部会把元门槛按类型换算
  （SC 直接比，礼物/上舰 ×1000），不会出现"3 分钱礼物当成 3 块钱"的错判。
- ⚠️ **SC 的金额单位尚未用真实样本验证**（实测蹲了 3 个高热度房间 4 分钟没出一条 SC）。
  如果拿到真实数据发现金额差了 1000 倍，用 `--price-unit milli` 覆盖即可，不必改代码。
- `--follow` 的告警**只打印、不落盘**：你要的是提醒，不是再存一份。
  想同时存档就另开一个 `bili-danmaku grab --min-price 30`。
- 面板侧对应能力：顶栏筛选区有「SC 提示 ≥ N 元」，达到门槛的 SC 会在弹幕流上方弹横幅
  （可改阈值，0 = 每条都提示；设置存在浏览器本地）。

---

## 隐私

**扫码登录得到的凭据只保存在本机，且项目不含任何上传逻辑。**

- 凭据位置：`.secrets/cookies.json`（已 gitignore，权限收紧为仅当前用户）
- 纯内存模式：设置 `BILI_SESSDATA` 环境变量，凭据完全不落盘
- 凭据永不进入日志：所有日志经统一脱敏过滤器（`SESSDATA=<redacted>`）
- **不保存账号资料**：只保留连接所需的 cookie 字段，昵称/mid/头像等一律丢弃
- 抓取到的第三方数据默认原样保留（含平台打码结果），可用 `--exclude-masked` / `--anonymize` 处理

详见 [`docs/PRIVACY.md`](docs/PRIVACY.md)。

---

## 开发者说明

### 项目结构

```
src/bili_danmaku/
  protocol.py      封包/拆包/zlib+brotli      ← 只认字节，可离线单测
  protobuf_lite.py 通用 protobuf 解析          ← INTERACT_WORD_V2 的 data.pb
  parser.py        cmd → Event 归一化          ← 只认 JSON，金样本回归
  text.py          分词 / 词频 / 词云渲染      ← V2；表情占位符单独处理，纯函数可离线单测
  analysis.py      本地感知分析 ← V3-b；**纯函数、无 IO/网络**，数据不出本机
  collector.py     连接状态机 + 自动重连 + 去重管道
  api.py           HTTP 接口 + 限流 + token 降级
  credentials.py   扫码登录 / 掩码 / 清除
  wbi.py           WBI 签名（含官方向量测试）
  client.py        WebSocket 连接与认证
  filters.py       过滤规则（类型/关键词/uid/金额/粉丝牌）
  store.py         SQLite 只读查询层（统计 / 导出 / **历史游标分页**）
  rooms.py         房间号 / 直播间链接解析 ← V3-a3；纯函数，识别不出就报错（不猜）
  annotations.py   标注（标记/备注）存储 ← V3-a2；**独立库**，与采集库分离
  sinks/           jsonl / csv / txt / sqlite + 扇出总线
  server/          hub.py（广播中枢）/ panel.py（多房间）/ app.py（FastAPI）
  cli.py           命令行入口（11 个子命令）
web/               Vite + Vue 3 面板前端（组件化，构建产物 dist/；开发与构建细节见 web/README.md）
web/test/          前端用例（node:test；纯逻辑 + **组件级**）：`cd web; npm test`
tools/
  preflight.py       依赖 + 连通性 + 风控探测
  handshake_check.py 协议握手实测与样本抓取
  wheel_fetch.py     pip 不可用时的离线 wheel 获取（约束感知）
  verify_formats.py  四种落盘格式的一致性校验
  build_wheel.py     构建 wheel（把 web/dist 一并打进包内 web_dist/）
  hot_rooms.py       查询当前人气最高的直播间（挑压测房间用）
  stress.py          高热度房间压力测试：吞吐 / 背压 / 四种产物完整性
  env_diagnose.py    环境诊断
tests/fixtures/    真实抓取的夹具（金样本回归依据）
```

### 文档地图

| 文档 | 什么时候读它 |
|------|--------------|
| [`HANDOVER.md`](HANDOVER.md) | **权威现状**：已完成什么、验收证据、实测协议结论、开发陷阱 |
| [`TECH_DESIGN.md`](TECH_DESIGN.md) | 实现级设计：为什么这么写、缺陷档案 |
| [`ROADMAP.md`](ROADMAP.md) | **未来方向**：复盘器 / 感知分析 / 工程债三条线、风险说明、预留扩展点、待决策项 |
| [`web/README.md`](web/README.md) | 前端工程约定与 15 条手测清单 |
| [`SESSION_HANDOVER.md`](SESSION_HANDOVER.md) | 新会话 10 分钟接手 |
| [`docs/PRIVACY.md`](docs/PRIVACY.md) | 隐私边界（引入任何"外发数据"功能前必读） |

### Web 面板

后端把采集到的事件扇出给所有 WS 订阅者（`server/hub.py`），
每个订阅者独立有界队列，**连续丢弃超阈值会被主动断开**——
面板是采集的消费者，不该反向影响数据完整性。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 前端页面（未构建时返回内建提示页） |
| GET | `/api/health` | 状态、房间数、落盘格式、前端是否已构建 |
| GET | `/api/rooms` | 监控中的房间与连接状态（含主播昵称 `anchor_name`） |
| GET | `/api/room-info` | `room=<房间号/短号>` → 主播昵称/头像、直播间标题、是否在监控中（**只查信息，不启动采集**） |
| POST | `/api/rooms` | `{"room": 804456}`，重复返回 409，失败返回 400 |
| DELETE | `/api/rooms/{id}` | 停止监控，不存在返回 404 |
| GET | `/api/events` | `room_id,type,q,since,until,limit,offset`（完整 `Event`，含 raw） |
| GET | `/api/history` | **历史浏览（V3-a）**：`room_id,session_id,type,q,limit` + 游标 `before_ts`+`before_id`（向更旧）或 `after_ts`+`after_id`（向更新）。返回**瘦投影**（不含 raw）、按时间升序 |
| GET | `/api/density` | **时间轴密度（V3-a）**：`bucket_s` 按桶聚合的条数分布（缺省按数据跨度自适应步长） |
| GET | `/api/sessions` | **抓取会话列表（V3-a）**：起止时间与事件数，用于"只看某一次抓取" |
| GET | `/api/annotations` | **列出标记（V3-a2）**：`room_id,since,until,limit`；库不存在时返回空列表 |
| POST | `/api/annotations` | **打标记**：`{room_id, recv_ts, label?, note?, anchor_id?}` |
| DELETE | `/api/annotations/{id}` | 删除一个标记 |
| GET | `/api/annotations/export` | **按标记区间导出**：`bookmark_id`（必填）+ `to_bookmark_id?` + `format`；只给起点时终点自动取下一个标记 |
| GET | `/api/stats` | `window` 秒数；**`window=0`（默认）= 自面板启动累计**，>0 为最近时段 |
| GET | `/api/analysis` | **本地感知分析（V3-b）**：`room_id,top,tokenizer` → 实时词频 / 热点突增 / 疑似刷屏 / 疑似异常账号。**全部本地计算，数据不出本机** |
| GET | `/api/export` | 流式导出 `csv/jsonl/txt`（CSV 带 UTF-8 BOM，Excel 直接打开不乱码） |
| POST | `/api/admin/shutdown` | **停止面板**（优雅退出，先 flush 落盘）。**只接受本机来源**，非回环一律 403 |
| WS | `/ws/live` | 实时事件推送，支持 `room_id` / `types` / `keyword` |

前端能力：实时弹幕流（贴底自动滚动，上滚时暂停并提示新消息）、
**复盘器 / 历史浏览（V3-a）**：滚到顶部自动加载更早的弹幕（**不受 2000 条缓冲限制**，
数据直接来自 SQLite）、**底部时间轴密度条**（点任意时刻跳转、当前窗口高亮）、
"↑更早 / ↓更新 / 回到实时"、**按抓取会话浏览**、历史浏览中导出自动限定在当前窗口的时间范围；
**标记与切片（V3-a2）**：时间轴下方打标记（可给标签）、弹幕行 ★ 钉住某一条、
标记列表可跳转、**选一个标记作为导出起点（终点自动取下一个标记）**、删除与清空。
标注存**独立库** `data/annotations.db`（可配 `sinks.annotations_path`），**采集库一张表都不动**；
**直播间链接解析（V3-a3）**：右栏顶部可直接粘 `https://live.bilibili.com/8432038?...`
自动识别房间号（也支持纯房间号/短号/`?room_id=`）；识别不出就明确报错，不猜；
**时间轴粒度可切换**（5/10/15 分钟，切换即时重算格数与平均密度）；
**发言 TOP 在"从启动累计"下也默认显示**（角标标明范围）；
**感知分析（V3-b 3A）**：右栏底部实时显示**词频榜**（点词即可只看含该词的弹幕）、
**热点突增词**、**疑似刷屏**（同一文本被反复发送）、**疑似异常账号**（密度/重复率/集中度打分，
并列出判定理由）。全部本地计算，面板上明确标注"仅供参考、非判定、数据不出本机"；
**类型多选过滤**（自绘下拉：收起态显示"已选摘要 + 色块"，展开态每行带类型条数；客户端过滤，切换不重连）、
**关键词双模式**（高亮 / 只显示命中，可随时切回）、
关键词高亮用分段渲染而非 `v-html`（避免弹幕正文造成 XSS）、
**表情占位符渲染成图片**（用 `extra.emots` 的映射，只接受 http(s) 地址）、
高额 SC 横幅（阈值可调）、**已运行时间 + 呼吸灯**、**统计默认从启动累计**（可切最近时段）、
**右栏顶部显示「主播昵称的直播间」并为房间号做本地记忆**（输入建议 + 一键填充）、
**类型分布横向彩色柱状图**（条数为右、每类型一色）、按当前筛选导出、**「停止面板」按钮**（二次确认，停止前先落盘）。

前端工程结构、行为约定与验收清单见 [`web/README.md`](web/README.md)。

### 面板常驻（本机专用）

```powershell
bili-danmaku web start --rooms 7878 --open   # 后台启动
bili-danmaku web status                      # 看状态
bili-danmaku web stop                        # 优雅停止
```

- **停止的三条路径**：① 面板右上角「停止面板」按钮；② `bili-danmaku web stop`；
  ③ 兜底 `taskkill /PID <pid> /F`（**会丢队列里未落盘的批次**，`web status` 会打印 pid）。
- **为什么不能靠信号停**：Windows 上 `CTRL_BREAK_EVENT` 对独立进程组的 uvicorn 无效，
  `taskkill`（不带 `/F`）会被系统拒绝。所以停止是"**让服务自己退出**"：
  CLI/按钮 → 仅回环的 `POST /api/admin/shutdown` → uvicorn 优雅收尾 →
  每个房间 `bus.stop()` 把队列 flush 落盘。
- **状态与日志**：`.runtime/web.json`（pid/端口/房间）、`.runtime/web.log`（后台输出），
  均在 `.gitignore` 内。进程崩溃后残留的状态文件会在下次 `status/start` 时自动清理。
- **仅本机使用**（当前阶段）：`web.host` 保持回环，不开放局域网/多用户；
  也**不做开机自启** —— 崩溃后数据仍保留在磁盘上，由你手动 `web start` 恢复。

### 压测

```powershell
# 吞吐 + 四产物一致性（房间 7878 约 300 条/分钟；输出到 data_stress/<房间>_<时间戳>/）
python tools/stress.py --room 7878 --duration 90

# 强制背压：队列压到 3 条，验证"丢最旧、如实计数、且不重连"
python tools/stress.py --room 7878 --duration 60 --queue-size 3 --flush-interval 5
```

实测（房间 7878）：默认配置约 **300 条/分钟**下零丢弃、零重连，JSONL/CSV/TXT/SQLite
四种产物条数完全一致；把队列压到 3 条时会丢最旧的若干条，
**但采集连接不受影响（0 重连）**，丢弃条数如实计入统计。

### 测试

```powershell
.\.venv\Scripts\python.exe -m pytest              # 默认跳过联网测试
.\.venv\Scripts\python.exe -m pytest -m live      # 需要真实联网
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing   # 覆盖率（门禁 85%）
```

覆盖率口径：只统计**核心模块**（协议 / 解析 / 连接 / 过滤 / 落盘 / 服务端 / 存储 / 网络接口），
`cli.py` 与 `__main__.py` 不参与百分比（它们由 `tests/test_cli.py` 的端到端用例保证），
配置见 `pyproject.toml` 的 `[tool.coverage.*]`。

前端部分的开发、构建与验收见 [`web/README.md`](web/README.md)。

### 协议关键事实（实测，勿凭记忆改写）

对房间 `804456` 的真实只读实测结论，详见 `TECH_DESIGN.md` §1：

- `Danmu/getConf` **匿名可用**，是保底路径；`getDanmuInfo` 匿名返回 **-352 风控**，
  **登录态下叠加 WBI 签名才可用**（这是 `wbi.py` 存在的原因）
- `protover=2` 为 zlib、`3` 为 brotli（实测均可用），解压后仍是**带头部的包，必须递归拆包**
- 心跳负载是字面量 `[object Object]`，回应前 4 字节为人气值
- 当前进场消息是 `INTERACT_WORD_V2`（不是 `INTERACT_WORD`），其 `data.pb` 是
  **base64 编码的 protobuf**，不含 uid，昵称/房间/时间戳/互动类型需自行解析
- **登录后昵称完全可见**：`LOG_IN_NOTICE` 明确下发「为保护用户隐私，未登录无法查看他人昵称」；
  匿名下部分用户被打码（`宅***`），登录后打码消失 —— 这是登录态的主要价值
- `DANMU_MSG` 优先读 `info[0][15].user` 结构化对象；其中 `extra` 是 **JSON 字符串**
- 去重键首选 `info[0][15].extra.id_str`（全局唯一）
- **礼物族事件（`COMBO_SEND`）字段是 snake_case**：`gift_name` / `combo_total_coin` /
  `total_num`，用户信息在嵌套的 `sender_uinfo`，粉丝牌在 `medal_info`
- 用户信息分散在多种嵌套块：点赞用 `uinfo`、礼物用 `sender_uinfo`、SC 用 `user_info`
- `getConf` 会返回重复节点（实测 6 条中只有 4 个唯一），必须去重
- 关键词检索**刻意不用 FTS5**：中文分词要么把整句当一个 token，要么要求 ≥3 字符，
  而中文关键词常为 2 字 —— 统一走 `LIKE` 子串匹配（详见 `store.py` docstring）

---

## 合规

仅用于个人学习、研究或自有直播间运营分析等授权场景。
请遵守 B 站用户协议与相关法律法规，不要用于骚扰、人肉、批量爬取或商业转售。
本项目默认单房间单连接、限制并发房间数、强制 HTTP 调用最小间隔。
