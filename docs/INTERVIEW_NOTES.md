# B站直播弹幕提取器 —— 面向技术面试的代码结构摘要

> 面向对象：面试官 / 代码评审者。所有函数名、常量名、字段名、数字均取自 `src/bili_danmaku/` 实际代码。
> 阅读范围：`protocol.py` `protobuf_lite.py` `parser.py` `models.py` `dedupe.py` `wbi.py` `client.py`
> `collector.py` `api.py` `sinks/*` `server/*` `store.py` `filters.py` `text.py` `analysis.py`
> `annotations.py` `rooms.py` `config.py` `runtime.py` `launcher.py` `credentials.py` `utils.py` `gui/*`。

---

## 1. 模块职责与「写进 docstring 的设计原则」

项目把**设计原则直接写进模块 docstring**，这是它的一个显著风格：每个模块顶部都有一句「改动前先读」级别的约束。摘录如下（原文照抄要点）。

| 模块 | 职责 | docstring 里写明的原则 |
|---|---|---|
| `protocol.py` | B 站直播信息流二进制协议：封包/拆包/解压 | 「封包格式（大端序，**实测确认**）」；「protover=2/3 解压后得到的仍是一串带头部的包，因此必须递归拆包，这是**必需而非优化**」；`unpack` 「绝不静默截断数据」 |
| `protobuf_lite.py` | 最小 protobuf 解析器（无第三方依赖） | 「只做通用 wire-format 解析，不依赖 `.proto` 定义，因此对协议演变更耐受」；「**不保证语义正确性** —— 字段号的含义仍需按实抓样本确认」；「本模块的价值是『不丢数据 + 可检视』，而不是『猜出字段含义』」 |
| `parser.py` | `cmd` → `Event` 归一化 | 三条策略：「**优先**使用结构化对象 —— 实测其值与位置下标一致但更稳定」；「结构化缺失时回退位置下标，并用 `field_source` 标注实际来源，以便通过 `field_quality` 指标监控协议漂移」；「所有取值容错：缺字段返回 None 而非抛异常；未知 cmd 走 other 兜底，**绝不丢消息**」 |
| `models.py` | 事件、房间、连接状态、统计的数据模型 | 「采集范围：只有产生**用户文字内容**的事件才入库」（`CONTENT_TYPES`）；`HostInfo.url()`「不做硬编码」——按是否声明 `wss_port` 自动选 wss/ws |
| `dedupe.py` | 有界 LRU + TTL 去重缓存 | 用途是「合并『实时流』与『历史补种』，并抵御服务端的重复下发」 |
| `wbi.py` | WBI 签名（纯函数 + 密钥缓存） | 「本模块**只负责纯函数签名与密钥缓存，不做网络请求**（由 `api.py` 注入 client）」；「调用方把它当作『-352 风控时的**可选重试手段**』，而非主路径」 |
| `client.py` | WebSocket 连接：认证、心跳、收包 | 三条实测要点；`messages()` 明确写了「**读超时在这里被吞掉**」的理由与「`should_stop` 之所以必须放在生成器**内部**」的死锁分析 |
| `collector.py` | 单房间连接状态机与自动重连 | 「每次重连都**重新获取 token**」；「用『多久没收到心跳回应』判假死，而不是『多久没收到消息』」；「读循环只做『解析 → 去重 → 投递』，投递是非阻塞的，**绝不因落盘慢而卡住心跳**」 |
| `api.py` | B 站 HTTP 接口封装 | 「`Danmu/getConf` 匿名可用，是保底路径」；「`getDanmuInfo` 匿名一律返回 -352 风控，因此仅在登录态下尝试，失败即降级且**不重试**（重试会加重风控）」 |
| `sinks/base.py` | Sink 抽象与基类 | 「`write()` 接收一批事件，同步写」；「sink 实例**不持有事件**（写完即丢），因此内存占用与运行时长无关」 |
| `sinks/bus.py` | 多 sink 扇出总线 | 「每个 sink 拥有**独立的消费任务与有界队列**」；「队列满时**丢最旧**并计数，绝不 OOM」 |
| `sinks/sqlite.py` | SQLite 落盘 | 「WAL 模式 + `synchronous=NORMAL`」；「一批事件 = 一个事务」；「`dedupe_key` 上建 `UNIQUE`，作为内存去重之外的第二道防线」 |
| `store.py` | SQLite **只读**查询层 | 「以**只读方式**打开数据库（`file:...?mode=ro`），避免分析操作意外写坏采集数据」；「关键词检索走 `LIKE` 子串匹配，**刻意不用 FTS5**」 |
| `filters.py` | 过滤规则 | 「`match()` 是**纯函数、无 IO**；关键词预编译为单个 alternation 交给 re 的 C 层扫描」 |
| `text.py` | 分词 + 表情占位符处理 | 「`count_words()` 是**纯函数、无 IO**，因此可以完全离线单测」；兜底分词「不是为了效果，而是为了『没装可选依赖时功能仍然可用、测试仍然可跑』」 |
| `analysis.py` | 本地感知分析 | 三重边界：「**纯函数 + 有界数据** —— 没有 IO、没有网络、没有全局状态，所以能完全离线单测」；「**不做外发** —— 本地统计是这个方向能被接受的前提」；「**不放采集热路径** —— `hub.publish()` 必须保持 O(1)」 |
| `annotations.py` | 标注（标记/笔记）存储 | 「**独立库**」的三条理由；「`store.py` 是**只读**的，这个模块是**可写**的 —— 两者刻意不合并」 |
| `rooms.py` | 房间号/直播间链接解析 | 「不支持的输入要**明确报错**（`ValueError`），而不是猜一个数字出来」 |
| `config.py` | 配置加载与校验 | 「优先级：CLI 参数 > 环境变量(`BILI_*`) > TOML 文件 > 内置默认值」；「配置非法一律『**启动即失败**』，不留到运行期」 |
| `runtime.py` | 面板托管运行时 | 「停止流程是「**让服务自己退出**」… **这条路是唯一保证不丢数据的**」 |
| `launcher.py` | 进程内托管面板 | 「本模块**刻意不碰 GUI**（tkinter/ctypes 在 `gui/` 里），因此可以完全离线单测」 |
| `credentials.py` | 凭据管理 | 「本模块**只**保存建立连接所必需的 cookie 值，绝不保存账号资料，且任何路径都不打印明文」 |
| `utils.py` | 日志、掩码、退避、限流、脱敏 | 「任何凭据值都不得进入日志。`setup_logging()` 会安装 `RedactingFilter`，作为『忘记手动掩码』的**最后一道防线**」 |
| `gui/_win32.py` | 零依赖托盘图标 Win32 绑定 | 「**为什么不用 `pystray` / `pywin32`**（对应 DEC-6）」三条理由；「**不自己起线程**」 |
| `gui/tray.py` | 托盘应用组装 | 「**全部在 tkinter 主线程**」；「**绝不在未 flush 的情况下静默退出**（DEC-3 的核心承诺）」 |
| `server/hub.py` | WebSocket 广播中枢 | 「**同步**接口，因为它在 sink 工作协程里被调用，绝不能阻塞」；慢客户端「直接断开并告知原因，而不是无限缓冲 —— 面板是采集的**消费者**，它不该反向影响数据完整性」 |
| `server/panel.py` | 多房间采集编排 | 「**面板不是另一套抓取逻辑** —— 它只是多挂了一个 `WebHubSink`」 |
| `server/app.py` | FastAPI 路由与 WebSocket | 「**若前端未构建，服务仍会启动**并给出一个内建提示页 —— 面板不该因为缺 Node 而让整个工具不可用」 |

---

## 2. 协议层实现细节

### 2.1 封包结构（大端序，`HEADER = struct.Struct(">IHHII")`，`HEADER_LEN = 16`）

| 偏移 | 长度 | 类型 | 含义 |
|---|---|---|---|
| 0 | 4 | uint32 | 包总长度（含 16 字节头） |
| 4 | 2 | uint16 | 包头长度，固定 16 |
| 6 | 2 | uint16 | 协议版本 `protover` |
| 8 | 4 | uint32 | 操作码 `operation` |
| 12 | 4 | uint32 | 序列号 `sequence` |
| 16 | .. | bytes | 负载 |

- `Operation(IntEnum)`：`HEARTBEAT = 2`、`HEARTBEAT_REPLY = 3`、`MESSAGE = 5`、`AUTH = 7`、`AUTH_REPLY = 8`。
- `ProtoVer(IntEnum)`：`PLAIN = 0`（未压缩 JSON）、`HEARTBEAT = 1`、`ZLIB = 2`、`BROTLI = 3`。
- `Packet` 是 `@dataclass(frozen=True, slots=True)`，字段 `(protover, operation, sequence, payload)`。

### 2.2 `pack()` 的不变量

```python
total = HEADER_LEN + len(payload)
if total > 0xFFFFFFFF: raise ProtocolError("负载过大，超出 uint32 长度上限")
```

### 2.3 `unpack()` —— 递归拆包 + 三重边界校验

循环条件 `while offset + HEADER_LEN <= total`，每轮用 `HEADER.unpack_from(data, offset)` 取头，然后：

1. `hdr_len < HEADER_LEN` → `ProtocolError(f"非法包头长度 {hdr_len}")`
2. `pkt_len < hdr_len` → `ProtocolError(f"包长 {pkt_len} 小于头长 {hdr_len}")`
3. `offset + pkt_len > total` → `ProtocolError(f"包长越界: 需 {pkt_len}，仅剩 {total - offset}")`

