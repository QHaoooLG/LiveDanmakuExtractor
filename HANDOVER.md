# 交接文档（HANDOVER）

> 面向**新开会话 / 新接手的人**。目标：读完这一份就能继续开发，不必翻聊天记录。
> 最后更新：**M0–M4 + V2（a/b/c）全部完成**（多房间并发 + 文档 + 覆盖率 + 打包 + 压测 + 词云 + SC + 面板常驻化，见 §5）。
>
> 📌 **新会话请先读 [`SESSION_HANDOVER.md`](SESSION_HANDOVER.md)** —— 那是专门为"零上下文接手"写的
> 快速通道（环境/决策/代码地图/待办分类/踩坑清单/基线重建命令）。本文档是**权威状态库**，
> 两者冲突时**以本文档为准**。
>
> 相关文档：[`README.md`](README.md)（用法）· [`TECH_DESIGN.md`](TECH_DESIGN.md)（实现级细节）· [`docs/PRIVACY.md`](docs/PRIVACY.md) · [`web/README.md`](web/README.md)（前端）

---

## 0. 一分钟上手

```powershell
# 1. 进入项目（本机开发环境）
cd F:\CodingBox\DeepseekHarness\LiveDanmakuExtractor

# 2. 依赖已装好，直接用即可（.venv 已存在）
.\.venv\Scripts\bili-danmaku.exe login          # 扫码登录（若要重新登录）
.\.venv\Scripts\bili-danmaku.exe check-auth     # 确认凭据有效

# 3. 跑测试（应全绿）
.\.venv\Scripts\python.exe -m pytest
# 3b. 覆盖率（门禁 85%，当前 90.90%，704 项；前端另有 cd web; npm test）
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing

# 4. 抓一段看看
.\.venv\Scripts\bili-danmaku.exe grab 804456 --duration 1m --out data

# 5. 起面板
.\.venv\Scripts\bili-danmaku.exe web --rooms 804456 --open
```

**当前状态**：M0–M4 **全部完成**，V2-a/b/c/d **全部完成**
（词云 / SC 汇总告警 / 面板常驻化与交互优化 / 面板排版与直播间信息 / **复盘器：历史浏览 + 标记切片**）——
**704 项离线测试全绿 / 核心模块覆盖率 90.90% / 前端 19 项逻辑用例 / wheel 打包可用 /
真实双房间并发与热门房间压测均已验收 / 词云可出终端榜单、JSON、HTML、PNG /
面板可后台常驻并优雅停止（flush 落盘）、右栏显示「主播昵称的直播间」并记忆房间号、
**历史可翻页/定位、可打标记并按区间导出（标注存独立库）**。
**没有进行中的半成品**；下一步是"待真机核实项"（SC 金额单位、面板常驻实机确认），见 §6.1。
**未来更新方向另见 [`ROADMAP.md`](ROADMAP.md)**（复盘器 / 感知分析 / 工程债三条线 + 风险说明 +
预留扩展点 + 待决策项）—— 本文只写现状，那份只写未来；两者冲突时以本文为准。

---

## 1. 这个项目做什么

抓取 B 站直播间**弹幕正文与 SC 内容**，落盘为 JSONL / CSV+TXT / SQLite，
并提供 Web 面板实时查看、过滤、导出。

**采集范围（M3 后收敛，务必先理解）**：

| 采集 | 不采集 |
|------|--------|
| `danmaku`：有文字的弹幕，**含表情占位符**（`哈哈[dog]`） | `interact` 进房/关注、`guard_buy` 上舰、`gift` 礼物 |
| `super_chat`：SC 正文 | `like` / `room_stats` / `notice` / `dm_interaction` / `other` |
| | 任何文本为空且无表情的弹幕 |

被排除的事件**仍会被接收并计入统计的「过滤」计数**（不是静默丢弃）。
实测比例：70 秒内收包 216 → 入库 57、过滤 169。

开关：`--all-events` 或 `config.toml` 里 `general.content_only = false`。

---

## 2. 架构与文件地图

```
用户侧入口（9 个 CLI 子命令）
  login  check-auth  purge          → credentials.py
  probe                             → client.py（单次会话探测）
  grab                              → collector.py + sinks/
  web                               → server/
  stats  export  replay             → store.py

数据流（grab 与 web 共用同一条）
  api.py              取房间信息 / token（getDanmuInfo+WBI → getConf 兜底）
    ↓
  client.py           WS 连接、认证、心跳、拆包
    ↓
  protocol.py         16B 头 + zlib/brotli（解压后仍是带头的包，需递归）
    ↓
  parser.py           原始 JSON → Event（含 protobuf_lite 解析 data.pb）
    ↓
  filters.py          content_only / 类型 / 关键词 / uid / 金额 / 打码
    ↓
  collector.py        去重（dedupe.py）→ bus.publish
    ↓
  sinks/bus.py        每 sink 独立有界队列 + 独立消费协程（故障隔离）
    ↓
  jsonl / csv / txt / sqlite          server/hub.py（WS 广播）
```

| 文件 | 职责 | 注意 |
|------|------|------|
| `protocol.py` | 二进制封包 | 只认字节，可离线单测 |
| `protobuf_lite.py` | 无依赖 protobuf 解析 | 解 `INTERACT_WORD_V2` 的 `data.pb` |
| `parser.py` | cmd → Event | 结构化优先、下标兜底，记 `field_source` |
| `client.py` | 单次 WS 会话 | `messages(should_stop=...)` 的语义见 §7 陷阱 |
| `collector.py` | 连接状态机 | 重连、心跳、退避、flap guard |
| `dedupe.py` | 有界 LRU + TTL | 优先 `id_str`，其次内容哈希 |
| `filters.py` | 过滤规则 | `content_only` 默认开 |
| `sinks/base.py` | `Sink` 抽象 + `RotatingFileWriter` | 三个文本 sink 共用轮转 |
| `sinks/bus.py` | 扇出总线 | 慢 sink 不拖累其他 sink |
| `store.py` | SQLite **只读**查询 | 统计/导出用，刻意不用 FTS5（见 §7）；连接 `check_same_thread=False`（见 D15） |
| `server/hub.py` | WS 广播中枢 | `accepts()` 是 publish 与 recent 的共用判定 |
| `server/panel.py` | 多房间编排 | 复用 `RoomCollector`，不是另一套抓取逻辑；上限检查在 `room_init` **之前**（见 D14）；V2-d 起 `_room_cache` + `lookup_room()` 提供主播昵称（不启动采集） |
| `server/app.py` | FastAPI | 端口自适应；前端未构建时返回降级页；`find_dist()` 优先读包内 `web_dist/`；控制端点 `POST /api/admin/shutdown` **仅回环**；V2-d 新增 `GET /api/room-info` |
| `runtime.py` | 面板托管运行时（V2-c） | 状态文件 `.runtime/web.json`、分离启动、优雅停止；**停止逻辑见模块 docstring** |
| `cli.py` | 命令入口 | `_make_filter` / `_build_config` / `_open_store` 是公共辅助 |
| `web/` | Vite + Vue 3 前端 | 构建产物 `web/dist`，未构建也不影响接口；开发细节见 `web/README.md` |
| `ROADMAP.md` | **未来方向**（不属于实现） | 只写"接下来做什么"：三条线 + 风险 + 预留扩展点；现状仍以本文为准 |
| `annotations.py` | 标注（标记/笔记）存储（V3-a2） | **独立库** `{out_dir}/annotations.db`（可配 `sinks.annotations_path`）；`store.py` 只读、这边可写，**两者刻意不合并** |
| `analysis.py` | 本地感知分析（V3-b 3A） | **纯函数**：无 IO / 无网络 / 无全局状态 ⇒ 完全离线可测。有界聚合在 `server/hub.py` 的 `LiveWindow`（`add()` 必须保持 O(1) 且有界） |

---

## 3. 关键协议事实（**实测所得，不要凭记忆改写**）

来源：对房间 `804456` 的真实抓取，全部记录在 `TECH_DESIGN.md` §1。

### 3.1 连接与认证

- `room/v1/Room/room_init` 解析真实房间号、主播 uid、开播状态。
- **`xlive/.../getDanmuInfo` 匿名一律返回 `-352` 风控；登录态下必须叠加 WBI 签名才成功**
  （实测走 `getDanmuInfo+wbi` 分支）→ 所以 `wbi.py` 是必需组件，不是备选。
- `room/v1/Danmu/getConf` **匿名可用**，是保底路径；返回的 6 个节点里有重复，必须去重。
- 认证包 `op=7`，5 秒内必须发出；`protover=3`（brotli）/`2`（zlib）都实测可用。
- 心跳 `op=2`，负载就是字面量 `[object Object]`；回应 `op=3` 前 4 字节是人气值。
- **返回节点端口是 2245**（登录态 getDanmuInfo）或 443（匿名 getConf），
  所以务必用响应里的 `wss_port`，不要硬编码。

### 3.2 封包

- 16 字节大端头：总长 / 头长(16) / protover / operation / sequence。
- **protover=2/3 解压后得到的仍是一串「带头部的包」**，必须递归拆包。
  （假服务端一度压了裸 JSON，客户端正确地报出「包长越界」——客户端是对的。）

### 3.3 消息内容

- `DANMU_MSG` 优先读 `info[0][15].user` 结构化对象；其中 `info[0][15].extra`
  是一个 **JSON 字符串**，必须二次解析。
- 去重键首选 `info[0][15].extra.id_str`（全局唯一）。
- **`INTERACT_WORD_V2` 的 `data.pb` 是 base64 编码的 protobuf**，
  旧版 JSON 字段已不存在；**该 protobuf 里根本没有 uid**（登录后也没有）。
- **礼物族（`COMBO_SEND`）字段是 snake_case**：`gift_name` / `combo_total_coin` /
  `total_num`，用户信息在嵌套的 `sender_uinfo`，粉丝牌在 `medal_info`。
- 用户信息分散在多种嵌套块：点赞用 `uinfo`、礼物用 `sender_uinfo`、SC 用 `user_info`。
- 表情占位符**已内嵌在 text**（`哈哈[dog]`），`extra.emots` 给出
  `[dog]` → `{url, width, height, emoticon_id, emoticon_unique}` 映射；
  主播自定义表情的 text 就是表情名（`大拇哥`）。
- **登录后昵称完全可见**；匿名下部分用户被打码（`宅***`）。
  `LOG_IN_NOTICE` 下发的文案就是「为保护用户隐私，未登录无法查看他人昵称」。

### 3.4 房间 804456 实测画像

- 主播 uid `8431064`，长期在播但热度低（人气值常为 1，约 10 条弹幕/分钟）。
- 消息构成以 `INTERACT_WORD_V2` 为主（但按新范围不采集），弹幕占比不高。
- 链路延迟 **P50 ≈ 0.9–1.4s，P95 ≈ 1.9–2.2s** —— 这是 B 站本身的延迟，不是程序问题。

---

## 4. 环境坑（**这一节能省你几小时**）

### 4.1 Python 依赖：pip **已经可用**（2026-10-01 修复，根因已改写）

> ⚠️ **本节此前长期写着"pip 完全不可用 / TLS 栈坏"，那是错的**。2026-10-01 已实测定位到
> 真实根因并修复。下面是**当前的事实**，旧结论已作废。

**当前状态**：`.venv` 的 pip 已升到 **26.2.1**，`pip install <包>` **直接可用**，不需要任何变通。

**真实根因（两个因素叠加，不是 TLS 栈坏）**：

1. 本机有一个**注册表级系统代理** `127.0.0.1:7890`
   （`HKCU:\...\Internet Settings` 的 `ProxyEnable=1`；`ProxyOverride` 里只排除了
   localhost / 内网 / Steam 域名，**没有** pypi.org）→ pip 会走它。
2. venv 里的 pip 是 **22.3.1**（`ensurepip` 自带，2022 年版本，vendored urllib3 1.26.12）
   → 经这个代理做 TLS/大响应时挂掉，并报出**与真实原因完全无关**的
   `SSLEOFError(8) ... problem confirming the ssl certificate`。

**已被实测证伪的两个旧猜测**（别再往这两个方向查）：

| 旧猜测 | 实测结论 |
|---|---|
| OpenSSL / TLS 栈坏 | ❌ 全局与 venv 是**同一个解释器**（`pyvenv.cfg` 的 `home = E:\Python`），直连 pypi.org 两者都是 **TLS 1.3 成功**。`ssl.OPENSSL_VERSION` = OpenSSL 1.1.1t |
| pip 自带的 CA 包过期 | ❌ 用 pip 22.3.1 那份 **2022.09.24** 的 certifi 包直连校验 pypi.org **成功**（GlobalSign 根证书在包里） |

**正确的用法**：

```powershell
# 1) 30 秒体检：先确认 pip 与代理是否健康
.\.venv\Scripts\python.exe -m pip download six --no-deps -d build\pipprobe

# 2) 正常装包（就是这一条，没有别的花活）
.\.venv\Scripts\python.exe -m pip install <包名>
```

**失败时的排查顺序**（从便宜到贵）：

1. **看 pip 版本**：`python -m pip --version`。低于 24 就先升级它（见下）。
2. **绕开那个代理**：`$env:NO_PROXY="*"`（或只列 `pypi.org,files.pythonhosted.org`）。
   - ⚠️ **`--proxy ""`、`HTTP_PROXY=""`、`HTTPS_PROXY=""` 全都无效**（实测）。
     pip 的代理来自 `getproxies()` **读注册表**，环境变量置空覆盖不掉；**只有 `NO_PROXY` 能绕过**。
   - 走代理约 **9 s**、直连约 **0.7 s**，慢 13 倍且偶发失败 —— 这就是那些随机 SSL 报错的来源。
3. **降级到离线兜底**：`tools/wheel_fetch.py`（见下）。

**修复动作（已执行，新机器重建 venv 时照做）**：

