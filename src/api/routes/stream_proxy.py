# -*- coding: utf-8 -*-
"""
HTTP 流式透传端点
==================
前端 POST /llm/generate-stream → 排队 → 透传 Ollama NDJSON 流。
协议与 Ollama 原生 /api/generate (stream=true) 一致。
"""

import asyncio
import queue
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.core.task_manager import TaskManager
from src.core.scheduler import scheduler
from src.core.response import error
from src.logic.logger import log

router = APIRouter(tags=["Streaming"])


class LLMGenerateStreamRequest(BaseModel):
    model: str
    prompt: str
    client_id: Optional[str] = None
    options: Optional[Dict[str, Any]] = {}


STREAM_START_TIMEOUT = 60  # 等待队列分配的超时时间（秒）


@router.post("/llm/generate-stream")
async def llm_generate_stream(req: LLMGenerateStreamRequest):
    if not req.client_id or not str(req.client_id).strip():
        return error("Missing required field 'client_id'. Please specify client_id.", 400)

    task_id = TaskManager.create_task("llm_streaming", client_id=req.client_id)

    output_queue: queue.Queue = queue.Queue(maxsize=256)
    started_event = threading.Event()

    scheduler.submit_task(
        "llm_streaming", task_id,
        {"model": req.model, "prompt": req.prompt, "options": req.options or {}, "client_id": req.client_id},
        output_queue=output_queue, started_event=started_event,
        client_id=req.client_id,
    )

    if not started_event.wait(timeout=STREAM_START_TIMEOUT):
        log.warning(f"[Stream] Task {task_id} queue timeout after {STREAM_START_TIMEOUT}s, draining")
        _drain_queue(output_queue)
        return error("Task queue timeout — service busy, retry later", 503)

    async def generate():
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, output_queue.get)
            if chunk is None:
                break
            yield chunk + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


def _drain_queue(q: queue.Queue):
    """后台清空队列，防止执行器阻塞在 put() 上"""
    def drain():
        while True:
            try:
                chunk = q.get(timeout=5)
                if chunk is None:
                    break
            except queue.Empty:
                break
    threading.Thread(target=drain, daemon=True).start()


@router.get("/tasks/{task_id}/stream")
async def tasks_stream(task_id: str):
    """
    通用流式代理通道。
    前端在提交任务获得 task_id，或者在 WS 收到 'task_running' 信号后，
    请求此端点，拉取实时流式响应（NDJSON 格式）。
    """
    with scheduler.api_lock:
        stream_data = scheduler.stream_events.get(task_id)

    if not stream_data:
        log.warning(f"[Stream Proxy] Stream not found or expired for task_id: {task_id}")
        return error(f"Stream not found or expired for task '{task_id}'", 404)

    started_event = stream_data["event"]
    output_queue = stream_data["queue"]

    # 唤醒执行器线程（执行器此时可能正在 started_event.wait() 阻塞）
    started_event.set()
    log.info(f"[Stream Proxy] Client connected to stream for task {task_id}, signaled executor.")

    async def generate():
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, output_queue.get)
            if chunk is None:
                break
            yield chunk + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")
