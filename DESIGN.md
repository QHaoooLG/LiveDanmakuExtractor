# B 站直播弹幕提取器 —— 设计文档（V1，待审阅）

> 状态：**草稿 / 等待评审**。评审通过后才进入编码。
> 目标读者：项目发起人（审阅者）+ 后续实现者。

---

## 1. 目标与非目标

### 1.1 目标

| # | 目标 | 验收标准 |
|---|------|----------|
| G1 | 从一个 B 站直播间实时抓取弹幕与相关事件 | 输入房间号，`probe` 自检通过，实时收到 `DANMU_MSG` |
| G2 | 三种落盘格式：JSONL / CSV+TXT / SQLite | 同一场采集可同时产出三种产物，内容一致 |
| G3 | 提供轻量 Web 面板，浏览器里实时看弹幕流 | `bili-danmaku web` 后打开 `http://127.0.0.1:8787` 能看到秒级实时弹幕 |
| G4 | 支持登录 Cookie（SESSDATA）以提升稳定性与消息完整度 | 配置文件 / 环境变量注入 Cookie，匿名模式仍可用 |
| G5 | 断线自愈、长时间运行不丢不爆 | 连续运行 6h，断网重启后自动重连，内存平稳 |
| G6 | 纯 CLI 可独立使用，不依赖 Web 面板 | 面板未启动时抓取功能完全正常 |

### 1.2 非目标（V1 明确不做）

- 不做直播流（视频/音频）录制与下载 —— 只处理弹幕数据面。
- 不做弹幕**发送**（不发弹幕、不刷屏、不互动）。
- 不做历史弹幕全量回溯爬取（只做 `gethistory` 最近窗口的补种，见 §5.5）。
- 不做用户画像 / 跨房间聚合分析 / 词云可视化（可作为 V2）。
- 不做多机分布式采集。

### 1.3 合规与边界

- 仅用于**个人学习、研究、自有直播间运营分析**等授权场景；使用时须遵守 B 站用户协议与 robots 约定。
- 礼物、SC 等含**金钱信息**，弹幕含 UID/昵称等**个人信息**，落盘数据默认保存在本地 `data/`，不自动外传。
- 提供 `--anonymize` 开关：对 uid 做加盐哈希、昵称做掩码，便于分享样本而不泄露隐私。
- 默认单房间单连接、默认最多 3 个并发房间，**不做高频重连与接口轮询**，从设计上避免给服务端造成压力。

---

## 2. 技术选型（已确认）

| 维度 | 选择 | 理由 |
|------|------|------|
| 语言 | Python 3.11（本机 3.11.3） | 协议处理 / 数据落盘生态最好；asyncio 适合长连接 |
| 并发模型 | asyncio 单事件循环，每房间 1 个连接任务 | 单连接语义简单，天然适合 IO 密集 |
| HTTP | `httpx`（已装） | 复用 cookie jar、支持 HTTP/2、async 原生 |
| WebSocket | `websockets`（需装） | asyncio 原生；**Plan B** 见 §11.3 |
| 压缩 | `brotli`（需装）+ 标准库 `zlib` | `protover=3` 是 brotli，`protover=2` 是 zlib，两者都要支持 |
| CLI | `click`（已装） | 子命令 / 参数校验清晰，依赖已有 |
| 数据模型 | `pydantic v2`（已装） | 配置与事件模型的校验、序列化 |
| Web 面板 | `fastapi` + `uvicorn`（需装） | REST + WebSocket 一体，自带 ASGI 测试客户端 |
| 前端 | 单文件原生 HTML/JS（无 node 构建） | 面板是附属功能，不引入构建链，改起来快 |
| 存储 | JSONL / CSV+TXT / SQLite（标准库 `sqlite3`） | 零额外依赖，SQLite 走 WAL 模式 |
| 测试 | `pytest` + `pytest-asyncio` | 离线夹具回放，不依赖真实直播 |

依赖清单（`pyproject.toml` 运行时）：

```
httpx>=0.27      websockets>=12     brotli>=1.1
click>=8.1       pydantic>=2        fastapi>=0.110   uvicorn[standard]>=0.29
```

**M0 前置检查**：确认能否 `pip install` 上述 3 个缺失包。若 PyPI 不可达，走 §11.3 的降级方案，不阻塞主线。

---

## 3. 总体架构

