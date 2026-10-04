# B 站直播弹幕提取器 —— 技术设计文档（实现级）

> 版本：V1.0　状态：**待审阅**
> 上游文档：`DESIGN.md`（已通过评审）
> 本文档定位：把 `DESIGN.md` 的方案落到**函数级 / 字节级 / 契约级**，实现时按本文档写代码，不再另行决策。

---

## 0. 已确认的需求输入

| 项 | 决定 | 落地位置 |
|----|------|----------|
| 验证房间 | **804456**（`https://live.bilibili.com/804456`） | 实测真实房间号就是 804456，非短号 |
| 运行态 | **使用登录态**（SESSDATA） | §5 凭据子系统 |
| `raw` 字段 | **默认保留** | §4 配置默认值、§12 Sink |
| Web 面板端口 | 默认 8787，**被占用时自动换** | §13.4 端口策略 |
| V2 范围 | **仅弹幕词云 + SC 汇总/告警** | §19 V2 预留接口 |

---

## 1. 实测验证结论（本方案的事实基底）

> 以下结论来自本机对房间 804456 的**真实只读实测**（`tools/preflight.py` + `tools/handshake_check.py`），不是文档推测。实测脚本已入库，可随时复现。

### 1.1 已验证成立

| # | 结论 | 证据 |
|---|------|------|
| V1 | 本机可正常访问 `api.live.bilibili.com` / `passport.bilibili.com` 与外网 WSS | 全部接口 HTTP 200 |
| V2 | 房间 804456 为**真实房间号**，`room_init` 返回 `room_id=804456`、`uid=8431064` | `room_init` code=0 |
| V3 | 实测时该房间 **`live_status=1`（直播中）**，可直接做真实抓取验证 | `room_init` |
| V4 | **兜底接口 `Danmu/getConf` 匿名可用**，返回 `token` + 6 个节点 | `code=0`，token 非空 |
| V5 | **`getConf` 的 token 可以完成 WS 认证**，`{"code":0}` | 实测 `op=8` 回应 code=0 |
| V6 | 心跳链路正常，`op=3` 回应可解析出人气值 | 实测人气值=1（房间热度低） |
| V7 | **匿名即可收到真实弹幕**，25 秒收到 11 条消息 | 实测 cmd 分布见 §1.3 |
| V8 | `protover=2`（zlib）路径可用，**不装 brotli 也能跑** | 本次全程仅用标准库 `zlib` |
| V9 | `nav` 接口匿名可访问，`wbi_img` 可用 → **WBI 密钥无需登录即可获取** | `code=-101` 但 `wbi_img` 非空 |
| V10 | `finger/spi` 可获取 buvid3（长度 46）/ buvid4 | `code=0` |
| V11 | **`protover=3`（brotli）路径可用**，装 brotli 后正常解包 | `probe` 报告 `protover=3` 且协议错误=0 |
| V12 | `getConf` 原始返回 6 个节点，**实际只有 4 个唯一节点** | 去重后 4 个，全部 `wss_port=443` |
| V13 | `INTERACT_WORD_V2` 的 `data.pb` 是 **base64 编码的 protobuf**，可完整还原 | 通用 protobuf 解析器成功解出昵称/房间/时间戳/互动类型 |
| V14 | `LOG_IN_NOTICE` 明确告知「**为保护用户隐私，未登录无法查看他人昵称**」 | 实抓文案，见 §1.5 |
| V15 | **登录态下 `getDanmuInfo` 可用，但必须叠加 WBI 签名**；返回节点端口为 2245 | `probe` 报告 `token来源=getDanmuInfo+wbi`，认证 `code=0` |
| V16 | **登录态下昵称完全恢复**，打码现象消失 | `coffee辞` / `求求动动脑子吧QAQ` / `战争乳牛warcow` / `鬼子进村298` |
| V17 | `INTERACT_WORD_V2` 的 protobuf **确实不含 uid**（登录后仍为 None） | 登录态样本 `uid=None name='陌路铭'` |
| V18 | `DM_INTERACTION` 是点赞聚合卡片，且 `data.data` 是**嵌套 JSON 字符串** | 解出 `cnt` / `suffix_text` / `pv_prefix_text` |
| V19 | **礼物族事件（`COMBO_SEND`）字段是 snake_case**，金额在 `combo_total_coin` | 实抓：`gift_name='传音符'`、`combo_total_coin=200`、`gift_num=0`、`total_num=2` |
| V20 | 用户信息分散在多种嵌套块里：点赞用 `uinfo`、礼物用 `sender_uinfo`、SC 用 `user_info`；粉丝牌用 `fans_medal` 或 `medal_info` | 实抓各命令结构 |

### 1.2 实测推翻的原假设（**重要**）

| # | 原设计假设 | 实测结果 | 修正 |
|---|-----------|----------|------|
| R1 | `getDanmuInfo` 是主路径 | **匿名**（含带 buvid3）一律返回 `code=-352` 风控；**登录态 + WBI 签名**下成功（V15） | 主路径：登录态走 `getDanmuInfo+wbi`；匿名/失败降级 `getConf` |
| R2 | `INTERACT_WORD` 是进场 cmd | 当前实际下发 **`INTERACT_WORD_V2`** | 映射表同时支持两者，并按 `:` 前缀归一 |
| R3 | `DANMU_MSG` 字段用 `info[n]` 下标取 | 新版在 **`info[0][15].user`** 提供了结构化对象，另含 `info[0][15].extra`（JSON **字符串**） | **优先用结构化对象**，位置下标仅作兜底（§10.2） |
| R4 | 去重键需要自造哈希 | 实测 `info[0][15].extra.id_str` 是**全局唯一消息 ID** | 去重键首选 `id_str` |
| R5 | 匿名与登录都能拿到 UID | 匿名下**部分**用户被打码（`逸***`/`宅***`），另一部分完整（`八斤半两` uid=600110） | 打码与未登录相关但非普遍现象；解析层必须容错，登录态应提高可见比例（§1.5） |
| R6 | 进场消息仍带 `uid`/`uname` JSON 字段 | V2 改为 protobuf，**不含 uid**，只有昵称 | 引入 `protobuf_lite.py` 解析；uid 置空并标记 `pb_encoded` |
| R7 | 礼物字段是 camelCase（`giftName`/`num`/`total_coin`） | 实测 `COMBO_SEND` 是 **snake_case**（`gift_name`/`combo_total_coin`），且用户信息在 `sender_uinfo` | 解析器同时支持两种形态；金额优先 `combo_total_coin`（V19） |

### 1.3 实测到的真实 cmd 分布（25 秒 / 11 条）

```
INTERACT_WORD_V2        4      ← 当前进场/关注消息（V2）
ONLINE_RANK_COUNT       2
WATCHED_CHANGE          1
DM_INTERACTION          1      ← 新 cmd，原方案未覆盖
LOG_IN_NOTICE           1      ← 新 cmd，原方案未覆盖
DANMU_MSG               1
LIKE_INFO_V3_UPDATE     1
```

结论：`other` 兜底分支不是可选项而是**必需项**；消息类型映射表必须以 `probe --dump-raw` 的实抓结果为准持续补充。

### 1.4 尚未验证（M1 补齐）

| # | 待验证项 | 验证方式 |
|---|---------|----------|
| ~~U1~~ | ~~登录态下 `getDanmuInfo` 是否成功；是否还需 WBI 签名~~ | ✅ **已解决（V15）**：成功，且 **WBI 签名为必需**，`wbi.py` 从"备选"升级为"登录态必需组件" |
| ~~U2~~ | ~~登录态下昵称是否恢复完整~~ | ✅ **已解决（V16）**：完全恢复，打码消失 |
| U3 | `SEND_GIFT` / `SUPER_CHAT_MESSAGE` / `GUARD_BUY` 的真实字段结构 | 🟡 **已按决策收敛**：礼物族（`COMBO_SEND`）已抓样并实现（V19/V20，snake_case）；**礼物/上舰不纳入采集（DEC-1），故 `SEND_GIFT`/`GUARD_BUY` 不再需要抓样**；只剩 `SUPER_CHAT_MESSAGE` 待抓（SC 仍采集，且其金额单位尚未用真实样本验证） |
| ~~U4~~ | ~~`protover=3`（brotli）路径~~ | ✅ **已在 M0 验证**（V11）：`probe` 全程走 brotli，协议错误=0 |
| ~~U5~~ | ~~高频房间下的吞吐与背压表现~~ | ✅ **已解决（M4）**：房间 7878 实测约 300 条/分钟，零丢弃零重连；`queue_size=3` 强制背压下丢最旧且不重连（见 §18 与 HANDOVER §5.2） |
| ~~U6~~ | ~~`gethistory` 匿名返回 0 条~~ | ✅ **已验证**：登录态下可用（实测补种 10 条），匿名空结果是「未登录看不到他人昵称」的连带限制 |

### 1.5 昵称打码：匿名专属的降级视图（**已确认**）

实测抓到这条 `LOG_IN_NOTICE`：

```json
{"cmd":"LOG_IN_NOTICE","data":{"notice_msg":"为保护用户隐私，未登录无法查看他人昵称", ...}}
```

匿名下同一次采集里同时观察到两种情况：

| 观察 | 样本 |
|------|------|
| 被打码 | `DANMU_MSG` 的 `逸***`；`INTERACT_WORD_V2` 的 `宅***` / `q***` / `愿***` / `夕***` |
| 完整可见 | `DANMU_MSG` 的 `uid=600110 八斤半两`；`LIKE_INFO_V3_CLICK` 的 `uid=445594178 欲心yvxin` |

**登录后复测（V16）：`INTERACT_WORD_V2` 昵称全部完整** ——
`coffee辞` / `求求动动脑子吧QAQ` / `浅色西淋` / `陌路铭` / `bili_15794994159` /
`鼠鼠爱吃红三角洲` / `战争乳牛warcow` / `傻子那F蛋疼`，打码现象消失。

结论：

- 打码是**匿名专属的降级视图**，登录后消失 —— 需求方选择登录态是**正确且必要**的；
- `INTERACT_WORD_V2` 的 protobuf **协议本身不含 uid**，登录后也没有（V17），
  因此 `interact` 事件的 `uid` 恒为空，这是协议限制而非解析缺陷；
- 解析层对 `uid=None` / 打码昵称完全容错，并提供 `--exclude-masked`。

---

## 2. 环境与依赖

### 2.1 实测环境

```
Python 3.11.3  (E:\Python\python.exe)
已装: httpx 0.28.1 | click 8.1.3 | pydantic 2.10.6 | websocket-client 1.8.0
缺失: websockets | brotli | fastapi | uvicorn
注意: websocket-client 的 import 名是 `websocket`（不是 websocket_client）
```

### 2.2 依赖安装（M0 第一件事）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install httpx websockets brotli click pydantic fastapi "uvicorn[standard]"
python -m pip install qrcode          # 可选：终端二维码登录
python -m pip install -e ".[dev]"     # 开发装 pytest pytest-asyncio
```

**降级预案（若 pip 不可达）**：本环境 `websocket-client` 已装且 **V8 已证明 zlib 路径可用**，因此可立即降级为：
- WS：`websocket`（websocket-client）跑在 `asyncio.to_thread` 线程中；
- 压缩：仅 `protover=2` + 标准库 `zlib`；
- 面板：标准库 `http.server` + SSE 只读替代 FastAPI。

即**最坏情况下项目仍可完成 M0–M2 全部核心功能**，面板降级。

### 2.3 `pyproject.toml`

```toml
[project]
name = "bili-danmaku"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
  "httpx>=0.27", "websockets>=12", "brotli>=1.1",
  "click>=8.1", "pydantic>=2", "fastapi>=0.110", "uvicorn[standard]>=0.29",
]

[project.optional-dependencies]
qr = ["qrcode>=7.4"]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]
wordcloud = ["jieba>=0.42"]   # V2 词云用，V1 不装

[project.scripts]
bili-danmaku = "bili_danmaku.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
```

---

## 3. 工程骨架

```
LiveDanmakuExtractor/
├─ DESIGN.md                    # 已评审
├─ TECH_DESIGN.md               # 本文档
├─ README.md                    # M4
├─ pyproject.toml
├─ .gitignore                   # .venv/ data/ .secrets/ *.jsonl cookies*.txt
├─ .env.example
├─ config.example.toml
├─ tools/
│  ├─ preflight.py              # ✅ 已实现：依赖 + 连通性 + 风控探测
│  └─ handshake_check.py        # ✅ 已实现：协议握手实测 + 样本抓取
├─ src/bili_danmaku/
│  ├─ __init__.py   __main__.py
│  ├─ cli.py                    # click 子命令
│  ├─ config.py                 # 配置加载与校验
│  ├─ credentials.py            # SESSDATA / buvid / QR 登录 / 掩码
│  ├─ api.py                    # HTTP 接口封装 + 限流 + 重试
│  ├─ wbi.py                    # WBI 签名
│  ├─ protocol.py               # 封包/解包/解压
│  ├─ client.py                 # WS 连接状态机
│  ├─ parser.py                 # cmd → Event
│  ├─ models.py                 # pydantic 模型
│  ├─ filters.py                # 过滤规则
│  ├─ sinks/{base,bus,jsonl,csv_txt,sqlite}.py
│  ├─ server/{app.py,static/index.html}
│  └─ utils.py                  # 日志/退避/限流/去重/统计
└─ tests/
   ├─ fixtures/danmu_msg_samples.json   # ✅ 已抓取的真实样本
   ├─ test_protocol.py  test_parser.py  test_filters.py
   ├─ test_sinks.py  test_wbi.py  test_client_reconnect.py
   ├─ test_server_api.py  fake_server.py
