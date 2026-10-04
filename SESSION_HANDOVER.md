# 新会话交接文档（SESSION_HANDOVER）

> **这份文档的用途**：让一个**全新会话**（没有本次对话历史）在十分钟内接手本项目。
> 它只写"新会话不知道、但一上手就会踩"的东西。
>
> **三份文档的分工**（别混）：
> * 本文 = **怎么上手**：环境、地图、坑、待办、负面清单；
> * [`HANDOVER.md`](HANDOVER.md) = **权威现状**：每个功能做完了什么、验收证据（§5.x 按版本追加）；
> * [`TECH_DESIGN.md`](TECH_DESIGN.md) = **为什么这么写**：实现级设计与缺陷档案；
> * [`ROADMAP.md`](ROADMAP.md) = **未来做什么**：三条方向、阶段门槛（G1–G4）、预留扩展点、待决策项。
>
> 基线数字请以你上手时重跑的结果为准（§10 给了命令）。
> 最近更新：V3-b 3A（本地感知分析）交付后。

---

## 0. 一句话现状

`LiveDanmakuExtractor`：B 站直播弹幕采集器（Python 包 + Vue 面板）。

**M0–M4、V2（a/b/c/d）、V3-a/a2/a3、V3-b 3A 全部完成**：

| 阶段 | 内容 | 证据 |
|------|------|------|
| M0–M4 | 采集、四格式落盘、多房间、常驻面板 | HANDOVER §5.1–5.2、§5.6 |
| V2-a/b | 词云、SC 汇总与告警 | HANDOVER §5.3、§5.5 |
| V2-c/d | 面板常驻化、交互优化、排版重排、主播昵称 | HANDOVER §5.6–5.7 |
| V3-a/a2/a3 | 历史浏览（游标分页）、标记/区间导出、链接解析与交互修正 | HANDOVER §5.8–5.10 |
| V3-b 3A | **本地感知分析**（词频/热点/刷屏/账号画像） | HANDOVER §5.11 |

**当前基线**：`pytest` **913 passed, 2 deselected**，覆盖率 **90.82%**（门禁 85%）；
前端 `cd web; npm test` **101 passed**。

> §13 交付物清单与 §14 自检表里的数字是 **V3-b 3A 那一轮的快照**（704 passed /
> 19 前端 / 38 modules），保留作历史对照；**当前基线就是上面这两行**。

> 📌 **本项目已有 git**（`main` 分支）。每个功能一个提交，提交信息用
> `tools/commit.py`（PowerShell 5 没有 `<<<`，中文提交信息会被截断/乱码）：
>
> ```powershell
> .\.venv\Scripts\python.exe tools\commit.py --dry-run          # 先看要提交什么
> .\.venv\Scripts\python.exe tools\commit.py -m build/msg.txt   # 提交（消息写成 UTF-8 文件）
> ```
>
> **每完成一项都要提交**（需求方 2026-10-04 明确要求）。

> ✅ **上一轮的三条"坏消息"都已修复/落地，别再照旧文档走**：
> * **pip 已可用**（旧说"pip 完全不可用"）→ 根因是注册表系统代理 + venv 老 pip，
>   现已升到 26.2.1。见 §7.5 与 HANDOVER §4.1。
> * **PyInstaller 已可用**（旧说"做不了"）→ 已装 6.22.3。见 HANDOVER §4.1b。
> * **托盘 + 启动面板 + 单文件 exe 已实现**（DEC-6）→ `dist\exe\B站弹幕提取姬.exe`。
>   见 HANDOVER §5.13.5。
>
> ✅ **`web start` 的终端连坐缺陷（§5.12）已修复**：`runtime.child_python()` 绕开 venv
> 的转发器 stub，实测进程树里只剩一个 python、无多余 conhost。见 §5.1 第 0 条。
>
> ⚠️ **托盘/启动面板的"点击手感"必须真人手测**（自动化验不了）：
> 右键菜单、气泡提示、高 DPI、关终端后托盘是否还在。见 §5.1。

**没有进行中的半成品**。剩下的分四类，见 §5：
① 🔴 上面那个缺陷；② 真机核实项（沙箱里做不到的）；③ ROADMAP 里**未被拍板**的方向
（3B LLM / 多平台 / 告警渠道）；④ 下一条推荐主线 **V3-c 工程债**。

---

## 1. 环境与如何跑起来

```powershell
# 工作目录（注意：这不是 git 仓库，没有 .git）
cd F:\CodingBox\DeepseekHarness\LiveDanmakuExtractor

# 中文输出别乱码（几乎每条命令都建议先设）
$env:PYTHONIOENCODING="utf-8"

# 入口（bili-danmaku 不在 PATH 上，必须走 .venv 的绝对路径）
.\.venv\Scripts\python.exe --version          # 3.11.3
.\.venv\Scripts\bili-danmaku.exe --help

# 全量测试（默认 -m 'not live'）
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term

# 前端：逻辑用例 / 重建产物
cd web; npm test; npm run build; cd ..
```

**已装依赖**（版本已实测）：

| 包 | 版本 | 包 | 版本 |
|----|------|----|------|
| httpx | 0.28.1 | click | 8.5.0 |
| websockets | 17.1 | brotli | 1.2.0 |
| pydantic | 2.13.5 | jieba | 0.42.1（分词，可选但已装） |
| fastapi | 0.141.1 | wordcloud | 1.9.6 |
| uvicorn | 0.53.0 | numpy / matplotlib | 2.4.6 / 3.11.2 |

> `pillow` **不在** `find_spec` 里能直接找到（wordcloud 自带依赖），别据此判断 wordcloud 不可用。

---

## 2. 决策记录（**不要重新讨论、不要擅自改**）

`HANDOVER.md` §6.4 是权威表。摘录：

| # | 决策 | 对代码的约束 |
|---|------|--------------|
| **DEC-1** | **礼物（`gift`）与上舰（`guard_buy`）不纳入采集** | 范围冻结为 `danmaku` + `super_chat`；`content_only=true`。**不要再问"礼物要不要恢复"**（旧文档里的提议已作废）。`--all-events` 只用于协议调试 |
| **DEC-2** | 压测放到功能齐全之后 | 已完成（房间 7878，见 HANDOVER §5.2） |
| **DEC-3** | **面板常驻化 + 面板内「停止面板」** | `web start/stop/status/logs/restart` + `POST /api/admin/shutdown`；停止必须走优雅路径 flush 落盘 |
| **DEC-4** | **仅供本机使用** | `web.host` 保持回环；控制端点只接受回环来源；**不要**改成 `0.0.0.0` |
| **DEC-5** | **不做开机自启** | 不装服务/计划任务；崩溃残留状态靠 `live_runtime()` 自动清理 |
| **DEC-6** | **托盘 + 启动面板 + 单文件 exe 作为 Web 主入口；CLI 降级为调试端** | 见 HANDOVER §5.13。托盘用**零依赖 ctypes**（**不要**引入 pystray，已卸载）；exe 用 **PyInstaller onefile**（已装 6.22.3）；面板**跑在托盘进程内**，不 spawn 子进程 |