```
                    ┌──────────────────────────────────────────┐
                    │              CLI  (click)                │
                    │  grab | web | replay | probe | export    │
                    └───────────────────┬──────────────────────┘
                                        │
                          ┌─────────────▼─────────────┐
                          │      Config (pydantic)    │
                          │  cookie / 房间 / 过滤 / 输出 │
                          └─────────────┬─────────────┘
                                        │
   ┌────────────────────────────────────▼─────────────────────────────────────┐
   │                        RoomCollector  (每房间一个)                        │
   │                                                                          │
   │  ┌──────────┐   ┌────────────┐   ┌──────────┐   ┌─────────┐   ┌───────┐  │
   │  │ api.py   │──▶│ client.py  │──▶│parser.py │──▶│filters  │──▶│ bus   │  │
   │  │ HTTP 元数据│   │ WS 长连接  │   │ 原始→事件 │   │  规则   │   │ 广播  │  │
   │  └──────────┘   └────────────┘   └──────────┘   └─────────┘   └───┬───┘  │
   │       ▲               ▲                                          │      │
   │       │          protocol.py (封包/解包/解压)                      │      │
   │       └── wbi.py (风控签名, 按需)                                  │      │
   └──────────────────────────────────────────────────────────────────┼──────┘
                                                                      │
        ┌──────────────┬──────────────┬──────────────┬────────────────┤
        ▼              ▼              ▼              ▼                ▼
   JsonlSink      CsvTxtSink     SqliteSink    WebHub         (V2: 更多 sink)
   逐行 append    轮转写文件      WAL 批量事务   广播给面板 WS
```

**关键设计原则**

1. **协议层与业务层解耦**：`protocol.py` 只认字节，`parser.py` 只认 JSON，便于离线单测与协议变更时快速定位。
2. **原始消息永远保留**：归一化事件里带 `raw` 字段（可 `--no-raw` 关闭），协议字段位置变了也能事后重新解析。
3. **Sink 背压可控**：每个 sink 一个有界队列，满了丢最旧并计数告警，绝不 OOM。
4. **面板是消费者，不是依赖**：`WebHub` 只是 bus 的一个订阅者，面板挂掉不影响采集。

---

## 4. 目录结构

```
LiveDanmakuExtractor/
├─ DESIGN.md                  # 本文档
├─ README.md                  # 使用说明（M4 完成）
├─ pyproject.toml             # 依赖 + entry point: bili-danmaku
├─ .gitignore                 # .venv/ data/ .env cookies.json
├─ .env.example               # BILI_SESSDATA= / BILI_BUVUD3=
├─ config.example.toml        # 配置模板
├─ src/bili_danmaku/
│  ├─ __main__.py             # python -m bili_danmaku
│  ├─ cli.py                  # click 子命令
│  ├─ config.py               # 配置加载（CLI > env > toml > 默认）
│  ├─ api.py                  # room_init / getDanmuInfo / fingerprint / gethistory
│  ├─ wbi.py                  # WBI 签名（getDanmuInfo 被风控时启用）
│  ├─ protocol.py             # 16B 包头、pack/unpack、zlib/brotli
│  ├─ client.py               # WS 连接、认证、心跳、重连、限流
│  ├─ parser.py               # cmd → 归一化 Event
│  ├─ models.py               # Event / RoomInfo / Config (pydantic)
│  ├─ filters.py              # 类型/关键词/UID/价格/等级 过滤
│  ├─ sinks/
│  │  ├─ base.py              # Sink 抽象 + 有界队列 + 统计
│  │  ├─ bus.py               # 多 sink 扇出
│  │  ├─ jsonl.py
│  │  ├─ csv_txt.py
│  │  └─ sqlite.py
│  ├─ server/
│  │  ├─ app.py               # FastAPI 应用 + WS 推送
│  │  └─ static/index.html    # 单文件前端
│  └─ utils.py                # 日志、退避、限流、去重 LRU
├─ tests/
│  ├─ test_protocol.py
│  ├─ test_parser.py
│  ├─ test_filters.py
│  ├─ test_sinks.py
│  ├─ test_client_reconnect.py
│  ├─ test_server_api.py
│  ├─ fake_server.py          # 脚本化假 WS 服务端
│  └─ fixtures/               # 实抓样本 .json / .bin
└─ data/                      # 运行期产物（gitignore）
```

---

## 5. B 站弹幕协议实现要点

> 以下为需要落进 `protocol.py` / `api.py` 的事实性约定。**字段位置以实抓样本回归测试锁定**，不以文档为最终依据。

