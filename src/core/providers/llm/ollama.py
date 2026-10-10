# -*- coding: utf-8 -*-
"""
Ollama 本地大模型提供商实现
==========================
支持本地微服务的智能启停、资源监控以及与 Ollama 原生接口的流式与非流式交互。
"""

import queue
import threading
import requests
import json
from typing import Dict, Any

from src.core.providers.llm.base import BaseLLMProvider, register_provider
from src.core.service_controller import service_controller
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.logger import log

@register_provider("ollama")
class OllamaProvider(BaseLLMProvider):
    def generate(self, model: str, prompt: str, options: dict) -> dict:
        service_name = options.get("_service_name") or self.config.get("service_name", "Ollama")
        base_url = service_controller.get_service_url(service_name)
        if not base_url:
            raise RuntimeError(f"Ollama service '{service_name}' not available")

        api_url = f"{base_url.rstrip('/')}/api/generate"
        req_data = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {k: v for k, v in options.items() if v is not None and not k.startswith("_") and k != "images"},
        }
        if "images" in options and options["images"]:
            req_data["images"] = options["images"]

        # 针对特定大模型过滤思考机制，避免冗余数据
        if any(tag in model for tag in ("qwen3", "deepseek", "qwq")):
            req_data["think"] = False

        resp = requests.post(api_url, json=req_data, timeout=1200)
        resp.raise_for_status()
        data = resp.json()

        return {
            "text": data.get("response", ""),
            "model": model,
            "total_duration": data.get("total_duration"),
        }

    def generate_stream(self, task_id: str, model: str, prompt: str, options: dict,
                        output_queue: queue.Queue, started_event: threading.Event = None) -> None:
        service_name = options.get("_service_name") or self.config.get("service_name", "Ollama")
        
        # 针对本地 LLM 的排队流式同步（如适用）
        if started_event is not None and not started_event.is_set():
            log.info(f"[Ollama Stream] Task {task_id} waiting for client stream handshake...")
            if not started_event.wait(timeout=8.0):
                log.warning(f"[Ollama Stream] Task {task_id} stream handshake timeout, aborting.")
                output_queue.put(json.dumps({"error": "Stream connection timeout"}))
                output_queue.put(None)
                TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": "Stream handshake timeout"})
                ws_manager.send(task_id, {"type": "task_failed", "message": "Stream handshake timeout"})
                return

        base_url = service_controller.get_service_url(service_name)
        if not base_url:
            raise RuntimeError(f"Ollama service '{service_name}' not available")

        if TaskManager.is_cancelled(task_id):
            raise RuntimeError("任务已被用户手动停止")

        api_url = f"{base_url.rstrip('/')}/api/generate"
        req_data = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "options": {k: v for k, v in options.items() if v is not None and not k.startswith("_") and k != "images"},
        }
        if "images" in options and options["images"]:
            req_data["images"] = options["images"]

        if any(tag in model for tag in ("qwen3", "deepseek", "qwq")):
            req_data["think"] = False

        full_text = ""
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Local LLM streaming started"})

        resp = requests.post(api_url, json=req_data, stream=True, timeout=1200)
        resp.raise_for_status()

        for raw_line in resp.iter_lines():
            if TaskManager.is_cancelled(task_id):
                log.info(f"[Ollama Stream] Task {task_id} was cancelled. Closing connection.")
                resp.close()
                raise RuntimeError("任务已被用户手动停止")
            if raw_line:
                decoded_line = raw_line.decode('utf-8') if isinstance(raw_line, bytes) else raw_line
                output_queue.put(decoded_line)
                try:
                    data = json.loads(decoded_line)
                    full_text += data.get("response", "")
                    if data.get("done"):
                        break
                except json.JSONDecodeError:
                    pass

        output_queue.put(None)
        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result={"text": full_text, "model": model})
        ws_manager.send(task_id, {"type": "task_complete", "status": "success", "text": full_text, "model": model})
