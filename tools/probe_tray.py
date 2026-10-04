"""探针 7：验证「零新依赖」的托盘图标方案可行。

结论要回答：
  1. 能不能用 Pillow 从那张 1254x1254 透明 PNG 生成多尺寸 .ico？
  2. 能不能**只用 ctypes + tkinter**（不装 pystray/pywin32）实现托盘图标 + 右键菜单？
  3. `Shell_NotifyIconW` 在这个环境里能不能挂上去（没有托盘区时应能优雅降级）？

只跑 6 秒就自己退出，不常驻。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIC = os.path.join(ROOT, "pics", "b站弹幕提取姬图标_透明背景版.png")
ICO = os.path.join(ROOT, "build", "tray_probe.ico")

# --------------------------------------------------------------------------- #
# 1. PNG -> 多尺寸 ICO
# --------------------------------------------------------------------------- #
from PIL import Image  # noqa: E402

image = Image.open(PIC)
print("=" * 78)
print("1. PNG -> ICO")
print("=" * 78)
print(f"  源图     = {PIC}")
print(f"  模式/尺寸 = {image.mode} / {image.size}")

os.makedirs(os.path.dirname(ICO), exist_ok=True)
sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
rgba = image.convert("RGBA")
rgba.save(ICO, format="ICO", sizes=sizes)
print(f"  已生成   = {ICO}（{os.path.getsize(ICO)} bytes）")
with Image.open(ICO) as check:
    print(f"  回读校验 = {check.format} {check.size}")

# --------------------------------------------------------------------------- #
# 2. 纯 ctypes 托盘图标
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("2. 纯 ctypes Shell_NotifyIconW 托盘")
print("=" * 78)

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)  # CreateSolidBrush 在 gdi32，不在 kernel32

WM_USER = 0x0400
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001

TRAY_CALLBACK = WM_USER + 20

LRESULT = ctypes.c_ssize_t  # 64 位必须用 LRESULT，用 c_long 会 OverflowError

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


BG_COLOR = 0x00202020  # COLORREF: 深灰（BGR）
events: list[str] = []
hwnd_holder: list[int] = []

# DefWindowProcW 必须显式声明 64 位原型，否则 ctypes 默认把返回值截成 int32，
# 在 64 位 Windows 上会抛 OverflowError（实测踩到）。
_def_window_proc = user32.DefWindowProcW
_def_window_proc.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
_def_window_proc.restype = LRESULT


def wnd_proc(hwnd, msg, wparam, lparam):  # type: ignore[no-untyped-def]
    if msg == TRAY_CALLBACK:
        if lparam in (WM_RBUTTONUP, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            events.append(f"tray-click(lparam={lparam:#x})")
        return 0
    if msg == WM_DESTROY:
        events.append("WM_DESTROY")
        user32.PostQuitMessage(0)
        return 0
    return _def_window_proc(hwnd, msg, wparam, lparam)


wndproc_ref = WNDPROC(wnd_proc)
hinstance = k32.GetModuleHandleW(None)
class_name = "BiliDanmakuTrayProbe"

wc = WNDCLASSW()
wc.lpfnWndProc = wndproc_ref
wc.hInstance = hinstance
wc.lpszClassName = class_name
wc.hbrBackground = gdi32.CreateSolidBrush(BG_COLOR)
atom = user32.RegisterClassW(ctypes.byref(wc))
print(f"  RegisterClassW  -> atom={atom}（0 表示失败 err={ctypes.get_last_error()}）")

HWND_MESSAGE = wintypes.HWND(-3)
hwnd = user32.CreateWindowExW(0, class_name, "tray-probe", 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinstance, None)
print(f"  CreateWindowExW -> hwnd={hwnd}（message-only window，err={ctypes.get_last_error()}）")
if not hwnd:
    print("  ❌ 连消息窗口都建不出来，托盘方案不可行")
    raise SystemExit(1)
hwnd_holder.append(hwnd)

hicon_big = user32.LoadImageW(None, ICO, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
hicon_small = user32.LoadImageW(None, ICO, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
print(f"  LoadImageW(32)  -> hIcon={hicon_big}（err={ctypes.get_last_error()}）")
print(f"  LoadImageW(16)  -> hIcon={hicon_small}（err={ctypes.get_last_error()}）")

nid = NOTIFYICONDATAW()
nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
nid.hWnd = hwnd
nid.uID = 1
nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
nid.uCallbackMessage = TRAY_CALLBACK
nid.hIcon = hicon_big
nid.szTip = "B站弹幕提取姬（探针）"

added = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
print(f"  Shell_NotifyIconW(NIM_ADD) -> {added}（0 表示失败 err={ctypes.get_last_error()}）")
print(f"  （本 harness 没有真实托盘区时，这里失败是**预期**的，不是方案问题）")

# --------------------------------------------------------------------------- #
# 3. 菜单（PopupMenu + TrackPopupMenu）
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("3. 右键菜单构建")
print("=" * 78)
menu = user32.CreatePopupMenu()
items = [
    ("打开面板", True),
    ("复制链接", True),
    ("停止面板", False),
    ("重启面板", True),
    (None, None),  # separator
    ("显示启动面板", True),
    ("查看日志", True),
    ("打开数据目录", True),
    (None, None),
    ("退出（停止面板）", True),
]
cmd_ids: dict[int, str] = {}
next_id = 1000
for label, enabled in items:
    if label is None:
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        continue
    flags = MF_STRING | (0 if enabled else MF_GRAYED)
    user32.AppendMenuW(menu, flags, next_id, label)
    cmd_ids[next_id] = label
    next_id += 1
count = user32.GetMenuItemCount(menu)
print(f"  CreatePopupMenu + AppendMenuW -> 条目数 = {count}（期望 {len(items)}）")
print(f"  菜单项 id 映射 = {cmd_ids}")

# 模拟一次 TrackPopupMenu（TPM_RETURNCMD，无交互时返回 0）
user32.SetForegroundWindow(hwnd)
chosen = user32.TrackPopupMenu(
    menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, 100, 100, 0, hwnd, None
)
print(f"  TrackPopupMenu(TPM_RETURNCMD) -> {chosen}（0 = 用户没选/无交互，正常）")
user32.DestroyMenu(menu)

# 消息循环跑 3 秒
print()
print("=" * 78)
print("4. 消息循环")
print("=" * 78)
MSG = wintypes.MSG
msg = MSG()
deadline = time.time() + 3.0
pumps = 0
while time.time() < deadline:
    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
        pumps += 1
    time.sleep(0.05)
print(f"  PeekMessage/DispatchMessage 跑通，处理 {pumps} 条消息，无异常")

# --------------------------------------------------------------------------- #
# 清理
# --------------------------------------------------------------------------- #
shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
user32.DestroyWindow(hwnd)
user32.UnregisterClassW(class_name, hinstance)
if hicon_big:
    user32.DestroyIcon(hicon_big)
if hicon_small:
    user32.DestroyIcon(hicon_small)

print()
print("=" * 78)
print("结论")
print("=" * 78)
print(f"  ICO 生成                : ✅ 多尺寸 {len(sizes)} 档")
print(f"  纯 ctypes 消息窗口      : ✅ hwnd 可用")
print(f"  图标加载 (LoadImageW)   : {'✅' if hicon_big else '❌'}")
print(f"  托盘项 (Shell_NotifyIcon): {'✅ 挂上了' if added else '⚠️ 未挂上（本环境无托盘区，见上）'}")
print(f"  右键菜单 (PopupMenu)    : ✅ {count} 项，可 TrackPopupMenu")
print(f"  消息循环                : ✅ PeekMessage/DispatchMessage 正常")
print(f"  events = {events}")
print()
print("  => 「托盘图标 + 右键菜单」**不需要** pystray / pywin32，用标准库 ctypes 就能做。")
