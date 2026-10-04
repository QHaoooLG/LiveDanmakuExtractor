"""扫码登录的**二维码弹窗**（独立顶层窗口）。

## 为什么必须独立成窗口（这次改动的直接原因）

原来二维码是塞在启动面板里的：面板 `resizable(False, False)`、宽度固定约 480px，
而二维码是 `box_size=8` 渲染出来的（约 296×296 起，内容长一点还会更大）。
两者一旦打架，二维码就被**裁掉** —— 而二维码缺一个角就扫不出来，
用户看到的只是"扫了没反应"。

独立窗口还顺带解决三件事：

1. **尺寸可以按图片算**，而不是"塞进剩下的空间"（见 `qr_area_px`）；
2. 面板本身不用再为二维码预留空间，登录中/未登录时的排版不再跳动；
3. 面板可以继续被关掉/最小化，二维码窗口自己待在最前面 —— 用户本来就该
   在手机上扫码，不该被面板挡住。

## 尺寸是**算出来的**，不是自适应出来的

`qr_area_px()` 把"图片像素 + 四周留白"换算成 Tk 需要的尺寸，并且**不信 `pack`
的自适应**。原因是 Tk 在高 DPI 下会按 `tk scaling` 放大控件，自适应出来的窗口
可能小于放大后的图片；而这里在 `update_idletasks()` 之后直接按下**真实大小**
并留出余量，所以"一定装得下"。

## 线程模型

轮询走 `root.after`，**全部在主线程**里跑（tkinter 不是线程安全的；
登录流程本身在 `login.QrLoginSession` 的后台线程里）。
`poll()` 只回最新一条事件，所以这里只按"当前状态"刷新，不重放历史。
"""

from __future__ import annotations

import base64
import logging
import tkinter as tk
from typing import Any, Callable

from ..credentials import png_size
from . import _win32

log = logging.getLogger("bili_danmaku.gui.qr_window")

#: 二维码四周到窗口边缘的像素留白（每边）
QR_PADDING_PX = 22
#: 两侧留白合计之外的额外余量：给字体渲染误差留一口气
QR_EXTRA_PX = 8
#: 面积兜底上限（异常大的二维码/极端 DPI 时，至少让窗口留在屏幕内）。
#: 定得比常见屏幕（约 864px 高）矮一点：超过这个数窗口下缘会被裁到屏幕外，
#: 用户连"把窗口拖上来"都做不到 —— 那比二维码小一点更糟。
QR_MAX_AREA_PX = 800
#: 轮询间隔（毫秒）。与面板原来的一致 —— 扫码状态变化本来就以秒计。
POLL_INTERVAL_MS = 250

#: 弹窗配色（与启动面板保持一致，但不 import 面板，避免两个 GUI 模块互相依赖）
BG = "#15181d"
FG = "#e6e6e6"
MUTED = "#8b93a1"
ACCENT = "#7dd3fc"
OK_FG = "#86efac"
BAD_FG = "#fca5a5"
WARN_FG = "#e3b341"
QR_BG = "#ffffff"


def qr_area_px(image_px: tuple[int, int] | None) -> int:
    """算出承载二维码所需的正方形边长（Tk 像素单位）。

    抽成纯函数是为了能离线断言"一定装得下"这条承诺（GUI 本身点不了）：
    * 没量到图片尺寸（PNG 解析失败）时给一个能装下常见二维码的保守值；
    * 结果**永远大于**图片边长（每边留白 + 额外余量）；
    * 再大也不超过 `QR_MAX_AREA_PX`（否则高 DPI 下窗口会跑到屏幕外，
      那比"稍微裁一点"更糟 —— 用户连扫都够不着）。
    """
    side = max(image_px or (0, 0))
    if side <= 0:
        return QR_MAX_AREA_PX
    return min(side + 2 * QR_PADDING_PX + QR_EXTRA_PX, QR_MAX_AREA_PX)


