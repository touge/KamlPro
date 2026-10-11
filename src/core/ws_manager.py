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
        self._client_disconnect_times: Dict[str, float] = {} # client_id -> 最后完全断开连接的时间戳
        self._last_progress_time: Dict[str, float] = {}     # task_id -> 上次推送进度的 timestamp
        self._last_progress_state: Dict[str, tuple[int, str]] = {} # task_id -> (progress, node_name) 状态记录
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

    def send(self, task_id: str, message: dict, throttle_interval: Optional[float] = None):
        """从同步线程推送消息到前端 WS。对 progress / overall_progress 默认应用 300ms 智能平滑节流。"""
        if not self._loop:
            return

        # 智能平滑节流：对单任务频道的 progress/overall_progress 高频消息进行 300ms 防洪拦截
        msg_type = message.get("type", "")
        if msg_type in ("progress", "overall_progress"):
            interval = throttle_interval if throttle_interval is not None else 0.3
            data = message.get("data", {})
            percent = data.get("percent") if isinstance(data, dict) else message.get("percent")
            is_finished = (percent == 100) or (msg_type in ("task_complete", "task_failed"))

            now = time.time()
            cache_key = f"single_{task_id}_{msg_type}"
            last_time = self._last_progress_time.get(cache_key, 0.0)
            if not is_finished and (now - last_time < interval):
                return  # 300ms 窗口内平滑丢弃，杜绝狂暴刷屏！
            self._last_progress_time[cache_key] = now

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

    def subscribe_queue(self, ws_id: str, websocket: WebSocket, client_id: str):
        """订阅队列广播。ws_id 为连接标识，client_id 为必填的业务客户端身份。无 client_id 直接抛出异常！"""
        if not client_id or not str(client_id).strip():
            raise ValueError("[WSManager] Missing required client_id! 严禁匿名订阅 WebSocket 队列。")
        with self._queue_lock:
            self._queue_subscribers[ws_id] = websocket
            self._subscriber_clients[ws_id] = client_id
            self._client_subscribers[client_id].add(ws_id)
            self._client_disconnect_times.pop(client_id, None)
        log.info(f"[WSManager] Queue subscriber '{ws_id}' connected, client_id='{client_id}' ({len(self._queue_subscribers)} total)")

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
                    self._client_disconnect_times[client_id] = time.time()

        if ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._close_queue_ws(ws_id, ws), self._loop)

    def is_client_connected(self, client_id: str) -> bool:
        """检查特定 client_id 当前是否有任何活跃的 WebSocket 连接"""
        if not client_id:
            return False
        with self._queue_lock:
            return bool(self._client_subscribers.get(client_id))

    def get_client_offline_duration(self, client_id: str) -> Optional[float]:
        """获取特定 client_id 的离线持续时间(秒)。若当前在线则返回 0.0；若从未连接则返回 None。"""
        if not client_id:
            return None
        with self._queue_lock:
            if bool(self._client_subscribers.get(client_id)):
                return 0.0
            dis_time = self._client_disconnect_times.get(client_id)
            if dis_time is not None:
                return time.time() - dis_time
            return None

    async def _close_queue_ws(self, ws_id: str, ws: WebSocket):
        try:
            await ws.close()
        except Exception:
            pass

    def send_to_client(self, client_id: str, message: dict):
        """定向推送：仅向特定 client_id 的所有活动 WS 连接发送消息。无 client_id 直接抛出异常！"""
        if not client_id or not str(client_id).strip():
            raise ValueError(f"[WSManager] Missing required client_id when sending message: {message.get('type')}. 严禁无 client_id 投递！")
        if not self._loop:
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

    def emit_to_client(self, target_client_id: str, message: dict):
        """精准定向发送给目标客户端。无 client_id 直接抛出异常中断！绝不做任何全网广播兜底！"""
        if not target_client_id or not str(target_client_id).strip():
            raise ValueError(f"[WSManager] Missing required client_id when emitting message: {message.get('type')}. 严禁无 client_id 投递，程序中断！")
        if not self._loop or self._loop.is_closed():
            return

        with self._queue_lock:
            target_ws_ids = list(self._client_subscribers.get(target_client_id, set()))
            target_sockets = [(ws_id, self._queue_subscribers[ws_id]) for ws_id in target_ws_ids if ws_id in self._queue_subscribers]

        if not target_sockets:
            log.warning(f"[WSManager] Target client '{target_client_id}' is offline (0 active WS connections). Message '{message.get('type')}' dropped without fallback.")
            return

        for ws_id, ws in target_sockets:
            asyncio.run_coroutine_threadsafe(
                self._send_queue_async(ws_id, ws, message), self._loop
            )

    def emit_to_target_or_broadcast(self, target_client_id: str, message: dict):
        """别名兼容：严格走 emit_to_client，无 client_id 直接抛异常中断，绝不兜底！"""
        self.emit_to_client(target_client_id, message)

    def emit_progress_throttled(self, task_id: str, client_id: str, message: dict, interval: float = 0.3):
        """单轨进度节流推送：对 ComfyUI 等高频进度以 interval(默认300ms) 为窗口合并下发。无 client_id 直接抛出异常！"""
        if not client_id or not str(client_id).strip():
            raise ValueError(f"[WSManager] Missing required client_id when emitting throttled progress for task {task_id}")
        now = time.time()
        last_time = self._last_progress_time.get(task_id, 0.0)
        progress = message.get("progress", 0)
        node_name = str(message.get("node_name") or "")

        # 🌟 进度数值去重：当进度未到达 100% 且 (progress, node_name) 完全未发生变动时，直接静默拦截，杜绝同一数值疯狂刷屏！
        last_state = self._last_progress_state.get(task_id)
        current_state = (progress, node_name)
        if progress < 100 and last_state == current_state:
            return

        # 满足时间间隔或进度到达 100% 终态时触发推送
        if (now - last_time >= interval) or (progress >= 100):
            self._last_progress_time[task_id] = now
            self._last_progress_state[task_id] = current_state
            self.emit_to_client(client_id, message)

    def clear_task_progress(self, task_id: str):
        """任务结束时清理节流时间记录与去重状态"""
        self._last_progress_time.pop(task_id, None)
        self._last_progress_state.pop(task_id, None)
        self._last_progress_time.pop(f"single_{task_id}_progress", None)
        self._last_progress_time.pop(f"single_{task_id}_overall_progress", None)

    async def _send_queue_async(self, ws_id: str, ws: WebSocket, message: dict):
        try:
            if yaml_config_loader.get("app.debug", False):
                with self._queue_lock:
                    client_id = self._subscriber_clients.get(ws_id, "unknown")
                log.info(f"[WS Broadcast] [{ws_id} | client: {client_id}] {json.dumps(message, ensure_ascii=False)}", extra={"markup": False})
            await ws.send_json(message)
        except Exception:
            log.warning(f"[WSManager] Failed to broadcast to queue subscriber {ws_id}, disconnecting")
            self.unsubscribe_queue(ws_id)


ws_manager = WebSocketManager()