`body = data[offset + hdr_len : offset + pkt_len]`，然后**递归**：

- `protover == ZLIB`：`zlib.decompress(body)` → `packets.extend(unpack(decompressed))`，`zlib.error` 包成 `ProtocolError("zlib 解压失败: ...")`。
- `protover == BROTLI`：先 `has_brotli()` 探测（结果缓存在模块级 `_brotli` / `_brotli_checked`）；缺依赖抛 `CompressionUnavailable`（`ProtocolError` 子类），提示 `pip install brotli` **或在配置中把 protover 设为 2**。
- 否则 `packets.append(Packet(protover, operation, sequence, body))`。

循环结束后 `if offset != total: raise ProtocolError(f"尾部残留 {total - offset} 字节无法解析")`。**这条是「绝不静默截断」的落点。**

`resolve_protover(requested)`：`requested == ProtoVer.BROTLI and not has_brotli()` 时降到 `ZLIB(2)`。

### 2.4 心跳与认证

- `HEARTBEAT_PAYLOAD = b"[object Object]"` —— 注释写明「**实测** B 站网页端心跳负载就是这串字面量」。
- `send_heartbeat()`：`pack(Operation.HEARTBEAT, HEARTBEAT_PAYLOAD, ProtoVer.HEARTBEAT)`，即以 `protover=1` 发送。
- `parse_popularity(payload)`：`len(payload) < 4` 返回 None，否则 `struct.unpack(">I", payload[:4])[0]`。设计文档补充：`body[4:]` 是心跳原文回显。
- 认证包（`DanmakuConnection.authenticate()`）的字段与取值：

```python
auth = {
  "uid": credentials.dede_user_id,   # 未登录为 0
  "roomid": room.room_id,            # 真实房间号
  "protover": self.protover,         # resolve_protover(config.client.protover)
  "platform": "web",
  "type": 2,
  "key": bundle.token,
}
if credentials.buvid3: auth["buvid"] = buvid3
```

序列化用 `json.dumps(auth, separators=(",", ":"))` 紧凑分隔符，`pack(Operation.AUTH, ...)`，随后 `asyncio.wait_for(ws.recv(), timeout=10.0)` 等 `op=8`。`code != 0` 抛 `AuthFailed(code)`；找不到 `AUTH_REPLY` 抛 `AuthFailed(-1)`。**认证成功后立刻 `last_heartbeat_reply_at = loop.time()`**，注释：「从这里开始计时：此后若长时间收不到心跳回应即判定假死」。

### 2.5 假死判定（`_raise_if_stale`）

```python
idle = loop.time() - self.last_heartbeat_reply_at
if idle > self.config.client.heartbeat_timeout:  # 默认 90.0
    raise ConnectionStale(f"心跳已 {idle:.0f} 秒无回应")
```

在 `messages()` 的**两条路径**上都会调用：读超时 `except asyncio.TimeoutError` 之后、以及每轮 yield 完之后。

### 2.6 `messages()` 的两个非平凡决定

```python
async def messages(self, should_stop: Callable[[], bool] | None = None) -> AsyncIterator[dict]:
```

1. **读超时被吞掉**：`recv_timeout=5.0` 对低热度房间是常态；若让 `TimeoutError` 逃出生成器，迭代器会被终结，调用方下次取值拿到 `StopAsyncIteration`，从而**误判连接结束并触发无谓重连（实测会形成重连风暴）**。
2. **`should_stop` 必须放在生成器内部**：「生成器只在收到消息时才把控制权交回调用方，若把截止判断只写在外层循环，无消息时外层永远等不到检查机会（**实测死锁**）」。

包分发：`HEARTBEAT_REPLY` → 更新 `stats.last_popularity` 与 `last_heartbeat_reply_at`；非 `MESSAGE` 跳过；`packet.protover` 不在 `(PLAIN, ZLIB, BROTLI)` 时 `stats.unknown_protover += 1` 并跳过（**不断连接**）；`json.loads` 失败则 `stats.bad_json += 1`。

### 2.7 WBI 算法（`wbi.py`）

三段式：

1. **取密钥**：从 `nav` 的 `data.wbi_img.img_url` / `sub_url` 取**文件名去扩展名**（`url.rsplit("/", 1)[-1].split(".")[0]`）得 `img_key` / `sub_key`。注释强调「**实测：未登录（code=-101）时 `wbi_img` 依然存在，因此无需登录即可签名**」。`WbiKeyCache` 的 TTL 是 `_KEY_CACHE_TTL = 3600.0`，理由「观测到密钥按日更替，这里**保守地 1 小时刷新**」。
2. **`get_mixin_key(raw_wbi_key)`**：`len(raw_wbi_key) < 64` 抛 `ValueError`；否则 `"".join(raw_wbi_key[i] for i in MIXIN_KEY_ENC_TAB[:32])`。`MIXIN_KEY_ENC_TAB` 是 64 项重排表（`46,47,18,2,53,8,...`，以 `36,20,34,44,52` 结尾）。
3. **`build_signed_query(params, img_key, sub_key, wts=None)`**：
   - `signed = {str(k): v}`，`signed["wts"] = int(time.time())`；
   - **按 key 升序**拼接 `<urlencode(k)>=<urlencode(v)>`；
   - `w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()`；
   - 返回 `f"{query}&w_rid={w_rid}"`。

`encode_uri_component` 是**手写的 ECMAScript `encodeURIComponent` 语义**：先 `"".join(ch for ch in text if ch not in "!'()*")`，再 `urllib.parse.quote(filtered, safe="")`。docstring 明确了两处与 `quote` 的差异：过滤 `!'()*`、百分号大写且空格编码为 `%20`（而非 `+`）。

`sign_params()` 是返回 dict 副本的包装（把 query 串再拆回 dict，注意它会 unquote）。

### 2.8 `-352` 降级路径（`api.py::acquire_token`）

常量：`CODE_RISK_CONTROL = -352`、`CODE_NOT_LOGGED_IN = -101`。构造条件：

```python
try_getdanmuinfo = (cfg.prefer_getdanmuinfo
                    and self._creds.logged_in
                    and room_id not in self._risk_blocked_rooms
                    and not force_getconf)
```

- 走 getDanmuInfo 时**直接叠加 WBI**（`use_wbi=True`），docstring：「实测：匿名一律 -352；**登录态下必须叠加 WBI 签名**才能成功。因此这里直接发签名请求，省掉一次注定失败的往返」。
- 成功 → `_bundle_from_info(result, TokenSource.GET_DANMU_INFO_WBI.value)`（`source` 取值 `"getDanmuInfo+wbi"`）。
- **`code == -352` → `self._risk_blocked_rooms.add(room_id)`**，本进程内该房间此后一律走 getConf。
- 其它失败 → 只 log warning，也降级。
- 一条容易踩的注释：「只有当 getDanmuInfo **确实失败**时才把房间加入本进程黑名单，否则一次成功之后的重连会被错误地永久降级到 getConf」。
- 保底路径 `danmu_conf()` → `/room/v1/Danmu/getConf`，`source = "getConf"`，匿名可用。
- 风控场景一律 `retry=1`（「重试会加重风控」）。`anchor_info` 同样 `retry=1`，且**失败返回 None 而不抛异常**——「主播昵称只是展示信息，取不到不该让『添加房间』这条主链路失败」。
- `_dedupe_hosts()`：注释「实测 `getConf` 会返回 6 条完全重复的记录」，按 `(host, wss_port)` 去重。

---

## 3. 数据模型 `Event` 的完整字段与语义

```python
class EventType(str, Enum):
    DANMAKU="danmaku"; SUPER_CHAT="super_chat"; GIFT="gift"; GUARD_BUY="guard_buy"
    INTERACT="interact"; LIKE="like"; ROOM_STATS="room_stats"; LIVE_STATUS="live_status"
    NOTICE="notice"; DM_INTERACTION="dm_interaction"; OTHER="other"
```

`CONTENT_TYPES = frozenset({EventType.DANMAKU.value, EventType.SUPER_CHAT.value})` —— 注释：「只有产生**用户文字内容**的事件才入库。进房/上舰/点赞/礼物/房间数据等事件不产生用户文字，按需求排除在外」（对应决策 DEC-1）。

`Event(BaseModel)` 全部字段：

| 字段 | 类型/默认 | 语义 |
|---|---|---|
| `seq` | `int = 0` | 会话内自增序号（`_seq`），落库后读回时被复用为 DB `id` |
| `session_id` | `str = ""` | 形如 `20261001-16-804456`（`%Y%m%d-%H%M%S-{room_id}`） |
| `room_id` / `short_id` | `int` / `int\|None` | 真实房间号 / 短号 |
| `type` | `str = "other"` | 归一化类型（见 `EventType`） |
| `cmd` | `str = ""` | **原始** cmd（如 `DANMU_MSG:4:0:2:2:2:0`） |
| `source` | `str = "live"` | `live` \| `history` |
| `recv_ts` | `float` | 本机墙钟接收时间（秒，浮点） |
| `server_ts` | `int\|None` | 服务端时间戳（秒） |
| `latency_ms` | `int\|None` | `max(int((recv_ts - server_ts) * 1000), 0)` |
| `msg_id` | `str\|None` | 实测 `info[0][15].extra.id_str` **全局唯一** |
| `uid` / `uname` | `int\|None` / `str\|None` | 用户标识 / 昵称 |
| `text` | `str\|None` | 正文（表情以占位符内嵌，如 `哈哈[dog]`） |
| `price` | `int\|None` | SC 单位为**元**；礼物/上舰单位为**金瓜子**（`extra.price_unit` 区分） |
| `guard_level` | `int\|None` | 默认被规范化为 `0` |
| `user_level` | `int\|None` | 用户等级 |
| `medal` | `Medal\|None` | 粉丝牌（见下） |
| `is_anchor` / `is_admin` | `bool` | `uid == room.anchor_uid` / `info[2][2]` |
| `field_source` | `str\|None` | `structured` \| `index` \| `mixed` |
| `extra` | `dict` | 结构化补充（`emots`、`msg_type`、`click_count`…） |
| `raw` | `dict\|None` | 原始消息（`sinks.keep_raw` 控制） |