**V3 新增的设计约束**（在 ROADMAP 里，但性质等同于决策）：

* **G4 门槛**：任何"外发数据"的功能（LLM / Webhook / 多平台回传）必须先有**书面**隐私决策。
  这就是 **3B（LLM）没做**的原因 —— 不是能力问题，是边界问题。
* **瘦投影契约**（ROADMAP §6.1）：新增只读端点默认返回窄投影；
  `/api/events` 与 `/api/export` 的完整 `Event` 宽度**不许悄悄改窄**。
* **标注存独立库**（ROADMAP §6.2）：`{out_dir}/annotations.db`，**采集库一张表都不加**。

---

## 3. 代码地图（只看这张表就够定位）

```
src/bili_danmaku/                     行数    职责
  cli.py                             2158   所有命令入口（含 web 组、verify、tray）
  launcher.py                    🆕   195   **进程内**托管面板（托盘/exe 的核心，可离线单测）
  runtime.py                          343   面板托管运行时：状态文件/分离启动/优雅停止
  config.py                           179   配置加载与校验（web.token 在非回环时强制）
  models.py                           177   Event / RoomInfo（anchor_name 等可选展示字段）
  filters.py                          204   FilterSpec / min_price_threshold（元 vs 金瓜子！）
  text.py                             551   分词 + 词频 + 词云渲染（txt/json/png/html）
  analysis.py                         412   V3-b 本地感知分析（**纯函数，无 IO/网络**）
  rooms.py                            102   房间号 / 直播间链接解析（识别不出就报错，不猜）
  annotations.py                      250   标注存储（**独立库**、可写）
  store.py                            653   SQLite **只读**层；V3-a 加了 iter_history/density/_project
  api.py                              348   协议层：token（getDanmuInfo+WBI）、主播信息
  collector.py                        458   采集状态机：重连/退避/flap guard
  client.py / protocol.py / protobuf_lite.py / parser.py / wbi.py / credentials.py / dedupe.py / utils.py
  sinks/                                      bus.py（每 sink 独立队列）+ jsonl/csv/txt/sqlite
  gui/                                🆕   托盘与启动面板（**零依赖**：tkinter + ctypes）
    _win32.py                             Win32 托盘：Shell_NotifyIcon/菜单/消息泵（**无 pystray**）
    panel_window.py                       tkinter 启动面板（状态区 + 链接入口 + 折叠高级设置）
    tray.py                               组装托盘应用；`build_menu_items()` 是纯函数（可单测）
  server/
    app.py                           1118   FastAPI：REST + WS + 控制端点（**全部端点都在这**）
                                              `find_dist()` 含**冻结分支**（见 HANDOVER §5.13.3）
    hub.py                            404   WebHub（广播/环形缓冲/累计计数）+ LiveWindow（分析窗口）
    panel.py                          298   PanelService：多房间生命周期 + lookup_room 房间缓存

web/src/                                     Vue 3 面板（约定见 web/README.md）
  App.vue                             19 KB  状态编排（房间/类型/关键词/历史/标记/分析）
  components/  TopBar / TypeSelector / TypeBars / FilterBar / StreamList / ScBanner /
               DensityBar / BookmarkBar / AnalysisPanel / RoomPanel / StatsPanel
  composables/ useLiveStream.js（实时层）/ useHistory.js（历史层 + 标记状态机）
web/test/history.test.mjs             17 KB  19 项前端逻辑用例（node:test，零新依赖）

tests/                                30 个文件 / 706 collected（见 §10 的分文件清单）
tools/                                stress.py / build_wheel.py / hot_rooms.py / preflight.py 等
docs/PRIVACY.md                       隐私说明
```

**端点全景**（都在 `server/app.py`，全部带 docstring）：

| 分组 | 端点 |
|------|------|
| 基础 | `GET /` `/api/health` `/api/rooms` `/api/room-info` |
| 房间管理 | `POST /api/rooms` · `DELETE /api/rooms/{id}` |
| 实时 | `WS /ws/live` |
| 统计 | `GET /api/stats`（`window=0` = 自面板启动累计） |
| 历史（V3-a） | `GET /api/history` `/api/density` `/api/sessions` |
| 标注（V3-a2） | `GET/POST /api/annotations` · `DELETE /api/annotations/{id}` · `GET /api/annotations/export` |
| 分析（V3-b） | `GET /api/analysis` |
| 导出 | `GET /api/export`（原始事件，流式）· `GET /api/stats/export`（**统计 CSV**，多表，前端拼文件名） |
| 控制 | `POST /api/admin/shutdown`（**仅回环**） |

**CLI 子命令**（`DEC-6` 后 CLI 定位为**调试端**）：

| 命令 | 用途 |
|------|------|
| `login` / `check-auth` / `purge` | 凭据 |
| `probe` / `grab` / `sc` / `wordcloud` | 抓取与离线分析（调试用） |
| `web start/stop/status/logs/restart` | **调试端**：分离式子进程托管（有 §5.12 的终端连坐问题） |
| `web _serve`（隐藏） | `web start` 的子进程入口 |
| `verify`（新增） | **冻结自检**：前端定位 + 面板本进程起停 + 首页可访问。打包后必跑 |
| `tray`（新增） | 托盘入口（面板跑在本进程内）。`--no-open` 不开浏览器；`--no-start` 只起托盘 |

---

## 4. 各轮做完的东西（按时间倒序，细节点进 HANDOVER）

### 环境修复轮（2026-10-01，最近）