```powershell
# 新 venv 的 pip 是 22.3.1；用 NO_PROXY 让旧 pip 先能联网，再让它升级自己
$env:NO_PROXY='*'
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

**兜底工具（保留，但已从"首选"降级为"离线场景专用"）**：
`tools/wheel_fetch.py` 用 httpx **约束感知**地解析依赖并下载 wheel，
再用 `pip --no-index --find-links wheels` 离线安装。

- 它是约束感知的（读 `requires_dist` 求版本交集 + 逐版本回溯）；
  第一版没做这个，结果装了 pydantic-core 2.49.0 而 pydantic 2.13.5 要求 `==2.46.5`。
- `wheels/` 是 wheel 缓存（已 gitignore），`wheels/requirements.lock.txt` 是锁定版本。
- **`httpx` 不走注册表代理检测**，所以它在旧 pip 坏掉时依然能联网 ——
  这也是当初"httpx 正常但 pip 失败"这个迷惑现象的来源。

**复现/复核用的诊断脚本**（`tools/` 下，按排查顺序编号）：
`diag_pip_tls.py`（对比两个解释器）→ `diag_ca_bundle.py`（证伪 CA 包）→
`diag_proxy.py`（分直连/代理）→ `diag_proxy_tunnel.py`（复刻 pip 的 CONNECT 隧道）→
`diag_pip_fix2.py`（逐条对比哪种禁用代理的写法真正生效）。
（还有一个 `diag_pip_root2.py`：证明"两个 urllib3 版本表现一致"。）

- **`pip wheel` 也有个怪癖：产物已经生成，但进程不退出**（实测多次，都会被外层超时杀掉）。
  所以 `tools/build_wheel.py` 构建完会正常返回，别被 `pip wheel` 的挂起吓到 ——
  判断成功与否看 `dist/*.whl` 存不存在，而不是看 pip 的退出码。
  **注意**：如果在脚本返回前强杀它，`finally` 里的清理不会执行，
  会留下 `src/bili_danmaku/web_dist/`（该目录已 gitignore，手动删掉即可）。
- **`pip install` 装 sdist 也一样会挂住**（V2 装 jieba 实测）：包已经装好、
  `import jieba` 正常，但 pip 进程不退出。判断依据是 `importlib.util.find_spec()`，
  不是 pip 的退出码。
- **`jieba` 只有 sdist（PyPI 无 wheel）**，所以 `tools/wheel_fetch.py --packages jieba`
  会报"无满足约束且带兼容 wheel 的版本"。手动下 sdist 再用
  `pip install --no-input --no-build-isolation --no-deps wheels/jieba-0.42.1.tar.gz` 装。
  **不装也能用**：`text.py` 会退到内置二元组分词器。
- `wordcloud` 有 win_amd64 wheel，但会带进 matplotlib/numpy/pillow 等一整套；
  只有 `--format png` 需要它，`text/json/html` 三种输出零依赖。
- **matplotlib 一导入就写用户目录字体缓存**，受限沙箱报
  `Could not save font_manager cache ... Permission denied`。
  `render_png()` 里已把 `MPLCONFIGDIR` 指到工作区内的 `.matplotlib-cache/`（已 gitignore）。

### 4.1b 打包成 exe：PyInstaller **可用**（2026-10-01 实测）

> 此前文档判定"PyInstaller 做不了"，依据是 **pip 坏掉**。pip 修好后（§4.1）
> 该结论**已作废** —— PyInstaller 当前装在 `.venv` 里，版本 **6.22.3**。

```powershell
# 已装；如需在新环境重装
.\.venv\Scripts\python.exe -m pip install pyinstaller
```

**实测结论**（`tools/test_pyinstaller.py`、`tools/test_pyinstaller_gui.py`）：

| 项 | 结果 |
|----|------|
| 能否在 venv 的**转发器 stub** 下构建 | ✅ 能（PyInstaller 自己解析 base 解释器） |
| 核心链路单文件体积 | **16.6 MB**（fastapi/uvicorn/httpx/websockets/pydantic/brotli + 前端产物） |
| 构建耗时 | 约 24 s |
| 冻结后导入 | `fastapi`/`uvicorn`/`httpx`/`websockets`/`pydantic`/`brotli`/`bili_danmaku`/`server` **全 ok** |
| 单文件**冷启动** | 约 **3.3 s**（onefile 每次启动都要解压到临时目录，这是它的固有代价） |

**两个必须知道的坑**：

1. **`find_dist()` 在冻结后只有候选 1 有效**。它按序找：
   ① `<包目录>/web_dist`（= `_MEIPASS/web_dist`）② `cwd/web/dist` ③ `parents[3]/web/dist`。
   冻结后 `__file__` 在 `_MEIPASS/bili_danmaku/server/app.py`，**候选 3 会算到 `_MEIPASS` 的上一层**，
   所以**打包时必须把前端放到 `web_dist`（候选 1）**：
   `--add-data "<web/dist>;web_dist"`。放成 `web/dist` 是**找不到的**。
2. **词云重型依赖要显式排除**，否则体积暴涨：`--exclude-module matplotlib --exclude-module numpy
   --exclude-module wordcloud --exclude-module jieba`（这些只有 CLI `wordcloud --format png` 用得到）。
   托盘/面板都不需要它们。
3. **入口不能直接用 `src/bili_danmaku/cli.py`**：PyInstaller 把它当"顶层脚本"执行，
   `from . import __version__` 会报 `ImportError: attempted relative import with no known
   parent package`。`tools/build_exe.py` 会生成一个**绝对导入**的引导脚本作入口。

**打包与自检命令**：

```powershell
.\.venv\Scripts\python.exe tools\make_icon.py     # 生成 assets/app.ico（多尺寸）
.\.venv\Scripts\python.exe tools\build_exe.py     # 构建 + 自动冻结自检
# 产物：dist\exe\B站弹幕提取姬.exe（23.8 MB，冷启动约 4.5s）
```

`--onedir` 可构建目录版（启动更快但要发一个目录）；`verify` 子命令是**唯一**的
"exe 到底能不能用"判据（必须看到 `served_real_frontend: True`）。

### 4.1c `tools/` 下的脚本：哪些该跑、哪些是手动排查用的

**⚠️ 不要把排查/打包脚本并进 pytest**：它们要么单次跑 20–60 s、要么产出几十 MB、
要么需要真实网络。它们只在**人工排查**或**手动打包**时跑：

| 分类 | 脚本 | 何时跑 |
|------|------|--------|
| **打包/资源**（手动，常规开发不跑） | `build_exe.py`、`build_wheel.py`、`make_icon.py` | 出包时 |
| **托盘/GUI 冒烟**（手动，需真实桌面） | `smoke_tray.py`（Tk 窗口 + 托盘 + 消息泵 + 退出路径）、`smoke_exe_doubleclick.py`（双击/`tray` 两种启动方式的**外部观察**验证） | 改 GUI/启动方式时 |
| **冻结布局排查**（手动，需 20–60 s） | `test_pyinstaller.py`、`test_pyinstaller_gui.py`、`test_frozen_layout.py`、`probe_frozen_layout.py` | 怀疑打包路径/体积变化时 |
| **托盘/进程排查**（手动） | `probe_tray.py`、`probe_job.py`、`probe_spawn.py`、`probe_base_exec.py`、`probe_console_owner.py`、`probe_kill_conhost.py`、`probe_real_panel.py` | 改启动方式时 |
| **pip/代理排查**（手动，**需要网络**） | `diag_pip_tls.py`、`diag_pip_root2.py`、`diag_ca_bundle.py`、`diag_proxy.py`、`diag_proxy_tunnel.py`、`diag_pip_fix.py`、`diag_pip_fix2.py` | pip 出问题时（见 §4.1） |

这些脚本的共同特点：**一次性、可复核、跑完不留副作用**（清理临时目录、绝不动 `web/dist`）。
真正的回归保护在 `tests/` 里（例如冻结路径由 `tests/test_server_sqlite.py` 的
`test_find_dist_uses_meipass_when_frozen` 系列钉住）。

### 4.2 前端依赖：npm 能装，但有三个坑
- `registry.npmjs.org` 从 Node 侧不可达 → `web/.npmrc` 已指向 npmmirror。
- npm 全局缓存在 `F:\nodejs_cache`（工作区外），受限沙箱拒写报 `EPERM`
  → `.npmrc` 把 `cache` 指到工作区内的 `.npm-cache`。
- **Vite 构建需要放宽沙箱**：它在 Windows 上执行 `net use` 探测网络驱动器，
  受限沙箱禁止带管道的子进程，报 `spawn EPERM`。
  Vite 源码里唯一绕过分支要求 `fs.realpathSync.native` 抛特定 EISDIR 错误，无法人为触发。
  当前会话的沙箱策略已是 `danger-full-access`，构建直接可用。

### 4.3 其他

- PowerShell 把原生命令的 stderr 当错误 → 日志走 stderr 时管道会显示 `NativeCommandError`，
  甚至让 `$LASTEXITCODE` 变 1。**判断成功与否要看程序自己的退出码**，
  或 `2>$null` 把日志丢掉。
- `Get-ChildItem` 对被其他进程持有句柄的文件**不刷新大小**（显示 0 字节），
  但内容实际已落盘。用 Python `os.stat` 或直接读取才准。
- 中文输出乱码时设 `$env:PYTHONIOENCODING="utf-8"` 和
  `[Console]::OutputEncoding=[System.Text.Encoding]::UTF8`。
- **`bili-danmaku` 不在 PATH 里**：交互式 shell 里直接敲会报
  `CommandNotFoundException`（本机是 PowerShell，不是 cmd）。
  用 `.\.venv\Scripts\bili-danmaku.exe <子命令>`，或先 `Activate.ps1`。
  注意 PowerShell 会把这类"程序写 stderr"当成错误显示，
  退出码要看程序自己的（见上一条）。
- **不要用 `Get-Content -Raw` + `Set-Content` 改这个仓库里的中文文件**：
  PowerShell 5 的编码往返会把中文变成乱码、还会吃掉换行（本轮 HANDOVER.md 就被这么毁过一次）。
  改文本一律用编辑工具，或用 Python 明确指定 `encoding="utf-8"`。

---

## 5. 已完成里程碑与验收证据

| 里程碑 | 内容 | 验收证据 |
|--------|------|----------|
| M0 | 骨架 + 协议层 + 凭据扫码 + `probe` | `probe 804456` 端到端通过 |
| M1 | 连接状态机 + 自动重连 + JSONL | 真实抓取 148 条，重连 0 次 |
| M2 | CSV/TXT/SQLite + 过滤 + replay + stats | 四格式**条数完全一致**；replay 与原始 CSV 逐字节一致 |
| M3 | Web 面板（Vue 3 组件化） | REST/WS 端到端；端口自适应实测；过滤订阅生效 |
| 范围收敛 | 只采文字弹幕与 SC | 收包 216 → 入库 57 / 过滤 169；库内只有 `danmaku`，空文本 0 |
| **M4** | 多房间并发 / 文档 / 覆盖率 / 打包 / 压测 | ✅ 全部完成，见 §5.1 与 §5.2 |
| **V2-a** | 弹幕词云（词频 / PNG / HTML，表情占位符单独处理） | ✅ 完成，见 §5.3 |
| **V2-b** | SC 汇总 / 告警（CLI + 面板横幅） | ✅ 完成，见 §5.5 |

### 5.1 M4 验收证据（本轮）

| 项 | 证据 |
|----|------|
| 多房间并发（离线） | `tests/test_panel.py` 5 项：两个假服务端并发、会话隔离、JSONL 分文件、同一 SQLite 库共存、停一个房间另一个继续 |
| 并发上限语义 | 达上限时用**不存在的**房间号也会报"并发上限"，且不发起网络请求（D14 修复） |
| **真实双房间并发** | `pytest -m live tests/test_live_multi_room.py`（`BILI_LIVE_ROOMS=804456,7878`，75 秒）：两房间同时 `STREAMING`、`token=getDanmuInfo+wbi`、重连各 0 次；会话 `…-804456` / `…-23438368` 互不相同且各自匹配；JSONL 分文件 `804456_20260921-22.jsonl` / `23438368_20260921-22.jsonl`；同一 SQLite 库 10 + 902 条，每行都指向本房间会话；库内只有 `danmaku`；冷热并存下热房间收 1026 条、入库 826、过滤 210，丢弃 0 |
| 真实压测（热门房间） | `python tools/stress.py --room 7878`：默认配置约 300 条/分钟下零丢弃零重连、四产物一致；`queue_size=3` 强制背压时丢最旧且**仍不重连**（修掉 D17 之后）。详见 §5.2 |
| 文档 | 新增 `web/README.md`；README / HANDOVER / TECH_DESIGN 同步；`docs/PRIVACY.md` 按新范围复核（结论：无需改动，见 §12） |
| 覆盖率 | 核心模块 **90.15%**（M4/V2-c 时的数字；V2-d 后为 **90.00%**，门禁 85%），口径与命令见 README；`cli.py`/`__main__.py` 不计入百分比，但由 `tests/test_cli.py` 端到端覆盖 |
| 打包 | `python tools/build_wheel.py` → `dist/bili_danmaku-0.1.0-py3-none-any.whl`（含 `web_dist/` 前端产物）；装到临时目录后 `--version`/`stats`/`find_dist()` 均正常 |

测试：M4 收官时 **516 项全绿**（离线，默认跳过联网）+ 2 项 `-m live` 真实验收；
**当前累计 704 项全绿 / 覆盖率 90.90%**；前端另有 19 项（cd web; npm test）。见 §5.6–§5.11。

### 5.2 压测结论（M4 补齐）

需求方原本要求"压测放到初版完全开发之后再做"。功能齐全后用 `tools/stress.py`
在房间 7878（真实房间号 23438368，实测约 300 条/分钟）跑了两轮：

| 场景 | 收包 | 入库 | 过滤 | 丢弃 | 重连 | 队列峰值 | 四产物一致性 |
|------|------|------|------|------|------|----------|--------------|
| 默认 `queue_size=20000`，90s | 450 | 179 | 281 | 0 | 0 | 0 / 20000 | **179 = 179 = 179 = 179** |
| 强制背压 `queue_size=3`，60s | 220 | 46 | 174 | 7 | **0** | 3 / 3（顶满） | **46 = 46 = 46 = 46** |

**压测发现并修掉一个真缺陷（D17）**：sink 的同步写原先在事件循环线程里执行，
磁盘一慢就会把心跳一起卡住 → 采集器判假死并重连。
现在 `Bus._flush_off_loop()` 把写入丢到工作线程（`asyncio.to_thread`）；
同一个"慢 sink"用例从"只收到 2 条就重连"变成"收满 12 条、零重连、丢最旧、留最新"。
**附带的坑**：SQLite 连接因此必须 `check_same_thread=False`（面板多房间用例当场抓到）。

已知限制：本机 pip 不可用且 PyPI 无匹配的 psutil wheel，压测报告里 RSS 一栏
显示"不可用"而不是编造数字；要内存曲线就手动 `pip install psutil`。

### 5.3 V2-a 弹幕词云验收证据

| 项 | 证据 |
|----|------|
| 新命令 | `bili-danmaku wordcloud`，四种输出 `text / json / png / html`；支持 `--session/--room/--since/--until/--limit/--types`，也能 `--source <jsonl>` 完全离线跑 |
| 表情占位符单独处理 | `tests/test_text.py::test_emote_is_counted_separately_from_words`：`[dog]` **不进**词语榜，单独成表情榜并带 `extra.emots` 的图片链接；`--include-emotes` 时才进榜（以 `[dog]` 原形，便于区分） |
| 真实数据 | `bili-danmaku wordcloud --session 20260921-131356-804456` 在**旧库**上跑通：53 条文本、120 词、TOP1 `喜欢`×9，表情榜 `[dog]`×2 |
| PNG | 用系统 `msyh.ttc` 渲染，中文正常（无方块）；**已人工看图确认** |
| HTML | 自包含单文件、零依赖、不引外部资源；正文转义（弹幕是不可信输入） |
| 前端联动 | 面板把 `[dog]` 渲染成图片（`textSegments()` 只接受 http(s) 地址，`javascript:` 会被降级成纯文本）；`web/dist` 已重新构建 |
| 覆盖率 | `text.py` **92%**、`store.py` 93%（V2-a 当时的总覆盖率 90.71%） |
| 测试 | 新增 `tests/test_text.py`（44 项）、`tests/test_store_extra.py`（12 项）、CLI 词云用例 9 项、真实表情金样本 2 项 |

**新增依赖（都可选）**：`jieba`（中文分词）与 `wordcloud`（仅 `--format png` 需要）。
两者本机都装上了：`wordcloud` 有 win_amd64 wheel；`jieba` **只有 sdist**，
`tools/wheel_fetch.py` 解析不到，得手动下 sdist 再 `pip install --no-build-isolation`。
没装 jieba 时自动退回内置二元组分词器 —— 功能不残，只是分词质量差些。

### 5.4 实测反馈修复（用户报障）

| 问题 | 根因 | 修复与验证 |
|------|------|------------|
| **Web 面板导出的 CSV 在 Excel 里中文乱码**（D19） | `/api/export` 的流式 CSV 只写 `utf-8`，**没有 UTF-8 BOM**；Excel 打开本地 CSV 只看文件头、忽略 `Content-Type` 的 charset，于是按系统码页（GBK）解码。对照：`sinks/csv_txt.py` 与 CLI `export --out *.csv` 一直用 `utf-8-sig` —— 所以只有面板这一条路径坏 | 新增 `CSV_BOM`，只在第一块 chunk（表头）前添加；补字节级回归用例 `test_export_csv_carries_utf8_bom_for_excel`（断言 `content[:3] == b"\xef\xbb\xbf"`）与 `test_export_jsonl_and_txt_have_no_bom`。**端到端复验**：本机起面板（端口 18899）拉 `/api/export?format=csv`，前 3 字节确认是 BOM、中文正常 |

> 用户给的文件 `danmaku-1790570543.csv`：12307 字节 / 155 行 / 合法 UTF-8 **但无 BOM**
> （前 3 字节 `74 73 2c` = `ts,`）。内容用 Python 能正确读出中文 —— 典型
> "文件没坏、打开方式不对"的编码陷阱，**根因在导出端缺少 BOM 这个信号**。

### 5.6 面板常驻化与交互优化验收证据（DEC-3/4/5）

| 项 | 证据 |
|----|------|
| 后台常驻 | `bili-danmaku web start --rooms 804456`：分离启动（`DETACHED_PROCESS` + 隐藏窗口 + 日志落盘），**1.5 秒返回**；`web status` 显示地址/pid/已运行/房间/落盘格式/前端状态。⚠️ **"关掉终端仍存活"经需求方实测为不成立** —— 见 §5.12（开放问题） |
| 优雅停止 | `POST /api/admin/shutdown` → uvicorn 优雅退出 → `panel.stop()` → 每房间 `bus.stop()` flush。**实测**：请求后进程退出、状态文件被清理、`session.ended_at` 已回填、库内数据完整；`web logs` 可见 `Application shutdown complete` |
| 控制端点边界 | 只接受回环（`192.168.*` / `10.*` / `0.0.0.0` / 外部域名全部 403）；`Origin` 校验（跨站 Origin 403）；`web.token` 配置时强制校验；只允许 POST（405） |
| 面板停止按钮 | 顶栏右上「停止面板」→ 二次确认「确认停止？」→ 调控制端点并轮询等待服务消失；成功后提示可关闭页面（不用 `window.close()`，非脚本窗口浏览器不允许关闭） |
| 类型下拉列表 | 顶栏「类型」下拉，列出全部已知类型 + 流里出现过的类型（**V2-d 起改为自绘复选框面板**，见 §5.7 ⑤：收起态显示已选摘要与色块、每行带类型条数） |
| 关键词过滤 | 顶栏关键词带模式开关：**高亮**（默认，只高亮不过滤）/ **过滤**（只显示命中弹幕）；上下文条显示 `只看「xx」· 命中的/总数`；切回高亮即恢复；输入框在过滤模式下有粉色描边提示 |
| 已运行时间 | 顶栏右上「已运行 12m30s」+ 呼吸灯（scoped keyframes + 尊重 `prefers-reduced-motion`） |
| 统计口径 | 默认**从启动累计**（`/api/stats?window=0`，数据来自 hub 内存计数）；可切「最近时段」（1/5/30/60 分钟，走滑动窗口 + SQLite 补延迟分位与发言榜）；侧栏标题标注当前口径 |
| 陈旧状态自愈 | 强杀/崩溃后残留的状态文件会被 `live_runtime()` 自动识别并清理，下次 `web start` 能正常起来（`test_stale_state_is_cleaned_automatically`） |
| 测试 | 新增 `tests/test_runtime.py`（21 项）+ hub 累计计数 3 项 + 控制端点 12 项；全套 **558 项全绿**，覆盖率 **90.15%** |

> ⚠️ **本环境的一个限制（不是代码问题）**：这里的命令沙箱在每条命令结束时回收整个进程组，
> 所以我无法在这个 harness 里演示"父命令退出后后台面板继续存活"——
> 但只要父进程活着（例如你自己开一个 PowerShell），`web start` 起来的面板就会一直跑
> （已实测：同一条命令内 `start`→`status`→HTTP 访问→`stop` 全链路正常）。
> 生命周期逻辑本身有 21 项离线用例覆盖，且强杀/崩溃后的自愈路径也测了。

### 5.5 V2-b SC 汇总 / 告警验收证据
| 项 | 证据 |
|----|------|
| 汇总命令 | `bili-danmaku sc [ROOM] [--room/--session/--db] [--since/--until-time/--limit/--top] [--min-price] [--json]`：条数、金额合计、单条最高/平均、贡献榜（按 uid 聚合）、最近明细 |
| 只统计 SC | 礼物/上舰/弹幕**不会**混进 SC 报表（`tests/test_sc.py::test_only_super_chat_is_counted`） |
| 数字自洽 | 合计 = 明细之和 = 贡献榜之和；`--limit` 取最新 N 条（`test_summary_is_internally_consistent`） |
| 实时告警 | `sc <房间号> --follow --min-price N [--duration/--until/--bell]`：复用 `RoomCollector` + `build_filter(types=["super_chat"])`，告警 sink **只打印不落盘**（`AlertSink`） |
| 面板告警 | 新增 `ScBanner.vue`：达阈值的 SC 在弹幕流上方弹横幅，可按条依次展示（队列上限 3，丢最旧）、可关闭；阈值输入在顶栏（V2-c 从 `FilterBar` 移入 `RoomBar`，V2-d 该组件更名为 `TopBar.vue`），存 localStorage，0 = 每条都提示 |
| 金额单位 | 统一为**元**：SC 直接比，礼物/上舰 ×1000（金瓜子）后再比 —— 修掉一处单位混用（见 D20） |
| 单位逃生舱 | `--price-unit yuan\|milli`：毫元模式下自动 ÷1000，输出保留 `raw_price` 便于追溯 |
| 测试 | 新增 `tests/test_sc.py`（26 项）+ `test_cli.py` 的 sc 端到端 9 项 |

> ⚠️ **本节唯一的未验证假设**：SC 的 `price` 单位是「元」。
> 我为此专门蹲了 3 个高热度房间（7878 / 6 / 1017）共 4 分钟，**一条 SC 都没抓到**，
> 所以这个单位是**按协议档位（30/50/100/500/1000/2000）与礼物金瓜子语义推断的**，
> 不是实测结论。若拿到真实样本发现差 1000 倍，`--price-unit milli` 即可覆盖，无需改代码。
> 拿到样本后请：① 把 `sc_samples` 存进 `tests/fixtures/`；② 在 §6.2 更新结论；
> ③ 若单位有变，顺手在 `TECH_DESIGN.md` §19.2 记录偏离原因。

---

### 5.7 V2-d 面板排版与直播间信息验收证据

需求方这一轮给了 6 条：删「服务」栏目、房间号输入搬到右栏顶部并记忆历史、
右栏顶部显示「XXX 的直播间」、类型分布改横向彩色柱状图、类型下拉提高可读性、重排三栏。

| 需求 | 落地 | 证据 |
|------|------|------|
| ① 删「服务」栏目 | `StatsPanel.vue` 移除版本/落盘格式/保留 raw/脱敏/前端 5 项 | 构建产物里 `落盘格式`/`保留 raw`/`脱敏` **均不存在**；版本等改由 `/api/health` / `web status` 承载 |
| ② 房间号输入搬到右栏顶部 + 本地记忆 | 🆕 `RoomPanel.vue`；顶栏**不再有**房间下拉与输入框 | 构建产物里 `未选择房间` 存在、`全部房间` **不存在**；记忆键 `bili-danmaku.room-history`（上限 12 条，添加成功后把短号替换成真实房间号）；建议用原生 `<datalist>` + 可点胶囊 |
| ③ 「XXX 的直播间」 | 🆕 `GET /api/room-info` + `RoomPanel` 顶部信息块，主播昵称高亮（琥珀色） | **真机实测**（房间 804456）：`anchor_name='六式zy'`、`title='六式zy的直播间'`、`anchor_uid=8431064`；面板截图确认信息块与「六式zy」高亮渲染正常 |
| ④ 类型分布横向柱状图 | 🆕 `TypeBars.vue`：条长按**占比**、**条数在横条右侧**、每类型一色（`format.typeColor()`）、底部合计 | 面板截图：`弹幕 ▬▬▬▬▬▬ 10` + `合计 10 条`；颜色与弹幕流高亮同源 |
| ⑤ 类型下拉可读性 | 🆕 `TypeSelector.vue`：自绘下拉，收起态=「全部 10 种」+色块，展开态=复选框+色标+中文名+`type`+条数，带全选/清空/收起 | 面板截图确认展开面板渲染正确；键盘可达性与点外部/Esc 收起已补齐 |
| ⑥ 重排布局 | 顶栏只留跨面板过滤/操作（类型、关键词、SC 阈值、计数、导出、停止）；右栏 = 上「房间（信息+输入+列表）」/ 下「统计」，中间分隔线 | `.app` 网格 4 行（顶栏/上下文条/SC 横幅/主体）、`.body` 右列 340px；面板截图逐项核对 |

**新增后端接口与数据来源（实测结论）**

| 接口 | 匿名可用性 | 说明 |
|------|-----------|------|
| `live_user/v1/UserInfo/get_anchor_in_room?roomid=` | ✅ **可用** | **采用**：`BiliApi.anchor_info()` 取 `info.{uid,uname,face}` |
| `xlive/web-room/v1/index/getInfoByRoom?room_id=` | ❌ **-352 风控** | **不要用**（与 `getDanmuInfo` 同一个坑） |
| `room/v1/Room/room_init?id=` | ✅ 可用 | 房间号/短号归一化（原有路径） |

- `RoomInfo` 新增可选字段 `anchor_name` / `anchor_face`；`/api/rooms` 快照与 `/api/room-info` 都会带上。
- `PanelService._room_cache` 进程内永久缓存 `room_id → RoomInfo`（`short_id` 也能命中），
  `lookup_room()` **只查信息、不启动采集**。
- **昵称取不到不阻断添加**：`anchor_info()` 失败返回 `None` 而不是抛异常
  （用例 `test_add_room_survives_anchor_lookup_failure`），面板退化成「房间 <id>」。

| 测试 | 结果 |
|------|------|
| 全量 pytest | ✅ **568 项全绿**（V2-c 基线 558 → 新增 10 项：api.py 主播信息 4 + panel.py 房间查询/缓存 3 + server 端点 3），覆盖率 **90.00%**（门禁 85%） |
| 前端构建 | ✅ `npm run build` → 30 modules（`index-B0PFjhPX.js` / `index-BLJvT0jg.css`）；构建产物逐关键字核对通过 |
| 真机端到端 | ✅ `web --port 8793 --rooms 804456` → `/api/health` ok → `/api/room-info` 返回真实昵称 → `POST /api/rooms` 快照含 `anchor_name`；浏览器截图核对布局与配色 |
| 错误路径（真机实测） | `room=99999999` → `400 room_init 失败 code=60004 房间不存在`；`room=abc` → `400 直播间不存在`；缺 `room` 参数 → `422`。**注意 `room=1` 是真实存在的房间**（不是"非法房间号"），别拿它当错误用例 |
| 已知取舍 | ⚠️ 前端没有单元测试设施，本轮前端验证靠**构建产物关键字核对 + 真机截图**（与之前几轮一致）；`npm run build` 仍需放宽沙箱（见 §4.2 的 `spawn EPERM`） |

---

### 5.8 V3-a 复盘器验收证据（历史浏览）

需求：按 [`ROADMAP.md`](ROADMAP.md) §2 把面板从"看直播"升级为"看数据"——
历史可翻页、可定位、可按抓取会话浏览。**核心矛盾是**：数据一直是全的（JSONL+SQLite），
但面板只缓冲 2000 条、只渲染 400 条，滚出去就摸不着了。

| 交付 | 落点 | 证据 |
|------|------|------|
| 游标分页 | `store.iter_history()` + `GET /api/history` | 复合游标 `(recv_ts, id)`；**实测**：房间 804456 有 **393 组重复时间戳**（最多 10 条同刻），只比 `recv_ts` 会漏条 |
| 瘦投影 | `store.HISTORY_COLUMNS` + `_project()` | 单条 **257 B**（对照完整 `Event` 实测 ~2.0 KB）⇒ 400 条一页从 ~800 KB 降到 ~100 KB，这是"不触发 `SLOW_CONSUMER`"的前提 |
| 时间轴密度 | `store.density()` + `GET /api/density` | SQL 聚合，20 万行实测 **68~95 ms**；对照旧路径 `iter_events(limit=100400)` **3.6 s** |
| 会话列表 | `GET /api/sessions` | 复用既有 `store.sessions()`，补 `room_id` 过滤 |
| 前端双层数据流 | `composables/useHistory.js` | 实时层缓冲不动，历史层按游标翻页；合并视图按 `id` 去重归并 |
| 时间轴 UI | `components/DensityBar.vue` | 点任意桶跳转；当前窗口高亮；P95 归一化避免毛刺压平 |
| 翻页控件 | `FilterBar.vue` + `StreamList.vue` | 滚到顶自动加载更早（并**补偿 `scrollTop`** 保持视口）；"↑更早/↓更新/回到实时"；会话下拉 |
| 前端测试（**首次**） | `web/test/history.test.mjs` | 11 项纯逻辑用例，`npm test`（`node --test --test-isolation=none`，零新依赖） |

**实测查询计划（20 万行合成库 + 真实 1 万行库）**

| 写法 | 计划 | 耗时 |
|------|------|------|
| 复合游标 + `room_id`（本实现） | `SEARCH ... COVERING INDEX idx_event_room_ts (room_id=?)`，**无临时排序** | 首屏 400 条 **18 ms** |
| `ORDER BY id DESC` 游标 | 索引 + `USE TEMP B-TREE FOR ORDER BY` | 12.3 ms |
| **无** `room_id` 过滤 | `SCAN` + 临时 B 树 | 39.8 ms/页 ⇒ 必须有兜底时间窗 |
| 密度 `GROUP BY CAST(recv_ts/b AS INT), type` | 临时 B 树 | 118 ms |
| 密度 `GROUP BY recv_ts, type`（本实现） | 覆盖索引 | **38 ms**（为此新增 `idx_event_room_ts_type`） |

**踩到的两个真实缺陷（都被离线用例挡住）**

1. **`has_newer` 不能照抄服务端**：它只说明"这一页之后还有没有数据"。用户向上翻几页后，
   窗口尾部已不含最新数据 ⇒ 服务端算出 `has_newer=false` ⇒ **再也翻不回最新**。
   正确语义是"窗口是否已离开实时边缘"（`detached`）。
2. **`has_older` 会永远为真**：窗口被 `HISTORY_CAP` 截断后，`has_older` 随丢弃的数据失真；
   必须靠"拿到过空页"（`exhausted`）来确认真的到头了。

> 这两个都是**状态机语义**问题，靠人眼点几下几乎发现不了；`npm test` 里各有一条用例守着。

| 测试 | 结果 |
|------|------|
| 全量 pytest | ✅ **605 项全绿**（V2-d 基线 568 → 新增 37：`test_history.py` 19 + `test_history_api.py` 18），覆盖率 **90.08%**（门禁 85%） |
| 前端测试 | ✅ `cd web; npm test` → **11 项全绿**（首次引入；满足 ROADMAP 的 **G1** 门槛） |
| 前端构建 | ✅ `npm run build` → 34 modules（`index-C4kQ7w-v.js` / `index-Cfrf1uBx.css`） |
| 性能门槛（§2.4） | ✅ 单页 400 条 **18 ms**（门槛 200 ms）；密度条 **38~95 ms**（门槛 100 ms） |
| 真机端到端 | ✅ 面板起在 8798，`/api/history` 300 条 / 78 KB、`/api/density` 10 桶 / 2963 条、`/api/sessions` 正常；浏览器截图确认时间轴与分区 |
| 已知取舍 | ⚠️ 浏览器点击验证受 GUI 窗口抢占干扰，**核心交互改由 `npm test` 的 11 项逻辑用例覆盖**（比截图更可靠）；`focus`/`scroll` 的真实手感仍需人工确认一次 |

---

### 5.9 V3-a2 标注与切片验收证据（标记 / 笔记 / 区间导出）

V3-a 复盘器把历史接通了，但"我看到这段有意思"之后**没有任何地方可以落笔**。
这一轮补上标注能力。**核心约束：标注存独立库，采集库一张表都不动**（ROADMAP §6.2 R2）。

| 交付 | 落点 | 证据 |
|------|------|------|
| 标注存储 | 🆕 `src/bili_danmaku/annotations.py`（`AnnotationStore`） | 独立库 `{out_dir}/annotations.db`（可配 `sinks.annotations_path`）；表结构照 ROADMAP §6.2 |
| 独立库隔离 | — | **真机实测**：打完标记后 `danmaku.db` 的表仍是 `event/meta/session/sqlite_sequence`，**没有 `bookmark`**；标注库只有 `bookmark` |
| 只读不建文件 | `AnnotationStore(readonly=True)` | 面板首屏会拉一次标记列表；实测此时**不会**在工作区凭空留下空库（`test_readonly_does_not_create_the_file`） |
| 新增/删除标记 | `POST /api/annotations`、`DELETE /api/annotations/{id}` | 404（不存在）/ 400（标签超长）/ 422（缺字段或 `recv_ts<=0`）各有用例 |
| 保留策略 | `GET /api/annotations?room_id=&since=&until=` | 返回 `{items, summary:{count,lo,hi}}`；库不存在返回**空列表而不是错误** |
| 按标记区间导出 | `GET /api/annotations/export` | **只给起点时终点自动取"下一个标记"**；给反了自动正过来；末尾标记则导到数据末 |
| 前端标记 UI | 🆕 `BookmarkBar.vue` + `StreamList` 的 ★ + `App.vue` 的 `playheadTs` | 放在时间轴下方（与 `DensityBar` 同一套时间语义）；可"打标记 / 跳到标记 / 选为起点 / 导出区间 / 删除 / 清空" |
| 前端逻辑用例 | `web/test/history.test.mjs` | 11 → **18 项**（新增 7 项覆盖标记的增删清空、排序、选中态、`anchor_id`） |

**两个刻意的设计决定**

1. **`playheadTs` 用"可见的最后一条弹幕"而不是墙钟**。正在看历史时，墙钟会把标记打到"现在"，
   而用户以为标的是屏幕上那一段 —— 这种错误从界面上无法察觉。一条弹幕都没有时按钮禁用并说明原因。
2. **`anchor_id` 不做存在性校验**。校验会引入"标注依赖采集库"的耦合，而标注本身只靠 `recv_ts` 就能用；
   被钉的弹幕后来被清理/归档，标注依然有效。前端用 ★ 表示"钉在某条"。

| 测试 | 结果 |
|------|------|
| 全量 pytest | ✅ **622 项全绿**（V3-a 基线 605 → 新增 17：`test_annotations.py`），覆盖率 **90.35%**（门禁 85%，`annotations.py` 自身 95%） |
| 前端测试 | ✅ `cd web; npm test` → **18 项全绿** |
| 前端构建 | ✅ `npm run build` → 36 modules（`index-CfsVfsm7.js` / `index-DmQ6Hiuh.css`） |
| 真机端到端 | ✅ 空库列空 → 建 3 个标记 → 列表聚合正确 → 区间导出 `X-Range-Since/Until/Total-Count` 与 CSV（37 行含表头、带 BOM）逐项核对 → 反向区间自动正过来 → 删除生效 |
| 已知取舍 | ⚠️ 没有"改标签/改备注"的接口（只支持增删）—— 本地用量下重打一个更直接；真需要再加 `PATCH` |

---

### 5.10 V3-a3 复盘器交互修正（实测反馈驱动）

需求方拿到面板后提了 5 条，其中一条是真 bug。逐条记录，**因为"看起来像主观偏好"的反馈里
常常藏着缺陷**。

| # | 反馈 | 性质 | 处置 |
|---|------|------|------|
| 1 | 点时间轴某一格，高亮的却是**前一格** | 🐞 **真 bug（off-by-one）** | 见下 |
| 2 | 「更早 / 更新」按钮实际体验完全错误 | 交互设计错误 | **删除这两个按钮**，保留"滚到顶自动加载" |
| 3 | 底栏要有自选每格时长（5/10/15 分钟） | 新需求 | `DensityBar` 加档位选择，切换即重算 |
| 4 | 发言 TOP 在"从启动累计"时也要默认显示 | 新需求 | `/api/stats` 累计口径补 `top_senders` |
| 5 | 右侧栏加"直播间链接"输入框并解析出房间 | 新需求 | 🆕 `src/bili_danmaku/rooms.py` |

#### 🐞 off-by-one 的根因（值得记住）

`jumpTo` 原来用 `before_ts = ts` 取窗口，含义是"**该时刻之前**的 N 条" ——
于是窗口右端正好落在**所点格子的起点**上，整段窗口落在**上一格**里，
`inRange()`（按窗口区间判定高亮）自然就亮前一格。

**修法**：用 `until = ts + bucket_s`（闭区间）把窗口右端划到**该格结束时刻**，
窗口左端就落在所点格子里。**判据**：`窗口右端 == 所点格子的结束时刻`，
且窗口内必须含有 `[ts, ts+bucket_s]` 区间内的事件 —— 这两条都有用例守着
（`web/test/history.test.mjs`）。同时高亮改成"**有 `focusTs` 时只亮那一格**"，
不再用窗口区间去推（窗口大小 300 条，跨多格时会亮一大片）。

#### 为什么删掉「更早 / 更新」

时间轴点选引入后，翻页有了三个入口（按钮 / 滚到顶 / 点格子），而按钮那条**语义最难说清**：
它翻的是"窗口游标"，翻完之后窗口既不在实时边缘、也不在某个具体时刻，
用户不知道自己现在看的是哪一段。删掉后每类意图只剩一个入口：

| 意图 | 入口 |
|------|------|
| 看更早的 | 弹幕流**滚到顶部**自动加载（且补偿 `scrollTop` 保持视口） |
| 看某个时间段 | 点时间轴的格子 |
| 回到最新 | 「回到实时」按钮 |

> 注意 `hasOlder` / `hasNewer`（含 `detached` / `exhausted` 状态机）**仍然保留** ——
> 滚到顶加载靠的是 `hasOlder`，只是不再有按钮直接暴露它。

#### 时间轴粒度

档位 5/10/15 分钟（`DensityBar.BUCKET_CHOICES`），切换即带 `bucket_s` 重新请求 `/api/density`。
首屏仍是**自适应**（后端按数据跨度挑人类可读步长），拿到实际步长后写回 `history.bucketS`，
`jumpTo` 用它算格子边界 —— 否则"跳到某格"会按 60 秒切，与画出来的格子对不上。
摘要行同时给出格数与"平均 N 条/格"，让用户看得见密度变化。

**实测**：同一房间 5 分钟 = 26 格、15 分钟 = 10 格。

#### 累计口径的发言榜（含成本控制）

`/api/stats?window=0` 现在也返回 `top_senders` 与 `top_senders_scope`。
**成本必须控住**：实测 `top_senders` 单房间 6.4 ms、**全库 101.6 ms**（按 uid 分组排序无合适索引），
而面板每 3 秒拉一次。所以：

* 选了具体房间 → 直接查（6 ms 可忽略）；
* **全部房间 → 走 `_TOP_SENDERS_CACHE`（TTL 15 秒）**，第二次起角标显示「全部房间（缓存）」。

`reset_top_senders_cache()` 是给测试用的：缓存是模块级的（必须跨请求复用），
不清就没法分别断言"首次查询"与"命中缓存"两条路径。

#### 直播间链接解析（`rooms.py`）

支持：纯房间号/短号、`live.bilibili.com/8432038?...`、`?room_id=`、`/index.html`、移动端域名、
协议相对写法。**不支持就明确报错**：

| 输入 | 结果 | 理由 |
|------|------|------|
| `https://live.bilibili.com/8432038?live_from=...` | `8432038` | 规范直播间链接（需求方样例） |
| `https://live.bilibili.com/blanc/8432038` | ❌ 报错 | 具名分区，`8432038` 不一定是房间号 —— **宁可不猜** |
| `https://example.com/12345` | ❌ 报错 | 非 B 站域名 |

规则是"**只把路径第一段当房间号**"（直播间链接的形状就是 `/<房间号>[/index.html]`）。
猜错的代价是把数据抓进一个完全无关的房间，而且不容易发现，所以宁可让用户粘一条规范链接。

**一处状态码修正**：`POST /api/rooms` 原来把所有 `ValueError` 映射成 409（"已在监控中"），
坏链接也是 `ValueError` ⇒ 会报成 409，完全误导。现在端点**先解析**（失败 → 400），
再调 `add_room`（`ValueError` → 409），两条路径分开。

| 测试 | 结果 |
|------|------|
| 全量 pytest | ✅ **660 项全绿**（V3-a2 基线 622 → 新增 38：`test_rooms.py` 21 + panel/server 17），覆盖率 **90.54%**（`rooms.py` 99%） |
| 前端测试 | ✅ `cd web; npm test` → **19 项**（新增 jumpTo 的两条回归） |
| 前端构建 | ✅ 36 modules（`index-BiSCQBAu.js` / `index--XTHaCgq.css`） |
| 真机五项验证 | ✅ 起面板逐项核对 **18 项检查全 PASS**：跳转窗口右端 = 格结束时刻且有该格数据、三档粒度格数不同（26/10）、累计榜（选中房间与全部房间都有）、链接解析成功、非 B 站与 `/blanc/` 被拒、重复添加 → 409 |

---

### 5.11 V3-b 3A 本地感知分析验收证据

需求：[`ROADMAP.md`](ROADMAP.md) §3.1 —— 把弹幕流变成"看得懂的几行字"。
**这一版只做 3A（本地纯统计），不做 3B（LLM）**：LLM 属于隐私基线变更，
按 G4 门槛必须先有需求方书面决策。

| 交付 | 落点 | 说明 |
|------|------|------|
| 分析函数 | 🆕 `src/bili_danmaku/analysis.py` | **纯函数 + 有界输入**：无 IO、无网络、无全局状态 ⇒ 完全离线可测 |
| 有界窗口 | `server/hub.py` 的 🆕 `LiveWindow` | 每房间增量维护；**`add()` O(1) 且有界**，所以能放在 `publish()` 热路径 |
| 端点 | 🆕 `GET /api/analysis` | `room_id` / `top` / `tokenizer`；返回词频、热点突增、疑似刷屏、疑似异常账号 |
| 前端 | 🆕 `components/AnalysisPanel.vue` | 右栏底部：可折叠四块；**点词即可只看含该词的弹幕**（复用顶栏关键词过滤） |

**四类指标的口径**

| 指标 | 口径 | 关键取舍 |
|------|------|----------|
| 实时词频 | 窗口内分词计数；同时给"多少人说过"（`users`） | 一条重发 20 遍 ≠ 20 个人都说，两个数都要给 |
| 热点突增 | 最近一片 vs 之前若干片的**篇均速率** | 用速率不用绝对条数：最后一片常常是半截 |
| 疑似刷屏 | 同文本重复 ≥4 次；**长度必须 ≥4 字** | `哈哈`/`666` 大量出现是正常气氛，报出来只会淹没真正的异常 |
| 疑似异常账号 | 密度 40 + 重复率 40 + 集中度 20，≥45 分上报 | **必须给 `reasons`**，让用户能复核而不是只看到分数 |

**性能实测**（决定"能不能放在热路径"）

| 项 | 数字 |
|----|------|
| `LiveWindow.add()` | **+47 µs/条**（jieba 已装；`model_dump` 对照 4.6 µs/条） |
| 按实测热门房间速率（300 条/分钟 = 5 条/秒） | 约 **0.02% 单核** |
| 极端 100 条/秒 | 约 **0.5% 单核** |
| `analyze()`（1200 条窗口，含刷屏 + 账号画像） | **6.3 ms**（前端 6 秒拉一次） |
| 内存上限 | 每房间 ≤1200 条原文 + ≤20 片 × ≤2000 词 |

**🐞 两个实测到的误报（都是"把正常人报成疑似异常账号"）**

这一类误报比"漏报"严重得多 —— 用户可能据此误伤真人，所以两个都补了回归用例：

1. **时间戳完全相同**：历史补种（`gethistory`）会把一批弹幕写成同一个 `recv_ts`，
   `末条 - 首条 = 0` ⇒ `count/max(span,1)` 算出"600 条/分钟"。
2. **跨度太短**：房间未开播时那批补种可能落在 1 秒内（实测 10 条 / 0.9 秒，
   且时间戳**各不相同**）⇒ 仍然算出"600 条/分钟"并命中"集中发送"。

处理方式是**认清数据退化并放弃速率类结论**（`_rate_window()` 返回 `rate_available=False`），
而不是硬套一个下限编出数字；重复率那一项与时间无关，仍然参与打分。
**真机复核**：修好后同一离线房间的 `suspicious` 从 1 条变为 **0 条**。

**🐞 第三个是公式错误**：重复率原本写成 `1 - 去重数/总条数`，
于是"10 条完全相同"算出 90% 而不是 100%（永远够不到满值）。
改为 `1 - (去重数-1)/总条数`：全不同时为 0、全相同时才为 1。

| 测试 | 结果 |
|------|------|
| 全量 pytest | ✅ **704 项全绿**（V3-a3 基线 660 → 新增 44：`test_analysis.py`），覆盖率 **90.90%**（`analysis.py` 95%、`hub.py` 96%） |
| 前端测试 | ✅ `cd web; npm test` → **19 项**（本版前端是展示层，逻辑在后端） |
| 前端构建 | ✅ 38 modules（`index-DYqGL4mp.js` / `index-CM2dI2GD.css`） |
| 真机验证 | ✅ 起面板实测 `/api/analysis` 在真实房间返回词频/词数/窗口跨度；**离线房间误报已消除**（`suspicious=0`、`rate_available=false`） |
| 热点突增 | ⚠️ **未用真实数据验证**：实测时段内两个房间都没有新弹幕（凑不出多片）。已用真 `hub` + 合成多片流验证（`抽奖` ⇒ `is_new=true`），单测 6 项覆盖该路径 |

---

### 5.12 ✅ `web start` 的面板会随启动它的终端一起停（**已修复**）

**需求方实测（修复前）**：`web start` 之后会多出一个终端窗口（标题 `.venv\Scripts\python.exe`、内容为空），
面板完全依赖它；一旦关掉那个终端，面板就停。这与 DEC-3 的承诺"关掉终端也继续跑"**相反**。

#### 实测现象（需求方原话整理）

```powershell
(.venv) PS F:\CodingBox\DeepseekHarness\LiveDanmakuExtractor> bili-danmaku web start
面板已在后台运行：http://127.0.0.1:8787/（pid 42448）
停止：bili-danmaku web stop　日志：bili-danmaku web logs
```

随后**另开一个终端**（标题为 `.venv\Scripts\python.exe`、内容为空）；
关掉它 → 面板停止。

#### ✅ 真正的机制：**多出来的一层 console**（2026-10-01 实测修正）

> ⚠️ **本节此前把根因写成 "stub 把子进程放进 Job Object"，已被实测证伪。**
> 照那个方向修（`CREATE_BREAKAWAY_FROM_JOB`）**完全解决不了问题**。以下为修正后的事实。

进程树与 console 归属（探针实测，`tools/probe_console_owner.py`）：

```
web start（前台，继承用户终端的 console）
  └─ Popen [DETACHED_PROCESS | NEW_PROCESS_GROUP]
     exe = .venv\Scripts\python.exe（转发器 stub）      ← 无 console（detach 生效）
       └─ 真 server（base 解释器 E:\Python\python.exe）  ← ❌ **有 console**
            └─ conhost.exe                              ← 用户看到的那个空终端
```

**机理**：`DETACHED_PROCESS` 只作用于**顶层那一层**。venv 的 `python.exe` 是**转发器 stub**，
它必须再起一个 base 解释器来真正干活；而 base 解释器是 **console 程序**，
又拿不到父进程的 console → **Windows 给它新分配了一个 console（conhost）**。
关掉那个窗口 = 杀掉挂在这个 console 上的进程 = **真 server 连坐**。

#### 已查实的事实（都是实测，可直接采信）

| # | 事实 | 证据 |
|---|------|------|
| 1 | **状态文件里的 pid 是对的** | `web start` 报的 pid、`.runtime/web.json` 的 pid、日志里 `Started server process [N]` 的 N **三者一致**（实测 45524）。曾怀疑"记错 pid"——**排除** |
| 2 | `.venv\Scripts\python.exe` 是**转发器 stub**，不是真解释器 | `Popen.pid=47268`，真正干活的是 **45300**，且 `45300.ParentProcessId == 47268`、命令行是 `"E:\Python\python.exe" ...` ⇒ `web start → stub → 真 server` **三层** |
| 3 | 那个"多出来的终端"就是**真 server 自己的 console** | `AttachConsole(真server pid)` **成功**（有 console）；而顶层 `Popen` **失败**（无 console）。`LoadImageW`/窗口枚举显示真 server 名下有一个 `PseudoConsoleWindow` 类窗口 |
| 4 | **杀 stub 会连带杀掉面板** | 杀 stub(34808) 后真 server(45524) 5 秒内退出、8787 不再监听。**原因不是 job，是两者共享同一个 console** |
| 5 | ~~推断机制：Job Object~~ | ❌ **已证伪**。`IsProcessInJob` 实测：探针 / 顶层 / stub / 真 server **四层全部 `True`** —— 那是 harness 自己的 job，**无法区分各层**；且顶层加 `CREATE_BREAKAWAY_FROM_JOB` **实测成功**（当前 job 允许 breakaway）。**job 不是根因** |
| 6 | `spawn_detached()` 本身**没问题** | 顶层进程确实**没有** console。问题不在"detach 是否生效"，而在**多出来的那层 stub 又建了一个 console** |
| 7 | `CREATE_NO_WINDOW` **消不掉**这个 conhost | 实测 `stub + CREATE_NO_WINDOW` 仍然产生 `conhost.exe` |
| 8 | 优雅停止路径不受影响 | `stop_runtime` 用的是状态文件里的 pid（= 真 server），`web stop` 正确优雅停止；受影响的是"**终端关闭**"这条路径 |

#### 修复方向（**已实测验证可行**）

1. **绕开 venv stub，直接启动 base 解释器**（✅ **已验证端到端可行**）
   `server_argv()` 目前用 `sys.executable`（= venv 的 stub）。改为
   `getattr(sys, "_base_executable", None) or sys.executable`
   （实测 = `E:\Python\python.exe`），并注入环境：
   `PYTHONPATH = <项目 src> + <venv 的 site-packages>`、`VIRTUAL_ENV=<venv 根>`、
   `PYTHONNOUSERSITE=1`、**并清掉 `PYTHONHOME`**（否则会劫持 base 的 stdlib 搜索）。
   实测结果（`tools/probe_real_panel.py`，用真 CLI 起完整面板）：
   - 进程树里**没有** conhost（顶层就是 server）⇒ **没有 console 窗口可关**；
   - `/api/health` 通、`.runtime/web.json` 的 pid == 顶层 pid；
   - 优雅停止 `HTTP 200` + `returncode=0` + 日志 `所有 sink 已 flush` 齐全。
   **必须带降级**：`_base_executable` 不存在或 venv site-packages 推导失败时退回 stub。
2. ~~先证实 Job Object 假设~~ → **已证伪，此路作废**（见事实 5）。
3. ~~`CREATE_BREAKAWAY_FROM_JOB`~~ → **无效**，不解决问题。
4. **兜底文档/交互**：若短期修不了，至少让 `web start` 输出**明确警告**
   "请勿关闭自动弹出的终端"。

> 📌 **另有一条正交的路线**：新的"托盘 + 启动面板"方案（见 §6.5）让面板**跑在托盘进程内**，
> 根本不 spawn 子进程 ⇒ 这个缺陷在架构上**直接消失**。
> 因此 §5.12 的修复优先级取决于托盘方案是否落地。

#### ✅ 修复内容（`runtime.child_python()`，已实现并验收）

`server_argv()` 不再用 `sys.executable`（venv 的 stub），改为：

1. 用 `sys._base_executable`（实测 `E:\Python\python.exe`）作为子进程解释器 ⇒
   **顶层进程本身就是 server，没有多余的 console 可关**；
2. 把**当前进程的 `sys.path` 原样注入** `PYTHONPATH`（base 解释器不认识 venv 的
   sys.path）—— 用 `sys.path` 而不是"猜 `src/` + site-packages"，因为它是**事实**，
   而且顺带覆盖了 `pip install -e .` 的 `.pth` 展开结果；
   只保留**真实存在**的绝对路径（空串会被解释成当前目录）；
3. 注入 `VIRTUAL_ENV` / `PYTHONNOUSERSITE=1`，并在 `spawn_detached()` 里
   **清掉 `PYTHONHOME`**（它会盖掉注入的 sys.path）；
4. **带降级**：拿不到 base 解释器就退回 stub（宁可保留"多余 console"，也不能起不来）。

**实测验收**（`tools/verify_web_start_fix.py`，全绿）：
`web start` 后进程树里**只有一个 python 进程**（顶层就是 server）、**没有 conhost**、
`/api/health` 通、`web status` 认识它、`web stop` 优雅停止并 flush。

> **验证提醒**：`web start` → **关掉终端窗口（点 X / exit，不要 Ctrl+C）** →
> 另开窗口 `web status` 应为"仍在运行"。这一步仍建议用真人终端确认一次
> （harness 的 console 是 ConPTY，与真 conhost 不完全等价）。

#### 修复后必须做的验证（**必须用真人终端，harness 测不了**）

```powershell
# 在你自己开的 PowerShell 里（不要用 harness 的单条命令，见 SESSION_HANDOVER §7.6）
bili-danmaku web start --rooms 804456
# 关掉终端窗口（点 X 或 exit）—— 不是 Ctrl+C
# 另开一个 PowerShell：
bili-danmaku web status          # 期望：仍在运行
# 顺便确认没有多余的 python.exe 残留
Get-Process -Name python, bili-danmaku -ErrorAction SilentlyContinue | Select-Object Id, ProcessName
bili-danmaku web stop            # 收尾
```

> ⚠️ **关于"孤儿面板进程"（§7.3 of SESSION_HANDOVER）——之前的方向写反了**：
> 既然 console 宿主是**真 server**，那么 console 一关应当是**真 server 先死、stub 随后**，
> 而不是旧文档猜的"stub 死了、真 server 反而活着"。定位时请按新机制理解。
>
> 📌 复核探针（`tools/`）：`probe_job.py`（证伪 job）、`probe_console_owner.py`（console 归属）、
> `probe_kill_conhost.py`（因果）、`verify_web_start_fix.py`（端到端验证修复）。

---

### 5.13 已落地：托盘 + 启动面板 + 单文件 exe（见 DEC-6）

需求方 2026-10-01 拍板。**本阶段只做简易启动面板，扩展功能留到未来**。

#### 5.13.1 架构决定

| # | 决定 | 说明 |
|---|------|------|
| D1 | **单文件 exe**（PyInstaller） | 实测可行，见 §4.1b。选 onefile 而非 onedir：发布是一个文件、启动 2.9s vs 2.1s 差距可接受 |
| D2 | **面板跑在托盘进程内**（内嵌 uvicorn 线程），**不 spawn 子进程** | 架构本来就支持：`_run_web_foreground()` 里 `hub`/`panel`/`uvicorn` 都是**进程内对象**。停止仍走 `_StopHandle.request()` → `server.should_exit = True` → lifespan 的 `panel.stop()` → 各房间 `bus.stop()` **flush 落盘** |
| D3 | **CLI 降级为调试端**；核心功能优先 Web 端 | 新增 `bili-danmaku tray` 作为托盘入口（同时保留 `web start/stop/...` 供调试） |
| D4 | **双击 exe = 只起托盘 + 显示启动面板**，**不自动开始采集**（房间号未定时不该猜） | |
| D5 | 退出语义：托盘「退出」= **先优雅 flush 再退出**，并二次确认 | 绝不在未 flush 时静默退出（沿用 DEC-3 的核心承诺） |

**托盘实现：零依赖 ctypes**（**不引入 `pystray`**，已按此决策卸载）。
理由：`pystray` 的菜单能力不足（状态行/动态置灰/分隔符/默认项都要手搓），
而纯 ctypes 已实测可用（见下）。

#### 5.13.2 已实测验证的技术事实（可直接采信）

| 项 | 结果 | 探针 |
|----|------|------|
| PNG(1254×1254 RGBA) → 九档 `.ico` | ✅ 16/20/24/32/40/48/64/128/256 | `probe_tray.py` |
| `Shell_NotifyIconW(NIM_ADD)` | ✅ 返回 1（挂载成功） | 同上 |
| `CreatePopupMenu` + `AppendMenuW` + `TrackPopupMenu` | ✅ 10 项菜单可用 | 同上 |
| 消息循环 `PeekMessage`/`DispatchMessage` | ✅ 正常 | 同上 |
| 冻结后 `tkinter` 建窗口 | ✅ `Tk 8.6` 可用 | `test_frozen_layout.py` |
| 冻结后 `PIL.Image`/`ImageTk` | ✅ 可用 | 同上 |

> ⚠️ **ctypes 踩坑（已定位，别重复踩）**：`WNDPROC` 的返回类型必须是 64 位
> **`LRESULT`（`ctypes.c_ssize_t`）**，用 `c_long` 会在 64 位 Windows 上抛
> `OverflowError: int too long to convert`。`DefWindowProcW` 也必须显式声明
> `argtypes`/`restype`（`CreateSolidBrush` 在 **gdi32**，不在 kernel32）。

#### 5.13.3 🔴 打包前必须修的一个真缺陷：`find_dist()` 在冻结后必然失败

**实测**（`tools/test_frozen_layout.py`，onefile 与 onedir **都**复现）：

```
_MEIPASS              = ...\Temp\_MEIxxxxxx     （onefile）
pkg___file__          = <_MEIPASS>\bili_danmaku\__init__.py
pkg_dir_listing       = <无法列举 FileNotFoundError>   ← 该目录**不存在**！
MEIPASS/web_dist      = True    ← 前端**确实**被打进去了
exe_dir/_internal/web_dist = True（onedir）
find_dist()           = None    ← ❌ 但函数找不到它
```

**原因**：PyInstaller 把 `bili_danmaku` 打进 **`base_library.zip`**，
所以 `bili_danmaku.__file__` 是**合成路径**，磁盘上并无该目录；
而 `find_dist()` 的候选 1 是 `Path(__file__).parent.parent / "web_dist"`
（= `<包目录>/web_dist`）→ 必然不存在。候选 2 是 `cwd/web/dist`、候选 3 是源码树，
在冻结分发场景下都不会命中。

**后果**：exe 起来后**面板显示降级页**，而不是真正的前端。
**修法**：在 `find_dist()` 增加**冻结分支**，把 `sys._MEIPASS` 作为首选候选：

```python
if getattr(sys, "frozen", False):
    candidates.insert(0, Path(getattr(sys, "_MEIPASS", "")) / "web_dist")
```

打包时的 `--add-data "<web/dist>;web_dist"` 已经把前端放到 `_MEIPASS/web_dist`，
所以**只需改 `find_dist()`**，打包参数不用动。**这个修复要先于 exe 开发完成，并补一条冻结路径的用例。**

#### 5.13.4 启动面板与托盘菜单（需求方已确认内容）

* **启动面板**（tkinter，约 480×380 固定窗口）：图标 + 名称/版本；
  状态区（运行中/已停止、地址、运行时长、房间号、收包/入库）；
  按钮：打开面板 / 复制链接 / 启动采集 / 停止采集 / 重启 / 打开数据目录；
  「高级设置」**默认折叠**（房间号、落盘格式、端口、输出目录）；右下「退出」。
  - 关窗口 = **最小化到托盘**（不是退出），首次关闭弹气泡提示。
  - 每 2 秒刷新状态，**直接读同进程的 `hub`**（不发 HTTP）。
  - 启动采集前用 `rooms.py` **校验房间号**，识别不出就报错、不猜。
  - 端口被占用时复用 `bind_port` 的自适应并提示改用了哪个端口。
* **托盘右键菜单**（动态构建）：状态行（置灰）+ 地址行（置灰）／打开面板·复制链接／
  停止采集（未运行时换成启动采集）·重启面板／显示启动面板·查看日志·打开数据目录／
  退出（停止采集并退出）。左键单击 = 显示启动面板，双击 = 打开 Web 面板。

#### 5.13.5 已实现（这一节记录**已经做出来的东西**）

| 模块 | 作用 |
|------|------|
| `src/bili_danmaku/launcher.py` | **PanelController**：在**本进程后台线程**托管 uvicorn；`start/stop/restart/status/add_room`。停止走 `should_exit` → lifespan `panel.stop()` → 各房间 `bus.stop()` **flush 落盘** |
| `src/bili_danmaku/gui/_win32.py` | 零依赖托盘：`Shell_NotifyIconW` / `CreatePopupMenu` / `TrackPopupMenu` / 消息泵。**不引入 pystray**（已卸载） |
| `src/bili_danmaku/gui/panel_window.py` | tkinter 启动面板（状态区 + 链接入口 + 按钮 + 折叠式高级设置） |
| `src/bili_danmaku/gui/tray.py` | 把上面三者组装成托盘应用；`build_menu_items()` 是**纯函数**，可离线单测 |
| `cli.py verify` | 冻结自检（`--json-out` 供 windowed 版取结果） |
| `cli.py tray` | 托盘入口；`--no-open`（不开浏览器）/ `--no-start`（只起托盘、不启动采集） |
| exe 无子命令 | **`sys.frozen` 时 = 启动托盘**（双击语义）；非冻结时打印帮助 |
| `tools/make_icon.py` + `assets/app.ico` | 源图 → 九档多尺寸 ico（exe 图标与托盘图标共用） |
| `tools/build_exe.py` | 一键构建（**默认 `--windowed`**，`--console` 供调试）+ 自动冻结自检 |

**实测产物**：`dist\exe\B站弹幕提取姬.exe`，**26.9 MB**，窗口版（双击不弹黑框），
启动面板与托盘图标均正常；`verify` 报告 `served_real_frontend: True` 并**自行退出**（约 4.8 s）。

**实测的启动行为**（`tools/smoke_exe_doubleclick.py`，A/B 两种模式都通过）：

| 启动方式 | 结果 |
|----------|------|
| **不带参数（= 双击）** | 托盘常驻 + 启动面板显示 + 托盘图标挂上；**面板未监听**（按设计不自动采集）✅ |
| `tray --rooms 804456 --no-open` | 面板在 8787 监听、`frontend_built=True`、`content_only=True`；启动面板显示 ✅ |
| 两者共同 | **无 python 子进程、无多余 conhost** ⇒ §5.12 的终端连坐缺陷在新入口上**消失** ✅ |

#### 5.13.6 实现期踩到的坑（已修，别重复踩）

1. **`auto_start` 与 `open_browser` 不能合并**：曾经把两者绑在一起，于是
   `tray --rooms X --no-open` 因为"不开浏览器"而**连面板都不启动** ——
   进程活着、窗口在、但 `127.0.0.1:8787` 没人监听。现已拆成两个独立开关
   （`--no-start` 只控制要不要起采集）。
2. **PyInstaller 入口不能用 `cli.py`**：会被当"顶层脚本"执行，
   `from . import __version__` 报 `attempted relative import with no known parent package`。
   `build_exe.py` 生成**绝对导入**的引导脚本。
3. **`--windowed` 下 `sys.stdout/stderr` 是 `None`**：任何 `print()` 都会崩。
   引导脚本把 None 换成 **`io.StringIO` 黑洞**（**不要**重定向到日志文件 ——
   实测那样会把 uvicorn 的 INFO 行混进日志且写成 GBK 乱码）；
   可观测性靠 `setup_logging()` 写的 `.runtime/web.log` 与 `verify --json-out`。
   **控制台代码页是 GBK 还会二次致命**：`click.echo("✅ …")` 抛
   `UnicodeEncodeError` 后进程直接带异常退出，所以 `verify` 的输出走
   `cli.safe_echo()`（逐字符降级成 `?`，中文照常保留）。
4. **托盘窗口是"消息专用窗口"（`HWND_MESSAGE`）**，**不会**出现在 `EnumWindows` 里 ——
   判断"托盘有没有挂上"要用 `FindWindowExW(HWND_MESSAGE, ...)` 或 `TrayIcon.is_visible()`。
5. **`bind_port(host, 0)` 曾返回错误端口**（实测返回 `1`）：老实现把 0 也丢进扫描列表，
   `bind((host,0))` 成功却返回了迭代值。已改为返回值一律取自 `getsockname()`，并补了 4 条用例。
6. **`PanelService.add_room` 是协程**且状态绑定在服务线程的 loop 上：
   必须 `asyncio.run_coroutine_threadsafe` 丢回那个 loop，不能另开 `asyncio.run`。

#### 5.13.7 需求方实测反馈后的修复与改进（2026-10-02）

需求方试用托盘/exe 后报了一组问题，全部已修/已做：

| # | 反馈 | 结论与落点 |
|---|------|-----------|
| 1 | **退出时 `.runtime/web.log` 没有内容** | 🐞 **真 bug，已修**（见 §5.13.8） |
| 2 | CLI `web start` 的终端连坐缺陷 | ✅ **已修**（§5.12 的 `child_python()`） |
| 3 | `web start/stop/status` 与托盘并存的语义 | ✅ **已定并实现**（见 §5.13.9） |
| 4 | 发言 top 统计应默认开启 | ⚠️ **实测它本来就默认开着**；用户看到的是**误导性提示**，已修文案（见 §5.13.10） |
| 5 | 弹幕区发言者 id 默认全显示 + 加隐藏开关 | ✅ 已做（`StreamList.vue` + `preferences.js`） |
| 6 | 时间轴时长增加 1min / 3min | ✅ 已做（`DensityBar.vue` 的 `BUCKET_CHOICES` → 60/180/300/600/900） |

#### 5.13.8 🐞 修复：exe 退出后 web.log 为空

**根因**：`setup_logging()` 只挂 `StreamHandler(sys.stderr)`，而 `--windowed` 打包的
exe **没有 stderr**（`sys.stderr is None`）⇒ 日志**全部丢弃**。托盘因此完全没有可排查的痕迹。

**修法**（`utils.setup_logging(level, *, log_file=None)`）：

1. 额外挂一个 **UTF-8 的 `FileHandler`**（父目录自动创建；写不了时**退回 stderr 并提示一次**，不崩）；
2. `PanelController._default_log_file()`：**冻结运行或没有可用 stderr 时自动**落到
   `.runtime/web.log`（不依赖调用方记得传参）；
3. **uvicorn 自己的 logger 也要接管**：`uvicorn.error`/`uvicorn.access` 是
   `propagate=False` 且自带 handler 的独立 logger，**不会**经过根 logger 的
   file handler ⇒ 通过 `uvicorn.Config(log_config=...)` 让它写同一份文件。
   否则 `Started server process` / `Application shutdown complete` 这些关键行仍然丢失。

**实测验收**（`tools/verify_exe_log.py`，用**真的 exe**）：清空日志 → 启动 →
日志 **1453 字节 / 15 行**，含项目日志（`面板就绪`、`开始采集房间`、`SQLite 输出`）
与 uvicorn 日志（`Started server process`），中文无乱码。

#### 5.13.9 并存语义：`web start/stop/status` ↔ 托盘

**决策**：**两者互不干扰，各自独立；谁都不自动去动对方的进程。**

| 项 | 语义 |
|----|------|
| 状态文件 | `.runtime/web.json` **归 CLI 管**。托盘与 `verify` **刻意不写**它 —— 否则 `web stop/status` 会把一个 GUI 托盘当成"后台面板"，甚至去 kill 它 |
| 端口 | 各自 `bind_port` 自适应。托盘默认也用 8787，与 CLI 相同 |
| 冲突处理 | `web start` 发现**请求端口被占用**时**明确提示**（"可能是托盘应用…"）再顺延，**绝不自动停止**别人的进程；用户可用托盘菜单操作托盘的面板 |
| `web stop` | 只停状态文件里那个 pid（永远是 CLI 自己 spawn 的子进程），不会误伤托盘 |

**已知且可接受的限制**：托盘占着 8787 时 `web start` 会顺延到 8788，
于是**同时存在两个面板**。这是刻意的 —— 隐私决策 DEC-4 是"仅本机"，
多一个回环端口不改变边界；而"一个全局单例锁"会带来托盘被 CLI 抢占/杀掉的更大风险。
提示文案已经把这个情况说清楚（实测：`端口 8787 已被占用…将顺延到下一个可用端口`）。

#### 5.13.10 「发言 TOP」的提示曾误导（已修）

需求方原话："发言top统计在添加房间开始采集时默认开启，无需手动启动。"
**实测结论：它本来就默认开着** ——

* 默认 `sinks.types = ["jsonl","csv","sqlite"]`（sqlite 默认就启用）；
* `/api/stats?window=0`（面板默认累计口径）直接返回 `top_senders` 10 条；
* 右栏「发言 TOP」无需任何手动开关。

真正的缺陷是**前端提示文案**：`StatsPanel.vue` 用 `source === 'memory'` 判断
"sqlite sink 没开"，但**累计口径下 `source` 恒为 `'memory'`**（那描述的是
"条数/速率"来自 hub 内存计数），而发言榜其实来自 SQLite 查询。于是**全新库**上
会显示「**启用 sqlite sink 后才有发言榜**」—— sqlite 明明开着，用户照做也没用。

**修法**：把判据换成**后端给的事实**，并抽成可单测的纯函数
`web/src/components/statsHint.js`：

| 后端字段 | 含义 | 提示 |
|----------|------|------|
| 有 `top_senders_scope` | 真的查过库，只是没数据 | 「库里还没有可聚合的发言（开始采集后就会出现）」 |
| `top_senders_error = '数据库尚未建立'` + `msgs_total > 0` | 收到弹幕却没建库 | 「收到弹幕但采集库还没建立，检查数据目录是否可写」 |
| `top_senders_error = '数据库尚未建立'` + `msgs_total == 0` | 全新安装，正常 | **不提示**（避免噪音） |
| `top_senders_error = 'sqlite sink 未启用'` | 真的没开 | 「未启用 sqlite sink（在配置的 sinks.types 里打开）」 |

后端相应地在 `_open_store()` 失败时区分这两种原因填入 `top_senders_error`。

> 📌 **教训**：这里第一版「修复」我写成了"看有没有 `top_senders_scope` 键"，
> 但全新库上后端**一个键都不带**，于是又给出「读不到采集库（检查 sqlite）」——
> 换个说法的同一个误导。**是复核工具本身救了这个错**：`tools/check_top_hint.py`
> 原本手抄了一份前端字符串，我把改成**直接 import 真的 `statsHint.js`** 之后，
> 它立刻报出真实行为与预期不符。**验证脚本绝不能手抄被测逻辑。**

#### 5.13.11 前端改进（需求方要求，2026-10-02 第二轮）

| 改进 | 落点 | 备注 |
|------|------|------|
| 弹幕区**发言者昵称**默认显示 + 隐藏开关 | `StreamList.vue`（`showName` prop）+ `FilterBar.vue`（开关，放在**「浏览历史」那一栏的最右侧**）+ `App.vue`（持有状态并写 `localStorage`） | ⚠️ 上一轮做成了 **uid** 开关，**功能做错了** —— 需求方要的是**昵称**。已改正：uid 那套连同 `formatUid()` 一起删掉，偏好键改为 `bili-danmaku.show-name`（默认显示）。状态上提到 App.vue 是因为**开关的位置在另一个组件里**，各存一份必然不同步 |
| 时间轴粒度增加 **1min / 3min** | `DensityBar.vue` 的 `BUCKET_CHOICES` → 60/180/300/600/900 | 后端 `bucket_s` 本来就允许 1–86400，**后端未改** |
| 时间轴**横向滚动** | `DensityBar.vue`：`> SCROLL_BARS(600)` 格时给每格 14px 下限、容器 `overflow-x:auto`；`onWheel` 把竖直滚轮当水平位移 | 只在**真能滚**时 `preventDefault`（滚到两端就放行），否则用户会以为页面卡住。与 `DENSE_BARS(400)` 的分工：≤400 铺满 / 400–600 压扁 / >600 滚动 |
| 时间轴悬停与概要**去噪** | 同上：悬停改两行 `13:10:00–13:15:00` + `114 条`（原来只给起点）；概要行从 6 段砍到 3 段 | 需求方反馈"移上去信息较为混乱"。"平均条数/格"删掉 —— 与总量是同一个信息的两种说法 |

> ⚠️ **一处指认偏差（已向需求方说明）**：需求方提到 `div data-v-a5f741b3`，
> 实测该 hash 属于 **`FilterBar.vue`**（`.badge` / `.session-select` / `.hint`），
> 而 `StreamList.vue` 的 hash 是 `data-v-2aed8395`。现在开关就放在 `FilterBar` 里
> （即需求方指的那条栏的最右侧），用 `margin-left:auto` 推到行尾。

#### 5.13.12 时间轴只统计**本次启动采集**之后的数据

**需求方判断**："时间轴上移到鼠标显示的信息较为混乱，推断时间轴的统计可能存在问题"。
**这个推断是对的**，根因有两条（都是实测确认）：

1. 前端**根本没有把"本次启动时刻"传下来**，后端于是按全库画轴；
2. **`store.density()` 的 `range` 取的是库里该房间的全部历史**（`time_range()`），
   与请求里的 `since`/`until` **无关** —— 只过滤桶、不过滤 range。
   所以一次 10 分钟的采集，时间轴会横跨好几天（实测某房间 **264 小时**），
   每格被压成看不见的细线。

**修法**：

* 前端：`useHistory.setStartTs()` 接收 `/api/stats` 的 `cumulative.started_at`
  （= `hub.started_at`），`loadDensity()` 把它当 `since`；浏览历史时再往左收窄到窗口起点。
  只在**累计口径**下这么做 —— 滑动窗口口径下 `window_started_at` 是窗口起点，
  拿它当轴的下界会把轴缩成窗口长度，反而看不到趋势。
* 后端（**真 bug**）：`range` 改为服从 `since`/`until`。不修就会出现
  "概要说跨了 264 小时、但一根柱子都没有"这种自相矛盾的样子。
  注意**不能**只算全量再裁剪 —— "窗口起点"要的是**范围内第一条数据**的时间，
  不是 `max(since, lo)`。

#### 5.13.13 导出：统计 CSV（多表）+ 新文件名格式

**需求**：① 每时段弹幕量统计与弹幕统计**合在同一 CSV**，单独新建表即可；
② 文件名 `<主播名字>_<房间号>_<日期>_<当天导出编号>.csv`。

| 部分 | 落点 | 说明 |
|------|------|------|
| 统计表拼装 | 🆕 `src/bili_danmaku/export_stats.py` | **纯函数** `build_stats_csv()`：4 张表（概要 / 类型分布 / **每时段弹幕量** / 发言 TOP），每表前一行 `# 表名`，表间空一行 |
| 端点 | `GET /api/stats/export` | 回 JSON（含 `csv` 文本 + `anchor_name` / `room_id` / `bucket_s`）。**不返回文件流** —— 文件名要带"当天导出编号"，那只有前端能维护 |
| 默认格宽 | `DEFAULT_BUCKET_S = 300` | 需求："若没有选择默认按照 5min 格式导出" |
| 代表房间 | `store.count_by_room()` + `_busiest_room()` | "全部房间"范围没有房间号，取数据量最大的那个（否则文件名失去信息量） |
| 文件名 | `web/src/preferences.js` | `buildExportFilename()` + `nextExportSeq()`（按**本地日期**分桶，跨天从 1 重数）；各段做**文件名清洗** —— 主播昵称带 `/` 会让"文件名"变成路径 |
| 下载 | `App.vue::exportStats()` | fetch + Blob + `download`，并加 **UTF-8 BOM**（中文 Excel 只看文件头，不修就是乱码 —— 即缺陷 D19 同一原因） |
| 入口 | `TopBar.vue` 新增「导出统计」按钮 | 与既有「导出 CSV」（原始事件逐条流式下载）**并存**，两者用途不同 |

> 📌 **两个导出不是替代关系**：`/api/export` = "把原始数据给我"（流式，数据量大）；
> `/api/stats/export` = "这段时间发生了什么"（聚合报告）。

#### 5.13.13b 导出升级：CSV → **多 sheet xlsx**（优化项 ④）

**需求**：优化统计导出，允许引入新依赖（已确认可接受）。

| 部分 | 落点 | 说明 |
|------|------|------|
| 表格定义 | `export_stats.StatsTable` + `build_stats_tables()` | CSV 与 xlsx **共用同一份表定义**（表名/表头/行）。分头写两份是这类功能的经典腐烂点：改一处忘另一处，两个导出慢慢就不一致了 |
| xlsx 组装 | `export_stats.build_stats_xlsx()` | 每张表一个 sheet，**表名截断到 31 字符**（Excel 硬限制，`SHEET_NAME_LIMIT`）；首行加粗、`freeze_panes="A2"` 冻结表头。**没装 openpyxl 返回 `None`**，调用方回退 CSV |
| 端点 | `GET /api/stats/export?format=xlsx` | 枚举参数（打错给 422）。xlsx 是二进制而这里必须回 JSON（文件名归前端），所以 **base64** 塞进 `xlsx` 字段 |
| 降级 | 同一次请求**照旧回 `csv`** | ① 前端拿不到 xlsx 时不用再请求一次；② openpyxl 缺失只让 `xlsx` 为 `None`，**不会让整个导出 409** —— 导出是用户明确点出来的动作，不该因为少个可选依赖就失败 |
| 前端 | `App.vue::exportStats()` | 固定带 `format=xlsx`；看 `payload.xlsx` 有没有值决定存 `.xlsx` 还是退回 `.csv`（**静默**，不打扰用户） |
| ⚠️ 坑 | `base64ToBytes()` | `Uint8Array.from(atob(s))` 是**错的**（按字符码点截断，>0x7F 的字节全毁，而 xlsx 是 zip 必然坏）；必须逐字符 `charCodeAt` |
| ⚠️ 坑 | `saveBinary()` 与 `saveBlob()` 分开 | xlsx **不能**加 BOM（前两字节得是 zip 魔数 `PK`）。测试就断言这个 |
| 打包 | `tools/build_exe.py` | **openpyxl 不能排除**（`EXCLUDES` 里没有它）。排掉的话 exe 里统计导出就只剩 CSV 了 —— 这类"只在冻结版才失效"的功能最容易漏测 |

#### 5.13.14 图标更换（exe + 托盘）

**需求方指定**：源图换成 `pics/B站弹幕提取姬_团子形象2.png`（1060×1034，RGBA，四角全透明）。

* 改 `tools/make_icon.py` 的 `DEFAULT_SOURCE` → 重新生成 `assets/app.ico`（9 档尺寸）
  → 重新打包 exe。**exe 图标与托盘图标共用这一份 ico**，所以一次替换两处都生效。
* **验收方式**（`tools/verify_exe_icon.py`）：直接从 exe 的 **PE 资源段**把
  `RT_GROUP_ICON` / `RT_ICON` 抽出来重拼成 ico，与 `assets/app.ico`
  **逐像素比对 = 0 差异**，与**旧**图对比差异率 95.52%（确认确实换了）。
  —— 刻意不看构建日志：日志只能证明 PyInstaller **尝试**写图标，
  证明不了写进去的是哪张图（换图时最容易犯"改了源图但没重新生成 ico"的错）。

#### 5.13.15 昵称显示 → **匿名化**开关（上一轮做错了功能）

| 轮次 | 需求方要的 | 实际做的 |
|------|-----------|----------|
| 第一轮 | （未提） | ❌ 做了 **uid** 开关（需求方从没要过） |
| 第二轮 | 昵称**隐藏**开关 | ✅ 改成"显示/隐藏昵称" |
| 第三轮 | 改成**匿名化**开关，默认不匿名，效果 = 只留首字符 + `*` 掩盖 | ✅ 已按此实现 |

**落点**：`FilterBar.vue` 的「浏览历史」那一栏**最右侧**一个 `匿名昵称` 复选框；
`format.maskName()` 负责掩盖；偏好键 `bili-danmaku.anonymize-name`（**默认 false**）。
`formatUid()` 与那套 uid 逻辑已删除（不留死代码）。

`maskName()` 的两个细节：
* 用 `Array.from()` 而不是 `name[0]` —— 昵称可能有 emoji（代理对），
  `name[0]` 会把一个 emoji 拆成半个乱码；
* 掩盖长度 = `min(原长-1, 8) + 3`：**不按原长给足星号**，避免"数星号猜出原长"。

#### 5.13.16 「弹幕区仍看到 `***`」的核实结论（**不是本程序的问题**）

需求方反馈"弹幕滚动区仍会有时将发言者的昵称匿名，只显示昵称第一个字符后面全是 `***`"。
**实测核实**（`tools/probe_masked_names.py` + `tools/probe_login_state.py`）：

| 检查 | 结果 |
|------|------|
| 库里 `uname` 含 `***` 的行数 | **0 / 18193** |
| 库里 `text` 含 `***` 的行数 | 0 |
| `danmaku` 类型缺 uid 的行数 | **0 / 18104** |
| `general.anonymize`（本程序自己的哈希脱敏） | `False`（而且它产出 `u_xxxx`，**不会**产生 `***`） |
| 登录态（权威判据 `nav.isLogin`） | **`True`**，账号 `歪比巴布325` |

**结论**：`***` 是 **B 站平台下发的**（未登录时会返回 `宅***` 这种打码昵称），
**本地无法还原** —— 改变显示逻辑不可能把 `***` 变回真名（它的真名根本不在数据里）。
当前库里一条都没有，说明**登录态采集时昵称本来就是全的**。

因此这一条**没有可改的代码**；如果再次看到 `***`，排查顺序是：
① 面板启动时凭据有没有加载上（`bili-danmaku check-auth`）；
② `.secrets/cookies.json` 是否过期；③ 是否在未登录的机器上跑。

#### 5.13.17 启动面板的扫码登录 + 凭证失效提示

**需求**："在启动面板添加扫码登录获取用户凭证这一功能，当凭证失效时，
在启动面板用较为明显的信息栏提示用户登录。"

**背景**：未登录时 B 站会把部分昵称打码成 `宅***`（见 §5.13.16），
用户会以为程序坏了 —— 所以这一条要"提示醒目 + 一步修好"。

| 部分 | 落点 | 说明 |
|------|------|------|
| 提示条 | `gui/panel_window.py::_build_login_banner()` | 放在**最上面**（在面板状态区之上，实测顺序 login=1 < status=2），所以很醒目 |
| 配色判定 | `gui/panel_window.py::describe_login()` | **纯函数**，可离线单测。见下方"三档语义" |
| 离线判断 | `PanelController.local_credential_status()` | 只看本地凭据文件（有 SESSDATA / source），**不联网** —— 首屏立刻有说法 |
| 权威判据 | `PanelController.check_login()` | 打一次 `nav`，只有 `isLogin` 说了算（本地有 SESSDATA **不等于**它还有效） |
| 扫码流程 | `credentials.request_qr()` / `poll_qr_once()` / `classify_qr_status()` / `render_qr_png()` | 从 `qr_login()` 里**拆出来的可复用件**；GUI 用它自己驱动，不再一路阻塞 |
| 后台驱动 | `launcher.QrLoginSession` | 小状态机：后台线程申请码 + 轮询，主线程 `poll()` 消费事件（tkinter 不阻塞） |
| 复查频率 | `LOGIN_RECHECK_S = 300` | 每 5 分钟联网复查一次（凭证会过期/被踢），不会把 `nav` 打太频繁 |

**三档配色语义**（这里踩过一次真坑，所以写清楚）：

| 级别 | 条件 | 颜色 |
|------|------|------|
| `ok` | 联网确认已登录 | 绿（显示昵称） |
| `pending` | 本地有凭据、还没校验 | 绿（"正在校验…"，别吓人） |
| `alert` | **有**凭据且**联网确认**已失效 | **红** + "请重新扫码登录" |
| `warn` | 没凭据，**或**有凭据但**没能**校验（网络问题） | 黄 |

> ⚠️ **踩过的坑**：第一版把"有凭据 + 有错误消息"一律当红色，于是**网络不通**
> 也显示"凭据失效，请重新扫码" —— 把用户赶去白折腾一趟。
> 现在只有"联网**确认**过失效"才是红色。这个判定已抽成 `describe_login()`
> 并用例守住（`tests/test_login.py`）。

**两类原因必须分开**（`local_credential_status` 的说明里也写了）：
网络异常**不等于**凭据失效 —— 前者是 `warn`（黄色、说明是网络问题），后者才是 `alert`。

#### 5.13.17b 二维码改为**独立弹窗**（**这一版修的是"扫不出来"**）

**需求**："重新设计为点击登录验证后，单独弹出窗口来显示完整二维码来进行登录，
获取到凭证后可以自动关闭二维码弹窗，要求二维码弹窗大小一定能完整显示二维码。"

**为什么要改**（不是排版偏好，是功能坏了）：二维码原来塞在启动面板里
（`resizable(False, False)`、宽度固定约 480px），而二维码是 `box_size=8` 渲染的，
内容长一点就 400px 上下 —— 被裁掉一角。而**二维码缺一个角就扫不出来**，
用户看到的只是"扫了没反应"。实测本机真二维码 **424px**、面板内容区装不下。

| 部分 | 落点 | 说明 |
|------|------|------|
| 弹窗 | 🆕 `gui/qr_window.py::QrLoginWindow` | 独立 `Toplevel`：标题 + 白底二维码 + 状态行 + 「取消登录」；`-topmost` 且**居中** |
| 尺寸 | `qr_window.QrLoginWindow._measure()` / `qr_area_px()`（**纯函数**） | 按 `winfo_reqwidth()`（已含 `tk scaling`）算出"控件 + 两侧留白 + 状态行/按钮行"，再 `geometry` + `minsize` 按下去 |
| 图片尺寸 | 🆕 `credentials.png_size()`（**纯函数**） | 只查 PNG 文件头（IHDR）拿宽高，**不解码图片** —— 窗口创建那一刻不需要开销 |
| 结束原因 | `QrLoginWindow.outcome`（`""`/`ok`/`cancelled`/`failed`） | 面板靠它区分"成功自动关"与"用户取消"。判断只在弹窗这一处做，面板不重复维护"成功了吗" |
| 面板 | `panel_window.py::start_qr_login()` / `_on_qr_window_closed()` / `shutdown()` | 面板里**不再有**二维码画布；点按钮 → 开会话 + 弹窗；窗口关掉 → 恢复按钮 + 按 outcome 收尾 |

**四条硬约束**（都踩过，改动前先读）：

1. **不能设 `transient(master)`**。Tk 会把 transient 子窗口跟着主窗口一起隐藏 ——
   而"关闭启动面板 = 最小化到托盘"是本程序常态，那一刻二维码正需要留在屏幕上
   给手机扫，跟主窗口绑在一起正好帮倒忙。实测：主窗口 `withdraw()` 时 transient
   的 Toplevel 停在 `state=withdrawn`、`winfo_width()==1`，**连 `deiconify()`
   都拉不起来**（弹窗根本不出现）。
2. **尺寸是算出来的，不是自适应出来的**。`pack` 自适应在高 DPI 下会算进 `tk scaling`，
   可能小于放大后的图片；所以量完真实请求尺寸后直接 `geometry` + `minsize`。
   本机实测：图片 424px → 窗口 585×585（Tk 单位），Windows 侧客户区 1000×1000（物理像素）。
3. **读窗口尺寸前要先 `update()`**。Tk 的 `geometry()` 请求不刷新就落不到
   `winfo_width()` 上，会读到默认的 `1x1` —— 看起来像"窗口只有 1 像素"（实测踩到）。
4. **不要用非 DPI 感知的进程去量窗口尺寸**。`GetClientRect` 会被 Windows 按系统缩放
   虚拟化放大（本机 `tk scaling` 1.3346，读数直接差一个缩放）。
   `tools/verify_qr_window_desktop.py` 里先调 `SetProcessDpiAwarenessContext`。

**验证**（三条互补，缺一条就盖不住这次的缺陷）：

* `tests/test_gui.py` —— `png_size()`（含**真 PNG**，不是只拼字节）、
  `qr_area_px()` 的"永远装得下"承诺与上限、以及弹窗事件处理
  （`ok` 自动关并记 outcome、等待/已扫码**不**关、失败显示原因后关、
  已成功后再取消**不能**覆盖结论）。弹窗用 `object.__new__` 造，
  **不走 `__init__`**，所以无桌面也能测。
* `tools/verify_login_banner.py` —— 真 Tk 窗口：弹出的是**独立窗口**、
  面板里不再有画布、图片控件 ≥ 图片、窗口内容区 ≥ 控件 + 留白、
  取消后按钮恢复且引用清干净、成功路径自动关窗并报告成功。
* `tools/verify_qr_window_desktop.py` —— **真实桌面**：起托盘、自动点一下
  「扫码登录」、用 `EnumWindows` 找到那个窗口并量客户区。
  这一条是唯一能证明"窗口真的出现在屏幕上"的（前两条读的都是 Tk 自报的数字）。

#### 5.13.18 存储位置：可选目录 + 占用显示

**需求**："更新出用户可自选存储位置这一功能，该功能放到启动面板中；
目前暂不涉及在程序内完成删除这一操作"，并要求**显示当前目录占用**。

| 部分 | 落点 | 说明 |
|------|------|------|
| 占用统计 | `launcher.scan_storage()` / `format_bytes()` (纯函数) | **只读**遍历 `{out_dir}` 的顶层文件，按后缀汇总（`.db` / `.jsonl` / `.csv`…）降序。`writable` 用"真写一个探针文件"实测 —— Windows 上权限位与实际写入经常不一致 |
| 目录切换 | `PanelController.set_out_dir()` / `current_out_dir()` | 自动建目录、校验可写；`current_out_dir()` **配置优先于选项**（否则界面显示的目录与实际写盘的对不上） |
| 界面 | `gui/panel_window.py::_build_storage()`（高级设置里） | 路径可点（直接打开目录）+ 占用分行 + 「更改…」（系统目录框）+「刷新占用」 |

**三条刻意定下的语义**（改动前先读）：

1. **不迁移、不删除**：切换目录只改"之后往哪写"，旧数据原样留在原地
   （实测断言：切换后旧 `danmaku.db` 字节不变，新目录起始为空）。
2. **运行中切换要明确说"重启采集后生效"**，并点明"当前这次仍写入原目录" ——
   悄悄"改成功了但没生效"是最糟的体验。
3. **占用按需刷新**，不进 2 秒一次的 `refresh()`：扫 138 MB / 66 文件要几十毫秒，
   而目录里可能有几十万文件（有 `DISK_SCAN_LIMIT=20000` 上限 + `truncated` 如实标记）。
   只在**采集起停**时自动刷一次，其余靠手动点。

> ⚠️ **界面上"历史仍来自旧目录"这件事的标注**（本轮已补）：当前实现是
> `store.py` 读哪个库取决于配置里的 `out_dir`，所以**重启后**历史浏览会切到新库，
> 而旧库数据仍在磁盘上（面板暂时看不到）。要看到旧数据需把目录切回去。

补标注时踩到的两个点（改动前先读）：

* 提示**不能**写在 `choose_out_dir()` 里"说一句就完事"：那句之后紧接着就是一次
  占用刷新，而刷新走 `_apply_storage()` 会重设这个标签 —— 提示会在一两百毫秒后
  被自己抹掉。所以判断依据做成**可反复查询的事实** `PanelController.storage_switched()`
  （比较 `options.out_dir` 与启动时记下的 `_initial_out_dir`），
  由渲染路径稳定输出。
* 比较**必须先 `abspath` 归一化**：exe 双击启动时 `out_dir` 是相对路径 `"data"`，
  而 `set_out_dir()` 存的是绝对路径。直接比字符串会把"选回当前目录"误判成换过。

#### 5.13.18b 空房间时的引导（优化项 ⑦）

**需求**：房间列表为空时提示用户去选择房间。

| 部分 | 落点 | 说明 |
|------|------|------|
| 展示 | 🆕 `web/src/components/EmptyRoomsBanner.vue` | `roomCount === 0` 才出现；房间一加上就自己消失 |
| 位置 | `App.vue`，健康横幅**之下**、SC 横幅之上 | 健康横幅讲的是"已有房间出了问题"，一个房间都没有时它本来就该是 ok/不显示 |
| 动作 | 「去添加房间」→ `RoomPanel.focusInput()` | 把光标送到**链接**输入框（需求方给的用法就是粘直播间链接；链接框也认纯数字） |

三条刻意的选择：

1. **不写"暂无数据"**，只写下一步做什么。空面板本身已经说明没数据了。
2. **第一句必须是"程序已经在正常运行"** —— 否则用户会误判成没启动成功，
   然后去点停止/重开（那是本需求真正要避免的损失）。
3. **不提登录**。web 面板上没有扫码入口（扫码要图形界面，只有托盘启动面板能做），
   在一个做不到的界面上提"去登录"等于把用户带进死胡同。没凭证的问题由
   `HealthBanner` 在**采集真的失败之后**报出来，那时再说才准确。
4. **不给关闭按钮**（与 `HealthBanner` 相反）：它不是提示噪音，而是当前状态下
   唯一有意义的下一步；关掉之后面板又回到完全没有指引的样子。

验证：`tools/verify_empty_rooms.py` —— 用真 Vue SSR 渲三遍（0/1/3 个房间），
断言"0 个时在、≥1 个时消失"，并钉住上面第 2、3 条文案（源码里 grep 不出来，
必须从**渲染结果**里查）。

#### 5.13.19 运行中凭据/风控失效提示

**需求**：凭据失效时要**看得见、能照做**。启动面板的登录条覆盖了"启动时"，
这里补"运行中"那一半。

| 部分 | 落点 | 说明 |
|------|------|------|
| 失败原因 | `collector.CollectorStats.last_error` / `last_error_kind` | 进 `snapshot()` 与 `as_dict()`，面板从快照就能读到 |
| 错误码翻译 | `collector.AUTH_CODE_HINTS` | `-101` → 「登录已失效，请重新扫码登录」（`-101` 就是"未登录/失效"，见 `TECH_DESIGN` 错误码表） |
| 判定 | `analysis.assess_health()`（纯函数） | `alert` / `warn` / `ok` 三档，见下表 |
| 传输 | `GET /api/health` 的 `attention` | 搭车返回，不新开端点（面板本来就在轮询它） |
| 展示 | 🆕 `web/src/components/HealthBanner.vue` | 直接渲染后端给的分级与文案 —— **判断逻辑只有一份**，不会出现前后端说法分裂 |

**分级**（措辞与阈值都在 `assess_health` 里，有用例守着）：

| 级别 | 条件 |
|------|------|
| `alert` | 某房间 `last_error_kind == "auth"`（凭证失效） |
| `warn` | 房间 `PAUSED`（连续认证失败后暂停）/ 重连 ≥8 次 / 网络错误且重连 ≥3 次 |
| `ok` | 其余，含**只重连过 1–2 次**（B 站会主动断连，偶发一次很正常，不该报警） |

**顺带修掉一个真 bug**：原来异常分支写的是

    log.warning("连接失败: %s: %s", type(exc).__name__, exc)

而 `BiliApiError("")` / `TimeoutError()` 的 `str(exc)` 是**空串**（实测），
于是那一行打成 `连接失败: TimeoutError: ` —— 什么都不剩。
现在 `_remember_error()` 用 `fallback + 类型名` 保证永远有内容；
并且**连上之后清掉旧原因**（不清的话一次早已恢复的抖动会永远挂在面板上）。

#### 5.13.20 尚未决定 / 遗留

- **真人手测仍是必需的**（自动化验不了）：托盘右键菜单点击、气泡提示、
  窗口手感、高 DPI 缩放，以及**时间轴横向滚动手感**（滚轮速率是否跟手）。
- 托盘进程内嵌 uvicorn 后，`web.token` / 仅回环等**安全约束照旧适用**（DEC-4 不变）。
- **切换存储目录后，旧目录的历史在面板里看不到**（见 §5.13.18 的警告框）——
  本轮已补标注（启动面板 "历史/统计仍来自原目录"）；"给一个历史库另选的入口"
  仍然**待定**。
- **`_drop_stopwords()` 的副作用**（被剔的词不腾榜位、`total_words` 仍是剔除前口径）
  —— 需求方要求**先搁置，后续再讨论**。
- **`web start` 的 `--no-open` 默认行为** —— 同样先搁置。
- 时间轴的 canvas 版**已落地并保留 DOM 版可回滚**（见 §5.13.22）。

#### 5.13.21 前端组件级测试（优化项 ⑨）

**需求**：引入前端**组件级**测试（此前只有纯逻辑的 `node:test`）。

| 部分 | 落点 | 说明 |
|------|------|------|
| `.vue` 编译 | 🆕 `web/test/vue-loader.mjs` + `register-vue.mjs` | 用 `@vue/compiler-sfc` 以**客户端模式**（`inlineTemplate: true`、`isProd: false`）现编 SFC，`npm test` 通过 `--import` 登记 |
| 挂载 | 🆕 `web/test/helpers/vue.mjs::mountComponent()` | 只用**公开 API**（`createApp` + `h` + `nextTick`），约 60 行 wrapper（`find`/`text`/`classes`/`attributes`/`trigger`/`emitted`） |
| DOM | `jsdom`（**唯一新增依赖**） | 组件测试必须有 `document` |
| 用例 | 🆕 `web/test/{timeline,density}.test.mjs` + `base64.test.mjs` | 纯计算、交互、渲染记录分层 |

**三个必须记住的环境事实**（踩了很久，改动前先读）：

1. **`setupDom()` 必须最先跑，Vue 相关模块只能动态 `import()`**。
   `@vue/runtime-dom` 在**模块求值那一刻**就把 `document` 抓进模块级变量，
   之后再设 `globalThis.document` 没用。ESM 的静态 import 全部先于模块体求值，
   所以测试文件顶部不能静态 import `vue`。
2. **不要用 Vite 的 `ssrLoadModule()` 编译组件**。它是给 SSR 用的：
   `@vitejs/plugin-vue` 按 SSR 模式编译，产物里只有 `ssrRender`、**没有** `render`，
   挂载时报的是完全指不到病根的
   `TypeError: Cannot read properties of undefined (reading 'modules')`
   （真提示是同时打出的 `Component is missing template or render function`）。
   换成自己的 loader 之后还顺带消掉了"SFC 走 Vite、测试走 Node，各拿到一份 Vue"。
3. **不要用 `@vue/test-utils`**。它的 `mount()` 依赖 Vue 的**内部** `currentInstance`，
   在"必须先装 jsdom 再 import Vue"的前提下极易撞上两份 Vue。
   实测装好后又卸掉了 —— `helpers/vue.mjs` 那 60 行公开 API 足够覆盖本项目要断言的东西。

**测试替身的两条要点**：

* `ResizeObserver` 必须由测试控制回调时机：jsdom 没有布局，`clientWidth` 恒为 0，
  而组件在宽度为 0 时**故意不画**。所以 `setBox()` 会顺带触发一次观察者回调
  （真实浏览器里这条路径由 `ResizeObserver` 走）。
* canvas 的 2D 替身要装在 **`HTMLCanvasElement.prototype`** 上、且**在挂载前**：
  组件在 `onMounted` 里就画第一次，挂完再注入替身看不到那一次，
  所有绘制断言都会在"什么都没画"的前提下通过。需要验证降级路径时用
  `restoreCanvasPrototype()` 撤掉。

#### 5.13.22 时间轴改为 canvas 绘制（优化项 ⑩，**保留旧版可回滚**）

**需求**："canvas 绘制时间轴，**保留旧版本以便回滚**"。

| 部分 | 落点 | 说明 |
|------|------|------|
| 共用数值 | 🆕 `web/src/timeline.js` | 高度比例 / P95 峰值 / 焦点下标 / 概要行 / 每格宽度 / 命中测试 / 滚轮位移。**两份渲染实现共用同一份计算**，否则用户切一次渲染方式就会看到不同的数字 |
| 共用表头 | 🆕 `web/src/components/DensityHead.vue` | 概要行 + 五个格宽档位 + 焦点/滚动提示。两份拷贝以后只会修一份 |
| canvas 版 | 🆕 `web/src/components/DensityCanvas.vue` | 默认。只画**可视区**（可滚动时按 `scrollLeft` 裁剪）、按 `devicePixelRatio` 设后备缓冲 |
| DOM 版 | `web/src/components/DensityDom.vue` | **原来的实现，保留**。每格一个真 `<button>`，因此天然键盘可达 |
| 门面 | `web/src/components/DensityBar.vue` | 按偏好选实现；偏好存 `localStorage['bili-danmaku.timeline-render']`，**默认 canvas** |

**回滚不是"代码还在 git 里"**：表头右侧有一个「改用 DOM / 改用 canvas」按钮，
当场切换并记住选择。真出问题时用户一步就能退回旧版，不用等我们发版。

**canvas 版的三个坑**（都在代码注释里，这里列一遍）：

1. `getContext('2d')` 可能是 `null`（jsdom / 极老浏览器）—— 那时**只是不画**，
   交互照常；直接 `ctx.clearRect` 会抛异常把整个面板带崩。
2. 可滚动时 **canvas 不做成全内容宽**：那样画布本身会上万像素。做法是 `track`
   撑出滚动条、canvas 只做可视区宽并 `position: sticky; left: 0` 吸附，
   滚动时按 `scrollLeft` 重画。命中测试的 `x` 要**加上 `scrollLeft`** ——
   这一条算错只在滚动之后复现，最难查（有用例专门钉它）。
3. 只有 canvas 有"内容之外的空白"这个概念：点到死区**不该** emit
   （否则点最右边会莫名跳到最后一格）。DOM 版没有死区（容器宽就是内容宽）。

**验证**：

* `cd web; npm test` → **101 passed**（含 28 项时间轴组件用例）；
* `tools/verify_timeline_canvas.py` —— 真 Vue + jsdom 画一整屏，检查
  "柱数 == 有数据的桶数""柱高随条数单调递增""最矮的非零柱不低于 4%"
  （画布空白的失败模式能立刻抓到）；
* `tools/verify_canvas_pixels.py` —— 真 Chromium（headless + CDP）读回**真像素**。
  jsdom 没有光栅化，前两步只能证明"我们**调用**了哪些绘制指令"，
  而"指令调了、画布却是白的"是另一类失败（尺寸算错、被 CSS 盖住）。
  判定刻意做成双向可证伪：画三条已知颜色的线 → 不同颜色数 ≥ 3；
  **清空之后**必须是 1 种。**不要**改成"看有没有深色像素"——canvas 的透明区域
  在截图里就是深色背景，那种判据永远为真。
  > ⚠️ 这个脚本用 Python 起一个 `node -e` 子进程来发 CDP 消息（借 Node 24 自带的
  > `WebSocket`，免得多引一个依赖）。两条踩过的坑写在代码注释里：① node 侧必须打
  > **真换行**才能让 Python 的 `splitlines()` 切出行来（Python 源码里写 `\\n`
  > 会变成字面量 `\n`，报 `Extra data`）；② 打完要立刻 `process.exit(0)`，
  > 否则兜底的 `setTimeout` 会再打一行 `ERR timeout` 混进 stdout。

---

### 5.14 实时词频统计：已按 A/B/C 落地（**不引入语言模型**）

**需求方反馈**："目前统计的词有一部分不符合日常表达，是否可以接入规模比较小的
语言模型来完成这一任务，对应的可以降低词频统计的更新频率，如 30s 更新一次。"
**决策（需求方 2026-10-02）**：**先做 A/B/C 三个零依赖方向，现阶段不上模型。**

#### 5.14.1 实测：当前产出的是什么词

`tools/probe_wordfreq.py` 在真实库上取样 **4000 条弹幕**（`data/danmaku.db`，
房间含 8432038 / 23438368 等），TOP 40 摘录：

| 词 | 次数 | 发言人数 | 性质 |
|----|------|----------|------|
| 电梯 / 说不出 / 害人精 / 堵门 / 尸横遍野 | 66 / 62 / 41 / 29 / 19 | — | **正常的词**，只是语境是游戏 |
| 军哥 / 周哥 / 布拉德 / 沃夫 | 60 / 18 / 19 / 20 | 49 / 16 / 13 / 14 | **主播/人物名**（有效信号，不是噪声） |
| 哈哈哈 / 哈哈 / 哈哈哈哈 | 65 / 18 / 18 | 30 / 14 / 15 | 语气词，但确实是"大家在聊什么" |
| 呼呼 | 59 | 58 | 叠字语气 |
| fumo / dps | 22 / 18 | 18 / 18 | **游戏/圈内专名**（ASCII） |
| 60 | 18 | 18 | 纯数字 |
| 灯丝 / 小兔 / 八音盒 | 26 / 26 / 17 | 24 / 21 / 16 | 半截词或专名 |

统计：去重词 1389 个，总词 3860 个，TOP40 里"明显可疑"的只有 **5 个**
（叠字 2、纯 ASCII 2、纯数字 1）。

**结论（与需求方直觉的差异）**：当前榜单的问题**主要不是"垃圾词"**，而是：

1. **专名（主播名/游戏名）占比高** —— 这其实是**有效信息**（"大家正在聊谁/聊什么"），
   只是看起来不像"日常表达"；
2. **个别数字/ASCII/叠字**确实是噪声，但占比很低（TOP40 里 5/40）；
3. 真正难看的是**半截词**（如 `灯丝`、`八音盒` 这类被 jieba 切碎或本来就短的专名）。

也就是说：**先把"专名 vs 噪声"这件事说清楚，比直接上模型更重要。**

#### 5.14.2 上小模型前先做三件事（**已实现**，零新增依赖）

| # | 做法 | 落点 |
|---|------|------|
| **A** | **词频榜按"说过的人数"排序**（`rank_by="users"`，可切回 `count`） | `analysis.words_from_buckets(by=...)`；`WordBucket.users` 记录每个词的不同 uid（**有界** `MAX_USERS_PER_WORD=50`），由 `hub.LiveWindow.add()` 在热路径顺带维护。并列时用次数 + 词序兜底，否则榜单每次刷新会"抖" |
| **B** | **自定义停用词**：`[analysis] stopwords_file`（一行一个，`#` 注释），给了就整份替换内置表 | `_drop_stopwords()` 对**已算好的**榜做事后剔除 —— 零额外分词开销（重跑 `word_frequency(messages)` 要把上千条重新切一遍）。代价：被剔的词不腾榜位、`total_words` 仍是剔除前口径，这是刻意的取舍 |
| **C** | **jieba 用户词典**：`[analysis] custom_words` + **当前房间主播昵称自动加入** | `text.add_custom_words()`；进程内去重（同批词不反复提交）、单字与空白跳过、**没有 jieba 时静默降级** |

**新增配置**（`config.example.toml` 的 `[analysis]`）：
`rank_by` / `stopwords_file` / `custom_words`。

> ⚠️ **两个已知取舍**：`hub` 是**按条增量分词**的，所以
> ① 自定义词典在面板启动后再改，只对**之后**进来的弹幕生效；
> ② 自定义停用词只能事后剔除，不能改变计数口径。
> 想彻底生效要重跑 `word_frequency(messages, stopwords=..., custom_words=...)`
> —— 那个函数**已支持**这两个参数，只是当前端点走的是省算路径。

#### 5.14.3 真要上小模型：三种形态与代价

| 形态 | 说明 | 代价 | 隐私 |
|------|------|------|------|
| **① 本地小模型做"词过滤/归并"**（推荐形态） | 每 30s 把**候选词表**（几十个词，不是几千条弹幕）交给模型，让它回答"哪些是噪声/哪些该合并" | 需要一个推理运行时（Ollama / llama.cpp）；`qwen2.5:0.5b` 约 400 MB，CPU 上几十个词的分类约 1–3 s | ✅ 完全不出本机 |
| ② 云端 API | 效果最好、零本地算力 | 需要 key、按量付费、**数据出网** | ❌ 需 **G4 书面隐私决策**（ROADMAP 门槛） |
| ③ 本地"语义聚类"（无 LLM） | 用词向量把"同一件事的不同说法"合并 | 要引入 embedding 模型（又是几百 MB） | ✅ 但收益与 ① 重叠 |

**实测：本机目前没有任何本地推理运行时** ——
`ollama` / `llama-cli` / `llama-server` 均未安装，
`llama_cpp` / `transformers` / `onnxruntime` / `torch` 也都未装。
所以形态 ① 需要**用户先装 Ollama**（一次性几百 MB），而不是打包进 exe
（exe 现在 26.9 MB，塞模型会变成几百 MB）。

**降频（30s 一次）的可行性**：面板现在的分析刷新是 **6s**（`ANALYSIS_INTERVAL_MS`），
词频来自 `hub.LiveWindow` 的**内存片段**（`words_from_buckets` 走"片合并"）。
改成 30s **完全可行**，而且与"模型推理要 1–3 s"是匹配的；
但要注意**它只影响词频**，热点突增/刷屏判定不该跟着降频（那两件是"及时发现"用的）。

#### 5.14.4 若将来仍要上模型（**本阶段不做**）

| 形态 | 说明 | 代价 | 隐私 |
|------|------|------|------|
| **① 本地小模型做"词过滤/归并"** | 每 30s 把**候选词表**（几十个词，不是几千条弹幕）交给模型 | **实测本机没有任何推理运行时**（`ollama`/`llama-cli`/`llama_cpp`/`transformers`/`torch` 全未装），需用户自备 Ollama；`qwen2.5:0.5b` 约 400 MB，CPU 上约 1–3 s | ✅ 不出本机 |
| ② 云端 API | 效果最好、零本地算力 | 需 key、按量付费 | ❌ **数据出网**，需 G4 书面隐私决策，与 `docs/PRIVACY.md` 冲突 |
| ③ 本地 embedding 聚类 | 用词向量合并同一件事的不同说法 | 又是几百 MB | ✅ 但收益与 ① 重叠 |

**降频可行性**：面板分析刷新是 **6s**；词频来自内存片段合并，降到 30s 完全可行，
且正好匹配"模型推理 1–3 s"。但**只该降词频** —— 热点突增/刷屏判定是"及时发现"用的，
跟着降频会失去意义。

---

## 6. 待办与开放问题

### 6.1 下一步（V2 已全部完成）

V2 两项（词云 §5.3、SC §5.5）都已完成。**下一步建议：真机实测与真实样本补齐**：

1. **拿真实 SC 样本验证金额单位**（唯一未验证的假设，见 §5.5 的警告框）。
   做法：`bili-danmaku probe <有大额 SC 的房间> --dump-raw tests/fixtures/capture_sc`，
   或 `bili-danmaku sc <房间号> --follow --min-price 0 --duration 1800`。
2. **补齐 `SEND_GIFT` / `SUPER_CHAT_MESSAGE` / `GUARD_BUY` 的真实夹具**
   （现在只有 `COMBO_SEND`），然后按 HANDOVER §13 的约定加金样本断言。
3. **面板 SC 横幅的真机确认**：需要一次真实 SC 才能看到横幅（离线测不了）。
   日志里 `SLOW_CONSUMER` 之类提示也可能一并验证。
4. **若要把告警做重**（Webhook / 桌面通知 / 常驻服务），需要先定需求：
   详见 §6.4 的"面板是否常驻"。

> 📌 **"值得做的新功能"不写在这里**，写在 [`ROADMAP.md`](ROADMAP.md)（它含优先级、风险与
> 待决策项）。本节只保留"现有功能的待核实项"。两者的分工在本轮明确：**本文写现状，那份写未来**。

### 6.2 已完成 / 不再推迟

- **M4 全部完成**：多房间真实验收（804456 + 7878）、压测、覆盖率、打包、文档，见 §5.1–§5.2。
- **压力测试（原 U5）已完成**：需求方要求"放到初版完全开发之后"，现已用
  `tools/stress.py` 在房间 7878 上跑完，结论与数据见 §5.2。
  若要更大压力，把多个热门房间号一起丢给 `BILI_LIVE_ROOMS` 或调大 `--duration`。
  注意公开的"人气房间列表"接口在登录态下也会返回 `-352` 风控
  （`tools/hot_rooms.py` 试过，未通过），热门房间号最好由需求方直接提供。
- **U3 剩余部分**：`SUPER_CHAT_MESSAGE` 的真实字段结构**仍缺样本**（SC 仍在采集范围内，
  且其金额单位尚未验证）。礼物/上舰已按 DEC-1 排除出采集范围，
  **因此 `SEND_GIFT` / `GUARD_BUY` 不再需要抓样**。

### 6.3 V2 范围与落地情况（需求方已确认：仅两项）

| 项 | 状态 | 说明 |
|----|------|------|
| 弹幕词云 | ✅ 完成 | `bili-danmaku wordcloud`；`text.py` 分词 + 词频；表情占位符单独成榜；`text/json/png/html` 四种输出 |
| SC 汇总/告警 | ✅ 完成 | `bili-danmaku sc`（汇总 + `--follow` 实时告警）；`--min-price` 复用过滤层；面板侧 `ScBanner.vue` 横幅 + 阈值输入 |

**词云的两个已知取舍**（都已写进代码注释与文档）：

1. 表情占位符默认**不进**词语榜（否则榜单被表情刷屏），用 `--include-emotes` 才进；
2. `--format png` 需要 `wordcloud` + 系统中文字体；`html` 输出零依赖、必定能看，
   所以**默认建议用 html**，PNG 作为"要贴到别处"的导出选项。

**SC 的一个未验证假设**：`price` 单位按「元」处理（礼物/上舰按金瓜子 ×1000 换算），
但没拿到真实 SC 样本验证；`--price-unit milli` 是逃生舱。见 §5.5 的警告框。

### 6.4 需求方已拍板的决策（**不要再问、不要擅自改**）

| # | 决策 | 日期/来源 | 影响 |
|---|------|-----------|------|
| **DEC-1** | **礼物（`gift`）与上舰（`guard_buy`）不纳入采集范围** —— 保持"无用户文字即排除"的现状 | 需求方明确声明 | 采集范围冻结为 `danmaku` + `super_chat`；`content_only` 默认 `true` 不变；**代码无需改动**（过滤层本来就这样） |
| DEC-2 | 压测放到初版功能齐全之后再做 | 需求方 | 已完成（M4 用房间 7878，见 §5.2） |
| **DEC-3** | **面板常驻化 + 从面板停止**：`web start/stop/status/logs/restart` 托管后台运行；面板提供「停止面板」按钮，点它**既关面板也停程序，并安全保存已读取的数据** | 需求方 | 新增 `runtime.py` + 仅回环的 `POST /api/admin/shutdown`；停止走 uvicorn 优雅退出 → `panel.stop()` → 每个房间 `bus.stop()` flush 落盘 |
| **DEC-4** | **本项目当前阶段仅供本机使用**（loopback-only） | 需求方 | 不开放局域网/多用户；控制端点**只接受回环来源**，非回环一律 403；`web.host` 保持 `127.0.0.1` |
| **DEC-5** | **不做开机自启**；崩溃时**只保证已有数据保留**，等用户手动 `web start` | 需求方 | 实现里不装服务、不注册计划任务；崩溃后残留的 `.runtime/web.json` 会在下次 `web status/start` 时被**自动识别为陈旧并清理**（否则会永远拒绝启动） |
| **DEC-6** | **新增"托盘 + 启动面板"作为 Web 端主入口，并打包成单文件 exe**；**CLI 降级为调试端**，核心功能优先在 Web 端实现 | 需求方（2026-10-01） | 见 §5.13。托盘用**零依赖 ctypes**（不引入 pystray）；exe 用 **PyInstaller 单文件**；面板**跑在托盘进程内**（不再 spawn 子进程），因此 §5.12 的终端连坐缺陷在**新入口路径上直接消失** |

**DEC-1 的具体含义（避免后人误改）**：

- **不做**：礼物/上舰的入库、统计、面板展示、词云纳入、金额汇总。
  `--min-price` 对礼物/上舰的换算逻辑（`filters.min_price_threshold`）**保留**，
  因为它同时服务于 SC，且这层代码是"类型无关"的正确实现 —— 但**不要**因此把它们放开采集。
- **不要再问**"礼物要不要恢复"。此前文档里反复出现的这句提议已作废。
- **副产品**：`SEND_GIFT` / `GUARD_BUY` 的真实样本**不再需要**（U3 相应缩到只剩 SC）。
- **SC 仍然采集**：DEC-1 只排除礼物与上舰；Super Chat 属于"有用户文字内容"，正常入库。

**DEC-3/4/5 的具体含义**：

- 停止面板有**三条**可控路径：面板按钮、`bili-danmaku web stop`、以及
  兜底强杀 `taskkill /PID <pid> /F`（**会丢队列里未落盘的批次**，日志/输出里会明确标注）。
- **不要**把控制端点放开给非回环来源，也不要为了"局域网方便"把 host 改成 `0.0.0.0`（DEC-4）。
- 崩溃后不需要人工删状态文件：`live_runtime()` 每次读取都会校验 pid 并清理陈旧记录。

### 6.5 仍待讨论

- **告警是否要做重**（Webhook / 桌面通知 / 常驻服务）：当前 `sc --follow` 只打印 + 面板横幅。

---

## 7. 开发陷阱（血泪记录）

以下是开发过程中**真实踩到并修掉**的坑，改代码前请先读：

### 7.1 异步与生成器

- **`messages()` 生成器里不能让读超时逃出去**。
  一开始 `asyncio.TimeoutError` 逃出生成器 → 迭代器被终结 → 调用方下次取值拿到
  `StopAsyncIteration` → 误判连接结束 → **2.5 秒内重连 26 次**。
  现在读超时在生成器内部吞掉，只有心跳真死才抛 `ConnectionStale`。
- **截止条件必须注入生成器内部**（`should_stop` 回调）。
  修上面那条时我一度把判断只放在外层循环，结果生成器只在**收到消息**时才交回控制权，
  无消息时外层永远等不到检查机会 → `grab --duration` **死锁**，第一个用例挂满 10 分钟。
- **Starlette 的 `StreamingResponse` 不能复用请求协程里建的 SQLite 连接**：
  同步生成器由线程池驱动，开连接和用连接可能不是同一个线程（见 D15）。
- **同步 IO 绝不能直接在事件循环里做**（见 D17）：sink 的 `write()` 一旦在循环线程里
  阻塞（磁盘慢、库锁、网络盘），心跳任务就发不出去，采集器会判假死并重连。
  现在统一走 `Bus._flush_off_loop()` → `asyncio.to_thread`。
  推论：**任何跨线程使用的 sqlite3 连接都要 `check_same_thread=False`**
  （`SqliteSink` 与 `DanmakuStore` 都已显式设置）。
- **假服务端推完脚本会立刻关连接**（`websockets` 的 `serve` 上下文退出时会关掉所有连接）。
  需要"推完再慢慢断言"的用例要给它 `linger=`，否则会收到莫名其妙的 `ConnectionClosedOK`。
- **`Bus` 的队列容量取自构造时的配置**：先 `Bus([sink], config)` 再改
  `config.sinks.queue_size` 是不会生效的（实测在背压用例里踩到，白折腾半天）。
- **`batch_size > 1` 时队列可能一直是空的**：worker 靠 `get()` 超时也会一条条攒 batch，
  只要它攒得比生产快，队列就永远填不满 —— 想压出背压必须 `batch_size=1`。

### 7.2 变["法"与静默错误

- **`@dataclass` 默认生成 `__eq__`，会把 `__hash__` 置为 `None`**。
  `Subscriber` 因此无法放进 `set`，任何 WS 连接一建立就 500。
  需要按身份区分时必须写 `@dataclass(eq=False)`。
- **`a or b` 会把合法的 0 当"没有值"**。`gift_num=0` 导致礼物数量取到 `None`。
  需要保留零值时用 `_first_int` / `_first_positive`。
- **过滤逻辑必须只有一处**。`hub.recent()` 曾经绕过订阅过滤，
  导致订阅 `types=danmaku` 仍收到其它类型。现在 `publish` 与 `recent` 共用 `hub.accepts()`。
- **写入用的 session_id 要以 sink 打开时的为准**，否则事件会指向不存在的 session 行，
  按 `--session` 统计恒为 0。
- **顺序错会掩盖真错**：`add_room` 先查房间号再查并发上限时，达上限的报错会被
  "房间不存在"盖掉（D14）。**先做本地约束检查，再打网络**。
- **只读层不会跑迁移**：`DanmakuStore` 是 `mode=ro` 打开的，`SqliteSink` 的 ALTER 迁移
  对它无效。给它加列（如 V2 的 `extra_json`）时必须让查询**自适应**
  （`has_column()` / `NULL AS extra_json`），否则老库直接 `no such column`（D18）。
  给已有表加列一律走 `sqlite.py` 的 `MIGRATIONS`，别只改 `CREATE TABLE`。
- **可选依赖必须延迟导入**：`text.py` 顶层不 import jieba/wordcloud/matplotlib ——
  matplotlib 一导入就写用户目录字体缓存，受限沙箱下报 `Permission denied`。
  PNG 渲染前把 `MPLCONFIGDIR` 指到工作区内（`.matplotlib-cache/`，已 gitignore）。
- **进程间"约定"只能靠文件**：面板是**分离进程**，CLI 要找到它就必须靠状态文件
  （`.runtime/web.json`）而不是扫进程名 —— 扫 `python.exe` 会撞到别的 Python。
  状态文件必须**每次读取都校验 pid**，否则崩溃残留会让下次启动永远被拒（实测踩到）。
- **Windows 上"优雅停止"不能靠信号**（实测三连）：
  `CTRL_BREAK_EVENT` 对独立进程组的 uvicorn **无效**（8 秒不退出）；
  `taskkill /PID`（不带 `/F`）直接 `Access denied`；
  `subprocess` 里 `close_fds=True` + 重定向句柄的组合还曾报出
  `FileNotFoundError: [WinError 2]`（报错信息与真实原因完全无关）。
  最终方案：**让服务自己退出**（控制端点 → `Server.should_exit`），强杀只作兜底。
- **分离进程不能继承父进程的 stdout**：否则 `bili-danmaku web start | ...` 这类管道
  会一直等 EOF 而"卡住"（命令早就退出了）。用 `close_fds=True` + 显式重定向，
  再配 `STARTUPINFO` 的 `SW_HIDE` 才真正安静。
- **`STARTUPINFO` 别忘**：只给 `creationflags` 不够 —— 父进程是控制台程序时，
  子进程仍可能被终端带走。`STARTF_USESHOWWINDOW + SW_HIDE` 是标准做法。
- **`loop.call_later` 排的回调在测试客户端里不会被执行**：TestClient 的请求跑在
  portal 的事件循环里。要"响应后执行"就用 Starlette 的 `BackgroundTask`。
- **`TestClient` 的客户端地址是字面量 `testclient`**（不是 IP），`http://testserver`
  也不是域名 —— 凡是要判来源的端点，测试必须显式 `TestClient(app, client=("127.0.0.1", ...))`，
  否则测到的是"拒绝对端"而不是"本机可用"。
- **`ipaddress.ip_address("::ffff:127.0.0.1").is_loopback` 是 False**，
  要额外看 `ipv4_mapped`；否则双栈/代理场景下回环判定会失败。

### 7.3 编码（D19 的教训）

- **CSV 一律带 UTF-8 BOM**（`utf-8-sig` / `CSV_BOM`），这一条在本项目里出现过三种写法：
  - `sinks/csv_txt.py` 落盘：`encoding="utf-8-sig"` ✅
  - CLI `export --out x.csv`：`_open_output` 按后缀选 `utf-8-sig` ✅
  - **`/api/export` 的流式导出：曾经只写 `utf-8`，于是浏览器下载的 CSV 在中文
    Excel 里全乱码** ❌（D19）
- 关键事实：`Content-Type: text/csv; charset=utf-8` 对**双击打开的本地文件无效**
  —— Excel 只看文件头。没有 BOM 就按系统码页（GBK）解，中文必乱。
- 加 BOM 要**只给 CSV 第一块 chunk**：`StreamingResponse` 是分块 yield 的，
  每块都加就会在正文中插入 `\ufeff`；JSONL/TXT 不能加（会污染首行解析）。

### 7.3 协议与数据

- 假服务端**必须按真实协议压「带头部的包」**，压裸 JSON 会被客户端正确拒绝。
- 关键词检索**不要用 FTS5**：`unicode61` 把连续中文当单个 token（搜「主播」找不到「主播好」），
  `trigram` 又要求查询 ≥3 字符，而中文关键词常为 2 字。现在统一走 `LIKE` 子串匹配。
- **测试夹具里每条事件的 `msg_id` 必须唯一**，否则会被 `UNIQUE(dedupe_key)` 正确合并，
  让你误以为"写丢了数据"（我第一次写 store 测试就中了这个）。
- **`top_senders` 按 uid 分组**（不是昵称）。夹具里同一个"人"要用同一个 uid，
  否则会得到一堆 count=1 的行。
- 组装多行帧时**别把"客户端发出去的帧"当成"能收到的东西"**：假服务端不回显，
  要测坏帧就得让服务端推 `raw_frames=`。

### 7.4 验收方法

- **单测通过 ≠ 能跑**。D12（unhashable）与 D13（回填绕过过滤）
  只有真正把服务跑起来、连上 WS 才会暴露；
  D15（导出 500）与 D16（`export` 崩）更是"单测全绿但功能全废" ——
  前者因为测试配置里没有 sqlite sink，后者因为从来没跑过不带 `--out` 的命令。
  **每个里程碑都要做端到端验收**，不能只看 `pytest` 绿灯。
- 补测试优先补"用户真会走的路径"：SQLite 分支、CLI 端到端、错误退出码。
- **编码问题只能看字节**：D19（导出的 CSV 在 Excel 里乱码）在 `pytest` 里永远测不出来 ——
  断言 `response.text` 拿到的是已经正确解码的字符串，中文当然"正常"。
  必须断言 `response.content[:3] == b"\xef\xbb\xbf"` 这种**字节级**事实。
  同理：凡是"交给别的程序打开"的产物（CSV/Excel、PNG、PDF），验收要覆盖
  "真实消费者怎么读它"，而不是我们自己怎么解码。
- 数值/时间断言要**固定时间基准**（见 `test_server_sqlite.py` 的 `BASE_TS`），
  否则会随"跑测试的时刻"随机飘。
- 命令行要 `--out` 与不带 `--out`（stdout）都测；`--out` 到文件只是其中一条路径。

---

## 8. 缺陷档案（D1–D19）

完整表格在 `TECH_DESIGN.md` 附二，这里只列索引：

| # | 一句话 | 阶段 |
|---|--------|------|
| D1 | 读超时终结消息迭代器 → 重连风暴 | M1 |
| D2 | 截止检查无处执行 → `grab` 死锁 | M1 |
| D3 | 服务端"接受后立即断开"重置退避 | M1 |
| D4 | `bus.stop()` 可能等满 `flush_interval`（最长 60s） | M1 |
| D5 | 脱敏过滤器抓不到裸凭据值（安全缺口） | M1 |
| D6 | 一次成功就把房间永久降级到 getConf | M1 |
| D7 | 事件可能指向不存在的 session 行 | M2 |
| D8 | FTS5 无法做中文子串检索 | M2 |
| D9 | 礼物金额恒为 `None`（字段名假设错） | M2 |
| D10 | 合法零值被 `or` 穿透 | M2 |
| D11 | 校验工具在不完整格式集上误报 | M2 |
| D12 | `Subscriber` 无法放进 set → WS 全挂 | M3 |
| D13 | 历史回填绕过订阅过滤 | M3 |
| D14 | 达并发上限时报错原因被掩盖（且白打一次接口） | M4 |
| D15 | **`/api/export` 实际 500（导出按钮全废）** | M4 |
| D16 | `export`/`replay` 不带 `--out` 时崩溃 | M4 |
| D17 | **同步 sink 写入阻塞事件循环 → 假死重连**（压测发现） | M4 |
| D18 | 只读查询层遇到**旧库缺列**直接 `no such column`（词云首跑就打脸） | V2 |
| D19 | **Web 面板导出的 CSV 中文乱码**（Excel 双击打开） | V2 |
| D20 | **`--min-price` 混用两种金额单位**（元 vs 金瓜子，差 1000 倍） | V2 |

---

## 9. 测试与夹具

```powershell
.\.venv\Scripts\python.exe -m pytest                 # 704 项，默认跳过联网
.\.venv\Scripts\python.exe -m pytest -m live         # 需要真实联网的（默认 skip）
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing   # 覆盖率（门禁 85%）
.\.venv\Scripts\python.exe -m pytest tests/test_server.py -v
```

`-m live` 里有两项是**真实多房间验收**，用环境变量指定房间号（见 §10）。

| 测试文件 | 覆盖 |
|----------|------|
| `test_protocol.py` | 封包/拆包/解压/边界（含真实字节） |
| `test_wbi.py` | WBI 官方向量逐字比对 |
| `test_parser.py` | 金样本字段映射、表情、protobuf、付费事件 |
| `test_filters.py` | **内容规则 + 各过滤条件** |
| `test_dedupe.py` `test_sinks.py` `test_sinks_m2.py` | 去重与四种落盘 |
| `test_store.py` | SQLite 查询层 |
| `test_collector.py` | 假服务端驱动的连接/重连/范围/脱敏 |
| `test_client.py` | WS 连接层直测：认证、心跳、假死、坏帧、`probe()` |
| `test_api.py` | token 获取策略（锁死 D6） |
| `test_api_http.py` | HTTP 层：重试、WBI 签名、`room_init`/`history`（MockTransport，不联网） |
| `test_server.py` | hub / REST / WS（StubPanel，不联网） |
| `test_server_sqlite.py` | REST/WS 的 **SQLite 路径** + 前端托管（覆盖 D15） |
| `test_panel.py` | **多房间并发**（覆盖 D14） |
| `test_cli.py` | CLI 端到端：`stats`/`export`/`replay`/`wordcloud` + 参数解析（覆盖 D16） |
| `test_login_utils.py` | 扫码登录全流程 + 脱敏/退避/限流 |
| `test_text.py` | **V2 词云**：表情占位符单独处理、分词、词频、四种输出 |
| `test_store_extra.py` | `extra_json` 落盘/读取 + **旧库（schema v1）兼容** |
| `test_live_multi_room.py` | 真实多房间验收（`-m live`） |
| `test_config.py` `test_credentials.py` `test_protobuf_lite.py` | 配置校验 / 凭据 / protobuf |

`tests/fixtures/` 里的**都是真实抓取的样本**，是字段映射的唯一可信依据：

| 夹具 | 用途 |
|------|------|
| `danmu_msg_samples.json` | `DANMU_MSG` 字段映射 |
| `emote_samples.json` | **含表情的弹幕**（`[dog]`/`[捂脸]`/`[花]` 与 `extra.emots` 图片映射） |
| `interact_v2_samples.json` | `INTERACT_WORD_V2` 的 protobuf |
| `priced_event_samples.json` | `COMBO_SEND` 的 snake_case 结构 |
| `capture*/` | `probe --dump-raw` 的原始帧 |

> **夹具来源与缺口**：`emote_samples.json` 是从 `data/804456_20260921-13.jsonl` 的
> `raw` 里筛出来的真实消息（同一份库里 `emots` 只在 4 条上出现，
> **旧 JSONL 的 `extra` 里没有 `emots`** —— 那是解析器支持表情之前写的，
> 用当前 parser 重新解析 `raw` 就能拿到完整映射）。
> 仍缺：`SEND_GIFT` / `SUPER_CHAT_MESSAGE` / `GUARD_BUY` 的真实样本，做 V2-b 时补。

**改协议相关代码前，先跑 `pytest tests/test_parser.py`**；它会在你改错字段时立刻报出来。

---

## 10. 常用命令速查

```powershell
# 凭据
bili-danmaku login / check-auth / purge

