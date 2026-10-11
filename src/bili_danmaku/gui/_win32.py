"""零依赖托盘图标的 Win32 绑定（只用标准库 `ctypes`）。

**为什么不用 `pystray` / `pywin32`**：
  * 本项目已经历过一次"pip 坏掉"的环境事故，**少一个依赖就少一份风险**；
  * `pystray` 的菜单能力不足 —— 状态行、动态置灰、分隔符、默认项都要自己手搓，
    既然如此不如直接用底层 API，反而更可控；
  * 实测 `Shell_NotifyIconW` + `CreatePopupMenu` 用标准库就能完整跑通。

## 踩过的坑（改这里之前务必看）

* **`Shell_NotifyIconW` 的结构体大小**：`NOTIFYICONDATAW` 的 `cbSize` 必须与
  Windows 期望的一致。用 `ctypes.sizeof` 得到的是完整 v4 大小，在 Win2000+
  都可以用，但**字段顺序/对齐必须完全正确**，否则 `Shell_NotifyIconW` 静默失败。
* **`Shell_NotifyIconW` 返回 0 不一定是致命错误**：在 Resource Explorer 未就绪、
  或进程刚启动时会失败，所以要**带重试**（见 `TrayIcon.show`）。
* **WNDPROC 是 64 位问题**：回调签名用 `WINFUNCTYPE` 时返回类型必须是
  `LRESULT`（`ctypes.c_ssize_t`），用 `c_long` 会抛 `OverflowError`。
* **`CreateSolidBrush`/`GetStockObject` 在 `gdi32`**，不在 `kernel32`。

## 与 tkinter 的协作

托盘需要一个**消息泵**才能收到鼠标事件。tkinter 自带事件循环，所以正确做法是
**把窗口消息泵进主循环**：`Win32Tray.pump()` 在每个 tick 里排空消息队列
（`PeekMessageW` + `TranslateMessage` + `DispatchMessageW`），由 `after()` 周期调用。
这样托盘与 tkinter 共用同一线程，无需多线程与锁。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

# --------------------------------------------------------------------------- #
# Win32 常量
# --------------------------------------------------------------------------- #

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WM_USER = 0x0400
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

IMAGE_ICON = 1
LR_DEFAULTCOLOR = 0x0000
LR_LOADFROMFILE = 0x00000010
LR_SHARED = 0x00008000

TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002
MF_DEFAULT = 0x00001000
MF_CHECKED = 0x00000008

PM_REMOVE = 0x0001
HWND_MESSAGE = wintypes.HWND(-3)

#: 托盘回调用的自定义消息（`WM_USER + N`，避免与系统消息冲突）
TRAY_CALLBACK = WM_USER + 20

#: `LRESULT` 是 64 位；用 `c_long` 会在 64 位 Windows 上 OverflowError（实测踩过）
LRESULT = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


# --------------------------------------------------------------------------- #
# 显式声明原型（不声明的话 ctypes 默认 int 返回，64 位上会截断句柄）
# --------------------------------------------------------------------------- #

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT

user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]

user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE

user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON

user32.DestroyIcon.argtypes = [wintypes.HICON]

user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.GetMenuItemCount.argtypes = [wintypes.HMENU]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, wintypes.LPVOID,
]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


# --------------------------------------------------------------------------- #
# 图标解析
# --------------------------------------------------------------------------- #

def icon_candidates() -> list[Path]:
    """按优先级列出 `app.ico` 的候选位置。

    与 `find_dist()` 同样的道理：**冻结后包内文件是合成的**，所以要找
    `_MEIPASS` 与 exe 自己所在目录（onedir 的 `_internal`）。
    """
    # 源码树布局：<repo>/src/bili_danmaku/gui/_win32.py
    #   parents[0]=gui  [1]=bili_danmaku  [2]=src  [3]=<repo>
    root = Path(__file__).resolve().parents[3]
    candidates = [root / "assets" / "app.ico"]

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / "app.ico")
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "assets" / "app.ico")
        candidates.append(exe_dir / "_internal" / "assets" / "app.ico")
    return candidates


def find_icon() -> Path | None:
    """定位 `app.ico`；找不到返回 None（托盘会退回内置应用图标）。"""
    for candidate in icon_candidates():
        if candidate.is_file():
            return candidate
    return None


def load_icon(path: Path | None = None, *, size: int = 0) -> int:
    """加载一个 `HICON`。

    `path=None` 时优先从**本进程 exe 内嵌资源**加载（`LoadIconW(None, "#1")`）——
    PyInstaller 的 `--icon` 会把图标写进 exe，用资源比找文件稳；
    找不到再退回磁盘上的 `assets/app.ico`。

    返回 0 表示失败（调用方应优雅降级，不要崩）。
    """
    # 1) exe 内嵌资源（`--icon` 写入的，资源 ID 通常是 1）
    if path is None:
        handle = kernel32.GetModuleHandleW(None)
        for res_id in (1, 2, 3):
            hicon = user32.LoadIconW(handle, wintypes.LPCWSTR(res_id))
            if hicon:
                return int(hicon)
        path = find_icon()

    # 2) 磁盘上的 .ico
    if path and Path(path).is_file():
        hicon = user32.LoadImageW(
            None, str(path), IMAGE_ICON, size, size, LR_LOADFROMFILE
        )
        if hicon:
            return int(hicon)

    # 3) 兜底：系统默认应用图标（保证托盘仍有东西可显示）
    IDI_APPLICATION = wintypes.LPCWSTR(32512)
    hicon = user32.LoadIconW(None, IDI_APPLICATION)
    return int(hicon) if hicon else 0


# --------------------------------------------------------------------------- #
# 菜单
# --------------------------------------------------------------------------- #

class MenuItem:
    """一个菜单项。

    `action` 为 None 表示分隔符；`enabled=False` 会置灰（用于"面板没跑时
    不能点停止"这类语义）。
    """

    __slots__ = ("label", "action", "enabled", "default", "separator")

    def __init__(
        self,
        label: str = "",
        action: object = None,
        *,
        enabled: bool = True,
        default: bool = False,
        separator: bool = False,
    ) -> None:
        self.label = label
        self.action = action
        self.enabled = enabled
        self.default = default
        self.separator = separator

    @classmethod
    def sep(cls) -> "MenuItem":
        return cls(separator=True)


def build_menu(items: list[MenuItem], *, base_id: int = 1000) -> tuple[int, dict[int, MenuItem]]:
    """创建 HMENU。返回 `(hmenu, {命令 id: MenuItem})`。

    调用方负责 `DestroyMenu`。
    """
    hmenu = user32.CreatePopupMenu()
    mapping: dict[int, MenuItem] = {}
    next_id = base_id
    for item in items:
        if item.separator:
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            continue
        flags = MF_STRING
        if not item.enabled:
            flags |= MF_GRAYED | MF_DISABLED
        if item.default:
            flags |= MF_DEFAULT
        user32.AppendMenuW(hmenu, flags, next_id, item.label)
        mapping[next_id] = item
        next_id += 1
    return int(hmenu), mapping


# --------------------------------------------------------------------------- #
# 托盘
# --------------------------------------------------------------------------- #

class TrayIcon:
    """一个托盘图标 + 消息窗口。

    用法（与 tkinter 共线程）::

        tray = TrayIcon(tooltip="...")
        tray.show()
        tray.on_click = lambda: ...
        tray.on_right_click = lambda: tray.popup_menu(items)
        root.after(50, lambda: (tray.pump(), root.after(...)))

    **不自己起线程**：消息必须在不拥有该窗口的线程外被 pump，
    这里选择让调用方（tkinter 主循环）统一驱动。
    """

    def __init__(self, *, tooltip: str = "", icon_id: int = 1) -> None:
        self.tooltip = tooltip[:127]  # szTip 是 128 个 wchar，含结尾 NUL
        self.icon_id = icon_id
        self.hwnd = 0
        self.hicon = 0
        self._nid: NOTIFYICONDATAW | None = None
        self._class_name = f"BiliDanmakuTray_{os.getpid()}_{id(self)}"
        self._wndproc_ref = WNDPROC(self._wnd_proc)  # 必须持有引用，否则会被 GC
        self._msg = wintypes.MSG()
        self._registered = False

        # 回调（由调用方赋值）
        self.on_click: object = None
        self.on_double_click: object = None
        self.on_right_click: object = None

    # -- 内部 ---------------------------------------------------------- #

    def _wnd_proc(self, hwnd, msg, wparam, lparam):  # type: ignore[no-untyped-def]
        if msg == TRAY_CALLBACK:
            if lparam == WM_LBUTTONUP and callable(self.on_click):
                self.on_click()
            elif lparam == WM_LBUTTONDBLCLK and callable(self.on_double_click):
                self.on_double_click()
            elif lparam in (WM_RBUTTONUP, WM_CONTEXTMENU) and callable(self.on_right_click):
                self.on_right_click()
            return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self) -> bool:
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = self._class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            # 已注册过（同一进程重复创建）也算可用
            if ctypes.get_last_error() not in (0, 1410):  # 1410 = class already exists
                return False
        else:
            self._registered = True

        hwnd = user32.CreateWindowExW(
            0, self._class_name, "tray", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, hinstance, None,
        )
        if not hwnd:
            return False
        self.hwnd = int(hwnd)
        return True

    def _build_nid(self) -> NOTIFYICONDATAW:
        self.hicon = load_icon()
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = wintypes.HWND(self.hwnd)
        nid.uID = self.icon_id
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = TRAY_CALLBACK
        nid.hIcon = wintypes.HICON(self.hicon)
        nid.szTip = self.tooltip
        return nid

    # -- 公开 API ------------------------------------------------------ #

    def show(self, *, attempts: int = 5) -> bool:
        """挂上托盘图标。

        会**重试**：Explorer 的托盘区在进程刚启动/刚登录时可能还没准备好，
        `Shell_NotifyIconW` 会返回 0，这不代表实现有问题。
        """
        if not self.hwnd and not self._create_window():
            return False
        self._nid = self._build_nid()
        for _ in range(max(attempts, 1)):
            if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)):
                return True
        return False

    def update_tooltip(self, tooltip: str) -> bool:
        """刷新悬浮提示（用于显示"运行中 12m30s"这类动态状态）。

        **先记状态再同步**：即使托盘图标还没挂上（`show()` 之前），
        这里也要把文字存下来 —— 否则"先设置状态、后挂图标"的顺序会丢掉设置。
        返回是否成功同步到系统托盘（未挂载时返回 False，但状态已保存）。
        """
        self.tooltip = tooltip[:127]
        if self._nid is None:
            return False
        self._nid.szTip = self.tooltip
        self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid)))

    def popup_menu(self, items: list[MenuItem], *, base_id: int = 1000) -> MenuItem | None:
        """在光标处弹出菜单，返回被点中的项（取消则 None）。

        用 `TPM_RETURNCMD`：菜单消息**不会**进本进程的队列，
        而是由 `TrackPopupMenu` 直接返回命令 id —— 这样就不必在 WNDPROC 里
        处理 `WM_COMMAND`，逻辑更集中（也因此**必须**显式 `SetForegroundWindow`，
        否则菜单不会因为没有前台窗口而立刻消失）。
        """
        if not self.hwnd:
            return None
        hmenu, mapping = build_menu(items, base_id=base_id)
        try:
            # 默认项（回车/双击触发）
            for cmd_id, item in mapping.items():
                if item.default:
                    user32.SetMenuDefaultItem(hmenu, cmd_id, 0)
                    break

            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(wintypes.HWND(self.hwnd))
            chosen = user32.TrackPopupMenu(
                hmenu,
                TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                point.x, point.y, 0, wintypes.HWND(self.hwnd), None,
            )
            # 规范要求的收尾（否则菜单可能残留）
            user32.PostMessageW(wintypes.HWND(self.hwnd), WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(hmenu)
        return mapping.get(int(chosen)) if chosen else None

    def pump(self, *, max_messages: int = 32) -> int:
        """排空窗口消息队列，返回处理的消息数。

        由 tkinter 的 `after()` 周期调用（见模块 docstring）。
        """
        count = 0
        while count < max_messages:
            if not user32.PeekMessageW(ctypes.byref(self._msg), None, 0, 0, PM_REMOVE):
                break
            user32.TranslateMessage(ctypes.byref(self._msg))
            user32.DispatchMessageW(ctypes.byref(self._msg))
            count += 1
        return count

    def is_visible(self) -> bool:
        """托盘图标当前是否**真的挂在系统托盘上**。

        注意：托盘的消息窗口是用 `HWND_MESSAGE` 建的**消息专用窗口**，
        它**不会**出现在 `EnumWindows` 的结果里 —— 所以不能靠枚举窗口判断，
        这就是本方法存在的理由。
        """
        return self._nid is not None and bool(self.hwnd)

    def hide(self) -> None:
        """移除托盘图标（退出前调用，避免留下"幽灵图标"）。"""
        if self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None
        if self.hwnd:
            user32.DestroyWindow(wintypes.HWND(self.hwnd))
            self.hwnd = 0
        if self._registered:
            user32.UnregisterClassW(self._class_name, kernel32.GetModuleHandleW(None))
            self._registered = False
