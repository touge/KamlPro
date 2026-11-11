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


_SUBMIT_TASK_OPENAPI_EXTRA = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["task_type", "payload"],
                    "properties": {
                        "task_type": {
                            "type": "string",
                            "description": "任务类型，如 tts / comfyui / llm / llm_streaming",
                        },
                        "client_id": {
                            "type": "string",
                            "description": "客户端标识（可选；未传时按请求 IP 自动分配）",
                        },
                        "asset_status": {
                            "type": "string",
                            "default": "ready",
                            "description": "素材就绪状态：ready 或 not_ready",
                        },
                        "required_assets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "需要延迟上传或复用的素材文件名列表",
                        },
                        "payload": {
                            "type": "object",
                            "description": "透传给对应执行器的任务参数字典",
                        },
                    },
                },
                "examples": {
                    "tts": {
                        "summary": "TTS 语音合成",
                        "value": {
                            "task_type": "tts",
                            "client_id": "swagger_debug",
                            "payload": {
                                "path": "/v1.5/generate",
                                "text": "欢迎使用 Kalm 智能配音平台。",
                                "speaker": "解说-魔魔",
                                "generate_subtitle": True,
                            },
                        },
                    },
                    "llm": {
                        "summary": "LLM 对话",
                        "value": {
                            "task_type": "llm",
                            "client_id": "swagger_debug",
                            "payload": {
                                "model": "gemma3:12b",
                                "prompt": "你好，请介绍一下你自己。",
                            },
                        },
                    },
                    "comfyui": {
                        "summary": "ComfyUI 生图",
                        "value": {
                            "task_type": "comfyui",
                            "client_id": "swagger_debug",
                            "payload": {
                                "workflow": {},
                                "final_output_node_id": "9",
                            },
                        },
                    },
                },
            },
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["task_type"],
                    "properties": {
                        "task_type": {
                            "type": "string",
                            "default": "subtitle",
                            "description": "表单任务类型，如 subtitle",
                        },
                        "client_id": {"type": "string"},
                        "text": {"type": "string", "description": "字幕对齐文本"},
                        "audio_file": {
                            "type": "string",
                            "format": "binary",
                            "description": "音频文件",
                        },
                        "output_filename": {"type": "string"},
                    },
                }
            },
        },
    }
}


@router.post("/tasks/submit", openapi_extra=_SUBMIT_TASK_OPENAPI_EXTRA)
async def submit_task(request: Request):
    """统一任务提交。支持 JSON 和 multipart/form-data 两种请求格式，支持可选的 client_id。"""
    content_type = request.headers.get("content-type", "")

    # ---- multipart/form-data（文件上传类任务，如 subtitle）----
    if "multipart/form-data" in content_type:
        form = await request.form()
        task_type = form.get("task_type")
        if not task_type:
            return error("Missing 'task_type' in form data", 400)

        client_id = (
            form.get("client_id")
            or request.headers.get("x-client-id")
            or request.query_params.get("client_id")
        )
        if not client_id or not str(client_id).strip():
            client_id = f"client_{request.client.host.replace('.', '_')}" if request.client else "client_default"
            log.info(f"[API] Multipart 表单未携带 client_id，自动按 IP 分配: '{client_id}'")
        asset_status = form.get("asset_status") or "ready"
        required_assets = form.get("required_assets")
        if isinstance(required_assets, str):
            try:
                required_assets = json.loads(required_assets)
            except Exception:
                required_assets = [required_assets]
        elif not required_assets:
            required_assets = []

        # 收集所有非文件字段为 payload
        payload: Dict[str, Any] = {}
        saved_files: Dict[str, str] = {}  # field_name → local_path

        for field_name, value in form.items():
            if field_name in ("task_type", "client_id", "asset_status", "required_assets"):
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
        task_id = TaskManager.create_task(
            task_type, client_id=client_id, asset_status=asset_status,
            required_assets=required_assets, payload=payload
        )

        # 将上传文件保存到任务目录
        task_dir = TASK_FOLDER / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        for field_name, finfo in saved_files.items():
            file_path = task_dir / f"{field_name}{finfo['ext']}"
            file_path.write_bytes(finfo["content"])
            payload[f"{field_name}_path"] = str(file_path)
            log.info(f"[API] Saved uploaded file: {file_path} ({len(finfo['content'])} bytes)")

        # 同步回写带有文件路径的 payload
        TaskManager.update_task_payload(task_id, payload)

    # ---- JSON（普通任务）----
    else:
        try:
            body = await request.json()
        except Exception as e:
            log.warning(f"[API 400] Request body is not valid JSON: {e}")
            return error("Request body must be valid JSON, or use multipart/form-data for file uploads", 400)

        task_type = body.get("task_type", "")
        payload = body.get("payload", {})
        client_id = (
            body.get("client_id")
            or payload.get("client_id")
            or request.headers.get("x-client-id")
            or request.query_params.get("client_id")
        )
        if not client_id or not str(client_id).strip():
            client_id = f"client_{request.client.host.replace('.', '_')}" if request.client else "client_default"
            log.info(f"[API] 客户端未显式携带 client_id，已自动按 IP 兜底分配: '{client_id}'")

        log.info(f"[API] 收到 /tasks/submit 请求: task_type='{task_type}', client_id='{client_id}'")
        payload["client_id"] = client_id
        
        asset_status = body.get("asset_status") or payload.get("asset_status") or "ready"
        required_assets = body.get("required_assets") or payload.get("required_assets") or []
        if isinstance(required_assets, str):
            required_assets = [required_assets]

        if not task_type:
            log.warning(f"[API 400] 缺少 task_type! 请求体: {body}")
            return error("Missing 'task_type' in request body", 400)

        task_id = TaskManager.create_task(
            task_type, client_id=client_id, asset_status=asset_status,
            required_assets=required_assets, payload=payload
        )

    # ---- 提交到调度器 ----
    internal_type = TYPE_ALIASES.get(task_type, task_type)
    known_types = list(scheduler.task_executors.keys())
    if internal_type not in known_types:
        log.warning(f"[API 400] 未知任务类型 task_type='{task_type}' (internal='{internal_type}'), 可用类型: {known_types}")
        return error(f"Unknown task type: '{task_type}'. Known types: {known_types}", 400)

    try:
        log.info(f"[API] >>> submit_task 调用前: type={internal_type}, task_id={task_id}, client_id={client_id}, asset_status={asset_status}")
        can_stream_immediately = scheduler.submit_task(
            internal_type, task_id, payload, client_id=client_id,
            asset_status=asset_status, required_assets=required_assets
        )
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
            "asset_status": asset_status,
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