# 协议自检（排障第一步；--dump-raw 可落夹具）
bili-danmaku probe 804456 --seconds 30 --messages 10 --dump-raw tests/fixtures/capture

# 抓取
bili-danmaku grab 804456 --duration 5m --out data
bili-danmaku grab 804456 --format jsonl,csv,txt,sqlite --history
bili-danmaku grab 804456 --all-events                 # 临时关掉内容过滤
bili-danmaku grab 804456 --keyword 主播 --exclude-masked

# 面板（默认只采文字弹幕/SC；端口占用会自动换）
bili-danmaku web --rooms 804456 --open                # 前台运行（Ctrl+C 停止，适合调试）
bili-danmaku web --rooms 804456,123456                # 多房间（上限见 general.max_rooms）

# 面板常驻（关掉终端也继续跑）与生命周期
# ⚠️ 注意：实测反馈"关掉启动它的终端后面板也会停" —— 见 §5.12（未修复的开放问题）
bili-danmaku web start --rooms 804456 --open          # 后台启动，1~2 秒返回
bili-danmaku web status                               # 地址 / pid / 已运行 / 房间 / 落盘格式
bili-danmaku web status --json                        # 给脚本用
bili-danmaku web logs --tail 50                       # 看后台日志（--follow 持续跟）
bili-danmaku web stop                                 # 优雅停止：先 flush 落盘再退出
bili-danmaku web restart                              # 等价 stop + start
bili-danmaku web stop --force                         # 强杀（会丢队列里未落盘的批次）
bili-danmaku web start --force                        # 已有实例在跑时先停再起