`Medal(BaseModel)`：`name`、`level`、`anchor_uname`、`anchor_roomid`、`anchor_uid`、`is_light`、`color`。

三个关键方法：

- **`dedupe_key()`**：优先 `f"{type}|{room_id}|{msg_id}"`；无 `msg_id` 时退化为 `sha1(text)[:16]`，有 `server_ts` 则带 `uid` 与 `server_ts`。
- **`has_content()`**：docstring 说明「实测两类表情都落在 text 上：普通表情在正文里内嵌占位符（如 `哈哈[dog]`），并在 `extra.emots` 给出映射；主播自定义表情的 text 就是表情名（如 `大拇哥`）。因此 text 非空即可判为有内容；另外保留 `emoticon_unique` / `emots` 兜底」。
- **`is_content_event()`**：`self.type in CONTENT_TYPES and self.has_content()`。

其它模型：`RoomInfo`（含 `is_living = live_status == 1`；`anchor_name` / `anchor_face` 注释「采集链路完全不依赖这两个字段」）、`HostInfo.url()`（自动选 wss/ws）、`TokenBundle`、`SessionMeta`、`SinkStats(written, dropped, failed, bytes, ok, error)`、`ConnState(IDLE/CONNECTING/AUTH_SENDING/AUTHED/STREAMING/RECONNECTING/PAUSED/STOPPED)`、`TokenSource(GET_DANMU_INFO/GET_DANMU_INFO_WBI/GET_CONF)`、`FieldSource(STRUCTURED/INDEX/MIXED)`。

---

## 4. 并发/异步模型

### 4.1 任务划分

| 任务名 | 归属 | 职责 |
|---|---|---|
| `collector-{room_id}` | `PanelService.add_room` 的 `asyncio.create_task` | 跑 `RoomCollector.run()` |
| `heartbeat` | `RoomCollector._pump` 内 `create_task` | 每 `heartbeat_interval` 发心跳 |
| `sink-{sink.name}` | `SinkWorker.start` 内 `create_task` | 每 sink 一个消费者 |
| `stats-pusher` | `create_app` 的 lifespan | 每 `STATS_PUSH_SECONDS = 2.0` 推一次房间状态 |
| `panel-uvicorn` | `PanelController.start` 的 `threading.Thread(daemon=True)` | 在**子线程**里 `asyncio.run(server.serve())` |

读循环本身不是单独任务：`_pump` 里 `async for` 式地 `await stream.__anext__()`，在**当前协程**里跑。

### 4.2 `asyncio.Queue` 与有界背压

两套队列，语义一致（满则**丢最旧**）：

1. **sink 队列**（`SinkWorker.queue`）：`asyncio.Queue(maxsize=max(queue_size,1))`，默认 `config.sinks.queue_size = 20000`。`submit()` 是**非阻塞**的 `put_nowait`，`QueueFull` 时 `get_nowait()` 丢最旧再放新的，`self.dropped += 1` 并返回 `False`。
2. **WS 订阅者队列**（`Subscriber.queue`）：`DEFAULT_QUEUE_SIZE = 500`，同样的「丢最旧」策略，`subscriber.dropped += 1`。

设计文档 §9.4 的表述：「队列满时**丢最旧**并 `stats.dropped += 1`（丢最旧而非阻塞读循环，避免心跳被阻塞导致被服务端断开）」。

### 4.3 有界环形缓冲

- `WebHub._buffers: dict[int, deque[str]]`，`DEFAULT_BUFFER_SIZE = 2000`（每房间），供新连接回填。
- `LiveWindow._messages: deque(maxlen=window_messages)`（`ANALYSIS_WINDOW_MESSAGES = 1200`）、`_buckets: deque(maxlen=bucket_count)`（`ANALYSIS_BUCKET_COUNT = 20`）。
- 词表上限 `ANALYSIS_WORDS_PER_BUCKET = 2000`，超了按「当前最少」淘汰（注释：「这是**近似**，但对『现在最热的词』这个问题足够」）。

### 4.4 flush 时机

`SinkWorker._run()` 的主循环：

```python
timeout = max(self.flush_interval - (time.monotonic() - last_flush), 0.01)
timeout = min(timeout, 1.0)   # 上限 1 秒
batch.append(await asyncio.wait_for(self.queue.get(), timeout=timeout))
due = bool(batch) and (len(batch) >= self.batch_size
                       or (time.monotonic() - last_flush) >= self.flush_interval)
if due: await self._flush_off_loop(batch); batch = []; last_flush = time.monotonic()
if self._stop.is_set() and self.queue.empty(): break
```

- 默认 `batch_size = 200`、`flush_interval = 1.0`。
- **1 秒上限有明确理由**：「否则 `flush_interval` 很大时会长时间阻塞在 `get()` 上，导致 `stop()` 无法被及时感知（**实测 stop 要等满 `flush_interval` 才返回**）。空闲时多醒几次的代价可以忽略。」
- 循环退出后再补一次 `if batch: await self._flush_off_loop(batch)`，然后 `self._close()`。

### 4.5 `_flush_off_loop` —— 项目里最重要的一处并发决策

```python
async def _flush_off_loop(self, batch):
    await asyncio.to_thread(self._flush, batch)
```

docstring（节选）：「**为什么必须这样做**：`write()` 是同步 IO（SQLite 提交、文件 fsync）。直接在事件循环里调用它，只要磁盘一慢，整个循环就被卡住 —— 心跳任务发不出去，采集器会判定『连接假死』并重连。离线用例 `test_queue_full_keeps_newest_and_never_blocks_reader`（一个故意 sleep 3 秒的 sink）实测复现了这条链路：收 2 条 → 假死 → 重连，而队列根本没机会顶满。」

并且**刻意不加超时**：「卡死的 sink 只该拖住它自己，不该拖累心跳或读循环。给它设超时反而危险 —— 超时后线程仍会跑完，下一批再进来就可能并发操作同一个 SQLite 连接。宁可让它一直卡着（有界：一个线程），由 `stop()`（15 秒上限后取消）负责收尾。」

### 4.6 优雅停止的完整链路

`collector.request_stop()` → `self._stop.set()`（`asyncio.Event`）；`_sleep_async(seconds)` 用 `asyncio.wait_for(self._stop.wait(), timeout=seconds)` 让退避等待**可被提前打断**。

`Bus.stop()` → `asyncio.gather(*(worker.stop() ...))`；`SinkWorker.stop()`：`self._stop.set()` → `asyncio.wait_for(self._task, timeout=15.0)` → 超时 `self._task.cancel()` → `finally` 里 `if not self._task.done(): cancel()` + `asyncio.gather(..., return_exceptions=True)`。

`RoomCollector.run()` 的 `finally`：`stats.ended_at = now()` → `await self.bus.stop()` → `_set_state(ConnState.STOPPED)`。`PanelService.remove_room()`：`request_stop()` → `asyncio.wait_for(runtime.task, timeout=20.0)` → 超时/取消则 `task.cancel()`。

**面板层的三条停止入口最终都汇到同一处**：`POST /api/admin/shutdown` → `_StopHandle.request()` 置位 `Server.should_exit` → uvicorn 收尾 → lifespan 的 `finally` → `await panel.stop()` → 每个房间 `bus.stop()` flush。`launcher.PanelController.stop()` 走 `server.should_exit = True` + `thread.join(timeout=30)`，且**超时也不强杀**：「**强杀会丢未落盘批次**；把实际情况如实报给调用方」。

### 4.7 重连退避与「稳定成功」判据

`collector.py` 常量：

- `MAX_CONSECUTIVE_AUTH_FAILURES = 5`、`PAUSE_SECONDS = 300.0`
- `MIN_STABLE_SECONDS = 5.0` —— 注释：「连接存活多久才算『稳定成功』。低于该值时**不重置退避**，避免服务端『接受后立刻断开』时退避被反复清零而形成重连风暴」。
- `DEDUPE_TTL = 300.0` —— 「需覆盖历史补种与实时流的重叠窗口」。

`utils.backoff_delay(attempt, base=1.0, cap=60.0, jitter=True)`：`min(base * 2**attempt, cap) + random.uniform(0.0, 1.0)`，`cap` 取 `config.client.reconnect_max_delay = 60.0`。

`RateLimiter`：docstring 明说「**诚实串行化，不做令牌桶**」，用 `asyncio.Lock` + `time.monotonic()` 保证最小间隔（默认 `min_request_interval = 1.0`）。

---

## 5. 落盘一致性与原子性

### 5.1 文件轮转（`RotatingFileWriter`，JSONL/CSV/TXT 共用）