class QrLoginWindow:
    """显示二维码 + 状态文案 + 取消按钮的独立窗口。

    生命周期由 `LauncherWindow` 管：它建窗口、喂事件、在登录结束时调 `close()`。
    这个类**不碰登录会话**（只读 `session.qr_png` / `session.poll()`），
    这样"登录流程"与"怎么显示"仍然是两件事。
    """

    def __init__(
        self,
        master: tk.Misc,
        session: Any,
        *,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.session = session
        self.on_closed = on_closed
        self.closed = False
        #: 结束原因：`""`（还在进行）/ `"ok"` / `"cancelled"` / `"failed"`。
        #: 调用方在 `on_closed` 里读它来决定收尾文案 —— 判断只有这一处，
        #: 不必再维护一个"成功了吗"的标志位跟它对账。
        self.outcome = ""
        self._photo: Any = None  # PhotoImage 必须留引用，否则被 GC 掉、图片变空白
        #: 已经显示过的二维码字节（同一张不重复设置，减少闪烁）
        self._shown_png: bytes | None = None
        self._area_px = QR_MAX_AREA_PX

        self.win = tk.Toplevel(master)
        self.win.title("扫码登录 · B站弹幕提取姬")
        self.win.configure(bg=BG)
        # **可调整大小**：Tk 会在 DPI 变化时按 `wm grid` 重算几何，不允许调整
        # 会让它连"把窗口放大到装下图片"这条路都走不了（见 _fit_image）
        self.win.resizable(True, True)
        # ⚠️ **刻意不设 `transient(master)`**：Tk 会把 transient 子窗口跟着主窗口
        # 一起隐藏/最小化。而"关闭启动面板 = 最小化到托盘"是本程序的常态，
        # 那一刻二维码正需要留在屏幕上给手机扫 —— 跟主窗口绑在一起正好帮倒忙。
        # （实测：主窗口 `withdraw()` 时 transient 的 Toplevel 会停在
        # `state=withdrawn`、`winfo_width()==1`，连 deiconify 都拉不起来。）
        #
        # 置顶：用户此刻正拿着手机对着屏幕，被别的窗口盖住是最糟的体验。
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        self._set_icon()

        outer = tk.Frame(self.win, bg=BG, padx=QR_PADDING_PX, pady=QR_PADDING_PX)
        outer.pack(fill="both", expand=True)

        tk.Label(
            outer, text="请用 B 站手机客户端扫码登录", bg=BG, fg=FG,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack()

        # 白色底：二维码必须落在白底上，深色主题下直接贴图会让识别率变差
        self.image_box = tk.Frame(outer, bg=QR_BG, bd=0, highlightthickness=0)
        self.image_box.pack(pady=(12, 0))
        self.image_label = tk.Label(self.image_box, bg=QR_BG, bd=0)
        self.image_label.pack()

        self.status_label = tk.Label(
            outer, text="正在申请二维码…", bg=BG, fg=MUTED,
            font=("Microsoft YaHei UI", 9), wraplength=QR_MAX_AREA_PX,
        )
        self.status_label.pack(pady=(12, 0))

        row = tk.Frame(outer, bg=BG)
        row.pack(pady=(12, 0))
        self.cancel_btn = tk.Button(
            row, text="取消登录", command=self.cancel,
            bg="#22262e", fg=FG, activebackground="#2c313a", activeforeground=FG,
            relief="flat", bd=0, padx=14, pady=5,
            font=("Microsoft YaHei UI", 9), cursor="hand2",
        )
        self.cancel_btn.pack()

        # 先按**真实布局**量一次：进到 `_measure()` 就不会停在兜底的大窗口上
        self._fit()
        self.win.after(POLL_INTERVAL_MS, self._poll)

    # ------------------------------------------------------------------ #
    # 外观
    # ------------------------------------------------------------------ #

    def _set_icon(self) -> None:
        path = _win32.find_icon()
        if path is None:
            return
        try:
            self.win.iconbitmap(default=str(path))
        except tk.TclError as exc:  # pragma: no cover - 取决于平台/文件
            log.debug("二维码窗口设置图标失败：%s", exc)

    def _center(self) -> None:
        """把窗口放到屏幕中央（而不是跟着面板偏移）。

        用户要拿手机对准它，居中比"贴着父窗口"更容易对准。
        """
        area = self._area_px
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        x = max((screen_w - area) // 2, 0)
        y = max((screen_h - area) // 3, 0)  # 略偏上：手机举起来正对视线
        self.win.geometry(f"{area}x{area}+{x}+{y}")
        self.win.minsize(area, area)

    def _measure(self) -> int:
        """按**当前布局**算出装得下二维码所需的正方形边长（Tk 像素）。

        `winfo_reqwidth()` 是 Tk 已经算进 `tk scaling` 的请求尺寸 ——
        在二维码这种"差几像素就扫不出来"的地方，宁可窗口大一点。
        """
        self.win.update_idletasks()
        if self._photo is not None:
            # 用图片控件的请求尺寸（含 DPI 缩放），它才是真正会被排布的大小
            need_w = self.image_box.winfo_reqwidth()
            need_h = self.image_box.winfo_reqheight()
        else:
            # 还没拿到二维码：按兜底值先开窗，等图片到了 `_fit()` 会重算
            need_w = need_h = QR_MAX_AREA_PX - 2 * QR_PADDING_PX
        # 标题 + 状态行 + 按钮行 + 三个间距
        extra = self.status_label.winfo_reqheight() + self.cancel_btn.master.winfo_reqheight() + 60
        return max(need_w, need_h) + 2 * QR_PADDING_PX + extra

    def _fit(self) -> None:
        """量一次并把窗口调到位（居中 + 最小尺寸）。"""
        self._area_px = min(self._measure(), QR_MAX_AREA_PX)
        self._center()
        # 拉起来：不显式 `deiconify` 的话，某些窗口管理器下 Toplevel 会停在
        # 未映射状态（`winfo_width()` 一直是 1），看起来就是"点了没反应"。
        try:
            self.win.deiconify()
            self.win.lift()
        except tk.TclError:  # pragma: no cover - 窗口已被销毁
            pass

    # ------------------------------------------------------------------ #
    # 事件循环
    # ------------------------------------------------------------------ #

    def _poll(self) -> None:
        if self.closed:
            return
        if self.session is None:
            self.close()
            return

        event = self.session.poll()
        if event is not None:
            self.handle(event)
            # `handle()` 在 ok / failed 时会关掉窗口，这时不必再排下一次
            if self.closed:
                return

        self.win.after(POLL_INTERVAL_MS, self._poll)

    def handle(self, event: dict[str, Any]) -> None:
        """按一条扫码事件刷新界面。**公开**是为了能在无桌面环境下直接测。"""
        state = str(event.get("state") or "")
        if state == "qr":
            self.show_qr(self.session.qr_png)
        elif state == "waiting":
            self.status_label.configure(
                text=str(event.get("text") or "等待扫码…"), fg=MUTED
            )
        elif state == "scanned":
            self.status_label.configure(text="已扫码，请在手机上确认", fg=OK_FG)
        elif state == "expired":
            # 换新码：把旧图标记为"待替换"，新图到了会自己更新（白底保持不变）
            self._shown_png = None
            self.status_label.configure(
                text="二维码已失效，正在换一张…", fg=WARN_FG
            )
        elif state == "ok":
            self.outcome = "ok"
            self.status_label.configure(text="登录成功，凭据已保存", fg=OK_FG)
            self.close()
        elif state in ("failed", "cancelled"):
            self.outcome = state
            self.status_label.configure(
                text=str(event.get("message") or "登录已取消"), fg=BAD_FG
            )
            self.close()

    def show_qr(self, png_bytes: bytes | None) -> None:
        """把二维码 PNG 显示出来（并且保证窗口装得下）。"""
        if not png_bytes or png_bytes == self._shown_png:
            return
        try:
            photo = tk.PhotoImage(data=base64.b64encode(png_bytes).decode("ascii"))
        except tk.TclError:
            self.status_label.configure(
                text="二维码显示失败，请关掉本窗口后重试", fg=BAD_FG
            )
            return

        self._photo = photo  # 必须留引用
        self._shown_png = png_bytes
        self.image_label.configure(image=photo)
        # 尺寸按**图片真实大小**算（`png_size` 只查文件头，不解码图片）
        self._area_px = max(qr_area_px(png_size(png_bytes)), self._area_px)
        self._fit()
        if not self.status_label.cget("text") or "申请" in str(self.status_label.cget("text")):
            self.status_label.configure(text="等待扫码…", fg=MUTED)

    def cancel(self) -> None:
        """用户主动取消（点按钮或关窗口）。"""
        if self.closed:
            return
        # 只有"还在进行"才记成 cancelled：否则会把已经判定成功的结论覆盖掉
        if not self.outcome:
            self.outcome = "cancelled"
        session = self.session
        if session is not None:
            session.cancel()
        self.close()

    def close(self) -> None:
        """关掉窗口并通知调用方（重复调用安全）。"""
        if self.closed:
            return
        self.closed = True
        self._photo = None
        try:
            self.win.destroy()
        except tk.TclError:  # pragma: no cover - 窗口已被销毁
            pass
        if self.on_closed is not None:
            callback, self.on_closed = self.on_closed, None
            callback()