| 主题 | 结论 | 落点 |
|------|------|------|
| **pip 可用性** | 🎉 **pip 已修好**（旧结论"pip 完全不可用"**作废**）：根因是注册表系统代理 + venv 的 pip 22.3.1；已升到 **26.2.1** | HANDOVER §4.1、本文 §7.5 |
| **PyInstaller** | 🎉 **可用**（旧结论"做不了"**作废**）：已装 **6.22.3** | HANDOVER §4.1b |
| **终端连坐缺陷的机制** | ⚠️ **旧机制（Job Object）已证伪**；真凶是**多出来的一层 console** | HANDOVER §5.12 |
| **新方案拍板** | **DEC-6**：托盘 + 启动面板 + 单文件 exe；CLI 降级为调试端 | HANDOVER §5.13 |
| **`find_dist()` 冻结分支** | ✅ **已修 + 真实 exe 验证**（旧版打包后只显示降级页） | HANDOVER §5.13.3 |
| **`bind_port(host,0)` 错端口** | ✅ **已修**（曾返回 `1`）+ 4 条用例 | HANDOVER §5.13.6 |
| **托盘 + 启动面板 + exe** | ✅ **已实现**：`gui/` 零依赖（tkinter + ctypes，**无 pystray**）；`dist\exe\B站弹幕提取姬.exe` 26.9 MB 窗口版 | HANDOVER §5.13.5 |

### V3-b 3A：本地感知分析

| 主题 | 落点 | 关键约束 |
|------|------|----------|
| 分析函数 | `analysis.py` | **纯函数**：无 IO/网络/全局状态 ⇒ 完全离线可测 |
| 有界窗口 | `hub.LiveWindow` | `add()` **O(1) 且有界**（实测 +47 µs/条），所以能放在 `publish()` 里 |
| 端点 | `GET /api/analysis` | 词频走"片合并"省算路径（不逐条重跑分词） |
| 前端 | `AnalysisPanel.vue` | 四块可折叠；**点词 → 只看含该词的弹幕**（复用顶栏过滤，不另造状态） |
| 措辞 | 面板底部 | 必须保留"仅供参考、非判定；数据不出本机" —— 那是隐私承诺 |

**🐞 三个缺陷**（前两个是"把正常人报成疑似异常账号"）：
① 历史补种把一批弹幕写成**同刻** ⇒ `span=0` ⇒ 算出"600 条/分钟"；
② 时间戳**各不相同但全挤在 1 秒内** ⇒ 仍然算出荒谬速率（**第一版修错了**：加"窗口下限"会把真实爆发一起压平；
正解是要求观测跨度 ≥30 秒，否则 `rate_available=false` **放弃速率类结论**）；
③ 重复率公式错：`1 - 去重数/总条数` ⇒ "10 条完全相同"只有 90%，改为 `1 - (去重数-1)/总条数`。

### V3-a3：交互修正（实测反馈驱动）

| 反馈 | 性质 | 处置 |
|------|------|------|
| 点时间轴某格却高亮**前一格** | 🐞 off-by-one | `jumpTo` 用 `until = ts + bucket_s`（窗口右端划到**该格结束**）；高亮改为"有 focusTs 时只亮那一格" |
| 「更早/更新」体验完全错误 | 设计错误 | **删除按钮**；保留"滚到顶自动加载"（`hasOlder` 状态机仍在） |
| 需要自选时间轴粒度 | 新需求 | 5/10/15 分钟档位，切换即重算 |
| 发言 TOP 累计口径没显示 | 新需求 | `/api/stats` 累计口径补 `top_senders`（**全部房间走 15 秒 TTL 缓存**，全库查询 101 ms） |
| 需要粘直播间链接 | 新需求 | `rooms.py`；**识别不出就报错，不猜**（`/blanc/8432038` 明确拒绝） |

> 顺带修了一个状态码 bug：`POST /api/rooms` 原来把所有 `ValueError` 映射成 409（"已在监控中"），
> 而坏链接也是 `ValueError` ⇒ 报成 409 完全误导。现在**先解析**（失败 → 400），再调 `add_room`（→ 409）。

### V3-a2：标记与切片

标注存**独立库**（`annotations.db`，可配 `sinks.annotations_path`），**采集库一张表都不加**（有测试断言）。
只读模式**不创建文件**（面板首屏会拉列表，不该凭空留空库）。
区间导出：只给起点时终点自动取**下一个标记**；给反了自动正过来。

> ⚠️ **流式导出的坑**：`_stream_export()` 收的是**工厂**而不是现成迭代器。
> Starlette 用线程池**惰性**驱动同步生成器 —— 在协程里开好连接、函数一返回就 `close()`，
> 生成器真正开始时数据库已经关了（实测 `sqlite3.ProgrammingError`）。

### V3-a：复盘器（历史浏览）

| 主题 | 关键约束 |
|------|----------|
| 游标分页 | **`(recv_ts, id)` 复合键**，且 `ORDER BY` 必须同键 —— `recv_ts` 会并列（实测 393 组），单键会漏条 |
| 瘦投影 | 单条 **257 B**（完整 `Event` ~2 KB）。这是"不触发 `SLOW_CONSUMER`"的前提，不是优化 |
| 时间轴密度 | 按**原始 `recv_ts`** 分组再在 Python 折算桶（按表达式分组会触发临时 B 树：118 → 38 ms） |
| 双层数据流 | `useHistory.js`：实时层缓冲不动，**进入历史浏览时收缩到 300 条** |
| 翻页交互 | 滚到顶加载更早并**补偿 `scrollTop`** 保视口 |

> ⚠️ 两个"状态机语义"缺陷（人眼点几下发现不了，各有用例守着）：
> ① `has_newer` 照抄服务端 ⇒ 向上翻页后**再也翻不回最新**（正确语义是"窗口是否已离开实时边缘"）；
> ② `has_older` 不设"已翻完"标记 ⇒ **永远为真**（窗口被截断后失真）。

### V2-d / V2-c（更早，备查）

* 右栏顶部 = 房间（直播间信息/输入/记忆/链接解析）；顶栏 = 跨面板过滤与操作；
  上下文条 = 视图状态（统计口径 + 历史控件）。
* `.app` 是 **4 行网格**（顶栏/上下文条/SC 横幅/主体）—— 改动要同步 `ScBanner`（它占第 3 行，空时高度为 0）。
* 面板常驻：子进程入口是隐藏命令 `web _serve --managed`；停止靠 `Server.should_exit`。

---

## 5. 待办（按"能不能立刻做"分类）

### 5.1 立刻能做的真机核实（沙箱里做不到）

0. ✅ **已修复**：`web start` 的面板曾随启动它的终端一起停。
   机制是**多出来的一层 console**（不是 Job Object，那个已证伪）：venv 的
   `python.exe` 是转发器 stub，它起的 base 解释器是 console 程序又拿不到父 console，
   Windows 就**新分配一个 conhost** —— 关掉它就连坐。
   修法：`runtime.child_python()` 绕开 stub 直接起 base 解释器（并把 `sys.path`
   注入 `PYTHONPATH`），实测**进程树里只剩一个 python、无 conhost**。
   详见 `HANDOVER.md` §5.12。

   **建议仍用真人终端确认一次**（harness 的 console 是 ConPTY，与真 conhost 不等价）：
   `web start` → **关掉终端窗口（点 X / exit，不要 Ctrl+C）** → 另开窗口
   `web status` 应为"仍在运行"。

