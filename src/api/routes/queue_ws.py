# -*- coding: utf-8 -*-
"""
队列 WebSocket 广播端点
========================
前端连 ws://host:port/interface/queue/ws 即可收到任务生命周期通知：
  - task_enqueued : 新任务入队
  - task_started  : 任务开始执行
  - task_completed: 任务完成（含下一任务信息）

所有连上的客户端收到相同的广播消息，客户端按 task_id 自行过滤。
"""

import uuid
from typing import Optional
from fastapi import APIRouter, WebSocket, Query

from src.core.ws_manager import ws_manager
from src.core.scheduler import scheduler
from src.core.task_manager import TaskManager
from src.logic.logger import log
from src.logic.yaml_config_loader import yaml_config_loader

router = APIRouter(tags=["Queue Broadcast"])


@router.websocket("/queue/ws")
async def queue_websocket(websocket: WebSocket, client_id: Optional[str] = Query(None)):
    await websocket.accept()
    ws_id = str(uuid.uuid4())
    ws_manager.subscribe_queue(ws_id, websocket, client_id=client_id)

    # 🌟 连上即送第 1 帧：主动推送当前队列大盘快照 (包含专属排队任务)
    try:
        snapshot = scheduler.get_queue_status(client_id=client_id)
        snapshot["client_id"] = client_id
        if client_id:
            snapshot["your_tasks"] = TaskManager.list_tasks(client_id=client_id)
        await websocket.send_json({
            "type": "queue_snapshot",
            "data": snapshot,
        })
    except Exception as e:
        log.warning(f"[QueueWS] Failed to send initial snapshot to {ws_id}: {e}")

    try:
        while True:
            # 保持连接，等待客户端消息（客户端发 ping，我们回 pong；亦支持 bind 动态绑定）
            data = await websocket.receive_text()
            if data == "ping":
                if yaml_config_loader.get("app.debug", False):
                    log.info(f"[WS Send] [Queue: {ws_id}] pong", extra={"markup": False})
                await websocket.send_text("pong")
    except Exception:
        pass
    finally:
        ws_manager.unsubscribe_queue(ws_id)
        log.info(f"[QueueWS] Subscriber '{ws_id}' disconnected")