### 5.1 房间元信息

| 用途 | 接口 |
|------|------|
| 短号 → 真实房间号、开播状态 | `GET https://api.live.bilibili.com/room/v1/Room/room_init?id={短号或真实号}` |
| 取 WS 地址 + token | `GET https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?id={真实房间号}&type=0` |
| 兜底取 WS 地址 + key | `GET https://api.live.bilibili.com/room/v1/Danmu/getConf?room_id={真实房间号}&platform=pc&player=web` |
| 设备指纹 | `GET https://api.bilibili.com/x/frontend/finger/spi` → `data.b_3`(buvid3) / `data.b_4`(buvid4) |
| 最近历史弹幕（补种/离线测试） | `GET https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory?roomid={真实房间号}&room_type=0` |

- `getDanmuInfo` 返回 `data.token` 与 `data.host_list[]`（含 `host` / `wss_port` / `ws_port`），**token 会过期**：每次重连都重新获取，不长期缓存。
- 匿名请求通常需要携带 `buvid3` Cookie；缺失时可能被拒。因此**首次运行自动获取并持久化 buvid3**（见 §7.2）。
- 若 `getDanmuInfo` 返回风控错误码（如 `-352`），启用 `wbi.py` 做签名重试；再失败则退回 `Danmu/getConf`。

### 5.2 封包格式（大端序）

```
偏移  长度  类型      含义
0     4    uint32   包总长度（含 16B 头）
4     2    uint16   包头长度，固定 16
6     2    uint16   协议版本 protover
8     4    uint32   操作码 operation
12    4    uint32   序列号，固定 1
16    ..   bytes    负载
```

protover：

| 值 | 负载 |
|----|------|
| 0 | 未压缩 JSON |
| 1 | 心跳包 / 心跳回应（含人气值） |
| 2 | **zlib** 压缩的 JSON |
| 3 | **brotli** 压缩的 JSON |

operation：

| 值 | 方向 | 含义 |
|----|------|------|
| 2 | C→S | 心跳（建议 30s 一次） |
| 3 | S→C | 心跳回应（含人气值） |
| 5 | S→C | 通知（弹幕/礼物/SC/公告等） |
| 7 | C→S | 认证（进房），建连后 5s 内必须发出 |
| 8 | S→C | 认证回应 |

- **嵌套包**：`protover=2/3` 解压后，负载内部仍是「16B 头 + 负载」的若干子包，需**循环解包**直到消费完。
- **认证包负载**：

```json
{
  "uid": 0,
  "roomid": 23242692,
  "protover": 3,
  "buvid": "<buvid3>",
  "platform": "web",
  "type": 2,
  "key": "<getDanmuInfo 返回的 token>"
}
```
  已登录时 `uid` 填真实 UID；`protover` 只声明 3（服务端会自动降级到 2，客户端两种情况都要能解）。

- **心跳包负载**：B 站网页端实际发送的是字符串 `[object Object]`；实现按此发送，负载内容服务端不校验。
- **认证回应**：`code == 0` 视为成功；失败立即断开并进入重连（先刷新 token/buvid 再重试）。

### 5.3 消息归一化

`cmd` 字段区分消息类型，部分带后缀（如 `DANMU_MSG:4:0:2:2:2:0`）——**匹配时按 `cmd.split(':')[0]` 归一**。

| 归一化 type | 原始 cmd | 关键字段 |
|------------|----------|----------|
| `danmaku` | `DANMU_MSG` | `info[1]` 文本；`info[2]` 用户；`info[3]` 粉丝牌；`info[7]` 房管等级；`info[9].ts` 时间戳 |
| `super_chat` | `SUPER_CHAT_MESSAGE` | `data.message` / `price` / `uid` / `user_info.uname` / `time` |
| `gift` | `SEND_GIFT` / `COMBO_SEND` | `data.giftName` / `num` / `price` / `total_coin` / `uid` / `uname` |
| `guard_buy` | `GUARD_BUY` / `USER_TOAST_MSG` | `data.guard_level` / `num` / `price` / `uid` |
| `interact` | `INTERACT_WORD` | `data.msg_type`(进房/关注/分享) / `uname` / `uid` / `fans_medal` |
| `like` | `LIKE_INFO_V3_CLICK` / `LIKE_INFO_V3_UPDATE` | `data.uname` / `like_text` / `click_count` |
| `room_stats` | `ROOM_REAL_TIME_MESSAGE_UPDATE` / `WATCHED_CHANGE` / `ONLINE_RANK_COUNT` | 粉丝数/看过/高能榜 |
| `live_status` | `LIVE` / `PREPARING` / `STOP_LIVE_ROOM` | 开播 / 准备中 / 下播 |
| `notice` | `NOTICE_MSG` / `SYS_MSG` / `ANCHOR_LOT_*` | 公告、系统消息、天选 |
| `other` | 未识别 | 原样透传，仅记 cmd + raw |