1. 常规确认：`web start` → 关终端 → 面板仍在 → `web stop` 收尾。
2. **多房间联动**：`web start --rooms 804456,23438368`，看右栏列表、按房间过滤、
   历史/时间轴/分析是否随房间切换。
3. **前端手感**：`web/README.md` §5 有 **37 条**手测清单。
   最值得亲眼确认的：**#17（点时间轴某格，高亮必须就是那一格）**、
   **#19（滚到顶保视口）**、**#32–37（感知分析六条）**。
   > GUI 自动化在本环境会被窗口抢占干扰；前端**逻辑**有 19 项用例托底，但滚动/点击的**手感**验不了。

### 5.2 需要真实数据才能做

4. **SC 金额单位验证**（**唯一未验证的假设**）：现在按"元"处理，但**从没抓到过真实 SC**。
   若实测发现差 1000 倍：① `--price-unit milli` 兜住；② 样本存进 `tests/fixtures/`；
   ③ 更新 HANDOVER §5.5 警告框与 TECH_DESIGN §19.2。
5. **`SUPER_CHAT_MESSAGE` 真实夹具**：`tests/fixtures/` 里没有 SC 样本。
6. **面板 SC 横幅确认**：需要一次真实 SC 才能看到 `ScBanner.vue`。
7. **热点突增的真实分布**：只用真 `hub` + 合成多片流验证过；
   实测时段内两个房间都没有新弹幕，凑不出多个时间片。

### 5.3 下一条推荐主线：V3-c 工程债

见 [`ROADMAP.md`](ROADMAP.md) §4。优先级（我的建议）：

1. **保留策略 / 磁盘治理**：JSONL 按小时轮转**永不清理**，`danmaku.db` 已 72 MB 且单调增长。
   ⚠️ **需你决策**：是否允许自动删除原始数据？（我建议默认"只归档不删除"）
2. **前端测试第二步**：`analysis.py`/`hub.py` 的后端逻辑已有 46+ 项用例，
   但 Vue 组件交互仍靠手测。要上 `@vue/test-utils` 时注意 `web/.npmrc` 的镜像与缓存策略。
3. **可观测性**：`GET /metrics`（Prometheus 文本）+ 面板"原始帧"调试抽屉。
4. **风控韧性**：`acquire_token` 加指数退避 + 抖动。

### 5.4 需要你拍板才能做（**不要擅自开工**）

| # | 决策 | 我的建议 |
|---|------|----------|
| D-1 | 分析能力是否引入 **LLM（3B）** | 属隐私基线变更，需 **G4 书面决策**；建议由你自备 key、默认关闭、只出 `text` 字段 |
| D-2 | 是否允许**自动删除**原始数据 | 建议默认只归档；删除需显式开启且 ≥7 天 |
| D-3 | **多平台 / 礼物上舰** | 建议暂不做；要做请先给第二个平台的**真实房间号与需求** |
| D-4 | **告警渠道**（Webhook/桌面通知） | 建议先做本机通知（不出网） |
| D-5 | 标注数据放哪 | 已按独立库实现（`annotations.db`） |
| D-6 | 是否引入前端测试工具链 | 已用 `node:test`（零新依赖）；需要组件级测再评估 |

---

## 6. 已知的"文档与实现不一致"的历史坑（别被旧文档带偏）

- 旧版 HANDOVER 曾写"SC 的 price 单位是**毫元**"。**现在按元处理** + `--price-unit milli` 逃生舱。
  根因是"没抓到真实样本"，不是笔误 —— 见到时以 §5.5 的警告框为准。
- 旧文档多处出现"礼物是否要恢复采集"，**已被 DEC-1 作废**。
- `data/danmaku.db` 可能是 **schema v1**（面板早于迁移启动时会这样），写一次就自动迁移；
  只读层（`store.py`）**不会迁移**，所以查询必须列自适应（`has_column()`）。
  当前库已是 **schema_version=3**。
- 旧 SESSION_HANDOVER 把 V3-a/a2/a3 与 V3-b 的工作记在"V2-c/V3-a2 本轮"标题下（本文已修正）。

---

## 7. 踩坑清单（**这一节最省时间，务必先读**）

### 7.1 PowerShell 会把中文文件写坏（**我自己又踩了一次**）

`Get-Content -Raw | Set-Content`、以及用 here-string 传给 `python -c` 处理中文，都会出问题：

* 读写管道会把 UTF-8 中文变成乱码；
* **实测教训（本轮）**：我用 `(Get-Content x.py -Raw) -replace ... | Set-Content` 改一个含中文的临时脚本，
  结果文件开头变成 `"""涓存椂锛?..`，脚本直接语法错误。
* 另一个高频坑：`python -c @"..."@` 里若含中文 + 引号/反引号，PowerShell 经常**截断字符串**
  （报 `unterminated string literal`）。

**规则**：中文文件一律用**编辑工具**（`edit`/`write`）或 Python 的 `encoding="utf-8"` 读写；
需要跑含中文的脚本就**先写成文件再执行**，不要塞进 `-c`。

### 7.2 Windows 上"优雅停止"不能靠信号（实测三连）

- `CTRL_BREAK_EVENT` 发给独立进程组里的 uvicorn：**8 秒无响应**；
- `taskkill /PID`（**不带** `/F`）：`ERROR: Access denied`；
- `subprocess` 里 `close_fds=True` + 句柄重定向曾报 `FileNotFoundError: [WinError 2]`——
  **报错信息与真实原因完全无关**（真实原因：把 `['web','_serve',...]` 当可执行文件传给了 `Popen`）。

**结论**：可靠路径只有"**让服务自己退出**"：控制端点 → `Server.should_exit`。
强杀（`taskkill /F`）只作兜底，且会丢 sink 队列里未落盘的批次。

### 7.3 分离进程

- 子进程**不能继承**父进程 stdout（否则 `web start | ...` 会一直等 EOF）；
  用 `close_fds=True` + 显式重定向。