from src.logic.asset_resolver import AssetResolver


@router.post("/tasks/{task_id}/assets")
async def upload_task_assets(task_id: str, request: Request):
    """
    按需延迟上传资产端点（JIT 素材注入接口 - 两级秒传架构）。
    支持两种开闸模式：
      1. 纯免传开闸 (application/json 或 multipart 表单中的 reuse_assets):
         无需上传任何二进制大文件，声明已存在的文件名，服务端自动寻址并软链开闸。
      2. 混合上传 (multipart/form-data):
         支持部分上传新文件，其余通过 reuse_assets 字段声明复用。
    """
    task = TaskManager.get_task(task_id)
    if task.get("status") == "not_found":
        return error(f"Task '{task_id}' not found", 404)

    if task.get("status") in (TaskManager.STATUS_SUCCESS, TaskManager.STATUS_FAILED):
        return error(f"Task '{task_id}' has already finished with status '{task.get('status')}'", 400)

    # 标记状态为正在上传，防止 30s 调度器超时误杀
    TaskManager.update_task(task_id, TaskManager.STATUS_ASSET_UPLOADING, {"message": "资产素材注入中..."})

    content_type = request.headers.get("content-type", "")
    task_dir = TASK_FOLDER / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    new_payload_fields: Dict[str, Any] = {}
    saved_files: Dict[str, str] = {}
    reused_files: Dict[str, str] = {}
    reuse_assets_raw = None

    try:
        # ---- 模式 1: application/json (纯免传开闸) ----
        if "application/json" in content_type:
            body = await request.json()
            reuse_assets_raw = body.get("reuse_assets")
            # 允许顺带传递其他普通 payload 字段
            for k, v in body.items():
                if k != "reuse_assets":
                    new_payload_fields[k] = v

        # ---- 模式 2: multipart/form-data (混合上传或表单免传) ----
        elif "multipart/form-data" in content_type:
            form = await request.form()
            for field_name, value in form.items():
                if field_name == "reuse_assets":
                    reuse_assets_raw = value
                    continue
                if hasattr(value, "filename") and value.filename:
                    filename = value.filename
                    ext = Path(filename).suffix or ".bin"
                    file_path = task_dir / f"{field_name}{ext}"
                    content = await value.read()
                    file_path.write_bytes(content)
                    new_payload_fields[f"{field_name}_path"] = str(file_path)
                    saved_files[field_name] = str(file_path)
                    log.info(f"[API] Asset uploaded for task {task_id}: {field_name} -> {file_path} ({len(content)} bytes)")
                else:
                    new_payload_fields[field_name] = value
        else:
            return error("Content-Type must be application/json or multipart/form-data", 400)

        # ---- 处理资产复用声明 (reuse_assets) ----
        if reuse_assets_raw:
            assets_to_resolve = []
            if isinstance(reuse_assets_raw, str):
                try:
                    parsed = json.loads(reuse_assets_raw)
                    if isinstance(parsed, (list, dict)):
                        assets_to_resolve = parsed
                    else:
                        assets_to_resolve = [s.strip() for s in reuse_assets_raw.split(",") if s.strip()]
                except Exception:
                    assets_to_resolve = [s.strip() for s in reuse_assets_raw.split(",") if s.strip()]
            elif isinstance(reuse_assets_raw, (list, dict)):
                assets_to_resolve = reuse_assets_raw

            # 遍历解析资产
            if isinstance(assets_to_resolve, dict):
                for custom_key, asset_name in assets_to_resolve.items():
                    resolved_path = AssetResolver.resolve_asset(asset_name)
                    if not resolved_path:
                        return error(f"Reused asset '{asset_name}' not found in server repository or ComfyUI input", 400)
                    mapped_path = AssetResolver.link_or_copy(resolved_path, task_dir)
                    new_payload_fields[f"{custom_key}_path"] = str(mapped_path)
                    new_payload_fields[custom_key] = str(mapped_path)
                    reused_files[custom_key] = str(mapped_path)
                    log.info(f"[API] Reused asset resolved for task {task_id}: key='{custom_key}', {asset_name} -> {mapped_path}")
            elif isinstance(assets_to_resolve, (list, tuple)):
                for asset_name in assets_to_resolve:
                    resolved_path = AssetResolver.resolve_asset(asset_name)
                    if not resolved_path:
                        return error(f"Reused asset '{asset_name}' not found in server repository or ComfyUI input", 400)
                    mapped_path = AssetResolver.link_or_copy(resolved_path, task_dir)
                    stem = Path(asset_name).stem
                    new_payload_fields[f"{stem}_path"] = str(mapped_path)
                    reused_files[asset_name] = str(mapped_path)
                    log.info(f"[API] Reused asset resolved for task {task_id}: {asset_name} -> {mapped_path}")

        if not saved_files and not reused_files and not new_payload_fields:
            return error("No assets uploaded, reused, or fields provided in request", 400)

        # 更新任务 payload 与资产状态
        TaskManager.update_task_payload(task_id, new_payload_fields)
        TaskManager.update_asset_status(task_id, "ready")

        # 解除调度器等待，毫秒级开闸交付 GPU！
        scheduler.notify_asset_ready(task_id)

        log.info(f"[API] Task {task_id} assets READY! Uploaded: {list(saved_files.keys())}, Reused: {list(reused_files.keys())}")
        return success(message="Assets injected successfully and task released", data={
            "task_id": task_id,
            "asset_status": "ready",
            "files": saved_files,
            "uploaded_files": saved_files,
            "reused_files": reused_files,
        })
    except Exception as e:
        log.error(f"[API] Failed to process assets for task {task_id}: {e}", exc_info=True)
        return error(f"Failed to process asset upload/reuse: {str(e)}", 500)


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

    # 🌟 若任务尚未执行（排队中或等待素材），立即从调度器队列移出并唤醒素材看门狗
    scheduler.remove_from_queue(task_id)
    scheduler.abort_asset_wait(task_id)

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
    # 🌟 动作 C：广播 task_cancelled 契约事件
    cancel_payload = {
        "type": "task_cancelled",
        "task_id": task_id,
        "message": "任务已被用户手动停止"
    }
    ws_manager.send(task_id, cancel_payload)
    ws_manager.broadcast_queue(cancel_payload)

    # 兼容历史消费方：同时保留 task_failed 广播
    failed_payload = {
        "type": "task_failed",
        "task_id": task_id,
        "message": "任务已被用户手动停止"
    }
    ws_manager.send(task_id, failed_payload)
    ws_manager.broadcast_queue(failed_payload)

    log.info(f"[API] Task {task_id} cancellation initiated successfully.")
    log_cancel_debug(f"cancel_task complete for task_id={task_id}")
    return success(message="Task cancellation initiated", data={"task_id": task_id})

