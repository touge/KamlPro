# -*- coding: utf-8 -*-
"""
任务端点
========
POST /tasks/submit   — 提交任务到 FIFO 队列（支持 JSON 和 multipart/form-data）
GET  /tasks/{id}/status — 查询任务状态
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, Query

from src.core.response import success, error
from src.core.task_manager import TaskManager
from src.core.scheduler import scheduler
from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.logger import log

router = APIRouter(tags=["Tasks"])

TASK_FOLDER = Path(yaml_config_loader.get("paths.task_folder", "tasks"))
TYPE_ALIASES = {}


@router.post("/tasks/submit")
async def submit_task(request: Request):
    """统一任务提交。支持 JSON 和 multipart/form-data 两种请求格式，支持可选的 client_id。"""
    content_type = request.headers.get("content-type", "")

    # ---- multipart/form-data（文件上传类任务，如 subtitle）----
    if "multipart/form-data" in content_type:
        form = await request.form()
        task_type = form.get("task_type")
        if not task_type:
            return error("Missing 'task_type' in form data", 400)

        client_id = form.get("client_id") or request.headers.get("x-client-id")

        # 收集所有非文件字段为 payload
        payload: Dict[str, Any] = {}
        saved_files: Dict[str, str] = {}  # field_name → local_path

        for field_name, value in form.items():
            if field_name in ("task_type", "client_id"):
                continue
            if hasattr(value, "filename"):
                # 文件字段：保存到任务目录
                filename = value.filename or f"{field_name}.bin"
                ext = Path(filename).suffix or ".bin"
                # 先创建临时目录，用 task_id 命名（稍后分配）
                saved_files[field_name] = {
                    "content": await value.read(),
                    "filename": filename,
                    "ext": ext,
                }
            else:
                payload[field_name] = value

        payload["client_id"] = client_id
        task_id = TaskManager.create_task(task_type, client_id=client_id)

        # 将上传文件保存到任务目录
        task_dir = TASK_FOLDER / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        for field_name, finfo in saved_files.items():
            file_path = task_dir / f"{field_name}{finfo['ext']}"
            file_path.write_bytes(finfo["content"])
            payload[f"{field_name}_path"] = str(file_path)
            log.info(f"[API] Saved uploaded file: {file_path} ({len(finfo['content'])} bytes)")

    # ---- JSON（普通任务）----
    else:
        try:
            body = await request.json()
        except Exception:
            return error("Request body must be valid JSON, or use multipart/form-data for file uploads", 400)

        task_type = body.get("task_type", "")
        payload = body.get("payload", {})
        client_id = body.get("client_id") or payload.get("client_id") or request.headers.get("x-client-id")
        payload["client_id"] = client_id
        log.info(f"[API] 客户端原始请求: task_type='{task_type}', client_id='{client_id}', payload keys={list(payload.keys())}")

        if not task_type:
            return error("Missing 'task_type' in request body", 400)

        task_id = TaskManager.create_task(task_type, client_id=client_id)

    # ---- 提交到调度器 ----
    internal_type = TYPE_ALIASES.get(task_type, task_type)
    known_types = list(scheduler.task_executors.keys())
    if internal_type not in known_types:
        return error(f"Unknown task type: '{task_type}'. Known types: {known_types}", 400)

    try:
        log.info(f"[API] >>> submit_task 调用前: type={internal_type}, task_id={task_id}, client_id={client_id}")
        can_stream_immediately = scheduler.submit_task(internal_type, task_id, payload, client_id=client_id)
        log.info(f"[API] >>> submit_task 返回后: type={internal_type}, task_id={task_id}, can_stream={can_stream_immediately}")
        
        # 闲时直连则直接变更为 RUNNING，否则进入 QUEUED 状态排队
        if can_stream_immediately:
            TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Cloud LLM processing started immediately"})
        else:
            TaskManager.update_task(task_id, TaskManager.STATUS_QUEUED)
            
        log.info(f"[API] Task {task_id} submitted: type={internal_type}")

        tasks_config = yaml_config_loader.get("tasks", {})
        track_mode = tasks_config.get(internal_type, {}).get("track_mode", "poll")

        hint = _track_hint(track_mode, task_id)

        response_data = {
            "task_id": task_id,
            "client_id": client_id,
            "status": TaskManager.STATUS_RUNNING if can_stream_immediately else TaskManager.STATUS_QUEUED,
            "track_mode": track_mode,
            "hint": hint,
        }
        if internal_type == "llm_streaming":
            response_data["can_stream_immediately"] = can_stream_immediately

        return success(data=response_data)
    except Exception as e:
        log.error(f"[API] Failed to submit task: {e}")
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        return error(f"Failed to submit task: {str(e)}", 500)


def _track_hint(mode: str, task_id: str) -> str:
    if mode == "ws":
        return f"Connect to ws://host:port/interface/tasks/{task_id}/ws for real-time progress"
    elif mode == "poll":
        return f"Poll GET /interface/tasks/{task_id}/status for results"
    elif mode == "stream":
        return "Streaming response is already in progress via NDJSON"
    return "Unknown track mode"


@router.get("/tasks")
async def list_tasks(client_id: Optional[str] = Query(None)):
    """返回当前所有活跃任务状态。支持传 client_id 进行专属客户端任务过滤。"""
    return success(data={"tasks": TaskManager.list_tasks(client_id=client_id)})


@router.get("/queue/status")
async def get_queue_status(client_id: Optional[str] = Query(None)):
    """查询全局调度队列水位与大盘状态。支持按 client_id 筛选属于该客户端的任务列表。"""
    queue_data = scheduler.get_queue_status(client_id=client_id)
    return success(data=queue_data)


@router.get("/tasks/{task_id}/status")
async def get_task_status(task_id: str):
    task = TaskManager.get_task(task_id)
    if task.get("status") == "not_found":
        return error(f"Task '{task_id}' not found", 404)

    return success(data=task)


def log_cancel_debug(msg: str):
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    try:
        with open("cancel_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] [API_Route] {msg}\n")
    except Exception:
        pass


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    import urllib.request
    from src.core.ws_manager import ws_manager

    task = TaskManager.get_task(task_id)
    status = task.get("status")
    log_cancel_debug(f"cancel_task called for task_id={task_id}, status={status}")

    if status == "not_found":
        log_cancel_debug(f"task_id={task_id} not found")
        return error(f"Task '{task_id}' not found", 404)

    if status in (TaskManager.STATUS_SUCCESS, TaskManager.STATUS_FAILED):
        log_cancel_debug(f"task_id={task_id} is already finished (status={status})")
        return success(message="Task is already finished", data={"task_id": task_id})

    # 1. 注册并触发中止信号
    TaskManager.register_abort_event(task_id).set()
    log_cancel_debug(f"abort event registered and set for task_id={task_id}")

    # 2. 如果任务正在运行，根据取消元数据调用对应物理后端的中断接口
    if status == TaskManager.STATUS_RUNNING:
        cancel_metadata = TaskManager.get_cancel_metadata(task_id)
        engine_type = cancel_metadata.get("engine_type")
        log_cancel_debug(f"task_id={task_id} is running, cancel_metadata={cancel_metadata}")

        if engine_type == "comfyui":
            prompt_id = cancel_metadata.get("prompt_id")
            server_addr = cancel_metadata.get("server_address")
            port = cancel_metadata.get("port")
            if prompt_id and server_addr and port:
                base_url = f"http://{server_addr}:{port}"
                log_cancel_debug(f"ComfyUI params: prompt_id={prompt_id}, base_url={base_url}")
                
                # 从 ComfyUI 队列删除
                try:
                    data = json.dumps({"action": "delete", "delete": [prompt_id]}).encode("utf-8")
                    req = urllib.request.Request(f"{base_url}/queue", data=data,
                        headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req) as resp:
                        resp_data = resp.read()
                        log.info(f"[Cancel] ComfyUI queue delete prompt {prompt_id} response: {resp_data}")
                        log_cancel_debug(f"ComfyUI queue delete success, response={resp_data}")
                except Exception as e:
                    log.warning(f"[Cancel] Failed to delete ComfyUI prompt {prompt_id} from queue: {e}")
                    log_cancel_debug(f"ComfyUI queue delete failed: {e}")

                # 中断 ComfyUI 当前执行
                try:
                    req = urllib.request.Request(f"{base_url}/interrupt", data=b"{}",
                        headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req) as resp:
                        resp_data = resp.read()
                        log.info(f"[Cancel] ComfyUI interrupt response: {resp_data}")
                        log_cancel_debug(f"ComfyUI interrupt success, response={resp_data}")
                except Exception as e:
                    log.warning(f"[Cancel] Failed to interrupt ComfyUI execution: {e}")
                    log_cancel_debug(f"ComfyUI interrupt failed: {e}")
            else:
                log_cancel_debug(f"ComfyUI metadata incomplete: prompt_id={prompt_id}, server_addr={server_addr}, port={port}")

        elif engine_type == "tts":
            svc_task_id = cancel_metadata.get("svc_task_id")
            base_url = cancel_metadata.get("base_url")
            headers = cancel_metadata.get("headers", {})
            if svc_task_id and base_url:
                try:
                    cancel_url = f"{base_url}/cancel/{svc_task_id}"
                    req = urllib.request.Request(cancel_url, data=b"")
                    for k, v in headers.items():
                        req.add_header(k, v)
                    with urllib.request.urlopen(req) as resp:
                        resp_data = resp.read()
                        log.info(f"[Cancel] TTS cancel response: {resp_data}")
                        log_cancel_debug(f"TTS cancel success, response={resp_data}")
                except Exception as e:
                    log.warning(f"[Cancel] Failed to cancel TTS task {svc_task_id}: {e}")
                    log_cancel_debug(f"TTS cancel failed: {e}")

    # 3. 更新任务状态为 FAILED (错误信息为：任务已被用户手动停止)
    TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": "任务已被用户手动停止"})

    # 4. 触发 WebSocket 广播事件（全局 Queue 频道 + 任务专有频道）
    ws_manager.send(task_id, {"type": "task_failed", "message": "任务已被用户手动停止"})
    ws_manager.broadcast_queue({
        "type": "task_failed",
        "task_id": task_id,
        "message": "任务已被用户手动停止"
    })

    log.info(f"[API] Task {task_id} cancellation initiated successfully.")
    log_cancel_debug(f"cancel_task complete for task_id={task_id}")
    return success(message="Task cancellation initiated", data={"task_id": task_id})

