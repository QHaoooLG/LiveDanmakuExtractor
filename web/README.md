# web/ · 实时面板前端

Vite + Vue 3（`<script setup>`）单页应用，构建产物被 FastAPI 从 `web/dist` 直接托管。
**它只是采集数据的消费者**：抓取、过滤、落盘全在后端，前端挂掉不影响数据完整性。

- 后端接口清单见 [`../README.md`](../README.md#web-面板)；
- 实现级细节见 [`../TECH_DESIGN.md`](../TECH_DESIGN.md)；
- 新接手请先读 [`../HANDOVER.md`](../HANDOVER.md)。

---

## 1. 起前端

### 开发模式（热更新）

```powershell
# 终端 1：后端（默认 127.0.0.1:8787；被占用会自动向后找端口，注意看它打印的地址）
.\.venv\Scripts\bili-danmaku.exe web --rooms 804456

# 终端 2：前端
cd web
npm install
npm run dev        # http://127.0.0.1:5173
```

`vite.config.js` 把 `/api`、`/ws` 代理到 `http://127.0.0.1:8787`（`/ws` 走 ws 代理）。
**如果后端换了端口**（8787 被占用），要同步改 `vite.config.js` 里的 target，否则接口 502/404。

### 构建产物

```powershell
cd web
npm run build      # → web/dist（含 assets/）
```

构建后直接访问后端根路径即可（`bili-danmaku web` 会打印 `前端产物` 一行）。
**不构建也能用**：后端检测不到 `web/dist/index.html` 时返回一个内建提示页，
REST 与 WebSocket 接口完全不受影响 —— 面板不该因为缺 Node 而让整个工具不可用。

> 判断当前是否用上了构建产物：`GET /api/health` 里的 `frontend_built` 字段。

### 本机环境的三个坑

| 现象 | 原因 | 处理 |
|------|------|------|
| `npm install` 卡住/超时 | `registry.npmjs.org` 从 Node 侧不可达 | `web/.npmrc` 已指向 npmmirror，保持即可 |
| `EPERM` 写缓存失败 | npm 全局缓存在工作区外（`F:\nodejs_cache`），受限沙箱拒写 | `.npmrc` 已把 `cache` 指到工作区内的 `.npm-cache` |
| `npm run build` 报 `spawn EPERM` | Vite 在 Windows 上执行 `net use` 探测网络驱动器，受限沙箱禁止带管道的子进程 | 放宽沙箱后再构建（见 HANDOVER §4.2） |

---

## 2. 目录结构

```
web/
  index.html                     入口 HTML（#app 挂载点）
  vite.config.js                 base:'./'（产物可被任意挂载点托管）+ dev 代理
  src/
    main.js                      createApp(App).mount('#app')
    App.vue                      状态编排：房间/类型/关键词/统计口径、SC 阈值、房间号本地记忆、停止面板、toast
    api.js                       REST 薄封装（统一错误体解析 + query 序列化 + 控制端点 + room-info）
    format.js                    展示格式化：时间/类型标签/**类型配色 typeColor()**/粉丝牌/打码判定/高亮分段/表情分段/
                                 发送者 uid（`formatUid()`：没有发送者的事件返回空串）
    preferences.js               面板偏好的本地存取（`bili-danmaku.show-uid`）：默认显示 uid、可隐藏并记住；
                                 `storage` 由调用方传入，所以这段逻辑能在 node 里直接跑用例
    style.css                    深色主题与弹幕行样式；`.app` 四行网格、`.body` 两列
    composables/
      useLiveStream.js           WebSocket 连接、指数退避重连、内存环形缓冲、房间列表；
                                 **`setLimit()`** 可运行时收缩缓冲（历史浏览时收到 300）
      useHistory.js              **复盘器（V3-a）**：游标分页、历史窗口、合并视图、
                                 会话/时间轴状态机（纯逻辑，有离线用例）
    components/
      TopBar.vue                 顶栏：品牌、连接状态、**类型多选（TypeSelector）**、关键词（高亮/过滤）、
                                 SC 阈值、已接收、已运行（呼吸灯）、导出、停止面板
      TypeSelector.vue           自绘类型多选：收起态摘要+色块；展开态复选框+每类型条数（全选/清空）
      FilterBar.vue              上下文条：统计口径 + **历史浏览控件（进入/更早/更新/回到实时/会话）** + 过滤状态
      StreamList.vue             弹幕流：贴底自动滚动、**顶部触发加载更早并补偿 scrollTop**、
                                 关键词分段高亮、表情图片（历史条目读顶层 `emots`）、
                                 每行 **★ 打标记**（已标记的行高亮）、
                                 **每行默认显示发送者 uid**（流顶部开关可隐藏，选择存 localStorage）
      DensityBar.vue             **时间轴密度条（V3-a）**：点桶跳转、当前窗口高亮、P95 归一化、
                                 每格时长 1/3/5/10/15 分钟（桶数极多时压缩格子，避免溢出面板）
      BookmarkBar.vue            **标记条（V3-a2）**：打标记 / 跳到标记 / 选为导出起点 /
                                 按标记区间导出 / 删除 / 清空（放在时间轴下方，与密度条同一套时间语义）
      ScBanner.vue               高额 SC 横幅（阈值可调，按条依次展示）
      RoomPanel.vue              右栏**顶部**：直播间信息（「XXX 的直播间」+ 主播昵称高亮）、
                                 房间号输入/添加、本地记忆建议（datalist + 胶囊）、监控中房间列表
      StatsPanel.vue             右栏下半：速率/延迟、**类型分布横向柱状图（TypeBars）**、发言 TOP
      TypeBars.vue               类型分布横向柱状图：条长=占比、条数在右、每类型一色、底部合计
      AnalysisPanel.vue          **感知分析（V3-b 3A）**：实时词频 / 热点突增 / 疑似刷屏 /
                                 疑似异常账号（可折叠；**点词 → 只看含该词的弹幕**）
  test/
    history.test.mjs             **前端逻辑用例（22 项）**：游标翻页/去重/截断/合并/会话状态机
                                 + 标记增删/时间排序/选中态/anchor_id + jumpTo 的 off-by-one 回归
                                 + uid 显示偏好（默认显示、关掉后记在本地、storage 不可用时退回默认）
```

> **顶栏 vs 右栏 vs 上下文条的分工（别再混）**：
> 顶栏 = **跨面板的过滤与操作**（类型/关键词/SC 阈值/导出/停止）；
> 右栏顶部 = **所有房间相关的输入、添加、切换**（`RoomPanel.vue`）；
> 上下文条 = **视图状态与导航**（统计口径、历史浏览、会话过滤）。
> 判据是"这个控件改变的是全局行为、数据源，还是我当前在看哪一段"。

### 数据流（V3-a 起是**双层**的）

```
FastAPI /ws/live ──► useLiveStream.js（环形缓冲 2000；历史浏览时收缩到 300）
                              │
                              ├──────────────┐
                              ▼              ▼
FastAPI /api/history ──► useHistory.js ──► merged（按 id 去重、按 (recv_ts,id) 归并）
        /api/density ────────┘              │
                                            ▼
                                      App.vue（房间/类型/关键词过滤）
                                            ├─► StreamList.vue   （渲染最后 400 条）
                                            ├─► DensityBar.vue   （时间轴 + 点选跳转）
                                            ├─► BookmarkBar.vue  （标记：打点/跳转/区间导出）
                                            ├─► TopBar.vue       （类型多选、关键词模式、导出、停止面板）
                                            ├─► FilterBar.vue    （统计口径 + 历史控件 + 过滤状态）
                                            ├─► ScBanner.vue     （达阈值的 SC 弹横幅）
                                            ├─► RoomPanel.vue    （直播间信息、房间号输入/记忆、监控中列表）
                                            └─► StatsPanel.vue   （每 3s 拉一次 /api/stats，含类型分布柱状图）
/api/health ─────────► App.vue ──► TopBar（已运行时间）
/api/stats ──────────► App.vue ──► RoomPanel（已监控房间的 anchor_name）/ StatsPanel
/api/room-info ──────► App.vue ──► RoomPanel（**只输入、未监控**的房间号也先显示「XXX 的直播间」）
/api/history ────────► useHistory.js ──► StreamList（**瘦投影**，游标翻页）
/api/density ────────► useHistory.js ──► DensityBar（服务端 SQL 聚合）
/api/sessions ───────► App.vue ──► FilterBar（"只看某次抓取"下拉）
/api/annotations ────► useHistory.js ──► BookmarkBar + StreamList 的 ★（标记存**独立库**）
/api/annotations/export ◄── BookmarkBar「导出该区间」（按标记区间流式导出）
/api/analysis ───────► App.vue ──► AnalysisPanel（**内存窗口**上的本地统计，无外发）
                                   └─ 点词 → keyword + 模式切「过滤」（复用顶栏过滤，不另造状态）
POST /api/admin/shutdown ◄── TopBar「停止面板」（仅本机可用）
localStorage ────────► App.vue ──► RoomPanel（房间号历史 datalist）/ TopBar（SC 阈值、关键词模式）
                            └──────► StreamList（显示 uid 开关，键在 preferences.js）
```

### 测试（V3-a 起有前端用例了）

```powershell
cd web
npm test        # node --test --test-isolation=none test/history.test.mjs
```

**为什么是 `node:test` 而不是 Vitest**：这一层要验证的是**纯数据流逻辑**
（游标翻页、去重、截断、合并、会话状态机、标记增删），用 Node 内置测试运行器 + 已装的 `vue` 就够了，
**零新依赖**（不必动 `package.json` 的 devDependencies，也绕开了镜像/缓存那两个坑）。
需要组件级交互（点击、渲染）时再评估是否引入 `@vue/test-utils`。

> ⚠️ 受限沙箱里 `node --test` 默认会**派生子进程**而失败（`spawn EPERM`），
> 所以 `npm test` 固定带 `--test-isolation=none`（在当前进程内跑）。这不是测试写得不对。

---

## 3. 前端行为约定（改代码前请先看）

这几条都是踩过坑之后定下来的，不是随意选择：

1. **类型过滤在客户端做**，切换不重连。服务端也支持 `types=` 订阅过滤，但那样每次改动
   都要重连并丢掉已缓冲的内容。只有**切换房间**才重连（历史回填是按房间取的）。
   类型控件是**自绘下拉 + 原生复选框**（`TypeSelector.vue`）：上一版用原生
   `<select multiple size="1">`，收起态看不到已选了什么、也没有每类型的颜色/条数，
   所以换掉；原生控件白送的键盘可达性与"点外部收起"由 `<button>` +
   `<input type="checkbox">` + `document.pointerdown`/`Escape` 补回（改动时别把这两条弄丢）。
2. **关键词有两种模式，默认只高亮**：`highlight` 不做过滤，避免"搜不到就以为没数据"的误判；
   `filter` 只显示命中的弹幕。两种模式都用同一个输入框，切回高亮即恢复全部。
   过滤状态在 `FilterBar` 上用 `只看「xx」· 命中/总数` 显示出来，让用户知道"是过滤了，不是没数据"。
3. **关键词高亮走分段渲染，绝不使用 `v-html`**。弹幕正文是不可信的用户输入，
   直接注入 HTML 就是 XSS。表情图片同样只接受 `http(s)` 地址（`textSegments()` 里过滤）。
4. **自动滚动只在"贴底"时生效**。用户上滚查看历史时暂停滚动并显示"N 条新消息 ↓"，
   否则读历史会被不断涌入的新消息顶走。
5. **渲染上限**：`StreamList` 只渲染最后 400 条，`useLiveStream` 只缓冲最后 2000 条。
   更早的内容**通过 `/api/history` 游标翻页拿**（V3-a），不要试图在前端无限堆积，
   也不要靠"把缓冲调大"来解决 —— 那既崩内存又崩渲染，而且翻不到更早的会话。
6. **慢客户端会被服务端主动断开**（`SLOW_CONSUMER`）：如果看到这个提示，
   说明渲染已经跟不上消息速率。前端不重试风暴 —— 它按指数退避重连，
   这是**设计如此**：面板不允许反向影响采集。
   这也是 `/api/history` 必须返回**瘦投影**的原因：完整 `Event` 单条约 2 KB，
   400 条一页接近 800 KB，很容易把自己变成慢客户端。
7. **房间过滤依赖 `room_id`**：多房间时选"全部房间"会收到所有房间的事件；
   只订阅单个房间时服务端只回填该房间的历史。
8. **统计口径默认"从启动累计"**，数据来自 `hub` 的内存计数（`/api/stats?window=0`），
   所以它只反映**这个面板启动之后**看到的事件 —— 不含历史补种与往次运行的库内数据。
   想按库内时间窗统计就切到"最近时段"（那一档会用 SQLite 补延迟分位与发言榜）。
   这也是为什么侧栏标题会带一个口径角标：避免把两种数字混着读。
9. **停止面板是两步确认**，且停止后页面会轮询 `/api/health` 直到服务消失。
   **不要**用 `window.close()` 收尾 —— 非脚本打开的标签页，浏览器不允许脚本关闭。
10. **房间号历史只存"用户主动输入/添加过"的号**（`localStorage['bili-danmaku.room-history']`，上限 12 条），
    不是服务端返回的房间列表 —— 服务端没在监控的号也要记得住，这正是"记忆"与"已监控列表"的区别。
    添加成功后用**解析出的真实房间号替换短号**，避免同一房间同时留下短号与真实号。
11. **主播昵称（`anchor_name`）只影响展示**：取不到时退化成「房间 <id>」，
    前端不做重试风暴（`/api/room-info` 失败就静默保留上次内容）。
    已监控房间的昵称来自 `/api/stats` 的房间快照，**不要**再额外发一次 `/api/room-info`（那是白打一次接口）。
12. **历史游标必须成对传**（`before_ts`+`before_id` / `after_ts`+`after_id`）。
    `recv_ts` 会并列（实测房间 804456 有 393 组重复时间戳、最多 10 条同刻），
    只传 `before_ts` 会被后端当成"没有游标"，于是**重复返回最新一页**。
13. **`has_newer` 是本地状态，不是照抄服务端**。服务端的 `has_newer` 只说明"这一页之后还有没有数据"；
    用户向上翻几页后窗口尾部已不含最新数据，服务端会算出 `false` —— 照抄就**再也翻不回最新**。
    正确语义是"窗口是否已离开实时边缘"（`useHistory` 里的 `detached`），
    对称地还有"是否已翻到最早"（`exhausted`）。这两条各有一条用例守着，别改坏。
14. **向上翻页要补偿 `scrollTop`**：历史是往数组头部插的，不补偿用户会被瞬间弹走。
    实现在 `StreamList.preserveAnchor()`：必须**先记 `scrollHeight`、`nextTick` 后再按差值补**；
    只记 `scrollTop` 是不够的（内容变高后它的语义已经变了）。
15. **进入历史浏览时实时缓冲会收缩到 300 条**（`LIVE_TAIL_WHEN_HISTORY`）。
    这是刻意的：否则"历史窗口 + 持续增长的实时流"会让合并视图无限膨胀。
    退出历史浏览会恢复 2000 —— 不要把这个"临时降级"改成永久。
16. **历史条目与实时事件形状不同**：历史是瘦投影（主键在顶层 `id`、表情在顶层 `emots`），
    实时是完整 `Event`（主键在 `seq`、表情在 `extra.emots`）。
    统一在 `App.vue` 的 `unify()` 里做一次，**不要**在渲染层到处写兼容分支。
17. **打标记的时间点取"可见的最后一条弹幕"，不是墙钟**（`App.vue` 的 `playheadTs`）。
    若按墙钟取，用户在看历史时会把标记打到"现在"，而屏幕上显示的是几小时前 ——
    **这种错误从界面上完全无法察觉**。一条弹幕都没有时按钮禁用并说明原因。
18. **标记存独立库**（`{out_dir}/annotations.db`），前端不要把它当成采集数据：
    库不存在时后端返回**空列表**（不是 404/409），所以"加载失败"和"还没有标记"要能区分开。
19. **`anchor_id` 只是溯源信息**，后端不校验它是否存在（被钉的弹幕清理后标注依然有效）。
    ★ 的判定用**时间戳**（带 0.0005 秒容差吸收浮点尾差），不要用 `id` 匹配。
20. **点时间轴某一格时，窗口右端必须划在"该格结束时刻"**（`jumpTo` 传
    `until = ts + bucket_s`）。曾经传 `before_ts = ts`（"该时刻之前 N 条"），
    窗口右端正好落在该格**起点**上，于是整段窗口落在**上一格**、
    高亮就偏了一格 —— 这是实测反馈过的 off-by-one，有用例守着，别改回去。
21. **高亮分两种情形**：有 `focusTs`（刚点过某格）时**只亮那一格**；
    否则才按窗口区间亮（"我正在看哪一段"）。窗口是 300 条、可能跨好几格，
    用区间去推"我点了哪格"必然亮一大片。
22. **`jumpTo` 的格宽来自 `history.bucketS`**（先由后端自适应、之后是用户选的档位）。
    写死 60 秒会让"跳到某格"与画出来的格子对不上。
23. **不再有「更早 / 更新」按钮**（实测那两个按钮的语义最难说清：翻完后窗口既不在实时边缘、
    也不在某个具体时刻）。三类意图各只有一个入口：看更早 = 滚到顶自动加载；
    看某段 = 点时间轴；回最新 = 「回到实时」。`hasOlder`/`hasNewer` 状态机仍然保留
    （滚到顶加载靠 `hasOlder`），只是不再有按钮直接暴露。
24. **累计口径下发言榜来自 SQLite 的"该房间全部时间"**，不是"自面板启动以来" ——
    范围角标必须显示出来。**全部房间**走 15 秒 TTL 缓存（全库查询实测 101 ms，
    而面板每 3 秒拉一次）。
25. **感知分析是本地启发式，不是判定**：UI 文案必须是"疑似/仅供参考"，
    并且**必须把 `reasons` 显示出来**（只给一个分数会让用户无法复核）。
    面板底部的"数据不出本机"说明是隐私承诺，**不要删**
    （引入任何外发能力都要先过 ROADMAP §5.2 的 **G4** 门槛）。
26. **速率类结论在退化数据上要被放弃**，不要"补一个窗口把数字算出来"：
    历史补种会把一批弹幕写成同刻（甚至全挤在 1 秒内），
    这时 `count/span` 会给出"600 条/分钟"并把正常人报成疑似异常账号 ——
    实测踩过两次。后端用 `rate_available=false` 明确表达"这个结论不可用"，
    重复率那一项与时间无关、仍参与打分。

---

## 4. 接口契约（前端只依赖这些）

| 方法 | 路径 | 用在哪 |
|------|------|--------|
| GET | `/api/health` | 版本、`frontend_built`、落盘格式、订阅者数 |
| GET | `/api/rooms` | 房间列表与连接状态（含 `anchor_name`；WS 的 `hello`/`rooms` 也会推） |
| GET | `/api/room-info` | `room=<房间号/短号>` → `{room_id, anchor_name, anchor_face, live_status, title, monitored}`；只查不采 |
| POST | `/api/rooms` | `{"room": 804456}`；已监控 → 409；房间不存在 → 400 |
| DELETE | `/api/rooms/{id}` | 停止监控；不在监控中 → 404 |
| GET | `/api/events` | `room_id,type,q,since,until,limit,offset`（SQLite 优先，无库时降级内存）；**完整 `Event`** |
| GET | `/api/history` | `room_id,session_id,type,q,limit` + 游标 `before_ts`+`before_id` / `after_ts`+`after_id`；**瘦投影**、升序、带 `has_older`/`has_newer`/`oldest`/`newest`/`window_s`/`range` |
| GET | `/api/density` | `room_id,session_id,type,since,until,bucket_s` → `{bucket_s,buckets:[{ts,count,by_type}],total,by_type,range}` |
| GET | `/api/sessions` | `room_id,limit` → `{items:[{id,room_id,started_at,ended_at,events}]}` |
| GET | `/api/analysis` | `room_id,top,tokenizer` → `{messages,window_s,words,hotspot,spam,suspicious,note,...}`（**本地统计，无外发**） |
| GET | `/api/annotations` | `room_id,since,until,limit` → `{items:[Bookmark], summary:{count,lo,hi}}`；库不存在 → 空列表 |
| POST | `/api/annotations` | `{room_id, recv_ts, label?, note?, session_id?, anchor_id?}` → `201 {item}` |
| DELETE | `/api/annotations/{id}` | `{removed}` / 404 |
| GET | `/api/annotations/export` | `bookmark_id` + `to_bookmark_id?` + `format` + 筛选 → 流式 + `X-Range-Since/Until/Total-Count` |
| GET | `/api/stats` | `room_id,window`；**`window=0`（默认）= 自面板启动累计**，`>0` 为最近时段 |
| GET | `/api/export` | `format=csv\|jsonl\|txt` + 与 `/api/events` 相同的筛选参数 |
| POST | `/api/admin/shutdown` | 「停止面板」按钮：优雅停止（先 flush 落盘）。**仅本机来源**可用 |
| WS | `/ws/live` | `room_id`（可逗号分隔）/ `types` / `keyword` |

> 三个历史端点（`/api/history`、`/api/density`、`/api/sessions`）在**未启用 sqlite sink** 时
> 一律返回 **409** —— 即便 `data/danmaku.db` 是上次运行遗留的、文件确实存在。
> 否则用户会看到旧数据却以为在看这次的采集（与 `/api/export` 同一口径）。

WS 消息类型（`payload.t`）：

| `t` | 含义 | 前端行为 |
|-----|------|----------|
| `hello` | 建连成功，带房间列表与生效的过滤条件 | 重置错误提示、更新房间列表 |
| `event` | 一条弹幕事件（`data` 是完整 Event） | 追加到缓冲 |
| `rooms` | 房间状态变化（增删/重连/停止） | 更新顶栏与右侧栏 |
| `ping` | 空闲探测 | 忽略 |
| `error` | 如 `SLOW_CONSUMER` | 顶部/左下角提示 |

`Event` 里前端真正用到的字段：`recv_ts`、`type`、`room_id`、`uid`、`uname`、`text`、
`price`、`medal.level`/`medal.name`、`extra.emots`（表情图片映射）、`session_id`+`seq`（列表 key）。

历史投影（`/api/history` 的条目）字段更窄：`id`、`room_id`、`session_id`、`recv_ts`、`type`、
`uid`、`uname`、`text`、`price`、`medal_name`、`medal_level`、`emots`。
**没有 `seq`**（`id` 就是主键），**没有 `raw`/`cmd`/`extra`** —— 合并视图里靠 `unify()` 把实时事件补成同一形状。

---

## 5. 检查清单（改完前端手测一遍）

1. `bili-danmaku web --rooms 804456`，页面能连上（顶栏显示"已连接"）。
2. 新弹幕实时出现；上滚后出现"N 条新消息 ↓"且不再被顶走，点它能回到底部。
3. 关键词高亮正确，且**正文里的 HTML 不会被解释**（发一条 `<img src=x onerror=alert(1)>` 验证）。
4. **类型多选**：收起态显示"已选 N 种 + 色块"，展开态每行显示 中文名/`type`/条数；
   勾选/取消不会重连（顶栏连接状态不闪断）；「全选/清空/收起」行为正确，**点面板外部或按 Esc 会收起**。
5. **关键词切到「过滤」**只显示命中的弹幕，上下文条显示 `只看「xx」· 命中/总数`；
   切回「高亮」立即恢复全部。
6. **已运行时间**在跳，呼吸灯在闪（系统开启"减少动态效果"时应不闪）。
7. **统计范围**默认"从启动累计"且数字只增不减；切到"最近时段"后数字随窗口变化，
   侧栏标题的角标同步变化。
8. 切换房间会重连并只显示该房间的事件；移除房间后流里不再出现它的新事件。
9. 导出按钮能下到文件（CSV 用 Excel 打开不乱码）；`/api/health` 里 `frontend_built` 为真。
10. **停止面板**：点一次变「确认停止？」→ 再点 → 提示已停止；浏览器里 `/api/health` 变为不可达。
    随后 `bili-danmaku web status` 应显示"未在运行"，`.runtime/web.json` 已消失。
11. **右栏顶部「XXX 的直播间」**：选中一个已监控房间后应显示 `主播昵称` 的直播间（昵称高亮）；
    点右栏房间列表 = 只看该房间；房间号/短号/直播状态角标齐全。
12. **房间号记忆**：输入一个**未监控**的房间号并添加 → 它出现在「记忆：」胶囊里；
    **刷新页面**后胶囊与输入框的 datalist 建议仍在；点胶囊会把号填进输入框。
    短号添加成功后，记忆里存的是**真实房间号**而不是短号。
13. **类型分布柱状图**：每类型一行横条，**条数在横条右侧**、颜色各不相同、底部有合计；
    条长按占比（不是相对最大值），切"最近时段"后随窗口变化。
14. **右栏没有「服务」栏目**（版本/落盘格式/脱敏等已移出面板）。
15. **布局**：顶栏只有跨面板的过滤/操作，**没有房间下拉、没有房间号输入框**；
    窗口缩窄到 ~1000px 时顶栏换行但不重叠，弹幕流仍占满左侧。

### V3-a 复盘器（历史浏览）手测

16. 底部**时间轴**有内容：显示 `起 → 止 · 每格 N 分钟 · N 格 · 共 M 条 · 平均 x 条/格`，
    柱子高低与热闹程度一致。
17. **点时间轴某一格**：**高亮的必须就是你点的那一格**（不是前一格！），
    弹幕流跳到那段时间，顶部出现"历史浏览中 · N 条"与「回到实时」。
    换几个位置多试几次（尤其前几格与最后几格）。
18. **时间轴粒度**：点「1 分钟 / 3 分钟 / 5 分钟 / 10 分钟 / 15 分钟」→ 格子数量与宽度立刻变化，
    摘要行的"每格/格数/平均条数"同步更新；切粒度后**再点某格**，高亮仍然准确。
    切到 1 分钟且库跨度很大时，格子会被压扁但**不许溢出到右栏**（见 `DensityBar` 的 `dense`）。
19. 点「浏览历史」→ 加载最新一页；**滚到最顶**会自动继续加载更早的，
    并且**视口停在原处**（不会被弹走）—— 这是最容易做错的一条，务必手动确认。
20. 顶栏**没有**「↑更早 / ↓更新」按钮（已按反馈删除）；「回到实时」工作正常。
21. 顶部**会话下拉**选一次抓取 → 弹幕与时间轴都收窄到该会话；切回「全部会话」恢复。
22. 历史浏览中点「导出 CSV」→ 导出的内容**只含当前窗口的时间范围**（行数明显小于整库）。
23. **慢速自检**：在历史里停留几分钟，`/api/health` 的 `hub.subscribers` 应保持稳定，
    日志里不出现 `SLOW_CONSUMER`（瘦投影 + 400 条渲染上限就是为这个）。
24. **发言 TOP**：右栏在"从启动累计"下**默认显示**榜单，角标写明范围
    （选中房间 = "该房间全部时间"）；切"最近时段"后范围角标同步变化。
25. **直播间链接输入**（右栏最上面）：粘
    `https://live.bilibili.com/8432038?live_from=85001&spm_id_from=...`
    → 立刻显示"已识别房间号 8432038"并填入下方输入框；回车或点「解析并添加」直接开播监控。
    粘非 B 站链接或 `/blanc/8432038` 这类具名分区链接 → 明确提示识别失败（**不会瞎猜一个房间号**）。

### V3-a2 标记与切片手测

26. 底部「标记」条上输入标签 → 点「＋ 打标记」→ 右侧出现该标记（时间与本条弹幕一致）；
    留空标签时应落成「标记」。
27. 弹幕行左侧的 ☆ 悬停变亮、点一下变成 ★ 且整行淡黄高亮；该标记的
    "钉"标签出现（表示钉在某条弹幕上）。
28. 点标记上的时间 → 弹幕流跳到那一段（**视口落在该页底部**，也就是标记所在的时刻）；
    点「选为起点」→ 该标记显示"已选"，下方出现「从「xx」导出 → 到下一个标记「yy」」的说明。
29. 点「导出该区间 CSV」→ 下到的文件**只含该区间**（行数明显小于整库），
    Excel 打开不乱码；且导出的是**标记到下一个标记**之间，不是到结尾。
30. 删除某个标记后列表与 ★ 同步消失；「清空」会删掉本房间全部标记。
31. **隔离自检**：打几个标记后确认 `data/danmaku.db` **没有多出表**
    （标注在 `data/annotations.db`）；删掉 `annotations.db` 刷新页面，面板应显示 0 个标记而不是报错。

### V3-b 感知分析手测

32. 右栏底部出现「感知分析」，标题旁显示 `N 条 / 跨 x 秒`；**词频块默认展开**，
    每行是 `词 + 横条 + 次数`，说过该词的人数以 `n人` 显示。
33. **点词频里的任意一个词** → 顶栏关键词被填上该词且模式切到「过滤」，
    弹幕流只剩含该词的弹幕；上下文条显示 `只看「xx」· 命中/总数`。
34. 词频块、热点突增、疑似刷屏、疑似异常账号四块的标题**可以点击折叠/展开**。
35. 换到弹幕密集的房间待几分钟，**热点突增**里应出现 `×N` 或「新词」标记。
36. **疑似异常账号**每行必须同时显示用户名、分数**和理由**（如"重复率高（xx%）"）。
    若窗口里时间戳挤在同刻（房间未开播、只有历史补种），应**不出现任何疑似账号**，
    且后端 `rate_available` 为 false —— 这是刻意为之，别当成"功能没生效"。
37. 面板底部的说明行必须写着"仅供参考、非判定；数据不出本机" ——
    这是本功能的隐私承诺，**不要删**。

### 弹幕流 uid 显示开关手测

38. **每条弹幕默认带 `uid xxxx`**（在昵称右侧、颜色比昵称暗一档）；`room_stats` / `notice`
    这类**没有发送者**的行不显示 uid —— 不许出现 `uid -` 或 `uid undefined` 这种半截占位。
39. 弹幕流顶部的「显示 uid」开关：取消勾选 → 所有行的 uid 立刻消失；
    **刷新页面**后仍是隐藏；再勾上后恢复、刷新后仍是显示
    （键 `localStorage['bili-danmaku.show-uid']`，缺省 = 显示）。