- 只给 `DETACHED_PROCESS` **不够**，还要 `STARTUPINFO` 的 `STARTF_USESHOWWINDOW + SW_HIDE`。
- **状态文件每次读取都要校验 pid**：崩溃残留会让下次 `web start` 永远被拒（`live_runtime()` 负责）。
- ⚠️ **实测到"孤儿面板进程"**：面板可能**脱离 `.runtime/` 状态文件继续存活**。
  症状：`web stop` 说"没有正在运行的面板实例"、端口却被占用（或 `data/danmaku.db-shm` 被更新）。
  处理：
  ```powershell
  Get-Process -Name bili-danmaku -ErrorAction SilentlyContinue | Select-Object Id, StartTime
  Stop-Process -Id <pid> -Force
  ```
  我在本会话遇到两次，两次都是 `bili-danmaku web`（**不带 `--rooms`**，即空面板）留在那里，
  且 `.runtime/` 已不存在 —— 推测与 §7.6 的进程组回收有关（harness 杀掉外层命令，
  但已经分离出去的子进程活了下来）。**见到时别怀疑停止逻辑**：优雅停止有端到端验证 +
  21 项离线用例；这只是沙箱收尾的副作用。**每次跑完面板/压测后顺手检查一下。**

### 7.4 测试基础设施的坑

- `TestClient` 的客户端地址是字面量 `'testclient'`（不是 IP），`http://testserver` 也不是域名。
  要判来源的端点必须 `TestClient(app, client=("127.0.0.1", 51234))`。
- `loop.call_later` 排的回调在 TestClient 里**不会被执行**；要"响应后执行"用 Starlette `BackgroundTask`。
  另外**别断言响应与 BackgroundTask 的先后**。
- `ipaddress.ip_address("::ffff:127.0.0.1").is_loopback` 是 **False**，要额外看 `ipv4_mapped`。
- `Bus` 的队列长度在**构造时**固定：测背压要先改 config 再 `Bus(...)`；`batch_size > 1` 时队列一直空。
- `FakeDanmakuServer` 推完就关连接 → 需要 `linger` 参数才能测后续行为。
- `/api/stats` 的条数/类型分布来自 **hub 内存计数**：测试要先 `hub.publish(...)`，否则是 0。
- **`node --test` 默认会派生子进程**，在受限沙箱报 `spawn EPERM`。
  `npm test` 已固定带 `--test-isolation=none`（在当前进程内跑）—— **这不是测试写得不对**。
- **`.vue` 组件怎么在 `node --test` 里测**（2026-10 新增）：
  `npm test` 还带 `--import ./test/register-vue.mjs`，它登记了
  `test/vue-loader.mjs`（用 `@vue/compiler-sfc` 以**客户端模式** `inlineTemplate` 编译），
  所以测试文件可以直接 `import Xxx from '../src/components/Xxx.vue'`。
  **不要**改回 Vite 的 `ssrLoadModule()` —— 那是 SSR 编译，产物里只有 `ssrRender`
  没有 `render`，挂载时报的是完全指不到病根的
  `TypeError: Cannot read properties of undefined (reading 'modules')`。
  写法见 `web/test/density.test.mjs` 顶部与 `web/test/helpers/vue.mjs` 的说明：
  `setupDom()` 必须**先**跑完，Vue 相关模块只能动态 `import()`（`@vue/runtime-dom`
  在模块求值那一刻就抓 `document`）。组件测试**不需要** `@vue/test-utils`
  （它依赖 Vue 内部状态，容易撞上"两份 Vue"），`helpers/vue.mjs` 里的
  `mountComponent()` 只用公开 API。
- **模块级缓存的坑**：`_TOP_SENDERS_CACHE` 是模块级的（必须跨请求复用），所以**会跨用例存活**。
  测试要用 `reset_top_senders_cache()` 清干净，否则"首次查询"与"命中缓存"两条路径没法分别断言。

### 7.5 环境相关的坑

- **pip 已经能用了（2026-10-01 修复）** —— 别再去翻旧结论。
  - 旧文档说"pip 完全不可用 / TLS 栈坏"，**是错的**，根因已定位（见 HANDOVER §4.1）：
    本机有**注册表级系统代理** `127.0.0.1:7890`（`ProxyOverride` 里没有 pypi.org），
    而 venv 里的 pip 是 **22.3.1**（ensurepip 自带）→ 经代理做 TLS/大响应时挂掉，
    报出**与真实原因无关**的 `SSLEOFError ... problem confirming the ssl certificate`。
  - **现在**：`.venv` 的 pip 已升到 **26.2.1**，`pip install <包>` 直接可用。
  - **体检命令**：`.\.venv\Scripts\python.exe -m pip download six --no-deps -d build\pipprobe`
  - ⚠️ **只有 `NO_PROXY` 能绕开那个代理**；`--proxy ""` / `HTTP_PROXY=""` / `HTTPS_PROXY=""`
    **实测全部无效**（pip 的代理来自 `getproxies()` 读注册表，环境变量覆盖不掉）。
  - 走代理约 9 s、直连约 0.7 s；**随机 SSL 报错就是代理不稳**。
  - `tools/wheel_fetch.py` 保留，但降级为**离线兜底**，不再是首选。
- `npm run build` 在受限沙箱会 `spawn EPERM`（Vite 执行 `net use` 探测网络驱动器）。
  把策略放宽到 `danger-full-access` 后可以正常构建。
- `pip install jieba` 会**挂住但可能已成功**；用 `importlib.util.find_spec` 判断，别信退出码。
- matplotlib 缓存默认写用户目录（受限沙箱 `Permission denied`）→ 已重定向到 `.matplotlib-cache/`。
- **`which` / `pythonw` 之类查询不可靠**，用 `.venv` 里的绝对路径。

### 7.6 ⚠️ 沙箱的进程组限制（**不是代码问题，但会让你困惑**）

harness 的命令沙箱在**每条命令结束时回收整个进程组**。
后果：用 `run_in_background` 起的面板会随那条命令一起消失，
所以我**无法在这里演示"父命令退出后后台面板继续存活"**。

已经验证到的：同一条命令内 `start → status → HTTP 访问 → shutdown` 全链路正常；
生命周期逻辑另有 21 项离线用例。
> **你要验证"关掉终端仍在跑"，请用你自己开的 PowerShell。**
>
> 🔴 **这个验证已经做过了、结果是"不成立"** —— 详见 **§5.1 第 0 条**。
> ⚠️ **但机制已被修正**（2026-10-01 实测）：**不是 Job Object**，而是
> **多出来的一层 console** —— venv 的 `python.exe` 是转发器 stub，它起的 base 解释器
> 是 console 程序、又拿不到父进程的 console，于是 Windows **新分配了一个 conhost**；
> 用户看到的"多出来的空终端"就是它，关掉窗口连坐的是**真 server（console 宿主）**。
> 实测证据：顶层 `Popen`（`DETACHED_PROCESS`）**没有** console，而真 server **有**。
> 修复方向随之改变（绕开 stub 直接起 base 解释器），详见 HANDOVER §5.12。