- 文件名 `{room_id}_{时间桶}{suffix}`，时间桶来自 `Sink.hour_bucket()`：`rotate_hours > 0` 时按 `int(moment / seconds) * seconds` 换算，`strftime("%Y%m%d-%H")`；`rotate_hours <= 0` 时退化为 `%Y%m%d`。默认 `rotate_hours = 1.0`。
- 体积上限 `MAX_FILE_BYTES = 128 * 1024 * 1024`；`ensure_open` 检查 `bucket == self._bucket and self._size < self._max_bytes`。
- 打开方式 `path.open("a", encoding=...)`，**已存在的文件追加而不是覆盖**；`is_new = not path.exists()` 时才写表头，并用 `path.stat().st_size` 初始化 `_size`（续写后体积判断仍然准确）。
- 编码：JSONL `utf-8`；CSV `utf-8-sig`（docstring：「Excel 直接打开不乱码」）；TXT `utf-8`。

### 5.2 关于「临时文件 + 重命名」

**这个项目在事件落盘路径上刻意不做 temp+rename**：因为 sink 是**追加式流式日志**（append-only），rename 反而会破坏续写与轮转语义。原子性的着力点换成了：

1. **批量事务**：`SqliteSink.write()` 里一批事件一个事务，`connection.commit()`；异常时 `connection.rollback()` 再 re-raise。
2. **`INSERT OR IGNORE` + `UNIQUE`**：`dedupe_key TEXT UNIQUE` 是「内存去重之外的第二道防线」；`cursor.rowcount` 为 0 则 `self.ignored += 1`。
3. **`close()` 里的收尾**：先 `UPDATE session SET ended_at = ? WHERE id = ?` 再 `commit()`，最后 `PRAGMA wal_checkpoint(TRUNCATE)` 收缩 WAL。
4. **`to_thread`**：让同步写入不阻塞事件循环（见 §4.5）。

### 5.3 WAL / PRAGMA

`SqliteSink.open()`：

```python
sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000
```

`check_same_thread=False` 有长注释解释：「`open()` 由 Bus 在事件循环里调用，而 `write()` 由 `Bus._flush_off_loop()` 丢到工作线程执行 —— 默认的线程检查会让每次写入都抛 `sqlite3.ProgrammingError`。本项目每个 sink 只有**一个** worker 任务在串行使用这条连接（`_run` 里 await 完 flush 才会进下一批），不存在并发访问。」

只读层 `DanmakuStore` 同样 `check_same_thread=False`，理由不同：「Starlette 用线程池执行同步生成器，一次流式导出的各次 `next()` 可能落在不同工作线程上」（由 `/api/export` 的缺陷暴露）。

### 5.4 迁移机制（`SqliteSink`）

```python
SCHEMA_VERSION = "3"
MIGRATIONS = (("extra_json", "ALTER TABLE event ADD COLUMN extra_json TEXT"),)
```

`_migrate()` 用 `PRAGMA table_info(event)` 取已有列名，缺哪列补哪列。docstring 里有一句**很关键的澄清**：

> **新增索引不需要写在这里**：`SCHEMA` 里的 `CREATE INDEX IF NOT EXISTS` 每次 `open()` 都会执行，老库下次启动时会自动补上索引（V3-a 的 `idx_event_room_ts_type` 就是靠这条自愈的）。**提升 `SCHEMA_VERSION` 只表示『结构期望变了』，不驱动迁移。**

只读层对应的是 `has_column(name)`：「本层是**只读**的，不会像 `SqliteSink` 那样执行 ALTER 迁移，所以旧库可能缺列（例如词云要用的 `extra_json`）。查询前必须先问一句，否则会抛 `no such column` —— 这不是配置错误，而是『老数据 + 新代码』的正常组合，应该降级而不是崩。」`_event_columns_sql` / `_history_columns_sql` 据此动态裁剪列。

### 5.5 索引与实测性能（写进注释的数字）

```sql
idx_event_room_ts        ON event(room_id, recv_ts)
idx_event_type_ts        ON event(type, recv_ts)
idx_event_uid            ON event(uid)
idx_event_msgid          ON event(msg_id)
idx_event_session        ON event(session_id)
idx_event_room_ts_type   ON event(room_id, recv_ts, type)
```

- `idx_event_room_ts_type` 注释：「V3-a：时间轴密度聚合用。把 type 也放进索引，聚合才能全程走覆盖索引。**实测 20 万行：`GROUP BY recv_ts, type` 从 127 ms 降到 38 ms。** 只用 `(room_id, recv_ts)` 时索引覆盖不到 type，SQLite 会退化成临时 B 树。」
- `session` 表 `_row()` 里注释：「以 sink 打开时的会话为准：保证每条事件都指向确实存在的 session 行，不会因为生产者给的 session_id 不一致而产生孤儿数据」（即**外键由代码保证，没开 `PRAGMA foreign_keys`**）。
- `store.density()` 的两种写法表格：`GROUP BY CAST(recv_ts/b AS INT), type` → 索引 + TEMP B-TREE，**118 ms**；`GROUP BY recv_ts, type`（本实现）→ `SEARCH ... USING COVERING INDEX`，**38 ms**。所以「按**原始 `recv_ts`** 分组、在 Python 里折算桶…把『表达式分组』这个唯一会触发临时 B 树的操作挪出 SQL」。

### 5.6 标注库（`annotations.py`）—— 独立库

不写进 `danmaku.db` 的三条理由（docstring 原文）：职责单一（采集产物 vs 人产生的数据）；采集库 schema 只由 `SqliteSink` 掌管，混在一起会让「只读分析层」与「用户可写 UI」共享迁移历史；可删（删一个文件即可）。

`SCHEMA`：`bookmark(id, room_id, session_id, anchor_id, recv_ts, label, note, created_at)` + `idx_bookmark_room_ts`。写连接用 WAL + `busy_timeout=5000`；**只读模式下库不存在就不建库不建表**（「面板只是打开一次页面、还没打过任何标记的情况下，不该在工作区里凭空留下一个空库」），并以 `available` 属性暴露。常量 `MAX_LABEL_LEN = 120`、`MAX_NOTE_LEN = 2000`、`MAX_LIST_LIMIT = 2000`；超长**拒绝而不是静默截断**（「静默截断会让用户以为存下来的就是他输入的」）。`WRITABLE_COLUMNS` 白名单防止任意键写进 SQL。

### 5.7 状态文件（`runtime.py`）