# 分析
bili-danmaku stats --top 10
bili-danmaku export --session <id> --format csv --out hits.csv
bili-danmaku export --format jsonl                     # 不带 --out 时打到 stdout
bili-danmaku replay data/xxx.jsonl --to csv --out converted.csv

# 词云（V2）
bili-danmaku wordcloud --session <id> --top 30                  # 终端榜单
bili-danmaku wordcloud --format html --out cloud.html           # 零依赖可视化
bili-danmaku wordcloud --format png --out cloud.png --top 120    # 需要 [wordcloud]
bili-danmaku wordcloud --source data/xxx.jsonl --format json     # 离线、不读库
bili-danmaku wordcloud --dict room.dict --include-emotes         # 补黑话 / 表情一起统计

# 校验产物一致性
python tools/verify_formats.py --dir data

# 环境诊断 / 依赖获取 / 打包 / 压测
python tools/preflight.py 804456
python tools/wheel_fetch.py --out wheels          # pip 网络不可用时的离线 wheel 获取
python tools/build_wheel.py --out dist            # 构建 wheel（把 web/dist 一起打进去）
python tools/hot_rooms.py --top 10                # 找人气房间（当前被 -352 挡着）
python tools/stress.py --room 7878 --duration 90  # 压测：吞吐 / 背压 / 四产物一致性
python tools/stress.py --room 7878 --queue-size 3 --flush-interval 5   # 强制背压（会丢最旧）