```

---

## 4. 配置系统

### 4.1 优先级

```
CLI 参数 > 环境变量(BILI_*) > --config 指定 TOML > 内置默认值
```

### 4.2 配置对象（`models.Config`，pydantic）

| 段 | 字段 | 默认 | 说明 |
|----|------|------|------|
| general | `log_level` | `INFO` | |
| general | `max_rooms` | 3 | 并发房间上限 |
| general | `anonymize` | `false` | 脱敏输出 |
| api | `cookie_file` | `.secrets/cookies.json` | 凭据文件 |
| api | `request_timeout` | 10 | 秒 |
| api | `min_request_interval` | 1.0 | HTTP 最小间隔（防风控） |
| api | `prefer_getdanmuinfo` | `true` | 登录态下优先走 getDanmuInfo |
| api | `retry_max` | 3 | 单接口重试次数 |
| client | `heartbeat_interval` | 30 | 秒，**不得大于 30** |
| client | `heartbeat_timeout` | 90 | 秒，超时判假死 |
| client | `recv_timeout` | 5 | 秒，单次 recv 等待 |
| client | `reconnect_max_delay` | 60 | 秒 |
| client | `protover` | 3 | 失败自动降 2 |
| sinks | `types` | `["jsonl","csv","sqlite"]` | 启用哪些 |
| sinks | `out_dir` | `data` | |
| sinks | `keep_raw` | **`true`** | 按需求默认保留 |
| sinks | `rotate_hours` | 1 | |
| sinks | `batch_size` / `flush_interval` | 200 / 1.0 | |
| sinks | `queue_size` | 20000 | 有界队列，满则丢最旧并计数 |
| web | `host` | `127.0.0.1` | |
| web | `port` | 8787 | **被占用自动递增**（§13.4） |
| web | `token` | `""` | 绑非回环时必填，否则启动报错退出 |

### 4.3 校验规则（启动即失败，不留到运行期）

1. `heartbeat_interval > 30` → 拒绝启动（会被服务端 60s 断开）。
2. `web.host` 非 `127.0.0.1/localhost/::1` 且 `web.token` 为空 → 拒绝启动。
3. `prefer_getdanmuinfo=true` 但无有效凭据 → 打 WARN，自动转 `getConf`。
4. `types` 为空 → 拒绝启动。
5. `out_dir` 不可写 → 拒绝启动。

---

## 5. 凭据与登录子系统（`credentials.py`）

### 5.1 安全约束（**请审阅者特别注意**）

> **不要把 SESSDATA 贴到聊天/issue/日志里。** SESSDATA 等价于账号登录态，泄露即可被用于以你的身份操作（含发弹幕、改资料）。因此本设计的凭据获取**默认走扫码**，凭据只落在本机 `.secrets/`，且全程掩码。

| 约束 | 实现 |
|------|------|
| 凭据来源 | ① `BILI_SESSDATA` 环境变量 ② `.secrets/cookies.json` ③ `--cookie-file` ④ `[api].cookie` |
| 推荐方式 | `bili-danmaku login` 扫码（**不经聊天、不手抄**） |
| 落盘权限 | POSIX `chmod 0600`；Windows 设隐藏 + 仅当前用户 ACL |
| 日志掩码 | 统一 `mask(s) -> 前4****后4`；任何异常/请求日志不得出现完整值 |
| 版本控制 | `.gitignore` 覆盖 `.secrets/`、`.env`、`cookies*.txt` |
| 上传 | 本项目**不含任何自动上传逻辑** |

### 5.2 扫码登录时序（`bili-danmaku login`）

```
Client                          passport.bilibili.com
  │
  ├─ GET /x/passport-login/web/qrcode/generate
  │◀── { data: { url, qrcode_key } }        密钥有效期 180s
  │
  ├─ 终端渲染二维码（qrcode 库 print_ascii；不可用时打印 url + 提示）
  │
  └─ 轮询 GET /x/passport-login/web/qrcode/poll?qrcode_key=…   每 2s
       │◀── data.code:
       │      86101 未扫码         → 继续轮询
       │      86090 已扫码未确认    → 提示"已扫码，请在手机确认"
       │      86038 已失效         → 重新申请二维码（最多 3 次）
       │      0     成功           → 读响应 Set-Cookie
       ▼
     保存 SESSDATA / bili_jct / DedeUserID / DedeUserID__ckMd5 → .secrets/cookies.json
```

- 轮询上限 180s，超时退出码 4。
- `qrcode` 未安装时：打印 `url` 并提示「用手机 B 站 App 扫描浏览器打开的该链接」，或直接提示改用 `BILI_SESSDATA`。
- 备份方案：`bili-danmaku login --manual` 交互式 `getpass` 输入 SESSDATA（不回显、不进 shell 历史）。

### 5.3 `check-auth` 判定矩阵

对 `GET https://api.bilibili.com/x/web-interface/nav`：

| 响应 | 判定 | 处理 |
|------|------|------|
| `code=0, isLogin=true` | 有效 | 打印 `mid` / `uname` / 大会员状态 |
| `code=-101` | 未登录/失效 | 提示重新 `login` |
| HTTP 412 / 风控 | 被限流 | 提示降低频率、检查 UA/Cookie 完整性 |

### 5.4 buvid 持久化

- 首次运行调 `GET https://api.bilibili.com/x/frontend/finger/spi` 取 `b_3`/`b_4`，存 `.secrets/buvid.json` 复用（同设备保持一致，降低风控概率）。
- 实测 `buvid3` 长度 46。
- **实测结论**：单靠 buvid3 **不足以**通过 `getDanmuInfo`（仍 -352），所以 buvid 是「必要条件非充分条件」，不能当作风控的唯一解法。

### 5.5 认证包里的 uid

```python
uid = int(cookies.get("DedeUserID") or 0)   # 未登录为 0（游客）
```

---

## 6. HTTP 层（`api.py`）

### 6.1 统一请求头（实测有效组合）

```python
HEADERS = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Referer": "https://live.bilibili.com/",
  "Origin":  "https://live.bilibili.com",
}
```

### 6.2 接口清单与函数签名

| 函数 | 端点 | 关键返回 | 登录必需 |
|------|------|---------|---------|
| `room_init(room) -> RoomInfo` | `room/v1/Room/room_init?id=` | `room_id, uid, live_status, live_time` | 否 |
| `danmu_conf(room_id) -> DanmuConf` | `room/v1/Danmu/getConf?room_id=&platform=pc&player=web` | `token, host_server_list[]` | **否（实测可用）** |
| `danmu_info(room_id) -> DanmuInfo` | `xlive/web-room/v1/index/getDanmuInfo?id=&type=0` | `token, host_list[], max_delay` | **是（实测 -352）** |
| `fingerprint() -> Buvid` | `api.bilibili.com/x/frontend/finger/spi` | `b_3, b_4` | 否 |
| `history(room_id) -> list[dict]` | `xlive/web-room/v1/dM/gethistory?roomid=&room_type=0` | `data.room[]` | 待验证 |
| `nav() -> NavInfo` | `api.bilibili.com/x/web-interface/nav` | `isLogin, mid, uname, wbi_img` | 否 |

### 6.3 token 获取策略（**基于实测修正**）

```python
def acquire_token(room_id, creds) -> TokenBundle:
    """返回 (ws_hosts, token, source)。source ∈ {getDanmuInfo, getConf}"""
    if creds.logged_in and cfg.api.prefer_getdanmuinfo:
        r = danmu_info(room_id)                    # 带 SESSDATA
        if r.code == 0 and r.token:
            return r, "getDanmuInfo"
        log.warning("getDanmuInfo 失败 code=%s，降级 getConf", r.code)
        if r.code == -352:
            r2 = danmu_info(room_id, wbi=True)     # 尝试 WBI 签名重试
            if r2.code == 0 and r2.token:
                return r2, "getDanmuInfo+wbi"
    return danmu_conf(room_id), "getConf"          # 实测匿名可用，必成功路径
```

**要点**：
- `getConf` 在实测中是**唯一稳定可用**的路径，因此它承担「保底」职责，且**不因 getDanmuInfo 失败而重试太多次**（避免风控叠加）。
- `-352` 出现时**必须**：停止对该房间的 `getDanmuInfo` 尝试（本进程内记住），直接走 `getConf`，并降低 HTTP 频率。
- 每次 WS 重连都重新 `acquire_token`（token 会过期）。

### 6.4 节点选择与去重

实测 `getConf` 返回 **6 个节点但内容重复**（全为 `zj-cn-live-comet`）。因此：

```python
seen = set()
hosts = [h for h in raw_hosts
         if (h["host"], h.get("wss_port")) not in seen
         and not seen.add((h["host"], h.get("wss_port")))]
```

- 优先 `wss_port`（实测 443）；`ws_port`(2244) 仅当 WSS 不可用时尝试。
- 节点轮换：连接失败按列表顺延，全部失败才进入退避重连。

### 6.5 限流与重试

| 项 | 策略 |
|----|------|
| 限流 | 全局 `asyncio.Semaphore` + 最小间隔 1.0s（`min_request_interval`） |
| 重试 | 仅对网络异常与 5xx 重试；`retry_max=3`，退避 0.5/1/2s + 抖动 |
| **不重试** | `code=-352`（风控）：立即降级 + 加长间隔，重试会加重风控 |
| 连接池 | `httpx.AsyncClient` 复用，`limits.max_connections=8` |
| cookie | 使用 `httpx.Cookies` 自动携带，**不手工拼 Cookie 头**（便于 QR 登录后自动生效） |

---

## 7. WBI 签名（`wbi.py`）

### 7.1 算法（三段式）

1. 取密钥：`nav` → `data.wbi_img.img_url` / `sub_url`，取文件名去 `.png` 得 `img_key` / `sub_key`（**每日更替，缓存 1 小时**）。
2. `raw = img_key + sub_key`，按 `MIXIN_KEY_ENC_TAB`（64 项）重排，**取前 32 位**得 `mixin_key`。
3. 参数加 `wts`（秒）→ 按 key 升序 → 值过滤 `!'()*` → URL 编码（**百分号大写、空格 `%20`**）→ 拼 `mixin_key` → MD5 = `w_rid`。

```python
MIXIN_KEY_ENC_TAB = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,
    33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,
    61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,
    36,20,34,44,52,
]
```

### 7.2 单元测试向量（官方给出，**必须逐字通过**）

```python
def test_mixin_key():
    assert get_mixin_key("7cd084941338484aae1ad9425b84077c"
                         "4932caff0ff746eab6f01bf08b70ac45") \
           == "ea1db124af3c7062474693fa704f4ff8"

def test_enc_wbi():
    # 固定 wts=1702204169
    assert enc_wbi({"foo":"114","bar":"514","zab":1919810}, IMG, SUB, wts=1702204169) \
        == "bar=514&foo=114&wts=1702204169&zab=1919810&w_rid=8f6f2b5b3d485fe1886cec6a0be8c5d4"
```

编码细节测试（易错点，单独用例）：

```python
def test_encode_uri_component():
    assert encode_uri_component("one one four") == "one%20one%20four"   # 不是 '+'
    assert encode_uri_component("五一四") == "%E4%BA%94%E4%B8%80%E5%9B%9B"  # 大写
```

### 7.3 使用范围（**实测驱动**）

- WBI 目前确认用于 `api.bilibili.com` 域接口。
- `getDanmuInfo` 属 `api.live.bilibili.com`，**WBI 是否生效未经实测**（U1）→ 因此设计为「`-352` 时的**可选**重试手段」，而非主路径。
- 密钥获取本身走匿名 `nav`（V9 已验证可行），所以 WBI 在未登录时也能用。

---

## 8. 协议层（`protocol.py`）

### 8.1 数据结构

```python
HEADER = struct.Struct(">IHHII")   # 总长, 头长, protover, operation, sequence
HEADER_LEN = 16

class Operation(IntEnum):
    HEARTBEAT = 2; HEARTBEAT_REPLY = 3; MESSAGE = 5; AUTH = 7; AUTH_REPLY = 8

class ProtoVer(IntEnum):
    PLAIN = 0; HEARTBEAT = 1; ZLIB = 2; BROTLI = 3

@dataclass(frozen=True, slots=True)
class Packet:
    protover: int
    operation: int
    sequence: int
    payload: bytes
```

### 8.2 函数签名与不变量

```python
def pack(operation: int, payload: bytes, protover: int = ProtoVer.PLAIN,
         sequence: int = 1) -> bytes:
    """打包。不变量: 总长 == 16 + len(payload)。"""

def unpack(data: bytes) -> list[Packet]:
    """拆包并递归解压。抛出 ProtocolError，绝不静默截断。"""
```

### 8.3 拆包伪代码（含实测校验点）

```python
def unpack(data):
    packets, offset, total = [], 0, len(data)
    while offset + 16 <= total:
        pkt_len, hdr_len, protover, operation, seq = HEADER.unpack_from(data, offset)
        if hdr_len < 16:              raise ProtocolError(f"非法头长 {hdr_len}")
        if pkt_len < hdr_len:         raise ProtocolError(f"包长小于头长 {pkt_len}")
        if offset + pkt_len > total:   raise ProtocolError(f"包长越界 {pkt_len}")
        body = data[offset + hdr_len : offset + pkt_len]
        if protover == ZLIB:
            packets += unpack(zlib.decompress(body))      # 实测路径
        elif protover == BROTLI:
            packets += unpack(brotli.decompress(body))    # 未装 brotli 则抛带提示的错
        else:
            packets.append(Packet(protover, operation, seq, body))
        offset += pkt_len
    if offset != total:               raise ProtocolError(f"尾部残留 {total-offset} 字节")
    return packets
```

**实测校验点**：
- 认证回应：`总长=26, 头长=16, protover=0, op=8`，body = `{"code":0}`。
- 心跳回应：`op=3, protover=1`，`body[:4]` 大端 uint32 = 人气值，`body[4:]` 是心跳原文回显（实测为 `b"[object Object]"`）。
- `protover=3` 解压后得到的是**一串带 16B 头的 protover=0 包**，因此递归拆包是**必需**而非优化。

### 8.4 异常类型

```python
class ProtocolError(Exception)          # 结构非法
class CompressionUnavailable(Exception) # 缺 brotli 且收到 protover=3
```

未知 `protover`（如 4）：记 WARN + 跳过该包 + `stats.unknown_protover += 1`，**不断连接**。

---

## 9. WS 客户端（`client.py`）

### 9.1 状态机

```
IDLE → CONNECTING → AUTH_SENDING → AUTHED → (STREAMING ⇄ HEARTBEATING)
                                        ↓
                                     RECONNECTING ──(退避)──→ CONNECTING
```

| 状态 | 进入动作 | 退出条件 |
|------|---------|---------|
| `CONNECTING` | 选节点、`websockets.connect(uri, origin=..., max_size=None, open_timeout=10)` | 成功 / 失败→退避 |
| `AUTH_SENDING` | 发 `op=7`（5s 内必须发出） | 收到 `op=8` |
| `AUTHED` | 记录 `authed_at`，启动心跳任务与读循环 | 连接断开 |
| `STREAMING` | 逐包解析 → 事件 → bus | 异常 / 取消 |
| `HEARTBEATING` | 每 30s 发 `op=2`，负载 `b"[object Object]"` | `heartbeat_timeout` 无回应→重连 |
| `RECONNECTING` | 退避 `min(2**n, 60) + jitter(0..1s)`；**重新 acquire_token** | 成功 |

### 9.2 认证包（实测格式）

```python
auth = {
    "uid": uid,                 # 登录态为 DedeUserID，否则 0
    "roomid": real_room_id,
    "protover": cfg.client.protover,   # 3；无 brotli 时自动用 2
    "platform": "web",
    "type": 2,
    "key": token,
    "buvid": buvid3,            # 有则带，提升稳定性
}
ws.send(pack(Operation.AUTH, json.dumps(auth, separators=(",", ":")).encode("utf-8")))
```

- `protover` 声明降级规则：启动时探测 `import brotli` 是否成功；失败则 `protover=2` 并日志说明。
- 收到 `op=8` 且 `code != 0`：计入 `auth_fail_count`，刷新 buvid → 重取 token → 重试；**连续 5 次失败**则该房间进入 `PAUSED`，面板显示原因，退避到 300s 再试。

### 9.3 超时参数