`.runtime/web.json` + `.runtime/web.log`；`save_state` 直接 `path.write_text(json.dumps(...))`（无原子写）。**幂等与自愈靠 `live_runtime()`**：读状态 → `pid_alive(runtime.pid)`（Windows 用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION=0x1000)` + `GetExitCodeProcess`，判 `exit_code == 259`（`STILL_ACTIVE`）；POSIX 用 `os.kill(pid, 0)`；**刻意不做进程名匹配**，「匹配 `python.exe` 会撞到别的 Python 进程，反而误判」）→ 不活则 `clear_state()`。注释点明这就是崩溃后能重新 `web start` 的原因。

`spawn_detached()` 的日志句柄设计：`log_path.open("a", encoding="utf-8", buffering=1)`（行缓冲），`stdout=handle, stderr=subprocess.STDOUT, stdin=DEVNULL`，**finally 里 `handle.close()`**（「父进程持有的句柄要关掉，否则子进程日志不会被及时刷出/无法删除」）。`close_fds=True` 在 Windows 上也显式传（「否则它会一直持有父进程的 stdout 管道写端，导致 `web start | ...` 永远等不到 EOF」）。

---

## 6. server 层

### 6.1 `WebHub` 广播

`publish(event)` 的顺序（全部**同步**、绝不阻塞、绝不抛异常）：

1. `record = event.model_dump(mode="json")`，`payload = json.dumps({"t": "event", "data": record}, ensure_ascii=False)`；序列化异常只 log warning。
2. `self._buffers.setdefault(event.room_id, deque(maxlen=self._buffer_size))` 追加；`self.published += 1`。
3. `self.warm_window(event)` —— 喂 `LiveWindow`（注释：「`add()` 是 O(1) 且有界，可以放在热路径上」）。
4. 累计计数：`cumulative_total` / `cumulative_by_type` / `cumulative_by_room` / `cumulative_by_room_type`。
5. `for subscriber in list(self._subscribers)`：先 `accepts()` 过滤，再 `put_nowait`，`QueueFull` 时丢最旧 + `subscriber.dropped += 1`。

`accepts(subscriber, record)` 三条件：`want_rooms`（`room_id` 命中）、`want_types`（`type` 命中）、`keyword`（`keyword.lower() in text.lower()`）。注释：「`publish()` 与 `recent()` 共用这一处判断 —— 否则历史回填会绕过过滤（**实测**：订阅 `types=danmaku` 仍会收到互动/点赞等其它类型）」。

`recent(room_id, limit, accept)`：单房间直接 `items[-limit:]`；`accept` 非空时**从新到旧**迭代（`for raw in reversed(items)`）边解析边过滤，凑够 `limit` 就 break，最后 `picked.reverse()` 保证升序。「全部房间」时把各 buffer 合并。

### 6.2 `SLOW_CONSUMER` 判定

```python
DEFAULT_QUEUE_SIZE = 500
SLOW_CONSUMER_DROPS = 200
@property
def is_slow(self): return self.dropped >= self.slow_threshold
```

`ws_live` 的发送循环里：每成功发一条 `subscriber.sent += 1`，然后 `if subscriber.is_slow:` 发一条 `{"t":"error","data":{"code":"SLOW_CONSUMER","dropped":..., "hint":"客户端消费过慢，已断开以免影响采集"}}` 并 `break`。设计取向写在 `hub.py` 顶部：「慢客户端的处理是刻意的：直接断开并告知原因，而不是无限缓冲 —— 面板是采集的**消费者**，它不该反向影响数据完整性。」

WS 还有 `WS_IDLE_PROBE_SECONDS = 15.0` 空闲探测：`asyncio.wait_for(queue.get(), timeout=15)` 超时就发 `{"t":"ping"}`（「对端已断开会在 send 处抛错」）。连接建立时先发 `hello`（含 `version`、`rooms`、`filter`），再用 `hub.recent(..., accept=lambda record: hub.accepts(subscriber, record))` 回填 200 条。

### 6.3 路由与安全

安全约束（注释引 HANDOVER §6.4 DEC-3 / DEC-4）：

- `POST /api/admin/shutdown` 三道校验：`loopback_host(request.client.host)` 否则 **403**；`_origin_ok(request)` 校验 **Origin 的主机名**是否回环（「只比对**主机名**、不比对端口：面板端口可变…而回环主机名已足够约束来源」；**缺失 Origin 视为非浏览器调用，放行**）；`config.web.token` 非空时要求 `x-bili-token` 匹配。
- `loopback_host()` 用 `ipaddress` 而非字符串前缀：覆盖 `127.0.0.0/8`、`::1`、以及 **IPv4-mapped 的 `::ffff:127.0.0.1`**（`is_loopback` 对它为 False，必须显式认出来）。
- `config.validate_semantics()`：`web.host not in LOOPBACK_HOSTS and not web.token` → `ConfigError("非回环地址，必须设置 web.token，否则拒绝启动")`。`LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}`。
- 幂等：`app.state.shutdown_requested` 已置位则直接返回「已接受过停止请求」。
- 用 `BackgroundTask(handler)` 而不是 `loop.call_later`：「它的语义正是『响应发完之后再执行』，而且跟着请求生命周期走 —— `call_later` 在测试客户端（portal 事件循环）里根本不会被跑到」。
- `bind_port(host, port, span=20)`：「刻意**不设 SO_REUSEADDR**：Windows 上它会像 SO_REUSEPORT 一样允许『抢占』已占用的端口，从而把冲突检测变成假阳性。」返回值一律取自 `getsockname()`（有一段「早期实现在 `port=0` 时…调用方拿到 `0`（某些平台上表现为 `1`）这种**无效端口**」的缺陷复盘）。

### 6.4 历史游标实现

- 端点 `GET /api/history`，页大小 `DEFAULT_HISTORY_LIMIT = 400`、上限 `MAX_HISTORY_LIMIT = 1000`。
- 游标是 **`(recv_ts, id)` 复合键**，`before_ts`/`before_id` 与 `after_ts`/`after_id` 必须**成对**出现（只传一个则视为无游标）；两者同时出现 → 400「before_* 与 after_* 不能同时使用」。
- `_cursor_clause`：

```sql
before: (recv_ts < ? OR (recv_ts = ? AND id < ?))
after:  (recv_ts > ? OR (recv_ts = ? AND id > ?))
```

docstring 给出了必须这么做的实测依据：「`recv_ts` **会并列**：实测房间 804456 有 **393 组重复时间戳，最多 10 条同刻**（同一批推送被解析成同一 `recv_ts`）。只用 `recv_ts` 做游标会在页边界**漏条**」；并且「`ORDER BY` 必须与游标键**一致**（都是 `recv_ts DESC, id DESC`）。若写成 `ORDER BY id DESC` 而游标用 `recv_ts`，排序键与过滤键不一致，同样会漏条」。
- `iter_history()` 内部：`ORDER BY recv_ts DESC, id DESC LIMIT ?` 取 `limit` 行 → `_project()` → **`items.reverse()`**（「降序取、升序回」）→ 用 `_exists()` 做两次 `LIMIT 1` 存在性探测算 `has_older` / `has_newer`。
- `limit` 语义注释：「**`limit` 作用于『命中行』**：带 `keyword`/`types` 过滤时，窗口会自动往更旧的时间戳延伸直到凑满 `limit` 条命中（而不是『先截最新 N 条再过滤』）。翻页搜索要的正是前者；代价是过滤很严时单页可能跨很长的时间跨度。」
- **「全部房间」的兜底时间窗**：`HISTORY_PAGE_WINDOW_S = 3600.0`（端点默认 `window_s`）。`_window_clause` 只在「既没有 `room_id`、也没有显式 `since/until`」时加窗：「全表扫描 + 临时排序实测 **39.8 ms/页**，加窗后退回索引区间扫描」。锚点 `_anchor_ts` 用**只有 `room_id` 的最小过滤集**算数据时间边界，理由「锚点要的是『这个库/房间最新到哪』，不该被当前关键词缩小」。查询计划对照：带 `room_id` 走 `SEARCH event USING COVERING INDEX idx_event_room_ts (room_id=?)`，**没有临时排序**，**0.3 ms / 400 行**。
- 返回体含 `items/has_older/has_newer/oldest/newest/source/room_id/session_id/window_s/range`。

### 6.5 瘦投影

`HISTORY_COLUMNS = (id, room_id, session_id, recv_ts, type, uid, uname, text, price, medal_name, medal_level, extra_json)` —— **`raw_json` 与 `dedupe_key` 刻意不在其中**：

> 实测完整 `Event` 的线上体积约 **2.0 KB/条**（含 raw），400 条一页接近 **800 KB**，足以触发服务端 `SLOW_CONSUMER` 断连；瘦投影后约 **0.3 KB/条**。

`_project()` 进一步「刻意**不**构造 `Event`：那条路要解析 `raw_json`（单条 ~2 KB），对『翻页显示』是几十倍的浪费」，并且 `extra` **只保留 `emots`**，其余分析型字段丢掉。

`/api/events` 与 `/api/history` **故意不合并**（注释原文）：「`/api/events` 面向脚本/小结果集：完整 Event、支持 offset；`/api/history` 面向面板翻页：**瘦投影** + `(recv_ts, id)` 复合游标。不要把 `/api/history` 的投影逻辑塞回 `/api/events` —— 那会悄悄改窄既有契约。」

### 6.6 其它值得点的 server 细节

- `/api/density`：「聚合在 SQL 里完成（**实测 ~1 ms / 104 桶**）。**不要**退回『把事件搬到前端再算』—— 那正是旧的 `iter_events(limit=20100)` 路径在干的事（约 **0.9 s**）。」
- `/api/stats?window=0`（默认）走 **hub 内存累计口径**：「数据来自 `hub` 的内存计数，统计范围就是『这个面板从启动到现在』，不含历史补种与往次运行的库内数据」。
- 发言榜缓存：`TOP_SENDERS_TTL_S = 15.0`，`_TOP_SENDERS_CACHE: dict[str, tuple[float, list]]`。理由：「实测本机库 `top_senders` 单房间 **6.4 ms**、**全库 101.6 ms**（按 uid 分组排序没有合适索引），而 `/api/stats` 每 3 秒拉一次 —— 直接查全库等于每 3 秒白扫一次全表。」`reset_top_senders_cache()` 的存在理由也写在 docstring 里（模块级缓存会跨测试用例存活）。
- 打开库的统一入口 `_open_store_or_409()` 判两件事：配置里启用了 sqlite sink、库真的能打开；「这样『看到 409』的原因始终是同一个，不需要用户猜」。
- `_stream_export()` 传的是**工厂**而不是现成迭代器：「Starlette 用线程池**惰性**驱动同步生成器，如果调用方在协程里先开好连接、函数一返回就把连接关掉，生成器真正开始跑时数据库已经关了（**实测** `sqlite3.ProgrammingError: Cannot operate on a closed database`）。所以连接的开与关都必须发生在生成器**内部**。」
- 可用性检查必须在**响应之前**：「一旦开始流式输出，状态码就已经发出去了，那时再抛 `HTTPException` 只会变成一个断掉的 200（实测由 `test_export_requires_sqlite` 抓到）」。
- CSV BOM：`CSV_BOM = "\ufeff"`，「中文版 Excel 会按系统码页（GBK）打开无 BOM 的 CSV，导致中文全部乱码」；但 `/api/stats/export` 的内容**刻意不带 BOM，由前端加**（「这样这个端点在调试时（curl / 测试）读到的就是干净的 UTF-8 文本」）。
- `find_dist()` 的**冻结场景**分析（一段很值得讲的工程细节）：PyInstaller 会把 `bili_danmaku` 打进 `base_library.zip`，于是 `bili_danmaku.__file__` 是**合成路径**，候选 `bili_danmaku/web_dist` 在冻结后必然落空；所以冻结时按 `_MEIPASS/web_dist` → `exe/_internal/web_dist` → `exe/web_dist` 排序，并且「**打包场景下没有工作区兜底**，命中不了就只剩降级页」。

---

## 7. 分析 / 词云算法要点

### 7.1 `analysis.py` 打分公式（`suspicious_users`）

常量：`MAX_WORDS_PER_MESSAGE = 30`、`TOP_WORDS = 20`、`SPAM_MIN_COUNT = 4`、`SPAM_MIN_LENGTH = 4`、`SUSPICIOUS_MIN_SCORE = 45`、`SUSPICIOUS_MIN_MESSAGES = 8`、`MIN_RATE_SPAN_S = 30.0`。

三个维度（上限 40 + 40 + 20 = 100）：

```python
effective_unique = max(unique - 1, 0)
repeat_ratio = 1.0 - (effective_unique / count if count else 0.0)
user_window, degenerate = _rate_window(entry["stamps"])
rate_per_min = count * 60.0 / user_window