# 覆盖率
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing

# 界面/交互的专项验收（GUI 点不了，但"逻辑与尺寸"要能验）
.\.venv\Scripts\python.exe tools\verify_login_banner.py          # 登录条四态 + 二维码弹窗尺寸 + 成功自动关窗
.\.venv\Scripts\python.exe tools\verify_qr_window_desktop.py     # 真桌面：弹窗真的出现且装得下整张码
.\.venv\Scripts\python.exe tools\verify_empty_rooms.py           # 空房间引导（真 Vue SSR）
.\.venv\Scripts\python.exe tools\verify_timeline_canvas.py       # 时间轴 canvas：柱数/柱高/表头
.\.venv\Scripts\python.exe tools\verify_canvas_pixels.py --port 8795   # 真 Chromium 读回真像素
.\.venv\Scripts\python.exe tools\check_stats_export.py           # 统计导出（CSV + 多 sheet xlsx）
.\.venv\Scripts\python.exe tools\verify_storage.py               # 存储位置与占用统计

# 真实多房间验收（需要第二个房间号）
$env:BILI_LIVE_ROOMS="804456,<房间号>"; $env:BILI_LIVE_SECONDS="60"
.\.venv\Scripts\python.exe -m pytest -m live tests/test_live_multi_room.py -v -s
```

退出码：`0` 正常 · `2` 配置错误 · `3` 房间/接口问题 · `4` 认证失败 · `5` 网络不可用

---

## 11. 数据产物

默认输出到 `data/`（可 `--out` 覆盖）：

| 产物 | 说明 |
|------|------|
| `{room_id}_{时间桶}.jsonl` | 每行一个事件，保留全部字段（含 `raw`） |
| `{room_id}_{时间桶}.csv` | `utf-8-sig`（Excel 直接打开），已做公式注入防护 |
| `{room_id}_{时间桶}.txt` | `[HH:MM:SS] 昵称: 内容`，仅弹幕 |
| `danmaku.db` | SQLite（WAL），`event` 表 + `UNIQUE(dedupe_key)` |
| `.secrets/cookies.json` | 凭据，0600 权限，**已 gitignore** |

多房间时 JSONL/CSV/TXT 按 `room_id` 分文件，SQLite 共用一个库（靠 `room_id`/`session_id` 区分）。

单条含 `raw` 约 2KB。`--no-raw` 可显著减小体积。

---

## 12. 隐私约束（**不要破坏**）

- 凭据只落 `.secrets/`（已 gitignore），或纯环境变量模式（完全不落盘）。
- **所有日志经统一脱敏**：`RedactingFilter` + 「已登记密钥」表
  （`Credentials` 构造时把凭据字面量登记进去，任何位置出现都被替换）。
  这是 D5 的修复，改动 `utils.py` / `credentials.py` 时务必保留。
- **不保存账号资料**：只保留连接所需 cookie 字段，昵称/mid/头像一律丢弃。
- 项目**不含任何上传逻辑**。

详见 [`docs/PRIVACY.md`](docs/PRIVACY.md)。
M4 复核结论：`docs/PRIVACY.md` 与采集范围、脱敏开关、导出实现**一致，无需改动**；
新增的覆盖率测试与打包脚本都不接触真实凭据（`test_live_multi_room.py` 只在
`-m live` 下读本机 `.secrets/`，且不打印任何凭据）。

---

## 13. 开发约定

1. **协议假设必须用实测样本验证**，不要凭记忆写下标。
   新命令/字段先用 `probe --dump-raw` 抓样本，再改 `parser.py`，再补金样本测试。
2. **未知 cmd 走 `other` 兜底，绝不丢消息**——但要记得按采集范围过滤掉它。
3. **日志里不要打印凭据**；用 `mask()` 或依赖登记表。
4. **改过滤/落盘/连接逻辑后，除单测外必须做一次端到端验收**（见 §7.4）。
5. **里程碑结束停下来交付验收**，不要自动推进到下一个。
6. 文档要跟着证据走：实测推翻假设时，**在 `TECH_DESIGN.md` 里记录结论与偏离原因**，
   而不是默默改代码。
7. **新增"用户会走的路径"时补测试**：SQLite 分支、CLI 端到端、错误退出码
   —— D14/D15/D16/D18 全是这么被抓出来的。
8. **改异步/线程边界前先看 §7.1**：sink 的同步 IO 必须在工作线程里做（D17），
   跨线程的 sqlite3 连接必须 `check_same_thread=False`。
9. **给表加列要同时管三处**：`SCHEMA`、`MIGRATIONS`、以及只读层的自适应查询（D18）。
10. `pytest` 必须保持**离线可跑**：需要联网的用例一律打 `@pytest.mark.live` 并用
    环境变量开关，不要在收集阶段就发请求。
11. 中文文件只用编辑工具改（见 §4.3 最后一条）。

---

## 14. 下一步：V2-b（SC 汇总与告警）

**V2-a 弹幕词云已完成**（见 §5.3）：`bili-danmaku wordcloud`，
`text/json/png/html` 四种输出，表情占位符单独成榜。

剩下 **V2-b**，建议这样落地（一次只做一件，做完端到端验收）：

1. **先做"列出 + 汇总"**：新增 `bili-danmaku sc` 子命令，从 SQLite 读 `super_chat`：
   - 列表：时间、用户、金额、正文（`--min-price` 过滤可直接复用 `filters`）；
   - 汇总：总额、条数、TOP 用户、时间分布（参考 `store.top_gifts`）；
   - **`price` 单位是毫元**（实测 30000 = ¥30），输出必须写清单位，别让用户误读。
2. **告警再做**：先明确需求（常驻进程？复用 `web` 的 WS 推送？还是 `grab` 时打印？）。
   现有基础设施：`server/hub.py` 的 `publish_control` 可以直接推给面板；
   `filters.min_price` 已经能筛出高价值事件。
3. **顺手抓 SC 真实样本**：`SEND_GIFT`/`SUPER_CHAT_MESSAGE`/`GUARD_BUY` 的字段结构
   仍缺真实样本（见 §9 的"夹具缺口"），做 V2-b 时正好用
   `probe --dump-raw` 或 `grab --all-events` 蹲一个有大额 SC 的房间补上。