| 参数 | 值 | 依据 |
|------|----|------|
| `open_timeout` | 10s | 实测 4 个节点内可连上 |
| `recv_timeout` | 5s | 低热度房间消息稀疏（实测 25s 仅 11 条），不能按"无消息=掉线"判死 |
| 假死判定 | 90s 无 `op=3` | 心跳 30s × 3 次 |
| 心跳间隔 | 30s | 服务端 60s 无心跳强制断开 |

### 9.4 背压

读循环 → `asyncio.Queue(maxsize=queue_size)` → sink 消费者。队列满时**丢最旧**并 `stats.dropped += 1`（丢最旧而非阻塞读循环，避免心跳被阻塞导致被服务端断开）。

---

## 10. 解析层（`parser.py`）

### 10.1 归一化路由

```python
def parse(msg: dict, room: RoomInfo, recv_ts: float) -> Event | None:
    raw_cmd = str(msg.get("cmd") or "")
    base = raw_cmd.split(":", 1)[0]        # 归一：DANMU_MSG:4:0:... → DANMU_MSG
    handler = HANDLERS.get(base)
    if handler is None:
        return Event(type="other", cmd=raw_cmd, ...)   # 兜底：不丢消息
    return handler(msg, raw_cmd, room, recv_ts)
```

### 10.2 `DANMU_MSG` 字段映射（**基于实测样本锁定**）

实测样本：`tests/fixtures/danmu_msg_samples.json`

```
info[0]  = [0, 1, 25, 16777215, 1789707654925, -1164395626, 0, "d750f8e6", 0,0,0,"",0,"{}","{}", {…15…}, {…16…}, 0]
info[1]  = "集市run博物馆了"
info[2]  = [0, "逸***", 0, 0, 0, 10000, 1, ""]
info[3]  = [15, "六式", "六式zy", 804456, 12478086, "", 0, 12478086,12478086,12478086, 0, 1, 8431064]
info[4]  = [11, 0, 6406234, ">50000", 0]
info[7]  = 0
info[9]  = {"ct": "33C0E5DC", "ts": 1789707654}
info[15] = 390
info[16] = [27]
```

**映射表**（`首选` = 结构化对象路径，`兜底` = 位置下标）：

| 归一化字段 | 首选路径 | 实测值 | 兜底路径 | 置信度 |
|-----------|---------|--------|---------|--------|
| `text` | `info[0][15].extra` → `json.loads` → `content` | `"集市run博物馆了"` | `info[1]` | **高**（两者一致） |
| `msg_id` | `info[0][15].extra.id_str` | `"5fd7e66727d03c9b40851feb116aacc58384"` | `info[0][9].ct` | **高**（全局唯一，用于去重） |
| `uid` | `info[0][15].user.uid` | `0`（打码） | `info[2][0]` | 中（**可能为 0**） |
| `uname` | `info[0][15].user.base.name` | `"逸***"`（打码） | `info[2][1]` | 中（**可能打码**） |
| `server_ts` | `info[9].ts` | `1789707654` | `info[0][4] // 1000` | **高**（两处一致） |
| `is_admin` | — | — | `info[2][2]` | 中 |
| `user_level` | `info[0][15].user.wealth.level` | `27` | `info[16][0]` | **高**（两处一致=27） |
| `medal.level` | `...user.medal.level` | `15` | `info[3][0]` | **高**（一致） |
| `medal.name` | `...user.medal.name` | `"六式"` | `info[3][1]` | **高**（一致） |
| `medal.anchor_uname` | — | — | `info[3][2]` | 中 |
| `medal.anchor_roomid` | — | — | `info[3][3]` | 中 |
| `medal.anchor_uid` | `...user.medal.ruid` | `8431064` | `info[3][12]` | **高**（一致） |
| `medal.is_light` | `...user.medal.is_light` | `1` | `info[3][11]` | **高**（一致） |
| `guard_level` | `...user.guard?.guard_level or 0` | `0`（`guard=null`） | `info[7]` | 中 |
| `color` | `info[0][15].extra.color` | `16777215` | `info[0][3]` | **高**（一致） |
| `font_size` | `...extra.font_size` | `25` | `info[0][2]` | **高**（一致） |
| `mode` | `...extra.mode` | `0` | `info[0][0]` | **高**（一致） |
| `emoticon` | `...extra.emoticon_unique` | `""` | — | 中 |

**实现铁律**：
1. `info[0][15].extra` 是**字符串**，必须 `json.loads`；解析失败不得抛异常，退回位置下标。
2. 所有取值走 `safe_get()`（返回 `None` 而非抛 `IndexError/KeyError`），取不到写 `extra._missing` 便于统计字段缺失率。
3. `info[3]` 在无粉丝牌时形态不稳定（可能是 `[]`、`[0,'','',0,0,0,'',0]` 或含 `null`），**必须双形态兼容**。
4. 位置下标只在首选路径缺失时启用，并在 `extra._source` 标注实际来源，便于 M1 验证登录态是否改善字段质量。

### 10.3 其余 cmd 映射

| 归一化 type | cmd（含 V2 变体） | 取值方式 | 状态 |
|------------|------------------|---------|------|
| `danmaku` | `DANMU_MSG` | §10.2 | ✅ 已验证 |
| `interact` | **`INTERACT_WORD_V2`**、`INTERACT_WORD` | V2：`data.pb` → protobuf（见 §10.5）；旧版：`data.msg_type` / `uname` / `uid` / `fans_medal` | ✅ 已实现并回归 |
| `like` | `LIKE_INFO_V3_CLICK`、`LIKE_INFO_V3_UPDATE` | `data.uname` / `like_text` / `click_count` | ✅ 实测存在 |
| `room_stats` | `WATCHED_CHANGE`、`ONLINE_RANK_COUNT`、`ONLINE_RANK_V3`(pb)、`ROOM_REAL_TIME_MESSAGE_UPDATE` | `data.num` / `text_large` / `text_small` / `count` / `online_count` / `fans`；V3 走 pb_strings | ✅ 已实现并回归 |
| `other`（全局广播） | `STOP_LIVE_ROOM_LIST` | 携带数百个房间号，只记 `room_count` + `global_broadcast` | ✅ 已实现并回归 |
| `dm_interaction` | `DM_INTERACTION` | 待抓样本 | ⚠️ 新 cmd（实测），映射待定 |
| `notice` | `LOG_IN_NOTICE`、`NOTICE_MSG`、`SYS_MSG` | 待抓样本 | ⚠️ 新 cmd（实测） |
| `super_chat` | `SUPER_CHAT_MESSAGE`、`SUPER_CHAT_MESSAGE_JPN` | `data.message` / `price` / `uid` / `user_info.uname` / `time` | ⚠️ 需 U3 抓样 |
| `gift` | `SEND_GIFT`、`COMBO_SEND` | **实测 snake_case**：`gift_name` / `combo_total_coin` / `total_num` / `gift_id` / `coin_type`；用户信息在 `sender_uinfo`；粉丝牌在 `medal_info` | ✅ 已实现并金样本回归 |
| `guard_buy` | `GUARD_BUY`、`USER_TOAST_MSG` | `data.guard_level` / `num` / `price` | ⚠️ 需 U3 抓样（本房间暂未出现上舰） |
| `live_status` | `LIVE`、`PREPARING`、`STOP_LIVE_ROOM` | `cmd` 本身即语义 | 待实测 |
| `other` | 其余全部 | 原样透传 | ✅ |

> **M1 强制动作**：对 U3 的每个 cmd 用 `probe --dump-raw` 抓真实样本后，把上表 ⚠️ 行补成与 §10.2 同等详细度，并补金样本测试。

### 10.4 去重键

```python
def dedupe_key(ev) -> str:
    if ev.msg_id:                                  # 实测最优
        return f"{ev.type}|{ev.room_id}|{ev.msg_id}"
    if ev.server_ts is not None:
        return f"{ev.type}|{ev.uid}|{ev.server_ts}|{_sha1(ev.text or '')}"
    return f"{ev.type}|{ev.uid}|{_sha1(ev.text or '')}"
```

- 内存 LRU + TTL 120s；SQLite 端用 `UNIQUE(dedupe_key)` 持久去重。
- 用于「实时流」与「历史补种」的交叉去重（实测 `id_str` 唯一，跨来源也可靠）。

### 10.5 `INTERACT_WORD_V2` 的 protobuf 结构（实测还原）

V2 不再提供 `uid` / `uname` / `msg_type` 等 JSON 字段，而是把全部数据塞进
`data.pb`（base64 编码的 protobuf）。因此新增无依赖模块
`src/bili_danmaku/protobuf_lite.py` 做通用 wire-format 解析。

实测还原出的字段（房间 804456）：

| 字段号 | 类型 | 实测值 | 推断含义 | 置信度 |
|-------|------|--------|---------|--------|
| 2 | string | `宅***` | 昵称（未登录被打码） | 高（与 22.2.1 一致） |
| 6 | varint | `804456` | 房间号 | **高**（与 `room_init` 一致） |
| 7 | varint | `1789710200` | 时间戳（秒） | **高**（与 8、15 同源） |
| 8 | varint | `1789710200932` | 时间戳（毫秒） | 高 |
| 15 | varint | `1789710199765947760` | 时间戳（纳秒） | 中 |
| 22.2.1 | string | `宅***` | 昵称 | 高 |
| 22.2.2 | string | `https://i0.hdslb.com/bfs/face/….jpg` | 头像 URL | 高 |
| 23.1 | string | `https://i0.hdslb.com/bfs/live/….png` | 互动图标 | 中 |
| 23.2 | string | `TA常看你的直播，但还没有关注` | 互动文案（**权威语义来源**） | 高 |
| 23.3 | varint | `1` / `3` | 互动类型 | 高（与 23.2 交叉验证） |

`23.3` 的实测映射：`1` = 进入直播间（文案"还没有关注"）、`3` = 关注（文案"近期关注了你"）。
**注意这与旧版 JSON 的 `msg_type`（1 进场 / 2 关注）不一致**，所以解析器优先采用
服务端下发的 `23.2` 文案作为标签，枚举只作补充。

已知局限（诚实记录）：

- protobuf 里**没有 uid**，所以 `interact` 事件的 `uid` 为空；
- 嵌套判定是启发式的（见 `protobuf_lite._looks_like_message`）：负载若恰好是纯可打印 ASCII，
  就保守地保留为字节串而不递归 —— 宁可少解一层，也不要把 URL/昵称误当子消息；
- 字段号含义由实抓样本推断，未经官方 schema 确认，协议变更时需重跑 `probe --dump-raw`。

---

## 11. 过滤层（`filters.py`）

### 11.0 采集范围（**v1 范围收敛，需求方明确要求**）

只采集**有用户文字内容**的事件，含正文内嵌的表情占位符：

| 采集 | 不采集 |
|------|--------|
| `danmaku` —— 有文字的弹幕（含 `[dog]` 这类占位符） | `interact` / `guard_buy` / `gift` / `like` |
| `super_chat` —— SC 正文 | `room_stats` / `notice` / `dm_interaction` / `other` |
| | 任何 `text` 为空白且无表情的弹幕 |

实现落在 `FilterSpec.content_only`（默认 `True`）：

- 判定函数是 `Event.is_content_event()` = 类型 ∈ `models.CONTENT_TYPES`
  **且** `Event.has_content()`（`text` 非空白，或 `extra.emoticon_unique`/`emots` 非空）；
- **被排除的事件仍会被接收并计入 `stats.filtered`**，不是静默丢弃 ——
  这样"漏了多少"是可观测的；
- 关闭方式：`--all-events` / `config.toml` 的 `general.content_only = false`。

> **表情占位符的实测形态**（房间 804456，225 条弹幕中 28 条含表情）：
> - 普通表情：占位符**已内嵌在 text 里**（`哈哈[dog]`），
>   `info[0][15].extra.emots` 给出 `[dog]` → 图片 URL/尺寸/emoticon_id 的映射；
> - 主播自定义表情：`emoticon_unique = room_804456_37238`，此时 **text 就是表情名**（`大拇哥`）。
>
> 因此"text 非空"已能覆盖绝大多数情况；`emots` 被完整保留，供 V2 词云把占位符还原成图片。
> 兜底保留 `emoticon_unique`/`emots` 判定，以防将来出现"纯表情、text 为空"的形态。

> **礼物与上舰不纳入采集范围（需求方决策，DEC-1，2026-09）**：
> 它们带金额但没有用户文字，按"无文字即排除"处理，**本项目不做礼物/上舰的入库与统计**。
> `content_only=false`（`--all-events`）只用于协议调试，不是"恢复礼物统计"的入口。
> `--min-price` 对金瓜子的换算逻辑仍然保留 —— 它同时服务 SC，且该实现与类型无关。

```python
@dataclass(frozen=True)
class FilterSpec:
    content_only: bool = True                    # 只保留有文字内容的弹幕与 SC
    types: frozenset[str] | None = None          # None = 全部
    keywords: tuple[KeywordMatcher, ...] = ()    # 任一命中即通过(AND 语义可配)
    include_uids: frozenset[int] | None = None
    exclude_uids: frozenset[int] = frozenset()
    min_price: int | None = None
    min_medal_level: int | None = None
    exclude_masked: bool = False                 # 过滤 uid=0/打码昵称

def match(ev: Event, spec: FilterSpec) -> bool
```

| 规则 | 语义 | 备注 |
|------|------|------|
| `content_only`（默认开） | 类型 ∈ {danmaku, super_chat} 且 text 非空白或有表情 | 见 §11.0 |
| `--keyword k`（可重复） | **OR**（任一命中） | 大小写不敏感；`re:` 前缀走正则（预编译） |
| `--keyword-mode and` | 改为 AND | 处理误报 |
| `--uid` / `--exclude-uid` | 白名单优先，其次黑名单 | `uid=0`（打码）永不命中白名单 |
| `--min-price` | 仅对 `gift`/`super_chat`/`guard_buy` 生效 | 其他类型 price=None 视为不通过；**与 `content_only` 叠加时实际主要作用于 SC** |
| `--exclude-masked` | 丢弃 `uid==0` 或昵称含 `***` | 应对实测 R5 打码问题 |

性能：`match()` 为纯函数、无 IO；关键词用预编译正则避免逐条扫描。
`build_filter()` **总是返回 FilterSpec**（因为 `content_only` 默认开启，
"无其它条件"不等于"不过滤"），调用方用 `spec.active` 判断是否有条件生效。

---

## 12. Sink 层与存储

### 12.1 接口

```python
class Sink(Protocol):
    name: str
    def open(self, meta: SessionMeta) -> None: ...
    def write(self, events: Sequence[Event]) -> None:   # 批量
    def flush(self) -> None: ...
    def close(self) -> None: ...
    @property
    def stats(self) -> SinkStats: ...   # written / dropped / failed / bytes
```

