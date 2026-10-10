# -*- coding: utf-8 -*-
"""
WebSocket 连接管理器
====================
管理前端 WS 连接，桥接同步执行器线程 → 异步 WS 推送。

执行器线程调用 send() → run_coroutine_threadsafe → 推送到前端 WS。
"""

import asyncio
import json
import threading
import time
from collections import defaultdict
from typing import Dict, Set, Optional
from fastapi import WebSocket

from src.logic.logger import log
from src.logic.yaml_config_loader import yaml_config_loader


class WebSocketManager:
    _instance: Optional["WebSocketManager"] = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._connections: Dict[str, WebSocket] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self._queue_subscribers: Dict[str, WebSocket] = {}  # 队列广播订阅者 (ws_id -> WebSocket)
        self._subscriber_clients: Dict[str, str] = {}       # ws_id -> client_id 反查字典
        self._client_subscribers: Dict[str, Set[str]] = defaultdict(set)  # client_id -> Set[ws_id]
        self._last_progress_time: Dict[str, float] = {}     # task_id -> 上次推送进度的 timestamp
        self._lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._initialized = True

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def register(self, task_id: str, websocket: WebSocket) -> asyncio.Event:
        """注册前端 WS 连接。返回一个 asyncio.Event，任务完成时会被 set。"""
        with self._lock:
            self._connections[task_id] = websocket
        if self._loop:
            event = asyncio.Event()
            self._events[task_id] = event
            return event
        return asyncio.Event()

    def send(self, task_id: str, message: dict):
        """从同步线程推送消息到前端 WS。"""
        if not self._loop:
            return
        with self._lock:
            ws = self._connections.get(task_id)
        if ws:
            asyncio.run_coroutine_threadsafe(
                self._send_async(task_id, ws, message), self._loop
            )

    async def _send_async(self, task_id: str, ws: WebSocket, message: dict):
        try:
            if yaml_config_loader.get("app.debug", False):
                log.info(f"[WS Send] [{task_id}] {json.dumps(message, ensure_ascii=False)}", extra={"markup": False})
            await ws.send_json(message)
            msg_type = message.get("type", "")
            if msg_type in ("task_complete", "task_failed"):
                event = self._events.get(task_id)
                if event:
                    event.set()
        except Exception:
            log.warning(f"[WSManager] Failed to send to task {task_id}, disconnecting")
            await self._close(task_id, ws)

    def unregister(self, task_id: str):
        """移除连接。从同步线程调用。"""
        with self._lock:
            ws = self._connections.pop(task_id, None)
        if ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._close(task_id, ws), self._loop
            )

    async def _close(self, task_id: str, ws: WebSocket):
        """异步关闭 WS 连接"""
        self._events.pop(task_id, None)
        try:
            await ws.close()
        except Exception:
            pass

    def close_all(self):
        """关闭所有连接（shutdown 时调用）"""
        with self._lock:
            task_ids = list(self._connections.keys())
        for task_id in task_ids:
            self.unregister(task_id)
        with self._queue_lock:
            subs = list(self._queue_subscribers.keys())
        for sub_id in subs:
            self.unsubscribe_queue(sub_id)

    # =========================================================================
    # 队列广播与单轨定向推送（全局/多租户通知频道）
    # =========================================================================

    def subscribe_queue(self, ws_id: str, websocket: WebSocket, client_id: Optional[str] = None):
        """订阅队列广播。ws_id 为连接标识，client_id 为可选的业务客户端身份。"""
        with self._queue_lock:
            self._queue_subscribers[ws_id] = websocket
            if client_id:
                self._subscriber_clients[ws_id] = client_id
                self._client_subscribers[client_id].add(ws_id)
        cid_info = f", client_id='{client_id}'" if client_id else ""
        log.info(f"[WSManager] Queue subscriber '{ws_id}' connected{cid_info} ({len(self._queue_subscribers)} total)")

    def unsubscribe_queue(self, ws_id: str):
        """取消队列广播订阅并清理 client_id 索引。"""
        ws = None
        with self._queue_lock:
            ws = self._queue_subscribers.pop(ws_id, None)
            client_id = self._subscriber_clients.pop(ws_id, None)
            if client_id and client_id in self._client_subscribers:
                self._client_subscribers[client_id].discard(ws_id)
                if not self._client_subscribers[client_id]:
                    del self._client_subscribers[client_id]

        if ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._close_queue_ws(ws_id, ws), self._loop)

    async def _close_queue_ws(self, ws_id: str, ws: WebSocket):
        try:
            await ws.close()
        except Exception:
            pass

    def send_to_client(self, client_id: str, message: dict):
        """定向推送：仅向特定 client_id 的所有活动 WS 连接发送消息。"""
        if not self._loop or not client_id:
            return
        with self._queue_lock:
            ws_ids = list(self._client_subscribers.get(client_id, set()))
            target_sockets = [(ws_id, self._queue_subscribers[ws_id]) for ws_id in ws_ids if ws_id in self._queue_subscribers]
        
        for ws_id, ws in target_sockets:
            asyncio.run_coroutine_threadsafe(
                self._send_queue_async(ws_id, ws, message), self._loop
            )

    def broadcast_queue(self, message: dict):
        """向所有队列订阅者全网广播消息（兼容公共/大盘广播）。"""
        if not self._loop:
            return
        with self._queue_lock:
            snapshot = list(self._queue_subscribers.items())
        for ws_id, ws in snapshot:
            asyncio.run_coroutine_threadsafe(
                self._send_queue_async(ws_id, ws, message), self._loop
            )

    def emit_to_target_or_broadcast(self, target_client_id: Optional[str], message: dict):
        """智能路由：若指定了 target_client_id 则定向发送；若未指定则退化为全网广播。"""
        if target_client_id:
            self.send_to_client(target_client_id, message)
        else:
            self.broadcast_queue(message)

    def emit_progress_throttled(self, task_id: str, client_id: Optional[str], message: dict, interval: float = 0.3):
        """单轨进度节流推送：对 ComfyUI 等高频进度以 interval(默认300ms) 为窗口合并下发。"""
        now = time.time()
        last_time = self._last_progress_time.get(task_id, 0.0)
        progress = message.get("progress", 0)
        
        # 满足时间间隔或进度到达 100% 终态时触发推送
        if (now - last_time >= interval) or (progress >= 100):
            self._last_progress_time[task_id] = now
            self.emit_to_target_or_broadcast(client_id, message)

    def clear_task_progress(self, task_id: str):
        """任务结束时清理节流时间记录"""
        self._last_progress_time.pop(task_id, None)

    async def _send_queue_async(self, ws_id: str, ws: WebSocket, message: dict):
        try:
            if yaml_config_loader.get("app.debug", False):
                log.info(f"[WS Broadcast] [{ws_id}] {json.dumps(message, ensure_ascii=False)}", extra={"markup": False})
            await ws.send_json(message)
        except Exception:
            log.warning(f"[WSManager] Failed to broadcast to queue subscriber {ws_id}, disconnecting")
            self.unsubscribe_queue(ws_id)


ws_manager = WebSocketManager()