> `DANMU_MSG` 的 `info` 数组下标较多且官方未公开，实现时以 `probe --dump-raw` 抓到的真实样本写**金样本测试**（§10.2）逐个锁定，不做"凭记忆写死"。

### 5.4 心跳与超时

- 发送周期 30s（配置可调，默认不高于 30s）。
- 记录 `last_hb_reply_at`；超过 **90s** 未收到 op=3 回应 → 判定连接假死，主动重连。
- 接收侧设读超时，避免半开连接永久挂起。

### 5.5 历史补种

抓取启动时可选调用 `gethistory`（返回最近约 10 分钟弹幕）写入 sink，并打标 `source: "history"` 与 `ts` 排序，避免和实时流重复（靠去重键合并）。用途：
1. 刚启动就能在面板看到内容，不是空白页；
2. 离线测试无需真实直播。

---

## 6. 数据模型与存储

### 6.1 归一化事件（`models.Event`）

```json
{
  "seq": 1024,
  "session_id": "20250101-153000-23242692",
  "room_id": 23242692,
  "short_id": 0,
  "type": "danmaku",
  "cmd": "DANMU_MSG:4:0:2:2:2:0",
  "source": "live",
  "recv_ts": 1767225600.412,
  "server_ts": 1767225598,
  "latency_ms": 2412,
  "uid": 415822879,
  "uname": "某观众",
  "text": "哈哈哈哈哈",
  "price": null,
  "guard_level": 0,
  "user_level": 20,
  "medal": { "name": "某主播", "level": 12, "anchor_uid": 22550271, "is_light": 1 },
  "is_anchor": false,
  "is_admin": false,
  "extra": {},
  "raw": { "cmd": "DANMU_MSG", "info": ["...省略..."] }
}
```

- `recv_ts`：本机收到时刻（float 秒，ms 精度）；`server_ts`：消息自带时间戳；`latency_ms` 为两者差，用于观测链路延迟。
- `raw` 可通过 `--no-raw` 关闭以显著减小体积（约省 60%+）。
- `--anonymize` 时 `uid` → 加盐 sha256 前 12 位，`uname` → `user_<hash6>`。

### 6.2 JSONL

- 每行一个事件 JSON，UTF-8，`ensure_ascii=False`。
- 文件按小时或大小轮转：`{room}_{YYYYmmdd-HHMM}.jsonl`，默认单文件上限 128MB。
- 行尾 `\n`，写入后 `flush()` 但不 `fsync`（每秒一次批量 flush，兼顾性能与可恢复性）。

### 6.3 CSV / TXT

- `*.txt`：纯弹幕文本，`[HH:MM:SS] 昵称: 内容`，用于词云/直接阅读。
- `*.csv`：`ts,type,uid,uname,text,price,guard_level,medal_level`，`utf-8-sig` 编码（Excel 直接打开不乱码）。
- 同样按小时轮转。

### 6.4 SQLite

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);        -- schema_version / room / 采集范围

CREATE TABLE session (
  id TEXT PRIMARY KEY, room_id INTEGER, short_id INTEGER,
  anchor_uid INTEGER, started_at REAL, ended_at REAL, note TEXT
);

CREATE TABLE event (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT NOT NULL,
  room_id       INTEGER NOT NULL,
  recv_ts       REAL    NOT NULL,
  server_ts     INTEGER,
  type          TEXT    NOT NULL,
  cmd           TEXT    NOT NULL,
  source        TEXT    NOT NULL DEFAULT 'live',
  uid           INTEGER,
  uname         TEXT,
  text          TEXT,
  price         INTEGER,
  guard_level   INTEGER,
  user_level    INTEGER,
  medal_name    TEXT,
  medal_level   INTEGER,
  dedupe_key    TEXT,
  raw_json      TEXT,
  UNIQUE(dedupe_key) ON CONFLICT IGNORE
);