- `bus.Bus` 把事件扇出到各 sink 的**独立有界队列 + 独立消费任务**：任一 sink 慢/挂不拖累其他 sink。
- 单 sink 连续写失败 3 次 → 标记 `failed`，打印明确错误（含路径/磁盘原因），其余 sink 继续；退出前汇总。
- `SIGINT` 流程：停采集 → `flush()` → 写 `session.ended_at` → 打印统计 → 退出码 0。

### 12.2 JSONL

- 文件名 `{room_id}_{YYYYmmdd-HH}.jsonl`，`ensure_ascii=False`，每行一事件。
- `flush` 每 `flush_interval`(1s) 或 `batch_size`(200) 条触发；不做 `fsync`（性能），崩溃最多丢 1s。
- `rotate_hours=1` 或单文件 128MB 轮转；轮转时写一行 `{"__meta__":"rotate", ...}` 便于拼接。

### 12.3 CSV / TXT

- `{room_id}_{YYYYmmdd-HH}.txt`：`[HH:MM:SS] 昵称: 内容`（仅 `danmaku`）。
- `{room_id}_{YYYYmmdd-HH}.csv`：`utf-8-sig`，表头 `ts,type,uid,uname,text,price,guard_level,medal_level,msg_id`。
- 注意 CSV 注入：`text` 以 `= + - @` 开头时前置 `'`（Excel 公式注入防护）。

### 12.4 SQLite

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS session (
  id TEXT PRIMARY KEY, room_id INTEGER NOT NULL, short_id INTEGER,
  anchor_uid INTEGER, token_source TEXT,
  started_at REAL NOT NULL, ended_at REAL, note TEXT
);

CREATE TABLE IF NOT EXISTS event (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL REFERENCES session(id),
  room_id     INTEGER NOT NULL,
  recv_ts     REAL NOT NULL,
  server_ts   INTEGER,
  latency_ms  INTEGER,
  type        TEXT NOT NULL,
  cmd         TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'live',   -- live | history
  msg_id      TEXT,
  uid         INTEGER,
  uname       TEXT,
  text        TEXT,
  price       INTEGER,
  guard_level INTEGER,
  user_level  INTEGER,
  medal_name  TEXT,
  medal_level INTEGER,
  field_source TEXT,                          -- structured | index（便于验证登录态改善）
  dedupe_key  TEXT,
  raw_json    TEXT,                           -- keep_raw=true 时写入
  extra_json  TEXT,                           -- schema v2：结构化补充（目前是 extra.emots 表情映射）
  UNIQUE(dedupe_key) ON CONFLICT IGNORE
);

CREATE INDEX IF NOT EXISTS idx_event_room_ts ON event(room_id, recv_ts);
CREATE INDEX IF NOT EXISTS idx_event_type_ts ON event(type, recv_ts);
CREATE INDEX IF NOT EXISTS idx_event_uid     ON event(uid);
CREATE INDEX IF NOT EXISTS idx_event_msgid   ON event(msg_id);
```

- 批量事务（200 条 / 1s），结束时 `PRAGMA wal_checkpoint(TRUNCATE)`。
- ~~FTS5 作为**可选增强**~~ → **M2 实现时推翻，改为统一 `LIKE` 子串匹配**（原因见下）。
- `raw_json` 在 `keep_raw=true`（默认）时写入；估算体积：单条弹幕 raw ≈ 1.2–2KB，10 条/秒 × 6h ≈ 26–43 万条 ≈ 300–800MB。README 需给出该量级提示。

#### schema 版本与迁移（v2）

| 版本 | 变更 | 说明 |
|------|------|------|
| v1 | 初版 | `raw_json` 为唯一的结构化补充 |
| **v2** | 新增 `extra_json` | 存 `Event.extra`（目前是 `extra.emots`：`[dog]` → 图片元数据）。**与 `raw_json` 独立** —— `--no-raw` 时 raw 不落盘，但词云仍需要表情图片链接 |

**加列必须走 `MIGRATIONS`**（`ALTER TABLE event ADD COLUMN ...`）：
`CREATE TABLE IF NOT EXISTS` 对**已存在**的表什么都不做，只改 `SCHEMA` 的老库会缺列。
`SqliteSink` 在 `open()` 里比对 `PRAGMA table_info` 后补齐。

**只读层不会迁移**（`DanmakuStore` 用 `mode=ro` 打开）：所以查询必须自适应 ——
`has_column()` 判断 + `NULL AS extra_json` 兜底。否则老库上跑 V2 词云会直接
`no such column: extra_json`（这就是 D18 的由来）。

> **设计偏离（M2 实现结论）**：原计划用 FTS5 做关键词检索，实现时发现它对中文不可用：
> - `unicode61` 分词器把连续中文当成**单个 token** —— 搜「主播」匹配不到「主播好」；
> - `trigram` 分词器虽支持子串，但要求查询**≥3 字符**，而中文弹幕关键词通常只有 2 个字
>   （「哈哈」「主播」「草」）。
>
> 因此改为 `LIKE '%kw%'` 子串匹配。对本项目量级（万~十万行）扫描成本可接受且语义正确；
> 已转义 `%` / `_` 通配符，避免用户输入被当成通配。决策记录在 `store.py` 模块 docstring。

### 12.5 导出与重放

- `export --session <id> --format csv|jsonl|txt [--where ...]` 直接 SQL 导出（流式游标，不全量载入内存）。
- `replay <file.jsonl> --to csv` 完全离线，不联网，用于把旧数据换格式/喂给 V2 词云。

---

## 13. Web 面板

### 13.1 REST 契约

| 方法 | 路径 | 请求 | 响应 |
|------|------|------|------|
| GET | `/` | — | `index.html` |
| GET | `/api/health` | — | `{status, rooms, uptime_s, version}` |
| GET | `/api/rooms` | — | `[{room_id, short_id, anchor_uid, anchor_name, anchor_face, live_status, conn_state, token_source, msgs, dropped, last_msg_at}]` |
| GET | `/api/room-info` | `room`（房间号，支持短号） | `{room_id, short_id, anchor_uid, anchor_name, anchor_face, live_status, title, monitored}`；房间不存在 → 400 |
| POST | `/api/rooms` | `{room: 804456}` | `{room_id}` / `409` 已存在 / `422` 无效 |
| DELETE | `/api/rooms/{room_id}` | — | `204` |
| GET | `/api/events` | `room_id,type,q,since,until,limit<=500,offset` | `{total, items:[Event]}`（**完整 Event，含 raw**） |
| GET | `/api/history` | `room_id,session_id,type,q,limit<=1000` + 游标 `before_ts`+`before_id` 或 `after_ts`+`after_id`；`window_s` | 见 §13.6（**瘦投影**，升序） |
| GET | `/api/density` | `room_id,session_id,type,since,until,bucket_s<=86400` | 见 §13.6 |
| GET | `/api/sessions` | `room_id,limit<=500` | `{items:[{id,room_id,started_at,ended_at,events}]}` |
| GET | `/api/analysis` | `room_id,top<=100,tokenizer=auto\|jieba\|fallback` | 见 §13.8（**本地统计，无外发**） |
| GET | `/api/annotations` | `room_id,since,until,limit<=2000` | 见 §13.7；库不存在 → 空列表（**不是**错误） |
| POST | `/api/annotations` | `{room_id, recv_ts, label?, note?, session_id?, anchor_id?}` | `201 {item}` / 400 标签超长 / 422 字段非法 |
| DELETE | `/api/annotations/{id}` | — | `{removed}` / 404 |
| GET | `/api/annotations/export` | `bookmark_id`（必填）+ `to_bookmark_id?` + `format` + 筛选 | 流式导出该区间，带 `X-Range-Since`/`X-Range-Until`/`X-Total-Count` |
| GET | `/api/stats` | `room_id?,window=60` | 见 §13.2 |
| GET | `/api/export` | `format,room_id,type,q,since,until` | 流式 `text/csv`/`ndjson`/`text/plain` |
| WS | `/ws/live` | 查询参数 `room_id,types,q` | 见 §13.3 |

### 13.2 `/api/stats` 指标定义

| 指标 | 定义 |
|------|------|
| `msgs_total` | 累计入库事件数 |
| `msgs_per_min` | 滑动 60s 窗口速率 |
| `by_type` | 各 type 计数 |
| `latency_p50/p95_ms` | `recv_ts - server_ts` 的分位数 |
| `top_senders` | 60s 内发言 TOP 10（`--anonymize` 时显示哈希） |
| `dropped` / `sink_failed` | 背压丢弃与 sink 失败计数 |
| `conn_state` / `token_source` | 连接状态与当前 token 来源（getConf / getDanmuInfo） |
| `field_quality` | `field_source` 结构化命中率（**用于验证登录态是否改善字段**） |

### 13.3 WS 推送协议

```jsonc
// 服务端 → 客户端
{"t":"hello","rooms":[...],"server_ts":...}
{"t":"event","data":{ /* Event */ }}
{"t":"stats","data":{ /* 每 2s 一次 */ }}
{"t":"state","data":{"room_id":804456,"conn_state":"RECONNECTING","reason":"..."}}
{"t":"error","data":{"code":"SLOW_CONSUMER"}}
```

- 客户端可发 `{"t":"ping"}`，服务端回 `{"t":"pong"}`（保活）。
- **慢客户端剔除**：单连接待发队列 > 2000 或积压超 30s → 发 `SLOW_CONSUMER` 并断开，避免拖累采集。

### 13.4 端口策略（按需求：允许更换）

```python
def bind_port(host, port, span=20):
    for p in ([port] if port else []) + list(range(port+1, port+1+span)) + [0]:
        try: return _try_bind(host, p)      # SO_REUSEADDR，返回实际端口
        except OSError: continue
    raise RuntimeError("无可用端口")
```

- 8787 被占用 → 依次试 8788…8807 → 仍失败则 `port=0` 由系统分配。
- **实际 URL 一定会打印**：`面板已启动: http://127.0.0.1:8788/  (8787 被占用)`。
- `--port 0` 显式要求自动分配。
- 注意：本机 3080 端口是 DSH Web GUI 占用，**不要占它**；若用户指定 3080 则拒绝并提示。

### 13.5 前端（Vite + Vue 3 组件化）

组件划分与行为约定见 [`web/README.md`](web/README.md)。要点：

| 主题 | 决策 |
|------|------|
| 类型过滤 | **自绘下拉 + 原生 checkbox 列表**（`TypeSelector.vue`，V2-d 改）。上一版用 `<select multiple size="1">`，收起态看不到已选了什么、也无法显示每类型的颜色与条数；换成自绘后收起态显示"已选摘要 + 色块"，展开态每行 = 复选框 + 色块 + 中文名 + 英文 type + 当前条数。原生控件白送的键盘可达性与"点外部收起"由 `<button>` + `<input type=checkbox>` + `document.pointerdown`/`Escape` 补回 |
| 关键词 | 两种模式：`highlight`（默认，只高亮）与 `filter`（只显示命中）。默认只高亮是为了避免"搜不到就以为没数据"；过滤态在上下文条显式标注命中/总数 |
| 高亮渲染 | 分段渲染，**绝不 `v-html`**（弹幕正文是不可信输入）；表情图片只接受 `http(s)` 地址 |
| 已运行时间 | 顶栏右侧，值来自 `/api/health.uptime_s`；呼吸灯用 scoped `@keyframes` + `prefers-reduced-motion` 降级 |
| 统计口径 | 默认 **`window=0` = 自面板启动累计**（数据源是 `hub` 的内存计数）；可切最近时段（走滑动窗口 + SQLite 补延迟分位/发言榜）。侧栏标题带口径角标，避免两种数字混读 |
| 类型分布 | **横向柱状图**（`TypeBars.vue`）：一行一类型，条长按**占比**（`count/总数`）而非相对最大值，**条数写在横条右侧**，每类型一色（`format.typeColor()`，与弹幕行高亮同源） |
| 直播间信息 | 右侧栏顶部 `RoomPanel.vue` 显示「**主播昵称**的直播间」，昵称高亮。数据来自 `/api/rooms[].anchor_name`（已监控房间，零额外请求）或 `/api/room-info`（只输入未监控的房间号） |
| 房间号记忆 | 用户输入/添加过的房间号存 `localStorage['bili-danmaku.room-history']`（上限 12 条，解析成真实房间号后替换短号），右侧栏用**原生 `<datalist>`** 给建议 + 可点的小胶囊填充 |
| 停止面板 | 两步确认 → `POST /api/admin/shutdown` → 轮询 `/api/health` 直到不可达；**不用 `window.close()`**（浏览器不允许脚本关闭非脚本打开的标签页） |

| 区域 | 内容 |
|------|------|
| 顶栏（`TopBar.vue`） | 品牌/版本、连接状态、**类型多选**、关键词（高亮/过滤）、SC 阈值 ‖ 已接收、已运行（呼吸灯）、导出 CSV、停止面板 |
| 上下文条（`FilterBar.vue`） | 统计口径（从启动累计 / 最近时段 + 起点）、关键词过滤状态 |
| 主区（`StreamList.vue`） | 弹幕流（保留最近 2000 条，只渲染最后 400 条），自动滚动 / 暂停 + "N 条新消息" 提示 |
| 右栏（`RoomPanel.vue` + `StatsPanel.vue`） | 上：直播间信息 + 房间号输入/添加 + 记忆胶囊 + 监控中列表；下：速率/延迟、类型分布柱状图、发言 TOP |
| 已删除 | 右栏「服务」栏目（版本/落盘格式/脱敏/前端是否构建）—— 属部署期信息，改由 `/api/health` 与 `bili-danmaku web status` 承载 |

> **排版原则（V2-d 定稿）**：顶栏只放"跨面板的过滤与操作"，**房间相关的输入/添加/切换全部归右栏**（房间本来就是右栏的主体）；右栏上半是"在看谁/加房间"，下半是"看到了什么统计"，中间用分隔线切开。
| 渲染 | SC 与上舰用色块；同用户连发折叠；`uid=0`/打码昵称加灰色标记（实测 R5 常见） |

### 13.6 历史浏览 / 复盘器（V3-a）

**要解决的问题**：数据一直是**全**的（JSONL + SQLite），但面板只缓冲 2000 条、只渲染 400 条，
一旦滚出缓冲，用户就只能回到 CLI。复盘器就是把这条通路接上。

#### 双层数据流（`web/src/composables/useHistory.js`）

```
/ws/live ──► useLiveStream（环形缓冲 2000；进入历史浏览时 setLimit(300)）
                 │
                 └─► merged：按 id 去重、按 (recv_ts, id) 归并
                        ▲
/api/history（游标）──► 历史窗口（HISTORY_CAP 1200，按 (recv_ts,id) 升序）
```

三条硬约束（违反任何一条都会出现"看起来能用但会丢数据"的缺陷）：

1. **不靠扩缓冲解决**。"把 2000 提到 50000"既崩内存又崩渲染，而且翻不到更早的会话。
2. **游标必须是 `(recv_ts, id)` 复合键**，且 `ORDER BY` 与游标键**同键**。
   实测房间 804456 有 **393 组重复时间戳**（最多 10 条同刻，同一批推送被解析成同一 `recv_ts`）；
   只比 `recv_ts` 会在页边界漏条，`ORDER BY id` 配 `recv_ts` 游标同样漏条。
