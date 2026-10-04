"""Web 面板：FastAPI 后端 + 构建式组件前端。

分层：
    * `hub.py`   —— 与框架无关的事件广播中枢（含慢客户端剔除）
    * `panel.py` —— 多房间采集编排，复用 `RoomCollector`
    * `app.py`   —— FastAPI 路由与 WebSocket
"""

from __future__ import annotations

from .app import bind_port, create_app, find_dist
from .hub import Subscriber, WebHub
from .panel import PanelService, RoomRuntime, WebHubSink

__all__ = [
    "PanelService",
    "RoomRuntime",
    "Subscriber",
    "WebHub",
    "WebHubSink",
    "bind_port",
    "create_app",
    "find_dist",
]