CREATE INDEX idx_event_room_ts ON event(room_id, recv_ts);
CREATE INDEX idx_event_type_ts ON event(type, recv_ts);
CREATE INDEX idx_event_uid     ON event(uid);

-- 可选：若运行环境 SQLite 支持 FTS5，则建全文索引用于关键词检索
CREATE VIRTUAL TABLE event_fts USING fts5(text, content='event', content_rowid='id');
```

- 写入策略：内存攒批（默认 200 条或 1s），单写事务提交。
- **去重键** `dedupe_key = sha1(f"{type}|{uid}|{server_ts}|{rnd_or_text}")`，`rnd` 可用时优先（B 站消息自带随机串，比内容更可靠）；不可用时退化为内容哈希 + 120s 时间窗，避免"同一秒两条相同弹幕"被误杀。

---

## 7. 配置与凭据

### 7.1 优先级

```
CLI 参数  >  环境变量(BILI_*)  >  --config 指定的 TOML  >  内置默认值
```

`config.example.toml`：

```toml
[general]
log_level = "INFO"
max_rooms = 3
anonymize = false

[api]
cookie = ""                  # 建议留空，改用环境变量或 cookie_file
cookie_file = ".secrets/cookies.txt"
request_timeout = 10
min_request_interval = 1.0   # 两次 HTTP 调用最小间隔（秒），防风控

[client]
heartbeat_interval = 30
heartbeat_timeout = 90
reconnect_max_delay = 60
protover = 3

[sinks]
types = ["jsonl", "csv", "sqlite"]
out_dir = "data"
keep_raw = true
rotate_hours = 1
queue_size = 20000

[web]
host = "127.0.0.1"
port = 8787
token = ""                   # 绑定非 127.0.0.1 时强烈建议设置
```

### 7.2 Cookie 与设备指纹

- 优先读 `BILI_SESSDATA` 环境变量；其次 `--cookie-file`；最后 `[api].cookie`。
- 首次运行若缺少 buvid3，自动调用 `finger/spi` 获取并写入 `.secrets/buvid.json` 复用（同设备保持一致，降低风控概率）。
- `.secrets/`、`.env`、`cookies*.txt` 全部进 `.gitignore`；**日志与错误信息中对 Cookie 值做掩码**，任何情况下不打印完整凭据。
- 提供 `bili-danmaku check-auth` 子命令验证 Cookie 是否有效（调 `nav` 接口看是否登录）。

---

## 8. CLI 设计

```
bili-danmaku <command> [options]