density_score = min(40.0, rate_per_min / 20.0 * 40.0)   # 20 条/分钟封顶
repeat_score  = min(40.0, repeat_ratio * 40.0)
burst = user_window / max(count - 1, 1)
burst_score   = 20.0 if (count >= min_messages and burst < 2.0) else 0.0

score = density_score + repeat_score + burst_score
if score < min_score: continue
```

两个**用 `unique - 1` 而不是 `unique`** 的注释：「每条不同的内容本身不算『重复』，全都不相同时应为 0、全部相同时才为 1 —— 用 `unique` 会得到『10 条完全相同 ⇒ 90%』这种永远够不到 100% 的结果（**实测踩到**）」。

**退化保护 `_rate_window(stamps)`**：先去重排序取 `distinct`，`span = distinct[-1] - distinct[0]`；`span < MIN_RATE_SPAN_S` 时返回 `(max(span,1.0), True)`。两类实测踩到的退化：

1. 历史补种把一批弹幕写成同一个 `recv_ts`，`末条-首条 = 0`，`count / max(span,1)` 算出「600 条/分钟」；
2. 房间未开播时那批补种落在 1 秒内（实测 10 条 / 0.9 秒 ⇒ 「600 条/分钟」、平均间隔 0.1 秒 ⇒ 直接命中「集中发送」）。

退化时 `density_score = 0.0; burst_score = 0.0`，但**重复率那项保留**（「它是文本层面的，与时间精度无关」），输出 `rate_available: False` 并附原因「（时间戳同刻，已忽略速率类指标）」。原则表述：「**宁可漏报，也不给用户一个假指控**」。

还有一处 `uid=0` 的处理：「`uid is not None and uid != 0`：uid=0 是匿名/打码用户，把它们并成一个『用户 0』会让那个假账号轻松冲到榜首（那是打码造成的，不是行为异常）。」

### 7.2 `hotspot_words(buckets, top=10, min_count=3, ratio=2.5)`

- 「最近一片 vs 之前若干片的**篇均速率**」，理由：「各片时长可能不同（最后一片常常是半截），直接比条数会把『这一片刚好长一点』误判成突增」。
- `history_rates[word] /= max(len(history), 1)`；当前片 `count < min_count` 跳过（「否则基线是 0 的词全都会变成『无限倍』」）。
- 基线为 0 的词 `spike = None` + `is_new: True`，「给一个『新词』标签，但**不编造倍率**」。
- 排序 `key=lambda i: (i["ratio"] is not None, -(i["ratio"] or 0), -i["count"])` —— 新词优先。

### 7.3 `spam_groups()`

「同一文本被反复发送的分组」。`min_length=4` 的理由：「`哈哈`、`6`、`？？？` 这类在直播间大量出现是正常气氛，把它们全报成『刷屏』只会淹没真正的异常。要短句统计就去看词频榜。」输出带 `single_user = len(uids) == 1`（「同一句话由少数人反复发 = 更可疑；人多则更像跟风」）、`span_s`、`sample_users`。

### 7.4 `words_from_buckets()`（省算路径）

把 `LiveWindow` 增量维护的时间片词频相加，**不再对窗口内每条弹幕重跑分词**；代价是拿不到 `users`（需要逐条看 uid），所以显式置 `"users": 0` 并标记 `"from_buckets": True`，「由调用方决定要不要花那份算力」。

### 7.5 `text.py` 分词与表情处理

- **表情占位符**：`EMOT_PATTERN = re.compile(r"\[([A-Za-z0-9_\-\u4e00-\u9fff ]{1,20})\]")`。设计理由（docstring）：「B 站的弹幕正文里，表情是内嵌的占位符文本（`哈哈[dog]`），`extra.emots` 给出 `[dog]` → 图片元数据的映射。如果把 `[dog]` 当普通词统计，词频榜会被表情刷屏（而且 `[`、`]` 也不该入词），所以这里把表情抽成**独立的 token**，默认不计入词语统计，但可以单独出榜。」长度上限 20 是为了「别把正文里的中文方括号整段吃掉」。
- `clean_text()`：依次抹掉 URL（`https?://\S+|www\.\S+`）、@提及（`@[\w\u4e00-\u9fff\-]+`）、表情占位符，再 `re.sub(r"\s+", " ", ...).strip()`。
- **兜底分词 `fallback_tokens()`**：「中文二元组 + 英文数字整词」。`_CJK_RUN = [\u4e00-\u9fff]+`，块内产出 `chunk[i:i+2] for i in range(len(chunk)-1)`；单字块原样产出交给 `min_length` 过滤。取舍写明：「二元组会产生重叠词（`主播`/`播好`），这是刻意取舍 —— 宁可多几个噪声词，也不要整句当一个 token。」`_ASCII_WORD = [A-Za-z][A-Za-z0-9_+#\-]*|[0-9]+(?:\.[0-9]+)?`（保留 `v50`、`666`、`up`）。
- `tokenize(text, tokenizer="auto", stopwords=None, min_length=2, max_length=12)`：`auto` 有 jieba 就用；token 统一 `strip().lower()`；过滤 stopword、长度、以及「不含任何字母数字或 CJK」的纯符号 token。
- `DEFAULT_STOPWORDS` 刻意很短，注释：「只放**真正的功能词/语气词**…弹幕里 `哈哈`/`主播`/`什么` 这类恰恰是有效信号，不能当噪声清掉。」
- `jieba_available()` 探测时 `_module.setLogLevel(60)`：「jieba 默认往 stderr 打『Building prefix dict…』，会污染 CLI 输出与管道」。
- `count_words()` 返回 `WordCloudReport`，排序 `sorted(counter.items(), key=lambda item: (-item[1], item[0]))` —— 「排序稳定，输出可复现（否则同一份数据两次跑出来的榜单会不一样）」。
- `find_cjk_font()`：`CJK_FONT_CANDIDATES` 20 个候选文件名，覆盖 Windows/macOS/Linux；`render_png()` 里 `os.environ.setdefault("MPLCONFIGDIR", ...)`（「wordcloud 会 import matplotlib，后者默认往用户目录写字体缓存；受限沙箱下会报 `Permission denied` 噪声」），`collocations=False`（「二元组已经是我们自己切的了，不要再合并」）。
- `render_html()` 是「**不依赖 wordcloud/PIL/字体**的可视化路径：字号按词频映射，颜色按排名循环取色，浏览器负责渲染。中文一定不会出现方块。」

---

## 8. GUI / 托盘 / launcher 的实现方式

### 8.1 纯 Win32 API（ctypes）+ tkinter，**零第三方依赖**

`gui/_win32.py` 用标准库 `ctypes` 直接绑定 `user32` / `shell32` / `kernel32` / `gdi32`，实现 `TrayIcon`（`Shell_NotifyIconW` + `CreatePopupMenu` + `TrackPopupMenu`）。docstring 里的「为什么不用 pystray / pywin32」三条理由：

1. 「本项目已经历过一次『pip 坏掉』的环境事故，**少一个依赖就少一份风险**」；
2. 「`pystray` 的菜单能力不足 —— 状态行、动态置灰、分隔符、默认项都要自己手搓，既然如此不如直接用底层 API，反而更可控」；
3. 「实测 `Shell_NotifyIconW` + `CreatePopupMenu` 用标准库就能完整跑通」。

**踩坑清单**（写在 docstring 里，属于很有说服力的细节）：

- `NOTIFYICONDATAW.cbSize` 必须与 Windows 期望一致，字段顺序/对齐错一点 `Shell_NotifyIconW` 就**静默失败**；
- `Shell_NotifyIconW` 返回 0 不一定是致命错误（Resource Explorer 未就绪），所以 `show(attempts=5)` **带重试**；
- **WNDPROC 的 64 位问题**：返回类型必须是 `LRESULT = ctypes.c_ssize_t`，用 `c_long` 会抛 `OverflowError`；
- `CreateSolidBrush`/`GetStockObject` 在 `gdi32`，不在 `kernel32`；
- 所有函数都**显式声明 `argtypes`/`restype`**：「不声明的话 ctypes 默认 int 返回，64 位上会截断句柄」；
- `_wndproc_ref = WNDPROC(self._wnd_proc)` 必须**持有引用**，否则会被 GC；
- `is_visible()` 的存在理由：托盘窗口用 `HWND_MESSAGE` 建，**不会**出现在 `EnumWindows` 里，所以不能靠枚举窗口判断。

菜单是纯函数 `build_menu(items, base_id=1000) -> (hmenu, {cmd_id: MenuItem})`；`MenuItem` 有 `enabled` / `default` / `separator`，`popup_menu()` 用 `TPM_RETURNCMD | TPM_NONOTIFY`（「菜单消息**不会**进本进程的队列，而是由 `TrackPopupMenu` 直接返回命令 id —— 这样就不必在 WNDPROC 里处理 `WM_COMMAND`」），并且**必须**显式 `SetForegroundWindow`（「否则菜单不会因为没有前台窗口而立刻消失」），收尾 `PostMessageW(hwnd, WM_NULL, 0, 0)`（「规范要求的收尾（否则菜单可能残留）」）。

### 8.2 消息泵与线程模型

