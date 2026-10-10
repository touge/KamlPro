# -*- coding: utf-8 -*-
"""
LLM 文本生成任务执行器
======================
双轨代理分发执行器：
不再硬编码分流逻辑与 API 请求。它将动态路由匹配所属大模型的 Provider 实例并直接委托执行。
"""

import queue
import threading
import json
from typing import Dict, Any

from src.core.providers.llm.manager import llm_provider_manager
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.core.utils.media_loader import normalize_images
from src.logic.logger import log


def _process_multimodal_images(model: str, payload: dict, options: dict) -> None:
    """提取、校验并归一化多模态图片输入"""
    images = payload.get("images") or options.get("images")
    if not images:
        return

    # 防呆拦截：校验模型是否支持 vision 模态
    if not llm_provider_manager.supports_modality(model, "vision"):
        supported = llm_provider_manager.get_model_modalities(model)
        raise ValueError(
            f"模型 '{model}' 不支持视觉多模态 (vision)，当前支持模态: {supported}。"
            f"如需图片理解，请选用配置了 'vision' 模态的模型（如 'orcarouter/Qwen3.8-27B-Uncensored:q3_K_M'）。"
        )

    # 规范化转码为纯 Base64 列表
    normalized = normalize_images(images)
    options["images"] = normalized


def execute(task_id: str, **payload):
    """
    LLM 生成任务入口（非流式）。由调度器动态调用。

    payload 期望字段:
      - model: str — 模型名称
      - prompt: str — 提示词
      - images: list[str] (可选) — 图片文件路径/URL/Base64
      - options: dict (可选) — temperature, max_tokens 等
    """
    model = payload.get("model", "")
    prompt = payload.get("prompt", "")
    options = dict(payload.get("options") or {})

    if not model or not prompt:
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
            {"message": "Missing 'model' or 'prompt' in payload"})
        return

    try:
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING,
            {"message": "LLM processing..."})

        if TaskManager.is_cancelled(task_id):
            raise RuntimeError("任务已被用户手动停止")

        # 校验并处理视觉多模态输入
        _process_multimodal_images(model, payload, options)
            
        provider = llm_provider_manager.get_provider_for_model(model)
        result = provider.generate(model, prompt, options)
            
        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result=result)
    except Exception as e:
        if "任务已被用户手动停止" in str(e):
            log.info(f"[LLM Executor] Task {task_id} was cancelled by user.")
        else:
            log.error(f"[LLM Executor] Task {task_id} failed: {e}", exc_info=True)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})


def execute_stream(task_id: str, output_queue: queue.Queue, started_event: threading.Event = None, **payload):
    """
    流式 LLM 执行器。由调度器或并发线程池调用。
    如果 started_event 不为空，代表是云端并发模式，我们需要在此阻塞等待前端 HTTP Stream 建立。
    """
    model = payload.get("model", "")
    prompt = payload.get("prompt", "")
    options = dict(payload.get("options") or {})

    if not model or not prompt:
        output_queue.put(json.dumps({"error": "Missing 'model' or 'prompt' in payload"}))
        output_queue.put(None)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
            {"message": "Missing 'model' or 'prompt' in payload"})
        ws_manager.send(task_id, {"type": "task_failed", "message": "Missing 'model' or 'prompt' in payload"})
        return

    try:
        # 将 _service_name 传递给 options 便于 Provider 确定关联 of 本地物理服务
        if "_service_name" in payload:
            options["_service_name"] = payload["_service_name"]

        # 校验并处理视觉多模态输入
        _process_multimodal_images(model, payload, options)

        provider = llm_provider_manager.get_provider_for_model(model)
        provider.generate_stream(task_id, model, prompt, options, output_queue, started_event)
            
    except Exception as e:
        if "任务已被用户手动停止" in str(e):
            log.info(f"[LLM Stream] Task {task_id} was cancelled by user.")
        else:
            log.error(f"[LLM Stream] Task {task_id} failed: {e}", exc_info=True)
        output_queue.put(json.dumps({"error": str(e)}))
        output_queue.put(None)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        ws_manager.send(task_id, {"type": "task_failed", "message": str(e)})