probe    <room>                        连通性与协议自检（M0 核心）
grab     <room>... [--duration 1h]     抓取并落盘
web      [--rooms 3,1017] [--open]     启动 Web 面板（可选同时开抓取）
replay   <file.jsonl> --to csv [--out] 离线转换/重放，不联网
export   --session <id> --format csv  从 SQLite 导出
stats    [--session <id>]              统计：总量/类型分布/发言 TOP/分钟速率
check-auth                             校验 Cookie 有效性
```

`grab` 常用参数：

| 参数 | 说明 |
|------|------|
| `--format jsonl,csv,txt,sqlite` | 选择 sink（默认全部） |
| `--out DIR` | 输出目录（默认 `data/`） |
| `--types danmaku,gift,super_chat` | 只要指定类型（默认全部） |
| `--keyword K`（可重复） | 关键词命中（支持 `re:` 前缀走正则） |
| `--uid U` / `--exclude-uid U` | 按 UID 白/黑名单 |
| `--min-price N` | 礼物/SC 最低金额过滤 |
| `--min-medal-level N` | 最低粉丝牌等级 |
| `--duration 1h` / `--until 23:00` | 采集时长或截止时间 |
| `--no-raw` / `--anonymize` | 体积与隐私控制 |
| `--cookie-file PATH` | 覆盖凭据 |
| `-v/-vv` | 日志级别 |

**`probe` 子命令（M0 的关键交付）** —— 它决定后续所有协议假设是否成立：
1. 打印 `room_init` 结果（真实房间号、开播状态、主播 UID）；
2. 获取 buvid3、`getDanmuInfo` token 与 host_list；
3. 建立 WS、发认证包、打印认证回应的原始 JSON 与 `code`；
4. 打印首个心跳回应的**人气值**；
5. 连续打印收到的前 N 条（默认 10）消息的 `cmd` 与原始 JSON；
6. `--dump-raw DIR` 把原始字节包与解压后 JSON 存成夹具，供 §10.2 金样本测试使用。

---

## 9. Web 面板

### 9.1 后端（FastAPI）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 单文件前端 |
| WS | `/ws/live?room_id=&types=&keyword=` | 实时推送归一化事件（JSON），支持前端侧过滤参数 |
| GET | `/api/rooms` | 当前监控的房间与状态（连接中/已连/重连中/未开播） |
| POST | `/api/rooms` / DELETE `/api/rooms/{id}` | 动态增删监控房间 |
| GET | `/api/events` | 历史查询：`room_id,type,q,since,until,limit,offset` |
| GET | `/api/stats` | 总量、类型分布、msg/min、延迟 P50/P95、丢弃计数 |
| GET | `/api/export` | 按查询条件导出 `jsonl/csv/txt`（流式响应） |
| GET | `/api/health` | 健康检查 |

- 面板复用同一套 `RoomCollector` 与 `bus`，**不是另起一套抓取逻辑**；WebHub 作为 sink 订阅者，用有界队列 + 慢客户端剔除（连续积压超阈值则断开该 WS 客户端并提示）。
- 默认只绑 `127.0.0.1`；绑 `0.0.0.0` 时必须配置 `[web].token`，否则启动直接报错退出（安全默认）。

### 9.2 前端（`static/index.html`，无构建）

- 顶部：房间切换下拉 + 连接状态灯 + 关键统计（在线/消息数/msg per min）。
- 中部：实时弹幕流（虚拟滚动，最多保留最近 2000 条），支持 `暂停滚动 / 自动滚动`。
- 侧栏：类型过滤复选（弹幕/礼物/SC/进场/上舰/系统）、关键词高亮、只显示含 SC 或高额礼物。
- 底部：导出按钮（按当前筛选导出 CSV/JSONL）。
- 交互细节：SC 与上舰用醒目色块；同用户连发做视觉去重折叠；新消息进入时若用户手动上滚则暂停自动滚动并显示"N 条新消息"。

---

## 10. 可靠性与测试

### 10.1 可靠性清单

| 场景 | 处理 |
|------|------|
| 网络中断 / WS 被服务端断开 | 退避重连：1→2→4→…→60s + 随机抖动；每次重连重新取 token 与 host_list |
| 认证失败（code != 0） | 刷新 buvid3 → 重取 token → 重试；连续 5 次失败则暂停该房间并告警 |
| 假死连接（无心跳回应） | 90s 无 op=3 → 主动断开重连 |
| 未开播 / 下播 | 收到 `PREPARING`/`STOP_LIVE_ROOM` 时保持连接但降低日志噪音，等待 `LIVE` |
| 服务端下发未知 protover / 畸形包 | 记 WARN + 跳过，不崩连接；计数进 stats |
| sink 消费过慢 | 有界队列丢最旧 + 丢弃计数；面板显示 `dropped` |
| 磁盘写满 / 文件被占用 | sink 抛错 → 标记该 sink 失败并降级，其余 sink 继续；退出前汇总报告 |
| Ctrl+C | 捕获 SIGINT → 关 WS → flush 所有 sink → 写 session.ended_at → 打印统计 |
| 时钟跳变 | 主用 `recv_ts`（wall clock），统计速率用 `time.monotonic()` 差值 |

### 10.2 测试策略（离线优先，不依赖真实直播）

1. **协议单测**：构造 → 解析 往返一致；zlib / brotli / 未压缩三种；**嵌套子包**；截断包、超长包、非法长度不崩。
2. **金样本解析测试**：`probe --dump-raw` 抓真实样本存 `tests/fixtures/`，逐条断言归一化字段（这是字段下标假设的唯一可信来源）。
3. **假服务端集成测试**：`fake_server.py` 按脚本回放「认证→认证回应→心跳回应→通知」，并注入中途断线，验证重连与去重。
4. **Sink 测试**：三格式写入/读回一致性、轮转、去重、SQLite 唯一约束与 FTS 查询。
5. **Web 测试**：`httpx.ASGITransport` 测 REST；WS 客户端测实时推送与慢客户端剔除。
6. **可选实况冒烟**：标记 `@pytest.mark.live`，默认 skip，手动 `pytest -m live` 才联网跑真房间。

目标覆盖率：核心模块（protocol / parser / filters / sinks）≥ 85%。

---

## 11. 风险与对策

### 11.1 协议漂移（中风险 / 高影响）
弹幕 `info` 数组下标、cmd 名称、风控策略都可能变。
→ 对策：保留 `raw`；`probe --dump-raw` 一键重新采样；parser 未知 cmd 不丢弃；归一化字段全部用 `.get()` 容错并写 `extra` 兜底。

### 11.2 风控与接口变更（中风险 / 高影响）
`getDanmuInfo` 可能要求 WBI 签名或更严的 Cookie；匿名连接可能被限。
→ 对策：三级降级链 `getDanmuInfo+签名` → `getDanmuInfo` → `Danmu/getConf`；自动获取并持久化 buvid3；强制 HTTP 调用最小间隔；支持 SESSDATA。

### 11.3 依赖安装受限（低风险 / 中影响）
若无法从 PyPI 安装 `websockets`/`brotli`/`fastapi`。
→ Plan B：WS 用**已装**的 `websocket-client` 放到 `asyncio.to_thread` 线程里跑；认证包里 `protover` 声明为 **2**，只走标准库 `zlib`（服务端支持）；面板降级为只读的极简 HTTP 服务（标准库 `http.server` + SSE）。主线代码把 WS/压缩抽象成接口，降级不改业务层。

### 11.4 数据量与磁盘（低风险 / 中影响）
热门直播间弹幕可达数十条/秒，长时间采集体积可观。
→ 对策：默认 `keep_raw=true` 但提供 `--no-raw`；按小时轮转；`stats` 可查体积；SQLite 走批量事务 + 定期 `wal_checkpoint`。

### 11.5 隐私与合规（低风险 / 高影响）
落盘含 UID/昵称。
→ 对策：`--anonymize`；默认本地 `data/`；README 写明用途边界与数据处置建议。

---

## 12. 里程碑与验收

| 阶段 | 内容 | 验收（可演示） | 预估 |
|------|------|----------------|------|
| **M0 骨架 + 协议自检** | 工程骨架、`protocol.py`、`api.py`、`config.py`、`probe` 命令 | `bili-danmaku probe <房间>` 打印房间信息 + 认证成功 + 前 10 条消息原文，并 dump 夹具 | 0.5 天 |
| **M1 抓取核心** | `client.py`（认证/心跳/重连）、`parser.py`、JSONL sink、`grab` | 对真实直播间抓 5 分钟，JSONL 内容正确，手动断网后自动续采 | 1 天 |
| **M2 完整落盘 + 过滤** | CSV/TXT/SQLite sink、`filters.py`、去重、轮转、`replay`/`stats` | 三种产物内容一致可读；关键词/类型过滤生效；离线 replay 不联网 | 1 天 |
| **M3 Web 面板** | FastAPI + WS 推送 + 单文件前端 + 导出 | 浏览器秒级实时弹幕流，可切房间、过滤、导出 | 1 天 |
| **M4 打磨** | 多房间、`check-auth`、README、测试补齐、`pyproject` entry point | `pytest` 全绿；`pip install -e .` 后 `bili-danmaku --help` 可用 | 0.5 天 |

共约 **4 个工作日**，每个里程碑结束都停下来给你验收，可按反馈调整后续。

---

## 13. 待你确认的开放问题

1. **主用房间号**：给我 1–2 个常看的房间号，M0 自检可以直接用真实目标验证（冷门房间更好调试）。
2. **SESSDATA**：你打算用登录态跑吗？如果打算，M0 就一起做 `check-auth`；如果先匿名，我把 WBI 降级链做得更厚一些。
3. **落盘体积取舍**：默认保留 `raw`（便于协议漂移后重新解析，体积约大 2–3 倍）。你接受默认保留，还是默认关闭？
4. **面板端口**：默认 `127.0.0.1:8787`。本机 8787 若被占用，是否换个端口？
5. **是否要 V2 范围**（本版不做，先记下）：跨房间聚合、弹幕词云、关键词告警（如 SC/上舰即时通知）、定时自动开录。

---

## 附：与最初需求的对照

| 你的选择 | 本设计落点 |
|----------|-----------|
| Python 3 + asyncio | §2 选型、§3 架构（每房间一个 asyncio 任务） |
| JSONL + CSV/TXT + SQLite | §6.2 / §6.3 / §6.4 |
| CLI + 轻量 Web 面板 | §8 CLI、§9 面板（单文件前端、无构建） |
| 必须支持 Cookie 登录 | §5.1（uid/token）、§7.2（凭据管理）、§11.2（降级链） |