3. **历史响应必须瘦投影**。完整 `Event` 单条约 2 KB（含 `raw_json`），
   400 条一页接近 800 KB，会把面板自己变成慢客户端（`hub.py` 会主动断开）。
   实测瘦投影 257 B/条。

#### 查询计划实测（20 万行合成库 / 真实 1 万行库）

| 写法 | 计划 | 耗时 |
|------|------|------|
| 复合游标 + `room_id`（**本实现**） | `SEARCH ... COVERING INDEX idx_event_room_ts (room_id=?)`，无临时排序 | 首屏 400 条 **18 ms** |
| `ORDER BY id DESC` 游标 | 索引 + `USE TEMP B-TREE FOR ORDER BY` | 12.3 ms |
| **无** `room_id` 过滤 | `SCAN` + 临时 B 树 | 39.8 ms/页 ⇒ 必须用兜底时间窗收窄 |
| 密度 `GROUP BY CAST(recv_ts/b AS INT), type` | 索引 + 临时 B 树 | 118 ms |
| 密度 `GROUP BY recv_ts, type`（**本实现**） | 覆盖索引（靠新增的 `idx_event_room_ts_type`） | **38 ms** |

> 密度那条值得记：**表达式分组用不上索引顺序**，所以把"折算桶"这一步挪到 Python
> （行数 = 不同时间戳数，远小于事件数），SQL 只做 `GROUP BY recv_ts, type`。
> 配套索引 `idx_event_room_ts_type` 放在 `SCHEMA` 里（`CREATE INDEX IF NOT EXISTS`
> 每次 `open()` 都会补，老库自动升级，**不需要**写 `MIGRATIONS`）。

#### 两个"状态机语义"缺陷（前端，都有用例守着）

| 缺陷 | 症状 | 正确做法 |
|------|------|----------|
| `has_newer` 照抄服务端 | 服务端的 `has_newer` 只说明"这一页之后还有没有数据"；向上翻几页后窗口尾部已不含最新数据 ⇒ 它变 `false` ⇒ **再也翻不回最新** | 语义应为"窗口是否已离开实时边缘"（`detached`） |
| `has_older` 不设终点 | 窗口被 `HISTORY_CAP` 截断后 `has_older` 失真 ⇒ 按钮永远可点、无限请求 | 用"拿到过空页"（`exhausted`）确认真的翻到头 |

这两条是"人眼点几下发现不了"的类型，`web/test/history.test.mjs` 各有一条用例。

#### 未启用 sqlite 时一律 409

三个端点都走 `_open_store_or_409()`：**先判配置里有没有启用 sqlite sink，再判库能不能打开**。
否则一个"上次运行遗留的 `danmaku.db`"会让历史接口返回陈旧数据，而用户以为在看这次的采集
（`/api/export` 早就是这个口径）。

#### 遗留

~~标注 / 切片~~ —— **已在 V3-a2 实现**，见 §13.7。

---

### 13.7 标注与切片（V3-a2）

**要解决的问题**：V3-a 让历史可浏览了，但"我看到这段有意思"之后无处落笔 ——
重新找回去只能靠时间轴肉眼估。

#### 为什么必须是**独立库**

`annotations.py` 的 `AnnotationStore` 读写 `{out_dir}/annotations.db`
（可配 `sinks.annotations_path`），**采集库一张表都不加**。三条理由：

1. **职责单一**：`danmaku.db` 是采集产物（四个 sink 写），标注是**人**写的，
   生命周期不同 —— 采集库会被归档/清理，标注不该跟着走；
2. **schema 主权**：`store.py` 是**只读**的（`mode=ro`），采集库的迁移只由 `SqliteSink` 掌管。
   把用户可写的表塞进同一份迁移历史，任一边改动都可能弄坏另一边；
3. **可删**：用户想清空标注，删一个文件即可，完全不必碰采集数据。

真机实测：标记操作后 `danmaku.db` 的表仍是 `event/meta/session/sqlite_sequence`，
标注库只有 `bookmark` —— 隔离是**可验证**的，不是口头承诺。

#### 表结构（照 ROADMAP §6.2）

```sql
CREATE TABLE IF NOT EXISTS bookmark (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- 要 AUTOINCREMENT：删除后不能复用 id
    room_id INTEGER NOT NULL,
    session_id TEXT,          -- 可空：允许跨会话标注
    anchor_id INTEGER,        -- 指向 event.id；**允许为空**（纯时间点标注）
    recv_ts REAL NOT NULL,    -- 排序与区间导出的唯一依据
    label TEXT,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookmark_room_ts ON bookmark(room_id, recv_ts);
```

两个刻意的决定：

* **`anchor_id` 不校验存在性**。校验会把"标注"耦合到"采集库"上，
  而标注只靠 `recv_ts` 就能用；被钉的弹幕清理/归档之后标注依然有效。
  前端用 ★ 表示"钉在某条"。
* **只读模式不建文件**。面板首屏就会拉一次标记列表，
  此时用户可能一条都没打过 —— 那样不该在工作区凭空留下一个空库。
  `AnnotationStore(readonly=True)` 在文件不存在时给 `available=False`，
  列表返回空数组而不是 409/404。

#### 区间导出的语义

`GET /api/annotations/export?bookmark_id=N`：

| 情况 | 行为 |
|------|------|
| 只给 `bookmark_id` | 起点 = 该标记；**终点 = 下一个标记**（没有则到数据末尾） |
| 同时给 `to_bookmark_id` | 两个标记的时间戳排序后**自动正过来**（给反了不该导出空结果） |
| `bookmark_id` 不存在 | 404（**在开始流式输出之前**判定） |
| 未启用 sqlite sink | 409（同上） |

响应带 `X-Range-Since` / `X-Range-Until` / `X-Total-Count`，便于前端/脚本核对范围，
也让"导出的到底是哪一段"可断言（测试就是靠它们）。

#### 流式导出的一个坑（本轮踩到）

`_stream_export()` 收的是**工厂**（`Callable[[], Iterator[Event]]`）而不是现成迭代器。
原因：Starlette 用线程池**惰性**驱动同步生成器 —— 如果调用方在协程里先开好连接、
函数一返回就 `close()`，生成器真正开始时数据库已经关了
（实测 `sqlite3.ProgrammingError: Cannot operate on a closed database`）。
所以连接的开与关都必须发生在生成器**内部**（这也是原实现把开关写在 `generate()` 里的原因）。

#### 前端

| 落点 | 说明 |
|------|------|
| `BookmarkBar.vue` | 放在**时间轴下方**（与 `DensityBar` 同一套时间语义）：打标记 / 跳到标记 / 选为起点 / 导出区间 / 删除 / 清空 |
| `StreamList.vue` 的 ★ | 每行一个，默认很淡（400 行都有它，太抢眼会压过正文），悬停或已标记时才亮 |
| `App.vue` 的 `playheadTs` | "可标记的此刻" = **可见的最后一条弹幕的时间**，不是墙钟 |

`playheadTs` 这个选择值得记：如果按墙钟取，用户在看历史时会把标记打到"现在"，
而屏幕上显示的是几小时前 —— **这种错误从界面上完全无法察觉**。
没有弹幕时按钮禁用并说明原因（而不是默默标一个错的时间点）。

---

### 13.8 本地感知分析（V3-b 3A）

**目标**：把弹幕流变成"看得懂的几行字"。**边界**：只做本地统计，
**不做任何外发**（LLM 属于 ROADMAP §3.2 3B，需过 G4 门槛）。

#### 模块划分与职责