### 7.7 分析相关的坑（V3-b 新增）

- **速率类指标必须能说"数据不支持"**：历史补种会把一批弹幕写成同刻（甚至全挤在 1 秒内），
  `count / span` 会给出荒谬值。`analysis._rate_window()` 要求观测跨度 ≥30 秒，
  否则返回 `rate_available=False`。**不要**为了"让数字好看"补一个下限 —— 那会把真实爆发也压平（我第一次就这么修错了）。
- **`uid=0` 是"被打码的匿名用户"**，不是"没有 uid"。用 `if message.uid:` 会把它漏掉，
  导致 `users` 计数偏小。要判 `is not None`（但**账号画像**里要**排除** uid=0，
  否则匿名用户会被并成一个"用户 0"冲到榜首 —— 那是打码造成的，不是行为异常）。
- **短句跟风不算刷屏**：`哈哈`/`666` 在直播间大量出现是正常气氛，刷屏判定要求文本 ≥4 字。
- **别把不可加的统计塞进 `publish()`**：`LiveWindow.add()` 只做可加的（词频计数）；
  热点突增/账号画像是跨片比较，放在请求时算（6.3 ms / 6 秒一次）。

---

## 8. 数据与房间的事实（省得重新摸索）

**房间（本会话实测，2026-10-01）**：

| 房间 | 真实 room_id | 主播 uid | 昵称 | 当时状态 |
|------|--------------|----------|------|----------|
| 804456 | 804456 | 8431064 | 六式zy | **直播中**，一度很冷（常只有 10 条补种） |
| 7878（短号） | 23438368 | 403637371 | 老飞宇66 | 未开播；压测/多房间验收用它 |
| 8432038 | 8432038 | 267766441 | 血狼破军 | 轮播（`live_status=2`） |
| 23776378 | 23776378 | 432131619 | 绝命笋干 | 直播中 |

**接口事实**：

* `get_anchor_in_room` **匿名可用**（主播昵称靠它）；`getInfoByRoom` 匿名 **-352**（别用）。
* 房间发现类接口（`getList`、`search/getSearchList`、`tools/hot_rooms.py`）会被 **-352**；
  `room_init` + `gethistory` 可用。**热门房间号请让需求方直接给**。
* 匿名连接时昵称会被打码（`LOG_IN_NOTICE`），登录态恢复正常。
* 落盘命名 `{room_id}_{bucket}.{ext}`（按小时轮转）；多房间 = 各自文件 + 共享 SQLite。

**本机数据现状**：

* `data/danmaku.db`：**72 MB**，`schema_version=3`，17,388 条事件，38 个会话。
* 主要房间分布：8432038（11,949）/ 804456（2,974）/ 23438368（1,676）。
* 索引齐全：`idx_event_room_ts`、`idx_event_room_ts_type`（V3-a 加的）、`type_ts`、`uid`、`msgid`、`session`。
* ⚠️ 38 个会话里有 **26 个 `ended_at` 为 NULL** —— 绝大多数是**沙箱回收进程组**的产物
  （见 §7.6），不是停止逻辑的 bug（优雅停止有端到端验证）。

---

## 9. 交接时的工作区状态

- **无未完成的重构**，代码与文档一致。
- **不是 git 仓库**（没有 `.git`），无法用 `git log`/`git diff` 追溯 —— 改动历史只能看文档。
- **无残留面板进程、无 `.runtime/`、无临时脚本**（本会话结束时已用
  `Get-Process -Name bili-danmaku` + `Test-Path .runtime` 逐项核对；见 §7.3 关于孤儿进程的说明）。
- `.gitignore` 覆盖：`.venv/`、`data/`、`web/node_modules/`、`web/dist/`、`.runtime/`、
  `.secrets/`、`.matplotlib-cache/`、`data_wc/`、`data_stress/`、`src/bili_danmaku/web_dist/`。
- `data/annotations.db`：**本来不存在**（我没有为验证留下标注数据）。

---

## 10. 上手第一件事：重建基线（复制粘贴即可）

```powershell
cd F:\CodingBox\DeepseekHarness\LiveDanmakuExtractor
$env:PYTHONIOENCODING="utf-8"

# 0) pip 体检（30 秒）。失败先看 §7.5，别再走离线兜底
.\.venv\Scripts\python.exe -m pip --version      # 期望 26.x（22.3.1 就会出 SSL 怪错）
.\.venv\Scripts\python.exe -m pip download six --no-deps -d build\pipprobe

# 1) 测试 + 覆盖率（期望：913 passed, 2 deselected，覆盖率 ~90.8%）
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term

# 2) 前端用例（期望 101 passed；--test-isolation=none 与 --import 都是必需的，见 §7.4）
cd web; npm test; cd ..

# 3) 确认前端产物是新的（当前应为 index-CPND-Vwp.js / index-CYieFQdb.css）
Select-String -Path web\dist\index.html -Pattern "assets/index-"

# 4) CLI 自检（不需要登录态）
.\.venv\Scripts\bili-danmaku.exe web --help
.\.venv\Scripts\bili-danmaku.exe web status
.\.venv\Scripts\bili-danmaku.exe sc --help

# 4b) 面板自检（前端定位 + 本进程起停 + 首页可访问；打包后必跑）
.\.venv\Scripts\bili-danmaku.exe verify

# 4b2) 时间轴的两种验证（都不需要真实桌面）
.\.venv\Scripts\python.exe tools\verify_empty_rooms.py       # 空房间引导（真 Vue SSR）
.\.venv\Scripts\python.exe tools\verify_timeline_canvas.py   # 真 Vue + jsdom：柱数/柱高/表头
#   下面这个需要面板已在跑（例如 exe web --port 8795）+ 本机装有 Chrome/Edge：
#   .\.venv\Scripts\python.exe tools\verify_canvas_pixels.py --port 8795
#   它用 headless Chromium 读回**真像素** —— jsdom 只能证明"调了哪些绘制指令"。

# 4b3) 扫码登录 / 二维码弹窗（**需要真实桌面 + 网络**）
.\.venv\Scripts\python.exe tools\verify_login_banner.py         # 登录条四态 + 弹窗尺寸承诺 + 成功自动关窗
#   下面这个会起托盘并**自动点一下「扫码登录」**，再用 EnumWindows 找那个窗口量尺寸：
#   .\.venv\Scripts\python.exe tools\verify_qr_window_desktop.py
#   （它先调 SetProcessDpiAwarenessContext，否则 GetClientRect 会被 DPI 虚拟化放大）

# 4c) exe 打包 + 冻结自检（约 30s；必须看到 served_real_frontend: True）
.\.venv\Scripts\python.exe tools\make_icon.py
.\.venv\Scripts\python.exe tools\build_exe.py

# 4d) 托盘/GUI 冒烟（**需要真实桌面**；不进入 mainloop）
.\.venv\Scripts\python.exe tools\smoke_tray.py
.\.venv\Scripts\python.exe tools\smoke_exe_doubleclick.py          # 需先 build_exe.py
#   双击行为（不带参数）用： $env:SMOKE_MODE="A"; .\.venv\Scripts\python.exe tools\smoke_exe_doubleclick.py

# 5) 面板 + 新接口自检（真机）
.\.venv\Scripts\bili-danmaku.exe web --port 8791 --rooms 804456
# 另开一个 PowerShell：
#   curl.exe "http://127.0.0.1:8791/api/room-info?room=804456"           # 期望 anchor_name
#   curl.exe "http://127.0.0.1:8791/api/history?room_id=804456&limit=5" # 期望 5 条瘦投影
#   curl.exe "http://127.0.0.1:8791/api/density?room_id=804456"         # 期望 buckets
#   curl.exe "http://127.0.0.1:8791/api/analysis?room_id=804456"        # 期望 words/spam/suspicious
#   curl.exe -X POST "http://127.0.0.1:8791/api/annotations" -H "Content-Type: application/json" -d "{\"room_id\":804456,\"recv_ts\":1790000000,\"label\":\"测试\"}"
# 用你自己的 PowerShell 验证「关终端仍存活」：
#   .\.venv\Scripts\bili-danmaku.exe web stop

# 6) 改前端之后必须重建
cd web; npm run build; cd ..
```