`TrayIcon.pump(max_messages=32)` 用 `PeekMessageW(PM_REMOVE)` + `TranslateMessage` + `DispatchMessageW` 排空队列。docstring：「托盘需要一个**消息泵**才能收到鼠标事件。tkinter 自带事件循环，所以正确做法是**把窗口消息泵进主循环**…这样托盘与 tkinter 共用同一线程，无需多线程与锁。」

`gui/tray.py` 的线程模型：

- 全部 UI 在 **tkinter 主线程**；`PUMP_MS = 50` 由 `root.after(PUMP_MS, self._pump)` 周期驱动；
- `PanelController` 把 uvicorn 跑在自己的**后台守护线程**；
- `REFRESH_MS = 2000` 刷新状态，**直接读同进程的 `PanelController`**（不发 HTTP）。

托盘交互：左键单击 = 显示启动面板（「避免误触弹浏览器」）、左键双击 = 打开 Web 面板、右键 = 菜单。`build_menu_items()` 是**纯函数**（只需要一个 `PanelStatus` 与若干回调），理由：「GUI 本身在 CI/沙箱里点不了，但『菜单项与置灰逻辑』是可以断言的」。

### 8.3 单实例

**没有使用 OS 级互斥量（`CreateMutex`）**。存在的两套机制：

1. **CLI 托管路径**：`.runtime/web.json` 状态文件 + `live_runtime()` 的 pid 校验。`web start` 发现 `existing is not None` 且未加 `--force` → 打印「已有面板在运行（pid …）」并 `raise SystemExit(EXIT_CONFIG)`。托盘**刻意不写**这个文件：「状态文件归 CLI 管。托盘与 `verify` **刻意不写**它 —— 否则 `web stop/status` 会把一个 GUI 托盘当成『后台面板』，甚至去 kill 它」。
2. **托盘路径**：`PanelController.start()` 里有 `if self.is_running(): return self.status()`，配合 `threading.RLock` 保证「重复 `start()` 不会起第二个实例」。

由此产生一个**刻意接受的并存语义**（`cli.py::web_start` 注释）：「最常见的场景是**托盘应用已经在跑**（它用默认端口 8787 起的进程内面板）。托盘刻意不写 `.runtime/web.json`，所以 `live_runtime()` 看不到它 —— 不提示的话就会出现『两个面板同时开着、而用户以为只有一个』。这里只**提示 + 顺延**，绝不自动去停止别人的进程（那可能是托盘）。」HANDOVER 也确认：「于是**同时存在两个面板**。这是刻意的 —— 隐私决策 DEC-4 是『仅本机』。」

### 8.4 控制台窗口处理（一条很完整的事故复盘链）

问题：`web start` spawn 子进程后，Windows 上会**多出一个空终端窗口**，而且**关掉它面板就停**（HANDOVER §5.12）。

根因（`runtime.child_python()` docstring）：「不能用 `sys.executable`（= venv 的 `python.exe`）：它在 Windows 上是**转发器 stub**，会再起一个 base 解释器，而那个 base 解释器是 **console 程序**、又拿不到父进程的 console ⇒ **Windows 给它新分配一个 conhost**。用户看到的就是『`web start` 之后多出来一个空终端窗口』，而且**关掉它面板就停**。」

修法：取 `sys._base_executable` 直接起 base 解释器 → 「顶层进程本身就是 server ⇒ **没有多余的 console 可关**，那个缺陷从根上消失」。代价与对策：base 解释器不认识 venv 的 `sys.path`，所以把**当前进程的 `sys.path` 原样注入 `PYTHONPATH`**，「这比『猜 `src/` 与 venv 的 site-packages』稳 —— `sys.path` 是**事实**，而且顺带覆盖了 `pip install -e .` 的 `.pth` 展开结果」，并只保留**真实存在的绝对路径**（「空串会被解释成当前目录（安全隐患），相对路径则依赖 cwd」），另外设 `VIRTUAL_ENV` 与 `PYTHONNOUSERSITE=1`。拿不到 base 解释器时**退回 stub**（「宁可保留『多余 console』这个问题，也不要起不来」）。

`_hidden_startup_info()` 补充：「只靠 `creationflags` 不够 —— 父进程是控制台程序时，子进程仍可能继承控制台/标准句柄，于是『关掉父终端』会连带把它带走」，所以 `STARTF_USESHOWWINDOW` + `wShowWindow = 0`（`SW_HIDE`）。`detached_flags()` 返回 `DETACHED_PROCESS(0x8) | CREATE_NEW_PROCESS_GROUP(0x200)`。

`launcher.PanelController` 则是**架构级消解**：把 uvicorn 跑在**本进程的一个后台线程**里 → 「完全没有子进程、没有多余 console，那个缺陷在架构上消失；同时托盘/启动面板可以直接读 `hub` 状态，不必发 HTTP」。

`--windowed` 打包后 `sys.stdout`/`sys.stderr` 是 `None`，因此 `LauncherOptions.log_file` 有默认兜底 `_default_log_file()`：`frozen or no_streams` 时返回 `.runtime/web.log`。注释：「GUI 子系统进程没有 stderr，只写 `StreamHandler(sys.stderr)` 等于什么都不写（**实测：exe 退出后 `.runtime/web.log` 是 0 字节**）」。并且 `_uvicorn_log_config()` 显式接管 `uvicorn.error`/`uvicorn.access`（它们 `propagate=False` 且自带 handler，**不会**经过根 logger 的文件 handler），`disable_existing_loggers: False`（「否则会掐掉项目自己的 logger」）。

`PanelController` 的跨线程细节：`_serve()` 在子线程里记下 `self._loop = asyncio.get_running_loop()`，主线程加房间用 `asyncio.run_coroutine_threadsafe(panel.add_room(room), loop)`，理由：「`PanelService.add_room` 是协程，且其状态绑定在服务线程的 loop 上…而不是新开一个 `asyncio.run`（跨 loop 用同一个 service 会出问题）」。`_wait_until_listening()` 用**轮询 TCP 连接**判断服务真的起来了（「这样调用方拿到状态就能放心 `webbrowser.open()`」）。

---

## 9. 代码里明确写出的「为什么不用 X」

| 不用 | 位置 | 原因（原文要点） |
|---|---|---|
| **FTS5** | `sinks/sqlite.py` 与 `store.py` docstring | `unicode61` 把连续中文当单个 token（搜「主播」匹配不到「主播好」）；`trigram` 要求查询 ≥3 字符，而中文关键词常只有 2 字（「哈哈」「主播」）。改用 `LIKE` 子串匹配，「对本项目的量级（万~十万行）扫描成本可以接受，且语义正确」 |
| **`GROUP BY CAST(recv_ts/bucket AS INT)`** | `store.density()` | 对表达式分组用不上索引顺序 → `USE TEMP B-TREE`。实测 20 万行 118 ms vs 本实现 38 ms |
| **`iter_events()` 做词云/翻页** | `store.iter_texts()` / `_project()` | 「那条路要构造完整 Event（含 raw JSON 解析），对『只想数词』的场景是几十倍的浪费」 |
| **把事件搬到前端算密度** | `app.py::density` | 「那正是旧的 `iter_events(limit=20100)` 路径在干的事（约 0.9 s）」 |
| **令牌桶** | `utils.RateLimiter` | 「异步最小间隔限流器（**诚实串行化，不做令牌桶**）」 |
| **`pystray` / `pywin32`** | `gui/_win32.py` | 少一个依赖少一份风险；pystray 菜单能力不足；标准库实测够用 |
| **写入超时** | `Bus._flush_off_loop` | 「卡死的 sink 只该拖住它自己…**刻意不加超时**：超时后线程仍在跑，下一批再进来就可能并发操作同一个 SQLite 连接」 |
| **`loop.call_later`** | `app.py::shutdown` | 「`call_later` 在测试客户端（portal 事件循环）里根本不会被跑到」 |
| **`SO_REUSEADDR`** | `server.bind_port` | 「Windows 上它会像 SO_REUSEPORT 一样允许『抢占』已占用的端口，从而把冲突检测变成假阳性」 |
| **`contextlib.nullcontext(sys.stdout)`** | `cli._open_output` | 「结果不写文件时拿到的是那个 nullcontext 而不是 stdout，`handle.write` 直接 AttributeError —— 也就是『导出到标准输出』这条最有用的路径整个坏掉」 |
| **重试 -352** | `api.py` | 「失败即降级且**不重试**（重试会加重风控）」；`retry=1` |
| **把标注表塞进 `danmaku.db`** | `annotations.py` | 职责单一 / schema 归属冲突 / 可删 |
| **把面板当作另一套抓取逻辑** | `server/panel.py` | 「面板不是另一套抓取逻辑 —— 它只是多挂了一个 `WebHubSink`」 |
| **为缺前端而拒绝启动** | `server/app.py` | 「面板不该因为缺 Node 而让整个工具不可用」→ `FALLBACK_HTML` |
| **在 `analysis.py` 里做外发** | `analysis.py` | 「本地统计是这个方向能被接受的前提…**不要**在本模块里加请求」 |
| **把启发式包装成结论** | `analysis.py` | 「把启发式包装成结论会让用户据此误伤真人」→ 输出一律带 `score` + `reasons`，UI 用「疑似/仅供参考」措辞 |
| **静默截断超长 label** | `annotations.py` | 「静默截断会让用户以为存下来的就是他输入的」 |
| **从任意链接猜房间号** | `rooms.py` | 「`/blanc/8432038` 里的 `8432038` 就不是房间号…猜错会把用户带到一个完全无关的房间」 |
| **进程名匹配判存活** | `runtime.pid_alive` | 「匹配 `python.exe` 会撞到别的 Python 进程，反而误判」 |
| **`catch` 掉解析异常丢消息** | `parser.parse_message` | 单条解析失败只 warn + 返回 None 计入 `bad_msg`，「**绝不丢消息**」；未知 cmd 走 `other` 兜底 |