| 位置 | 职责 | 约束 |
|------|------|------|
| `analysis.py` | 纯函数：词频 / 热点突增 / 刷屏 / 账号画像 | **无 IO、无网络、无全局状态** ⇒ 可完全离线单测 |
| `hub.LiveWindow` | 增量维护有界窗口（时间片词频 + 弹幕原文） | `add()` **O(1）且有界**，因为它挂在 `publish()` 上 |
| `app.py` `/api/analysis` | 组合 + 裁剪载荷 | 词频走"片合并"省算路径，不再逐条分词 |

**为什么把"跨片比较"类指标放在请求时算**：热点突增与账号画像需要全局排序/跨片比较，
不是可加的；而它们是一天几次的计算。把不可加的统计塞进热路径是这类功能最常见的错误。

#### 有界性（面板要开一整天）

| 容器 | 上限 | 淘汰策略 |
|------|------|----------|
| 弹幕原文 `deque` | 1200 条/房间 | 最旧淘汰 |
| 时间片 | 20 片（`bucket_s=60`） | 最旧淘汰；**空档过大时整体重置**（避免拿旧数据当基线造出假突增） |
| 每片词表 | 2000 词 | 淘汰"当前最少"的词（近似，但热词不受影响） |

#### 性能实测（决定它能不能留在 `publish()` 里）

| 项 | 数字 |
|----|------|
| `LiveWindow.add()` | **47 µs/条**（jieba 已装；`model_dump` 对照 4.6 µs） |
| 实测热门房间速率（5 条/秒） | ~**0.02% 单核** |
| 极端 100 条/秒 | ~**0.5% 单核** |
| `analyze()`（1200 条窗口） | **6.3 ms**（前端 6 秒一次） |

#### 两个"把正常人报成疑似异常账号"的误报（都实测踩到）

历史补种（`gethistory`）会让时间戳退化，`count / (末条 - 首条)` 于是给出荒谬速率：

| 退化形态 | 实测现象 |
|----------|----------|
| 一批弹幕**同一个** `recv_ts` | `span=0` ⇒ "600 条/分钟" |
| 时间戳**各不相同但全挤在 1 秒内** | 10 条 / 0.9 秒 ⇒ 仍算出"600 条/分钟"，并命中"集中发送" |

处理方式：`_rate_window()` 要求观测跨度 ≥ `MIN_RATE_SPAN_S`（30 秒），
否则返回 `rate_available=False` 并**放弃速率类结论** ——
不是"补一个下限把数字算出来"。重复率那一项与时间无关，仍然参与打分。
修好后同一离线房间的 `suspicious` 从 1 条变为 0 条。

> ⚠️ 这条经验通用：**做启发式评分时，"数据不支持这个结论"必须能表达出来**。
> 硬算出来的数字会被用户当成事实，而误报的代价（误伤真人）远高于漏报。

#### 第三个是纯公式错误

重复率原写 `1 - 去重数/总条数` ⇒ "10 条完全相同"算出 90%（永远够不到满值）。
改为 `1 - (去重数-1)/总条数`：全不同时为 0、全相同时才为 1。

#### 前端措辞（合规要求）

`AnalysisPanel.vue` 一律用"疑似/仅供参考"，**必须显示 `reasons`**（只给分数用户无法复核），
底部固定一行"本地启发式统计，仅供参考、非判定；数据不出本机" —— 那是隐私承诺，不要删。
点词复用顶栏的关键词过滤（不另造筛选状态），避免"两个过滤条件互相打架"。

---

## 14. CLI

```
bili-danmaku <command> [options]

login        [--manual]                     扫码登录并保存凭据
check-auth                                  校验凭据有效性
probe        <room> [--dump-raw DIR] [--seconds 30] [--cookie-file P]
grab         <room>... [options]            抓取并落盘
web          [--rooms 804456] [--port 8787] [--open] [--also-grab]
replay       <file.jsonl> --to csv [--out DIR]
export       --session <id> --format csv [--out P]
stats        [--session <id>] [--room <id>]
```

`grab` 参数：`--format jsonl,csv,txt,sqlite` / `--out DIR` / `--types` / `--keyword`(可重复) / `--keyword-mode or|and` / `--uid` / `--exclude-uid` / `--exclude-masked` / `--min-price` / `--min-medal-level` / `--duration 1h` / `--until 23:00` / `--raw|--no-raw`（默认 raw） / `--anonymize` / `-v`

### 退出码

| 码 | 含义 |
|----|------|
| 0 | 正常 |
| 1 | 未分类错误 |
| 2 | 参数/配置非法 |
| 3 | 房间不存在或接口拒绝 |
| 4 | 登录超时/凭据无效 |
| 5 | 全部节点连接失败（网络问题） |
| 6 | 所有 sink 均失败 |

---

## 15. 端到端数据流

```
cli.grab(room=804456)
  └─ Config.load() ─→ validate()
      └─ credentials.load()          # SESSDATA + buvid（掩码日志）
          └─ api.room_init(804456)   # → real_room=804456, uid=8431064, live_status=1
              └─ SessionMeta 创建 → bus.open() → sinks.open()
                  └─ RoomCollector.run()  [asyncio.Task]
                      ├─ api.acquire_token()          # getDanmuInfo(登录) → 失败→ getConf
                      ├─ client.connect(hosts, token) # wss://…:443/sub
                      │   ├─ send op=7 auth           # 实测 → op=8 {"code":0}
                      │   ├─ heartbeat task (30s)     # 实测 → op=3 人气值
                      │   └─ recv loop
                      │       └─ protocol.unpack → json.loads
                      │           └─ parser.parse → Event
                      │               └─ filters.match
                      │                   └─ bus.publish(ev)
                      │                       ├─ JsonlSink.write   (batch 200/1s)
                      │                       ├─ CsvTxtSink.write
                      │                       ├─ SqliteSink.write  (事务 + UNIQUE 去重)
                      │                       └─ WebHub.broadcast (WS 客户端)
                      └─ 断线 → RECONNECTING → 重新 acquire_token → connect
  SIGINT → stop → flush → session.ended_at → print stats
```

---

## 16. 测试计划

### 16.1 测试矩阵

| 文件 | 覆盖 | 关键用例 |
|------|------|---------|
| `test_protocol.py` | 打包/拆包/解压 | 往返一致；zlib 嵌套（**实测结构**）；`protover=3` 无 brotli 时报 `CompressionUnavailable`；截断包/超长包/非法头长抛 `ProtocolError` 不崩；尾部残留检测 |
| `test_parser.py` | 归一化 | **金样本** `fixtures/danmu_msg_samples.json` 逐字段断言；`info[3]` 空/短/含 null 三种形态；`extra` 非法 JSON 时退回下标；未知 cmd → `other`；`INTERACT_WORD_V2` 归一为 `interact` |
| `test_wbi.py` | 签名 | §7.2 三个官方向量（mixin_key / w_rid / 编码）**逐字断言** |
| `test_filters.py` | 过滤 | OR/AND 关键词、正则、UID 白黑名单、`min_price` 类型限定、`exclude_masked` |
| `test_sinks.py` | 落盘 | 三格式写读一致；轮转；去重幂等；SQLite `UNIQUE` 冲突忽略；FTS 可用性探测；CSV 注入防护 |
| `test_client_reconnect.py` | 连接 | 假服务器注入：认证失败、中途断线、心跳无回应 → 验证退避、重取 token、5 次失败进 PAUSED |
| `test_server_api.py` | 面板 | `httpx.ASGITransport` 测 REST；WS 客户端测推送；慢客户端剔除；端口占用时返回实际端口 |
| `test_credentials.py` | 凭据 | 优先级链；`mask()` 不泄露；QR 状态码 86101/86090/86038/0 分支 |
| `test_history.py` | 历史浏览（V3-a） | **游标不重不漏**（含并列时间戳）；瘦投影宽度；无房间过滤的时间窗；密度聚合；会话过滤 |
| `test_history_api.py` | 历史端点（V3-a） | 契约形状；与 `/api/events` 的**宽度分工**；半截游标；未启用 sqlite → 409；历史端点**不广播 WS** |
| `test_annotations.py` | 标注（V3-a2） | 独立库**隔离**（采集库不多表）；只读模式**不建文件**；增删清空；区间导出语义（下一个标记/反向纠正/BOM/范围头）；**标注不要求 sqlite sink** |
| `test_analysis.py` | 感知分析（V3-b 3A） | 词频计数/人数/超长裁剪；热点**用篇均速率**（持平不报、基线 0 给"新词"）；短句跟风**不报刷屏**；账号画像三维打分 + 理由；**两个时间戳退化误报的回归**；`LiveWindow` 有界性/容错/空档重置 |

**前端**（V3-a 起，`web/test/`，用 Node 内置 `node:test`，零新依赖）：

| 文件 | 覆盖 | 关键用例 |
|------|------|---------|
| `web/test/history.test.mjs` | 复盘器状态机 + 标记 | 进入历史收缩缓冲；向上翻到顶不重不漏；**能翻回最新**（`detached`）；**翻到头后按钮变灰**（`exhausted`）；合并视图去重归并；窗口截断；会话/房间切换；游标成对；请求失败不清窗口；标记增删清空/时间升序/选中态/`anchor_id` |

### 16.2 夹具

| 夹具 | 来源 | 状态 |
|------|------|------|
| `fixtures/danmu_msg_samples.json` | 真实抓取（房间 804456） | ✅ 已入库 |
| `fixtures/packets/op8_auth_reply.bin` | 真实 `op=8` 原始字节 | M0 补充 |
| `fixtures/packets/op3_heartbeat.bin` | 真实 `op=3` 原始字节 | M0 补充 |
| `fixtures/packets/op5_zlib_notify.bin` | 真实 zlib 通知包 | M0 补充 |
| `fixtures/gift_sc_samples.json` | U3 抓样 | M1 补充 |

### 16.3 假服务器（`fake_server.py`）

基于 `websockets.serve` 的脚本化回放：按 `AUTH → AUTH_REPLY(0) → HEARTBEAT_REPLY → N 条通知` 顺序推送，并支持注入：
- `drop_after=n`：第 n 条后断开（测重连）
- `auth_fail=n`：前 n 次返回非 0（测刷新重试与 PAUSED）
- `silent_heartbeat=True`：不回心跳（测 90s 假死判定，用假时钟加速）

### 16.4 覆盖率与实况测试

- 核心模块（protocol / parser / filters / wbi / sinks）行覆盖 **≥ 85%**。
- `@pytest.mark.live` 标记的实况测试**默认 skip**，`pytest -m live` 才联网（目标房间 804456）。

---

## 17. 可观测性

### 17.1 日志

- 格式：`{ts} {level} {room=804456} {component} {msg} {kv...}`
- 级别：`INFO` 关键状态迁移；`DEBUG` 每包 cmd 与原始 JSON；`WARNING` 降级/丢包/风控；`ERROR` sink 失败/全部节点失败。
- **强制**：`-vv` 才输出 raw；`cookie/sessdata/buvid` 一律走 `mask()`。

### 17.2 运行期指标（同 §13.2）

`msgs_total / msgs_per_min / by_type / latency_p50/p95 / dropped / unknown_protover / unknown_cmd / field_quality / auth_fail_count / token_source`

`unknown_cmd` 与 `field_quality` 是**协议漂移的早期预警指标**——若 `unknown_cmd` 升高或 `field_quality` 下降，说明 B 站改了协议，需重跑 `probe --dump-raw`。

---

## 18. 实施顺序与逐步验收

### M0 骨架 + 协议自检（**部分已完成**）

| 步骤 | 产出 | 验收命令 | 状态 |
|------|------|---------|------|
| 0.1 | 依赖预检 | `python tools/preflight.py 804456` | ✅ 已跑通 |
| 0.2 | 握手实测 + 样本 | `python tools/handshake_check.py 804456 25` | ✅ 已跑通 |
| 0.3 | venv + 装依赖 + `pyproject.toml` | `pip install -e ".[dev]"` | ✅ 完成（本机 pip 网络不可用，改走 `tools/wheel_fetch.py` 离线方案） |
| 0.4 | `protocol.py` + `api.py` + `config.py` + `models.py` | `pytest tests/test_protocol.py` | ✅ 完成 |
| 0.5 | `credentials.py`（含 `login` / `check-auth`） | `bili-danmaku check-auth` 返回 `isLogin=true` | ✅ 已由需求方扫码验证 |
| 0.6 | `bili-danmaku probe 804456 --dump-raw tests/fixtures/packets` | 打印房间信息 + `code=0` + 前 10 条 + 落夹具 | ✅ 完成 |
| 0.7 | 补 U1/U2：登录态下 `getDanmuInfo` 与 UID 打码对比 | probe 报告 `token_source` 与 `field_source` | ✅ 完成（U1/U2/U6 均解决） |

### M1 抓取核心 ✅
`client.py` 状态机 + `parser.py`（含 U3 抓样补全）+ `sinks/jsonl.py` + `grab`
**验收**：`grab 804456 --duration 5m` 产出 JSONL；手动断网 30s 后自动续采且无重复（`msg_id` 去重生效）。
**实测**：见附二「M1 实现状态」。

### M2 完整落盘 + 过滤 ✅
`csv_txt.py` / `sqlite.py` / `filters.py` / 去重 / 轮转 / `replay` / `stats`
**验收**：三种产物条数一致；`--keyword` 生效；`replay` 离线换格式；`stats` 输出正确分位数。
**实测**：见附二「M2 实现状态」。

### M3 Web 面板 ✅
`server/app.py` + 前端 + 导出 + 端口自适应
**验收**：`web --rooms 804456` 浏览器秒级实时流、可过滤、可导出；占用 8787 时自动换端口并打印实际 URL。
**实测**：见附二「M3 完成项」。前端按需求方选择采用**组件化方案**（Vite + Vue 3），
需 `npm install && npm run build`；未构建时后端返回内建提示页，接口不受影响。

### M4 打磨
多房间、`README.md`、测试补齐、entry point、覆盖率达标
**验收**：`pytest` 全绿；`pip install -e .` 后 `bili-danmaku --help` 可用。

> 每个里程碑结束停下来交付验收，不自动推进。

---

## 19. V2（词云 + SC）

按需求把 V2 范围收敛为两项，**V1 只需保证数据够用、不写死结构**。

### 19.1 弹幕词云 —— ✅ 已实现（V2-a）

命令：`bili-danmaku wordcloud [--session|--room|--db|--source] [--since --until --limit --types]`。

| 关注点 | 落地方式 |
|--------|----------|
| 分词 | 装了 `jieba` 就用；否则退到内置**二元组分词器**（`fallback_tokens`）。后者是为"没装可选依赖也能用/能测"而存在，不是效果方案 |
| **表情占位符** | 用 `EMOT_PATTERN` 抽成**独立 token**，默认**不计入词语榜**，单独出表情榜并带上 `extra.emots` 的图片链接；`--include-emotes` 时才以 `[dog]` 原形进榜（便于和正文词区分） |
| 清洗 | 丢掉 URL、@提及、表情占位符；压缩空白。不做"哈/233"这类噪声过滤 —— 它们在弹幕里恰恰是有效信号 |
| 停用词 | 内置表**刻意很短**（只放功能词/语气词），可用 `--stopwords-file` 整体覆盖 |
| 房间黑话 | `--dict` 加载 jieba 自定义词典（一行一词） |
| 排序 | 次数降序、同频**按词形升序** —— 保证同一份数据两次跑出的榜单一致 |
| 输出 | `text`（终端榜单，零依赖）/ `json`（带元信息，便于二次加工）/ `html`（自包含单文件、零依赖、浏览器渲染，**没有中文字体也不会变方块**）/ `png`（需 `wordcloud` + 系统 CJK 字体，字体路径自动探测） |
| 数据读取 | 新增 `DanmakuStore.iter_texts()`：只 `SELECT type, text, extra_json`。**刻意不走 `iter_events()`** —— 后者要构造完整 Event（含 raw JSON 解析），对"只想数词"是几十倍的浪费 |

**需要的数据支撑（V1 补的）**：`extra_json` 列（见 §12.4 的 schema v2），
把 `extra.emots` 落盘，这样 `--no-raw` 时词云仍能拿到表情图片链接。

### 19.2 SC（Super Chat）—— ✅ 已实现（V2-b）

命令：`bili-danmaku sc [ROOM] [--room|--session|--db] [--since|--until-time|--limit|--top] [--min-price] [--price-unit] [--json]`。

| 关注点 | 落地方式 |
|--------|----------|
| 汇总 | `cli.run_sc()`：条数、金额合计/最高/平均、贡献榜（**按 uid 聚合**，不是昵称）、最近明细；纯函数式返回 dict，便于单测 |
| 只统计 SC | `DanmakuStore.iter_events(types=["super_chat"])`。礼物/上舰的 `price` 是金瓜子，语义完全不同，绝不能混进 SC 报表 |
| 金额门槛 | `--min-price` 的输入单位是**元**；SC 直接比、礼物/上舰 ×1000（见 `filters.min_price_threshold`） |
| 单位可覆盖 | `--price-unit yuan\|milli`：毫元模式 ÷1000 后再比较与展示；输出保留 `raw_price` 便于追溯 |
| 实时告警 | `sc <房间号> --follow --min-price N`：复用 `RoomCollector` + `build_filter(types=["super_chat"])`，配一个只打印不落盘的 `AlertSink`；`--bell` 可响铃 |
| 面板告警 | `web/src/components/ScBanner.vue`：达阈值的 SC 在流上方弹横幅（队列上限 3、丢最旧、可关闭），阈值在 `FilterBar` 里配置并存 localStorage |

⚠️ **未验证假设**：SC 的 `price` 单位按「元」处理。为此专门蹲了 3 个高热度房间共 4 分钟，
**一条 SC 都没抓到**，所以该单位是按协议档位（30/50/100/500/1000/2000）+ 礼物金瓜子语义
推断的，**不是实测结论**。若实测发现差 1000 倍，用 `--price-unit milli` 覆盖并回来更新本节。

**仍缺真实样本**：`SUPER_CHAT_MESSAGE` / `SEND_GIFT` / `GUARD_BUY`（现只有 `COMBO_SEND`）。

**V1/V2 明确不做的事**：不做聚合表、不做通知渠道、不为词云引入额外存储。

---

## 19.3 面板排版与直播间信息（V2-d）—— ✅ 已实现

需求输入：删掉右栏「服务」栏目、把房间号输入搬到右栏顶部并记忆历史、右栏顶部显示「XXX 的直播间」、
类型分布改横向彩色柱状图、类型下拉提高可读性、重排顶栏/左栏/右栏。

### 主播昵称从哪来（**实测结论，勿凭记忆改写**）

| 接口 | 匿名可用性（2026-01 实测） | 用途 |
|------|---------------------------|------|
| `GET api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room?roomid=<真实房间号>` | ✅ **可用**，返回 `data.info.{uid,uname,face}` | **本项目采用**：`BiliApi.anchor_info()` |
| `GET api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id=<真实房间号>` | ❌ **-352 风控** | **不要用**（与 `getDanmuInfo` 同样的坑） |
| `GET api.live.bilibili.com/room/v1/Room/room_init?id=<房间号或短号>` | ✅ 可用，返回 `room_id/short_id/uid/live_status` | 房间号归一化（原有路径） |

* `BiliApi.room_info(room, with_anchor=True)` = `room_init` + `anchor_info` 合并成 `RoomInfo`
  （模型新增可选字段 `anchor_name` / `anchor_face`）。
* `anchor_info()` **失败返回 `None` 而不是抛异常**：昵称只是展示信息，
  取不到不该让「添加房间」这条主链路失败（回归用例 `test_add_room_survives_anchor_lookup_failure`）。
* `PanelService._room_cache` 在**进程内永久缓存** `room_id → RoomInfo`：两次网络接口换一个几乎不变的昵称，
  重复查只是在给风控送请求。缓存按 `room_id` 与 `short_id` 双键命中。
* `PanelService.lookup_room()` 刻意与「添加监控」解耦 —— 只查信息、**不启动采集**（`monitored` 字段告诉前端当前状态）。

### 前端文件变动

| 文件 | 变化 |
|------|------|
| `components/TopBar.vue` | 🆕 由 `RoomBar.vue` 更名而来：**去掉房间下拉与房间号输入**，只留跨面板的过滤/操作；右侧按"只读信息 ‖ 动作"加分隔线 |
| `components/TypeSelector.vue` | 🆕 自绘类型多选（摘要 + 色块 + 复选框面板 + 每类型条数） |
| `components/TypeBars.vue` | 🆕 类型分布横向柱状图（占比为条长、条数在右、每类型一色） |
| `components/RoomPanel.vue` | 🆕 右栏顶部：直播间信息 + 房间号输入/添加 + datalist 记忆 + 监控中列表 |
| `components/StatsPanel.vue` | 删「服务」栏目与房间列表；改用 `TypeBars` |
| `App.vue` | 右栏改为 `RoomPanel` + `StatsPanel`；新增 `roomInfo`/`roomHistory`/`typeCounts`，房间历史存 localStorage |
| `format.js` | 新增 `typeColor()`（每类型一色，弹幕行/柱状图/选择器共用一处） |
| `style.css` | `.app` 网格行数改为 4（顶栏/上下文条/SC 横幅/主体）；`.body` 右列 320→340；新增 `.side-stats` 分隔线 |

**注意**：`ScBanner` 的横幅是 `.app` 网格的第 3 行，它为空时该行高度为 0；
改 `.app` 的行定义时必须同步（否则主体区域会被挤掉或错位）。

**未来的前端结构**：`App.vue` 现在是"单层"数据流（`/ws/live` → 环形缓冲 → 过滤 → 渲染）。
「历史分页 + 时间轴」会把数据流改成**实时层 + 历史层**两层（含 `event.id` 游标与瘦投影），
设计与约束见 [`ROADMAP.md`](ROADMAP.md) §2.1 与 §6.1。**动这条线之前先读那份文档**，
否则很容易走成"把 2000 条缓冲调大"这种错误解法。

---

## 20. 风险登记（技术级）

> 本表只登记**现有实现**的技术风险。**未来功能的**技术/隐私/产品风险
> （历史分页、分析模块、外发适配器等）登记在 [`ROADMAP.md`](ROADMAP.md) §7，
> 避免两份文档各写一半。

| # | 风险 | 概率 | 影响 | 对策 | 触发信号 |
|---|------|------|------|------|---------|
| T1 | `getDanmuInfo` 长期 -352，登录态也无法用 | 中 | 低 | **已实测 `getConf` 匿名可用**，主路径不受影响；`getConf` 也失效则整体不可用（需重新逆向） | probe 报告 `token_source=getConf` 持续 |
| T2 | `getConf` 被下线（历史接口） | 低 | **高** | 双路径互备；保留 `--token` / `--ws-host` 手工注入逃生舱 | `getConf` 返回非 0 |
| T3 | 字段位置漂移（如 `info[0][15]` 消失） | 中 | 中 | 结构化优先 + 下标兜底 + `field_source` 监控 + `raw` 保留可重解析 | `field_quality` 下降 |
| T4 | 新 cmd 不断出现（已实测 2 个） | **高** | 低 | `other` 兜底不丢消息；`unknown_cmd` 计数告警 | `unknown_cmd` 升高 |
| T5 | UID/昵称打码导致数据价值下降 | **已发生** | 中 | **根因已定位为未登录（§1.5）**；登录态应恢复昵称；`--exclude-masked` 兜底；`field_source` 监控 | 样本 `uid=0` / `name=***` |
| T6 | 风控升级（412 / 频繁 -352） | 中 | 中 | 最小请求间隔 1s、单房间单连接、**-352 不重试**、buvid 持久化 | 412 / 拒绝连接 |
| T7 | pip 不可达 | 低 | 中 | §2.2 降级方案（websocket-client + zlib + SSE），M0–M2 不受阻 | `pip install` 失败 |
| T8 | 高频房间背压丢消息 | 中 | 中 | 有界队列 + `dropped` 指标 + SQLite 批量事务；必要时 `--no-raw` 降 IO | `dropped > 0` |
| T9 | `raw` 默认开启导致磁盘膨胀 | 中 | 低 | 按小时轮转；README 给体积估算；`--no-raw` 一键关闭 | 磁盘占用 |
| T10 | 凭据泄露 | 低 | **高** | §5.1 全链路掩码 + 扫码优先 + `.gitignore` + 不采集上传 | 日志出现明文 |
| T11 | `get_anchor_in_room` 被下线/风控 | 低 | **低** | 昵称只是展示信息：`anchor_info()` 失败即返回 `None`，面板退化成「房间 <id>」，采集链路完全不受影响（`getInfoByRoom` 实测已 -352，故不采用） | `/api/room-info` 的 `anchor_name` 恒为 `null` |

---

## 21. 待你确认

1. **凭据提供方式**：请**不要**把 SESSDATA 贴到对话里。推荐 M0 阶段我实现 `bili-danmaku login`（扫码），由你本人在终端完成扫码。若你更想手工配置，我就实现 `login --manual` + `.env` 读取。**请选一种**。
2. **是否需要我处理 U3**（礼物/SC/上舰字段）：房间 804456 实测热度低（25s 仅 11 条），大概率抓不到礼物和 SC。若你希望 M1 就补齐这些映射，请提供一个**有礼物/SC 活动的房间号**，或同意先用 `other` 兜底、后续再补。
3. **`--exclude-masked` 默认值**：实测 UID 与昵称存在打码（`uid=0` / `逸***`）。默认**保留**（含打码数据）还是默认**排除**？我建议默认保留，只在分析时按需排除。
4. **面板前端风格**：单文件原生 JS（我推荐，改起来快）还是你想要更现代一点的组件化方案（需引入构建链，工期 +0.5 天）？

确认后我从 **M0.3（venv + 依赖 + protocol/api/config/models）** 开始动手。

---

## 附：文档变更记录

| 版本 | 变更 |
|------|------|
| V1.0 | 首版。基于对房间 804456 的真实只读实测（`preflight.py` / `handshake_check.py`）修正了 5 项原假设（R1–R5），锁定 `DANMU_MSG` 字段映射，明确 token 双路径与降级策略。 |
| V1.1 | M0 实现完成后回写实测结论：新增 V11–V14 与 §1.5（**打码根因是未登录**）；新增 R6 与 §10.5（`INTERACT_WORD_V2` 的 protobuf 结构）；`protover=3`（brotli）路径实测通过；节点去重实测为 4 个唯一节点。 |

---

## 附二：实现状态

### M0 完成项

| 交付项 | 文件 | 状态 |
|--------|------|------|
| 协议层（封包/拆包/zlib+brotli） | `protocol.py` | ✅ 含真实字节夹具回归 |
| 通用 protobuf 解析 | `protobuf_lite.py` | ✅ 14 项单测 |
| HTTP 接口 + 限流 + token 双路径降级 | `api.py` | ✅ |
| 凭据与扫码登录 / 掩码 / 清除 | `credentials.py` | ✅ 已由需求方扫码验证通过 |
| WBI 签名 | `wbi.py` | ✅ 官方测试向量逐字通过；**已确认为登录态必需组件** |
| 解析层（DANMU_MSG + data 型 + protobuf） | `parser.py` | ✅ 金样本回归 |
| CLI（`login`/`check-auth`/`purge`/`probe`） | `cli.py` | ✅ `probe 804456` 端到端通过 |
| 配置系统与语义校验 | `config.py` | ✅ |

### M1 完成项

| 交付项 | 文件 | 状态 |
|--------|------|------|
| 连接状态机 + 自动重连 + 心跳 | `collector.py`、`client.py` | ✅ 13 项集成测试（假服务端驱动） |
| 有界队列 / 故障隔离的多 sink 扇出 | `sinks/bus.py` | ✅ 含「单 sink 连续失败被停用、其余照常」测试 |
| JSONL 落盘（按时间/体积轮转、续写） | `sinks/jsonl.py` | ✅ |
| 去重（有界 LRU + TTL，优先 `id_str`） | `dedupe.py` | ✅ |
| `grab` 命令（时长/截止/脱敏/历史补种/Ctrl+C 收尾） | `cli.py` | ✅ 真实房间验收通过 |
| 假服务端测试夹具 | `tests/fake_server.py` | ✅ 支持认证失败注入、中途断开、心跳静默 |

**测试规模**：164 项全绿（M1 里程碑时的数量；当前累计见下方 M2 小节）。
**真实抓取验收**：`grab 804456 --duration 90s --history` → 148 条落盘
（live 138 + history 10），`seq` 与 `msg_id` 均唯一，重复剔除 116 条，重连 0 次，退出码 0。

### 实现期间发现并修复的缺陷（记录备查）

| # | 缺陷 | 表现 | 根因 | 修复 |
|---|------|------|------|------|
| D1 | **读超时会终结消息迭代器** | 消息稀疏时持续无谓重连（实测 2.5s 内重连 26 次），服务端重放导致重复入库 | `messages()` 让 `asyncio.TimeoutError` 逃出生成器，迭代器被终结；调用方下次取值拿到 `StopAsyncIteration`，误判连接结束 | 读超时在生成器内部吞掉；仅「心跳长时间无回应」抛 `ConnectionStale` |
| D2 | **截止时间检查无处执行**（修 D1 时引入） | `grab --duration` 永不返回（死锁），第一个用例挂满 10 分钟 | 生成器只在**收到消息**时才交回控制权；外层循环的截止检查在无消息时永远等不到执行 | 把 `should_stop` 回调注入生成器**内部**，每次读超时后检查一次 |
| D3 | 服务端「接受后立即断开」会重置退避 | 形成重连风暴 | 认证成功即 `attempt = 0` | 引入 `MIN_STABLE_SECONDS`（5s）：存活不足 5 秒不重置退避 |
| D4 | `bus.stop()` 可能等待整个 `flush_interval` | 停止最长挂 60 秒 | 工作协程阻塞在 `queue.get()`，感知不到停止事件 | 单次等待上限 1 秒，空闲时多醒几次 |
| D5 | 脱敏过滤器抓不到裸凭据值 | 日志若只打印 token 本身（不带字段名）会泄露 | 仅按 `key=value` 模式匹配 | 引入「已登记密钥」表，凭据构造时登记，任何位置出现都被替换 |
| D6 | **一次成功就把房间永久降级** | 无 WBI 的请求触发 -352 后立即拉黑房间，即使用 WBI 随后成功了；该房间之后所有重连都退化到 `getConf` | 拉黑动作发生在 WBI 重试**之前**且未回滚 | 既然 V15 已证明 WBI 是必需环节，改为**直接发签名请求**；只有确实失败才拉黑（且非风控错误不拉黑）；响应不完整时降级而非抛异常 |

> D1–D3 都是**协议/并发层面**的真实缺陷，且 D1 只在「消息稀疏」时暴露 ——
> 正是房间 804456 这类低热度房间才会触发。假服务端测试把它们固定了下来。

### M2 完成项

| 交付项 | 文件 | 状态 |
|--------|------|------|
| CSV 落盘（utf-8-sig、表头、公式注入防护） | `sinks/csv_txt.py` | ✅ |
| TXT 落盘（`[HH:MM:SS] 昵称: 内容`，仅弹幕） | `sinks/csv_txt.py` | ✅ |
| SQLite 落盘（WAL、批量事务、`UNIQUE(dedupe_key)`） | `sinks/sqlite.py` | ✅ |
| 轮转/续写逻辑抽公共件 | `sinks/base.py` `RotatingFileWriter` | ✅ JSONL/CSV/TXT 共用 |
| 过滤规则（类型/关键词/uid/金额/粉丝牌/打码） | `filters.py` | ✅ |
| SQLite 只读查询层（统计/导出用） | `store.py` | ✅ |
| `stats` / `export` / `replay` 命令 | `cli.py` | ✅ |
| 产物一致性校验工具 | `tools/verify_formats.py` | ✅ |

**测试规模**：229 项全绿（全部离线，不依赖真实直播）。

**真实验收**：

- `grab 804456 --format jsonl,csv,txt,sqlite` → 114 条，**四格式条数完全一致**、msg_id 集合一致、序列号无重复；
- `grab --types danmaku,gift --exclude-masked` → 收包 88 / 入库 14 / **过滤 74**，产物中打码身份 0 条；
- `stats` → 类型分布、链路延迟 **P50 1228ms / P95 2162ms**、发言 TOP、会话列表；
- `export --keyword 主` → 5 条；`export --types gift,super_chat,guard_buy` → 1 条；
- `replay`（离线，全程不联网）→ 114 条，与原始 CSV **逐字节一致**。

### M2 期间发现并修复的缺陷

| # | 缺陷 | 表现 | 根因 | 修复 |
|---|------|------|------|------|
| D7 | **事件可能指向不存在的会话行** | 按 `--session` 统计恒为 0 | sink 写入用了 `event.session_id` 而非打开时的 `meta.session_id`，两者不一致即成孤儿数据 | 统一以 sink 的会话为准，保证引用完整性 |
| D8 | **FTS5 无法做中文子串检索** | 搜「主播」匹配不到「主播好」 | `unicode61` 把连续中文当单个 token；`trigram` 又要求查询 ≥3 字符，而中文关键词常为 2 字 | 弃用 FTS5 改用 `LIKE` 子串匹配（并转义 `%`/`_`）；已在 §6.4 记录该偏离 |
| D9 | 礼物金额恒为 `None` | 礼物金额统计全为 0 | 按文档假设取 camelCase `total_coin`，实际是 snake_case `combo_total_coin` | 同时支持两种形态，用实测样本回归固定（V19） |
| D10 | 合法零值被 `or` 穿透 | `gift_num=0` 导致数量取到 `None` | `a or b` 把 0 当成"没有值" | 引入 `_first_int` / `_first_positive`，显式保留 0 |
| D11 | 校验工具在不完整格式集上误报 | 只落盘 jsonl+sqlite 时报告"不一致" | 工具拿未落盘格式的 0 去比对 | 只比对**实际落盘**的格式 |

### M3 完成项

| 交付项 | 文件 | 状态 |
|--------|------|------|
| 事件广播中枢（订阅过滤、最近缓冲、慢客户端剔除） | `server/hub.py` | ✅ 16 项单测 |
| 多房间编排（复用 `RoomCollector`，非另一套抓取逻辑） | `server/panel.py` | ✅ |
| FastAPI 路由 + WebSocket + 端口自适应 + 前端降级页 | `server/app.py` | ✅ |
| 组件化前端（Vite + Vue 3，SFC + composable） | `web/` | ✅ 构建产物 ~80KB / gzip 31KB |
| `web` 命令（预置房间 / 端口自适应 / 自动开浏览器） | `cli.py` | ✅ |
| 服务端测试（StubPanel，全程不联网） | `tests/test_server.py` | ✅ 26 项 |

**测试规模**：255 项全绿（全部离线，不依赖真实直播）。

**真实验收**：

- `web --rooms 804456` → 前端产物被正确托管（`/` 返回构建页而非降级页，`/assets/*` 均 200）；
- `/api/health` → `rooms=1`、`frontend_built=true`；`/api/rooms` → 房间 `STREAMING`、`token=getDanmuInfo+wbi`；
- `/api/stats` → `source=sqlite`、链路延迟 P50 910ms / P95 1910ms、类型分布与发言 TOP；
- `/api/events` → 真实弹幕数据；
- **WebSocket 端到端**：`hello` → 历史回填 → 实时推送（18 秒收到 36 条），
  `types=danmaku` 只收到 danmaku（13 条），`types=gift,super_chat` 正确为空；
- 房间管理：重复添加 409、删除不存在 404。

### M3 期间发现并修复的缺陷

| # | 缺陷 | 表现 | 根因 | 修复 |
|---|------|------|------|------|
| D12 | **`Subscriber` 无法放进 set** | 任何 WS 连接一建立就 500，服务端抛 `TypeError: unhashable type` | `@dataclass` 默认生成 `__eq__` 并把 `__hash__` 置为 `None` | 改为 `@dataclass(eq=False)`（订阅者本就该按身份区分） |
| D13 | **历史回填绕过订阅过滤** | 订阅 `types=danmaku` 时，新连接仍先收到互动/点赞等其它类型 | `hub.recent()` 直接把缓冲原样发出，没走订阅者的过滤条件 | 抽出统一的 `hub.accepts(record)`，`publish` 与 `recent` 共用 |

> D12 与 D13 都只在**真正把服务跑起来**之后才会暴露：单测覆盖不到"能不能放进 set"，
> 而回填路径只有连上 WS 才会走到。这印证了里程碑验收必须做端到端，而不能只看单测。

**离线依赖方案（Python）**：本机 pip 网络栈不可用（`SSLEOFError` / 镜像挂死），
故新增 `tools/wheel_fetch.py`（httpx **约束感知**解析 + 下载），
配合 `pip --no-index --find-links` 完成安装。详见 README。

**离线依赖方案（前端）**：`registry.npmjs.org` 在 Node 侧不可达，改用 npmmirror；
npm 全局缓存位于工作区外会被文件沙箱拒绝（`EPERM`），故 `web/.npmrc` 把
`cache` 指向工作区内的 `.npm-cache`。此外 Vite 在 Windows 上会执行 `net use`
探测网络驱动器，受限沙箱会因禁止带管道的子进程而报 `spawn EPERM`，
需要放宽沙箱才能构建。

---

### M4 完成项（多房间并发 / 文档 / 覆盖率 / 打包）

| 项 | 做法 | 证据 |
|----|------|------|
| 多房间并发 | 新增 `tests/test_panel.py`：两个假服务端 + 两个房间同时采集，验证并发上限、会话隔离、JSONL 分文件、同一 SQLite 库共存、停一个房间不影响另一个 | 5 项离线用例 + 真实双房间验收（804456 + 7878，见 HANDOVER §5.1） |
| 并发上限语义 | `PanelService.add_room` 把 `max_rooms` 检查**移到 `room_init` 之前**（详见 D14） | `test_max_rooms_is_enforced_before_any_network_call` |
| 背压与故障隔离 | `Bus._flush_off_loop()`：sink 同步写改到工作线程（详见 D17）；新增背压契约用例 | `test_queue_full_keeps_newest_and_never_blocks_reader`、`test_blocking_sink_does_not_delay_heartbeat` |
| 文档收尾 | 新增 `web/README.md`（前端开发/构建/行为约定/验收清单）；README、HANDOVER 同步 M4 状态 | 文档本身 |
| 测试覆盖率 | 引入 `pytest-cov`（`dev` 可选依赖 + `tools/wheel_fetch.py` 根包清单）；`pyproject.toml` 定义度量口径与 85% 门禁 | 核心模块 **90.58%**（408 项测试全绿） |
| 打包 | 新增 `tools/build_wheel.py`：构建前把 `web/dist` 暂存到包内 `web_dist/`，构建后清理；`find_dist()` 增加包内候选 | `dist/bili_danmaku-0.1.0-py3-none-any.whl` 含前端产物；临时目录安装后 `--version`/`stats` 可用 |
| 压测 | 新增 `tools/stress.py`：吞吐、队列深度/丢弃、四产物一致性；在热门房间 7878 上跑默认与强制背压两轮 | 见下表与 HANDOVER §5.2 |

### V2-a 完成项（弹幕词云）

| 项 | 做法 | 证据 |
|----|------|------|
| 词云模块 | 新增 `text.py`（分词 / 清洗 / 词频 / 四种渲染），纯函数无 IO，顶层不 import 可选依赖 | `tests/test_text.py` 44 项；覆盖率 92% |
| 表情占位符 | `EMOT_PATTERN` 抽成独立 token，默认不进词语榜，单独成表情榜并带图片链接；`--include-emotes` 才进 | `test_emote_is_counted_separately_from_words`、`test_include_emotes_marks_them_with_brackets` |
| 分词 | 有 `jieba` 用它；无则退到内置二元组分词器（保证可选依赖缺失时功能与测试都不残） | `test_tokenize_fallback_bigram` |
| 新命令 | `bili-danmaku wordcloud`，`text/json/png/html` 四种输出 + `--source` 离线读 JSONL | `tests/test_cli.py` 的 9 项词云用例 |
| 数据支撑 | `extra_json`（schema v2）+ `DanmakuStore.iter_texts()`（只取三列，避免构造完整 Event） | `tests/test_store_extra.py` 12 项 |
| 老库兼容 | `has_column()` + 查询自适应，老库照常出词云（D18） | `test_wordcloud_works_on_legacy_db`、`test_iter_texts_works_on_legacy_db` |
| 前端联动 | 面板把 `[dog]` 渲染成图片（`textSegments()`，只接受 http(s) 地址，挡掉 `javascript:`） | `web/dist` 已重建；node 直跑 `textSegments` 验证分段与协议过滤 |

### V2-b 完成项（SC 汇总 / 告警）

| 项 | 做法 | 证据 |
|----|------|------|
| 汇总命令 | `cli.run_sc()` 纯函数式汇总 + `print_sc_report()` 终端渲染（金额整数不留 `.0`） | `tests/test_sc.py` 26 项 + `test_cli.py` 9 项 |
| 金额单位 | 统一为元：`filters.min_price_threshold` 对礼物/上舰 ×1000；`--price-unit milli` 可覆盖未验证的 SC 单位 | `test_min_price_threshold_converts_yuan_to_gold_bean`、`test_price_unit_milli_divides_by_1000` |
| 实时告警 | `sc <房间> --follow`：`RoomCollector` + `build_filter(types=["super_chat"])` + 只打印的 `AlertSink`（不落盘） | `test_sc_follow_requires_room`（参数校验即退出，不触网） |
| 面板横幅 | 新增 `web/src/components/ScBanner.vue`；`FilterBar` 增加阈值输入（localStorage 持久化） | `web/dist` 已重建（21 modules）；阈值/队列/去重逻辑见组件注释 |
| 修复 D20 | 金额单位混用（差 1000 倍） | 见缺陷表 |

### V2-c 完成项（面板常驻化 + 交互优化）

| 项 | 做法 | 证据 |
|----|------|------|
| 托管运行时 | 新增 `runtime.py`：`.runtime/web.json` 状态文件、`spawn_detached()`（`DETACHED_PROCESS` + `STARTUPINFO/SW_HIDE` + 日志重定向 + `close_fds=True`）、`pid_alive()`、`live_runtime()`（陈旧自愈） | `tests/test_runtime.py` 21 项 |
| 优雅停止 | `POST /api/admin/shutdown`（**仅回环** + Origin 校验 + 可选 token + 仅 POST）→ 回调置位 `Server.should_exit` → `lifespan` 的 `finally` 调 `panel.stop()` → 每房间 `bus.stop()` flush | 控制端点 12 项用例 + 真实端到端（pid 退出、状态清理、`ended_at` 回填） |
| CLI 生命周期 | `web` 改为命令组（`invoke_without_command=True` 保留前台用法）+ `start/stop/status/logs/restart`；`web_options` 复用选项装饰器 | `test_web_group_and_subcommands_registered` 等 |
| 面板交互 | 类型下拉、关键词双模式、已运行时间呼吸灯、统计口径切换、停止面板按钮 | `web/dist` 重建（24 modules）；构建产物里逐项核对关键字；`web/README.md` §5 手测清单 |
| 统计口径 | `WebHub` 增加累计计数（total/by_type/by_room/by_room_type）；`/api/stats?window=0` 走内存计数，`>0` 走窗口 + SQLite 补量 | `test_hub_tracks_cumulative_counts`、`test_stats_cumulative_is_default_and_scoped` |

> **环境限制（非代码问题）**：本会话的命令沙箱在每条命令结束时回收进程组，
> 因此无法在这里演示"父命令退出后后台面板继续存活"。
> 只要父进程活着（用户自己的 PowerShell），面板就会持续运行 ——
> 同一条命令内的 `start → status → HTTP → stop` 全链路已实测通过。

#### 覆盖率口径（刻意如此）

`cli.py`（581 条语句，是全项目最大模块）与 `__main__.py` **不计入覆盖率百分比**，
但由 `tests/test_cli.py` 的端到端用例覆盖（`stats` / `export` / `replay` 三个离线命令、
参数解析边界、错误退出码）。理由：把命令编排层混进百分比，会让"核心逻辑有没有被测到"
这件事变得不可读；而 CLI 的真实行为本来就该用"跑一遍看输出"来验证。

### M4 期间发现并修复的缺陷

| # | 缺陷 | 表现 | 根因 | 修复 |
|---|------|------|------|------|
| D14 | **达到并发上限时报错原因被掩盖** | 已达 `max_rooms` 时再添加一个**不存在的房间号**，报的是"房间不存在"，把真正原因（已达上限）盖掉；而且白打一次网络接口 | `add_room` 先 `room_init` 再检查上限 | 上限检查移到 `room_init` 之前 |
| D15 | **`/api/export` 实际 500（面板导出按钮全废）** | 任何格式的导出都返回 500：`sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread` | Starlette 用线程池驱动同步生成器，而 `DanmakuStore` 连接是在请求协程里开的；一次流式导出的各次 `next()` 落在不同工作线程上 | 连接改在**生成器内部**开关；`DanmakuStore` 显式 `check_same_thread=False`（只读层、串行使用） |
| D16 | **`export`/`replay` 不带 `--out` 时崩溃** | `bili-danmaku export --format csv` 报 `AttributeError: 'nullcontext' object has no attribute 'write'` —— 也就是"导出到标准输出"这条最常用的路径整个坏掉 | `_open_output` 用 `contextlib.nullcontext(sys.stdout)` 包了一层，调用方拿到的不是 `sys.stdout` | 直接返回 `sys.stdout` |
| D17 | **同步 sink 写入阻塞事件循环 → 假死重连**（压测发现） | 一个"慢 sink"（磁盘卡顿的等价物）会让心跳任务停摆，采集器判定连接假死并重连；压测里表现为"明明只是落盘慢，却整条连接重启" | `Bus` 的消费协程在**事件循环线程**里直接调用 `sink.write()`（同步 IO）；SQLite 提交/文件 fsync 一慢，整个循环连带心跳一起卡住 | 新增 `Bus._flush_off_loop()`，写入走 `asyncio.to_thread`。**刻意不加超时**：卡死的 sink 只该拖住它自己（超时后线程仍在跑，下一批再进来就可能并发用同一个 SQLite 连接） |
| D18 | **只读查询层遇到老库缺列直接崩**（V2 词云首跑暴露） | `bili-danmaku wordcloud` 在 M4 时期产生的库上报 `sqlite3.OperationalError: no such column: extra_json`，功能完全不可用 | `extra_json` 是 schema v2 新增列；`SqliteSink` 会 ALTER 迁移，但 `DanmakuStore` 是 `mode=ro` **只读**打开的，永远不会执行迁移 | `DanmakuStore.has_column()` + 查询自适应（缺列时 `NULL AS extra_json`）；`iter_events` 也改成按实际列拼接。同时补 `test_store_extra.py` 覆盖"老库 + 新代码" |
| D19 | **Web 面板导出的 CSV 在 Excel 里中文乱码**（用户实测反馈） | 从面板下载 `danmaku-*.csv` 后中文版 Excel 打开全是乱码；而文件本身是**合法 UTF-8**（Python/VS Code 打开正常），容易误判成"数据写坏了" | `/api/export` 的流式 CSV 分支只写 `utf-8`，**没有 UTF-8 BOM**；Excel 打开本地 CSV 只看文件头、忽略 `Content-Type` 的 charset，于是按系统码页（GBK）解码 | 新增 `CSV_BOM`，只在第一块 chunk（表头）前添加；补字节级回归用例 `test_export_csv_carries_utf8_bom_for_excel`，并断言 JSONL/TXT **不带** BOM |
| D20 | **`--min-price` 混用两种金额单位**（V2-b 做 SC 时发现） | 门槛写 100（元）时，`price=500` 的礼物（500 金瓜子 = **0.5 元**）会被判为通过 —— 差 1000 倍；既有测试还把错误行为固化成了断言 | `filters.FilterSpec.match()` 拿元门槛直接和所有类型的 `price` 比，但 SC 的 `price` 是元、礼物/上舰是金瓜子 | 新增 `min_price_threshold(event_type, min_price)`：SC 直接用、礼物/上舰 ×1000；`describe()` 标注单位；修正既有测试并新增 `test_min_price_threshold_converts_yuan_to_gold_bean` |

> D19 的教训值得单独记一笔：**编码缺陷在 `pytest` 里天然测不出来** ——
> 断言 `response.text` 拿到的是已经正确解码的字符串，中文当然"正常"。
> 必须断言 `response.content[:3] == b"\xef\xbb\xbf"` 这种字节级事实；
> 凡是"交给别的程序打开"的产物（CSV/Excel、PNG、PDF），验收都要覆盖
> **真实消费者怎么读它**。
> 同类风险点：同一件事在本项目里有三种写法 —— `sinks/csv_txt.py` 用 `utf-8-sig`、
> CLI `export --out` 按后缀选 `utf-8-sig`、`/api/export` 曾经漏了；
> 改动其中一处时务必对照另外两处。

> D15 与 D16 都属于"单测绿灯但功能全废"的典型：`test_server.py` 的用例配置里没有
> sqlite sink，所以导出接口只跑到内存降级分支；`export --out` 则从来没被跑过。
> 补上 SQLite 路径与 CLI 端到端测试后立刻暴露 —— 再次印证 HANDOVER §7.4。
>
> D17 由**压测**发现，而且是在补"背压"用例时才稳定复现的：一开始只在离线用例里
> 用一个 sleep 3 秒的 sink，结果采集器只收到 2 条就报假死重连 —— 队列根本没机会顶满。
> 修完后同一用例的断言才能成立（收满 12 条、零重连、丢最旧、留最新）。

### 压测结论（M4 补齐，房间 7878 / 真实房间号 23438368）

需求方要求"压测放到初版功能齐全之后"，现在用 `tools/stress.py` 补上。实测两轮：

| 场景 | 收包 | 入库 | 过滤 | 丢弃 | 重连 | 队列峰值 | 产物一致性 |
|------|------|------|------|------|------|----------|------------|
| 默认（`queue_size=20000`，90s，四格式） | 450（≈300 条/分钟） | 179 | 281 | 0 | 0 | 0 / 20000 | JSONL = CSV = TXT = SQLite = **179** |
| 强制背压（`queue_size=3`，`flush_interval=5`） | 220 | 46 | 174 | **7** | **0** | 3 / 3（顶满） | JSONL = CSV = TXT = SQLite = **46** |

结论：

* 采集链路在约 **300 条/分钟**（含 60% 非文字事件）下**零丢弃、零重连**，
  四种产物条数完全一致 —— 包括 SQLite 与文本格式；
* 队列容量远大于实际需要（峰值 0），默认配置有充足余量；
* 把容量压到 3 条时确实会丢，但**丢的是最旧的那批**、条数如实计入
  `stats.dropped` 与 `sink.dropped`，而且**不再触发重连**（D17 修复前会重连）；
* 过滤比例符合采集范围预期（约 62% 事件是进房/点赞等无文字事件）。

已知限制：本机 pip 不可用且 PyPI 无匹配的 psutil wheel，所以压测报告里
**RSS 一栏显示"不可用"而不是编造数字**；需要内存曲线时手动 `pip install psutil`。

#### 打包策略（明确取舍）

- **wheel 携带前端**：`tools/build_wheel.py` 把 `web/dist` 暂存进 `src/bili_danmaku/web_dist/`
  再构建，`find_dist()` 优先读包内目录。这样 `pip install` 之后面板开箱可用，
  不依赖工作区目录结构。
- **不把 `npm run build` 塞进 Python 构建流程**：构建 wheel 不应该要求 Node 环境；
  前端未构建时后端仍会返回内建提示页（M3 起就是这个约定）。
- **`pip install .`（不带前端）依然可用**，只是面板显示降级页 —— 这是有意的降级路径。