**注意**：`pytest` 默认 `-m 'not live'`；联网用例要
`$env:BILI_LIVE_ROOMS="7878"` 再用 `-m live`。

**分文件测试数**（706 collected，供你判断"我的改动没弄丢用例"）：

```
test_analysis.py 46 | test_annotations.py 19 | test_api.py 7   | test_api_http.py 26
test_cli.py 59     | test_client.py 17       | test_collector.py 19 | test_config.py 15
test_credentials.py 22 | test_dedupe.py 8    | test_filters.py 34 | test_gui.py 22
test_history.py 19 | test_history_api.py 18 | test_launcher.py 10 | test_login_utils.py 26
test_panel.py 10   | test_parser.py 46       | test_protobuf_lite.py 25 | test_protocol.py 17
test_rooms.py 27   | test_runtime.py 30      | test_sc.py 28 | test_server.py 39
test_server_sqlite.py 47 | test_sinks.py 13  | test_sinks_m2.py 22 | test_stats_export_api.py 9
test_store.py 18   | test_store_extra.py 12  | test_analysis_words.py 14 | test_export_stats.py 13
test_text.py 44    | test_utils.py 5         | test_wbi.py 9     | test_login.py 25
test_login_module.py 26 | test_launcher_extra.py 29 | test_health_attention.py 19
```

---

## 11. 会话目标（goal）的状态

**当前没有活跃的 goal**（上一个 V3-a 目标已 `complete`）。
历史遗留的 `goal-1be86381-...` 处于 `paused`（它写的是 M4 待办，**早已完成**）。

> **模型无法自行恢复或重写 paused 的 goal** —— 要么由人类在界面上 resume/edit，
> 要么忽略它、用普通对话继续（V2/V3 的工作都是在它 paused 时完成的）。

---

## 12. 一句话给新会话的建议

> 1. 先跑 §10 的基线命令（约 1 分钟）；
> 2. 读本文件 **§7（踩坑）与 §15（负面清单）** —— 这两节能省掉大部分试错；
> 3. 想做功能：先读 [`ROADMAP.md`](ROADMAP.md) 的**阶段门槛 G1–G4 与预留扩展点**
>    （新增东西该从哪儿进、不该改哪儿）；**G4 未过就不要碰外发数据的功能**；
> 4. 想做工程债：**保留策略/磁盘治理**是收益最直接的一条（库已 72 MB 且单调增长）；
> 5. 验证功能时**优先写真机脚本 + 单测**，别依赖截图 ——
>    本会话证明"逻辑用例 + HTTP 断言"比 GUI 点击可靠得多（GUI 会被窗口抢占干扰）。

---

## 13. 交付物清单（**V3 全阶段**新增/大改的文件）

| 文件 | 角色 | 规模 |
|------|------|------|
| `src/bili_danmaku/store.py` | V3-a：`iter_history()`（复合游标）/ `density()` / `_project()`（瘦投影） | 653 行 |
| `src/bili_danmaku/server/app.py` | V3 全部端点：history/density/sessions/annotations/analysis + `_stream_export` | 1098 行 |
| `src/bili_danmaku/sinks/sqlite.py` | 新增 `idx_event_room_ts_type`；`SCHEMA_VERSION` 2→3 | 224 行 |
| `src/bili_danmaku/annotations.py` | 🆕 V3-a2 标注存储（**独立库**） | 250 行 |
| `src/bili_danmaku/rooms.py` | 🆕 V3-a3 房间号/链接解析 | 102 行 |
| `src/bili_danmaku/analysis.py` | 🆕 V3-b 本地感知分析（纯函数） | 412 行 |
| `src/bili_danmaku/server/hub.py` | V3-b：`LiveWindow` 有界分析窗口 + `window_snapshot()` | 404 行 |
| `src/bili_danmaku/config.py` | `sinks.annotations_path` | 179 行 |
| `src/bili_danmaku/server/panel.py` | V2-d/V3-a3：房间缓存 + 链接解析接入 | 298 行 |
| `src/bili_danmaku/cli.py` | 接入 `parse_room_input`（`probe`/`sc` 也吃链接） | 2038 行 |
| `web/src/composables/useHistory.js` | 🆕 双层数据流 + 游标分页 + 标记状态机 | 16 KB |
| `web/src/components/DensityBar.vue` | 🆕 时间轴密度条（粒度可切、单格高亮） | 8 KB |
| `web/src/components/BookmarkBar.vue` | 🆕 标记条（打点/跳转/区间导出） | 7 KB |
| `web/src/components/AnalysisPanel.vue` | 🆕 感知分析面板（点词过滤） | 10 KB |
| `web/src/components/{TopBar,TypeSelector,TypeBars,RoomPanel,StatsPanel,StreamList,FilterBar}.vue` | V2-d/V3-a/a3 大改 | — |
| `web/src/{App.vue,api.js,format.js,style.css}` | V3 全部接线 | — |
| `web/test/history.test.mjs` | 🆕 **19 项**前端逻辑用例（node:test，零新依赖） | 17 KB |
| `tests/test_history.py` / `test_history_api.py` | 🆕 19 + 18 项 | — |
| `tests/test_annotations.py` | 🆕 19 项 | — |
| `tests/test_rooms.py` | 🆕 27 项 | — |
| `tests/test_analysis.py` | 🆕 46 项 | — |
| `HANDOVER.md` | 权威状态库（V2-d §5.7、V3-a §5.8、V3-a2 §5.9、V3-a3 §5.10、V3-b §5.11） | 71 KB |
| `TECH_DESIGN.md` | 实现级文档（§13.6 复盘器、§13.7 标注、§13.8 感知分析） | 103 KB |
| `ROADMAP.md` | 未来方向（V3-a/a2/R1/R2/3A 标记实现；3B/V3-c 待做） | 39 KB |
| `README.md` / `web/README.md` | 用户用法 / 前端说明 + **37 条手测清单** | — |
| `SESSION_HANDOVER.md` | 本文件（V3-b 3A 版本） | — |