---

## 10. 面试官可能追问的技术细节与薄弱点

### 10.1 大概率被追问的强项（准备好讲）

1. **递归拆包为什么是必需而非优化** —— `protover=2/3` 解压后仍是带头部的包串；能背出实测校验点：认证回应 `总长=26, 头长=16, protover=0, op=8`，body = `{"code":0}`；心跳回应 `op=3, protover=1`，`body[:4]` 大端 uint32 = 人气值，`body[4:]` 是 `b"[object Object]"` 回显。
2. **`to_thread` 那条因果链** —— 「慢 sink → 事件循环卡 → 心跳发不出 → 判假死 → 重连」，并且有**故意 sleep 3 秒的 sink 的复现用例** `test_queue_full_keeps_newest_and_never_blocks_reader`。以及「为什么不加超时」（超时后线程仍在跑 → 下一批可能并发用同一个 SQLite 连接）。
3. **`(recv_ts, id)` 复合游标** —— 说不出「393 组重复时间戳、最多 10 条同刻」这个实测数字就弱了；以及「`ORDER BY` 必须与游标键一致」这条。
4. **瘦投影** —— 2.0 KB/条 → 0.3 KB/条，400 条一页 800 KB 会触发 `SLOW_CONSUMER`。这把「协议层 → 落盘 → 广播 → 前端」串成一条完整的容量链路。
5. **`_rate_window` 的退化保护** —— 能讲出「历史补种同刻 → 600 条/分钟 → 假指控」，以及「宁可漏报，也不给用户一个假指控」这个产品判断。
6. **零依赖 Win32 托盘** —— `LRESULT = c_ssize_t`（64 位 OverflowError）、`cbSize` 对齐导致静默失败、`TPM_RETURNCMD` 避开 `WM_COMMAND`、`HWND_MESSAGE` 让 `EnumWindows` 看不到。
7. **`child_python()` 的 base 解释器绕行** —— venv stub → base interpreter → 新 conhost → 关窗连坐。这题很能体现 Windows 进程/控制台模型的理解深度。

### 10.2 真正薄弱 / 值得主动承认的点

1. **没有任何 OS 级单实例互斥**：`CreateMutex`（或命名管道/锁文件）都没用。所以「CLI 托管面板 + 托盘面板」可以同时在跑（文档承认这是刻意的），而且**两个进程可以写同一个 `danmaku.db`**。SQLite WAL 会串行化写入，但 `dedupe_key UNIQUE` 之外的语义（同一消息在两个进程各存一份、`session_id` 交错）没有跨进程协调。**追问预期**：「如果两个实例同时抓同一个房间会怎样？」——老实说：不会崩（WAL + busy_timeout），但会重复入库，去重只在单进程内存里（`DedupeCache`）。
2. **`raw_json` 体积未做压缩/裁剪**：`keep_raw=True` 是默认值，`raw` 存的是整条原始消息 JSON（含 `info` 数组里所有冗余字段）。文档量到 2.0 KB/条，但没做字段裁剪或 zlib 压缩。十万条就是 ~200 MB。**追问预期**：「怎么控住库体积？」——现成的答案是 `--no-raw` 与 `rotate_hours`/`annotations` 分层，但没有自动归档/清理策略。
3. **`DedupeCache` 的 TTL 语义**：命中重复时**刷新时间戳并 `move_to_end`**，所以一个被反复重发的 `msg_id` 可以永久驻留（不会过期），只是被挤到 LRU 尾部。`_evict_expired` 依赖「OrderedDict 按插入顺序，过期项必然聚集在头部」——这个前提在 `move_to_end` 之后依然成立（是按最近访问时间排序），但值得能说清楚。
4. **`parse_data_cmd` 硬编码了大量 cmd 分支**（`elif base in (...)` 长链），扩展性一般。`CMD_TYPE_MAP` 只管类型映射，字段提取仍是一串 if/elif。
5. **`_first_int` / `_FieldPicker.pick` 用真值判定 `0`/`""`**：`pick()` 把 `0`/`""`/`[]`/`{}` 都视为「无有效值」，注释解释了这是为了让 `field_source` 指标不被 `guard_level=0` 污染。但代价是**真实值为 0 的字段会退回下标取值**——如果结构化对象的 `level=0` 而下标给了一个非 0 的历史值，就会取到错的那个。这是一个明确写进注释的取舍，但要能讲出它的代价。
6. **`protobuf_lite._looks_like_message` 是启发式**：判据是「非完全可打印 UTF-8」或「字段号有多样性」。docstring 自己承认：「纯文本经常能"恰好"被解析成 protobuf（例如 `hi` → field 13 / varint）」。所以嵌套还原**可能漏**（可打印且字段号单一的嵌套消息不会被递归）。`MAX_DEPTH = 8` 是硬上限。
7. **`web.token` 的传递方式**：放在 `X-Bili-Token` 头。仅回环时默认空 token，即**任何本机进程都能调 `/api/admin/shutdown`**（`Origin` 缺失被视为非浏览器调用而放行）。这是「仅供本机」决策下的合理取舍，但要能说清威胁模型（本机任意进程 → 能停面板，但不能读数据）。
8. **`config.py::_apply_env` 只支持 3 个环境变量**（`BILI_LOG_LEVEL` / `BILI_OUT_DIR` / `BILI_COOKIE_FILE`），但 `credentials.load_credentials` 又读 `BILI_SESSDATA` 等 `BILI_<COOKIE_KEY>`。两套环境变量机制并列，`_apply_env` 里的 `mapping` 是硬编码列表。另外 `_apply_env` 用 `raw.setdefault(section, {})` 再判 `isinstance(raw[section], dict)`，如果 TOML 里 `[sinks]` 写成了标量就会静默跳过——**没有报错**。
9. **`_default_db_path` 与 `store` 的路径来源不一致风险**：`app._open_store` 从 `cli._default_db_path(config)` 取，`SqliteSink.db_path` 却是「配置优先，否则 `out_dir/danmaku.db`」。两处逻辑分开实现，配置改动时要同时看。
10. **`annotations` 与采集库的时间对齐**：`anchor_id` 被刻意**不校验**（「校验会引入对采集库的依赖」）。所以按标记区间导出时，如果采集库被替换过，`recv_ts` 区间是对的，但被钉的那条弹幕可能已经不是同一条。
11. **`LiveWindow` 词表淘汰是近似的**：「超了淘汰『当前最少』的词。这是**近似**，但对『现在最热的词』这个问题足够 —— 冷词被淘汰不影响热词计数，而精确去重需要额外索引。」注意 `sorted(counts.items(), key=...)[:overflow]` 是**每来一条消息就可能触发一次 O(n log n) 排序**（n 最多 2000），这在 `publish()` 热路径上，严格说不是 O(1)——与该类 docstring 声称的「`add()` 必须 O(1)」有张力。**这是最值得主动指出的一个点。**
12. **`hub.window_snapshot(room_id=None)` 合并多房间时**：`messages.extend` 所有房间后排序再裁到 `ANALYSIS_WINDOW_MESSAGES`，bucket 按 `start` 合并 `counts.update`。合并后的词频上界是「房间数 × 2000 词/片」，没有全局上限——`_windows` 只增不减（「房间移除后保留，重新添加时还能用旧基线」），长时间反复添加大量房间会累积。
13. **`_TOP_SENDERS_CACHE` 是模块级全局**，docstring 自认「它会跨测试用例存活」，为此专门加了 `reset_top_senders_cache()`。生产上多 worker 部署会各自缓存（本项目单进程 + 回环，没问题）。
14. **`bind_port` 的 TOCTOU**：`_try_bind` 用完就 `with` 关闭 socket 返回端口号，端口在「探测成功」与「uvicorn 真正 bind」之间可能被别人抢走。`span=20` 只是缓解。
15. **`launcher.PanelController._build_config()` 重写了一遍 CLI 的选项→配置映射**（`cli._web_config`），注释说明是为了不依赖 click 的 `Context`。这是**两份需要同步维护的映射**（`LauncherOptions` 与 `web_options`），是真实的漂移风险。
16. **测试覆盖作为证据**：`tests/` 下有 40+ 个测试文件（`test_protocol.py`、`test_parser.py`、`test_wbi.py`、`test_sinks.py`、`test_store_extra.py`、`test_history_api.py`、`test_analysis.py`、`test_launcher.py`、`test_gui.py`、`fake_server.py`、`tests/fixtures/`），并且**多处注释直接引用测试名**（如 `test_export_requires_sqlite`、`test_queue_full_keeps_newest_and_never_blocks_reader`）作为缺陷已被固定的证据。被问「你怎么保证」时，这是最好的抓手。

### 10.3 可以主动抛出的「设计取舍升级路线」

- 跨进程单实例 → 命名互斥量或 `out_dir/.lock` + pid；
- `raw_json` → 按需落盘（只在解析失败/未知 cmd 时保留）+ 冷数据归档；
- `LiveWindow` 词表淘汰 → 改成定期批量裁剪（移出热路径），兑现 O(1) 承诺；
- `_windows` → 加 LRU 上限或房间移除时释放；
- cmd 分支链 → 表驱动（`CMD_TYPE_MAP` 再挂一个字段提取器注册表）。
