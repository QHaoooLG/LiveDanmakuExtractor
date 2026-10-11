"""GUI 子包：托盘应用与启动面板。

* `_win32.py` —— 零依赖的 Win32 托盘绑定（ctypes）
* `panel_window.py` —— tkinter 启动面板（状态展示 + 跳转链接 + 基础操作）
* `qr_window.py` —— 扫码登录的**独立二维码窗口**（面板宽度固定，二维码会被裁掉）
* `tray.py` —— 把上面两者与 `launcher.PanelController` 组装成托盘应用

设计约束：**只依赖标准库**（tkinter + ctypes + Pillow 只用于生成 ico）。
"""

from __future__ import annotations