---

## 14. 交接前自检（诚实记录）

| 检查 | 结果 |
|------|------|
| 全量测试 | ✅ 实测 **704 passed, 2 deselected**（706 collected） |
| 前端测试 | ✅ `cd web; npm test` → **19 passed** |
| 覆盖率门禁 | ✅ **90.90%**（门禁 85%）；`analysis.py` 95%、`hub.py` 96%、`rooms.py` 99%、`annotations.py` 95% |
| 前端构建 | ✅ `npm run build` → 38 modules（`index-DYqGL4mp.js` / `index-CM2dI2GD.css`） |
| V3-b 真机验证 | ✅ `/api/analysis` 在真实房间返回词频/词数/窗口跨度；**离线房间误报已消除**（`suspicious=0`、`rate_available=false`） |
| V3-a3 真机验证 | ✅ 18 项检查全 PASS（跳转锚点、三档粒度、累计榜、链接解析、错误码） |
| V3-a2 真机验证 | ✅ 建/列/区间导出（范围头 + CSV BOM）/删除；**采集库无 `bookmark` 表** |
| V3-a 性能门槛 | ✅ 单页 400 条 **18 ms**（门槛 200）、密度条 **38~95 ms**（门槛 100），20 万行合成库 |
| 面板端到端 | ✅ `start → health → stats → shutdown → 进程退出 + 状态文件清理 + session.ended_at 回填` |
| 工作区清理 | ✅ 无残留面板进程、无 `.runtime/`、无临时脚本、无 `data/annotations.db` |
| **"关终端仍存活"** | 🔴 **实测不成立** —— 需求方报告关掉终端后面板也停；本会话已定位机制（见 **§5.1 第 0 条** / HANDOVER §5.12），**未修复** |
| SC 金额单位 | ⚠️ **仍未验证**（没抓到真实 SC），有 `--price-unit milli` 逃生舱 |
| 热点突增真实分布 | ⚠️ **未验证**（实测时段两个房间都没新弹幕）；合成多片流 + 单测覆盖 |
| 前端交互手感 | ⚠️ 逻辑有 19 项用例；滚动/点击手感需人工过一遍（GUI 自动化会被窗口抢占） |

---

## 15. 不要做的事（负面清单）

**范围与边界**

- ❌ 不要把礼物/上舰重新纳入采集（DEC-1）。
- ❌ 不要把面板 host 改成 `0.0.0.0`、或给控制端点放开非回环来源（DEC-4）。
- ❌ 不要加开机自启 / Windows 服务（DEC-5）。
- ❌ **不要接入任何"把弹幕发出去"的能力**（LLM / Webhook / 云端）—— 那是隐私基线变更，
  未过 **G4**（书面决策）之前一律不做。
- ❌ 不要为"未来可能的多平台"提前抽象 `Adapter`：**先有第二个平台，再抽接口**。

**实现约束**

- ❌ 不要在只读的 `store.py` 里做 schema 迁移（`mode=ro`，会直接报错）；迁移只走 `sinks/sqlite.py`。
- ❌ 不要把标注表加进 `danmaku.db`（独立库是对外承诺，有测试断言守着）。
- ❌ 不要把 `/api/history` 的瘦投影逻辑塞回 `/api/events`（会悄悄改窄既有契约）。
- ❌ 不要把不可加的统计塞进 `hub.publish()`（它必须保持 O(1)，`LiveWindow.add` 是上限）。
- ❌ 不要用 `v-html` 渲染弹幕正文（XSS）；表情图片只接受 `http(s)`。
- ❌ 不要用 `getInfoByRoom` 取主播昵称（实测 -352）；用 `get_anchor_in_room`。
- ❌ 不要给 `anchor_info()` 加重试（昵称是展示信息，重试只会加重风控）。
- ❌ 不要为了让数字好看给速率类指标"补一个窗口下限"——
  那会把真实爆发一起压平（我第一次就这么修错了）。
- ❌ 不要把启发式评分包装成结论：UI 必须用"疑似/仅供参考"并**显示理由**。
- ❌ 不要删掉感知分析面板底部那行"仅供参考、非判定；数据不出本机"（隐私承诺）。

**交互与布局**

- ❌ 不要把房间号输入搬回顶栏（顶栏只放跨面板过滤/操作，房间归右栏）。
- ❌ 不要恢复时间轴的「更早/更新」按钮（实测语义最难说清，已删）。
- ❌ 不要改 `jumpTo` 的窗口锚点语义（必须是"该格结束时刻"，否则高亮又偏一格）。
- ❌ 不要把"自启动累计"与数据库时间窗统计混着读（侧栏有口径角标就是为这个）。
- ❌ 改 `.app` 网格行数时要同步 `ScBanner`（它是第 3 行，空时高度为 0）。

**工具与流程**

- ❌ 不要用 PowerShell 的读写管道改中文文件（会把 UTF-8 变乱码）。
- ❌ 不要把含中文的脚本塞进 `python -c "..."`（PowerShell 会截断字符串）—— 先写成文件。
- ❌ 不要为了"停止面板"改用 `CTRL_BREAK_EVENT` 或 `taskkill`（不带 `/F`）—— 实测无效。
- ❌ 不要在一个被拒的沙箱命令上换写法重试：读 `[sandbox: ...]` 标记，需要放宽就**当次**申请。
