# -*- coding: utf-8 -*-
"""
OpenAI 兼容协议大模型提供商实现
=============================
支持各大云端大模型底座 API (包括 DeepSeek, OpenAI, Gemini-OpenAI 等)。
原生集成 `reasoning_content` 推理深度思考流解析，并自动完成 `<think>` 标签包装。
"""

import queue
import threading
import requests
import json
import time
from typing import Dict, Any

from src.core.providers.llm.base import BaseLLMProvider, register_provider
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.logger import log

@register_provider("openai_compatible")
class OpenAICompatibleProvider(BaseLLMProvider):
    def generate(self, model: str, prompt: str, options: dict) -> dict:
        base_url = self.config.get("base_url", "")
        api_key = self._resolve_env_var(self.config.get("api_key", ""))

        # 💡 调试辅助：如果未配 Key，自动进入本地模拟测试模式，确保测试用例开箱即用
        if not api_key:
            log.warning(f"[LLM] No API Key configured for '{self.name}', running in Simulated Mode.")
            time.sleep(0.5)  # 模拟网络延迟
            sim_text = f"[Simulated Cloud API Response via {self.name}] You asked: '{prompt}'. This is a one-time full package response."
            return {
                "text": sim_text,
                "model": model,
            }

        if not base_url:
            raise RuntimeError(f"Cloud provider '{self.name}' has no base_url configured")

        api_url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        req_data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": options.get("temperature") if options.get("temperature") is not None else 0.7,
            "max_tokens": options.get("max_tokens") or options.get("max_predict"),
        }
        req_data = {k: v for k, v in req_data.items() if v is not None and not k.startswith("_")}

        resp = requests.post(api_url, json=req_data, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Empty response from cloud API")

        content = choices[0].get("message", {}).get("content", "")
        return {
            "text": content,
            "model": model,
        }

    def generate_stream(self, task_id: str, model: str, prompt: str, options: dict,
                        output_queue: queue.Queue, started_event: threading.Event = None) -> None:
        # 等待前端流式连接信号（针对云端 API 模式且尚未建立流连接时进行握手同步）
        if started_event is not None and not started_event.is_set():
            log.info(f"[LLM Stream] Cloud task {task_id} waiting for client stream handshake...")
            if not started_event.wait(timeout=8.0):
                log.warning(f"[LLM Stream] Cloud task {task_id} stream handshake timeout, aborting.")
                output_queue.put(json.dumps({"error": "Stream connection timeout"}))
                output_queue.put(None)
                TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": "Stream handshake timeout"})
                ws_manager.send(task_id, {"type": "task_failed", "message": "Stream handshake timeout"})
                return

        base_url = self.config.get("base_url", "")
        api_key = self._resolve_env_var(self.config.get("api_key", ""))

        # 💡 调试辅助：如果未配 Key，自动进入本地模拟测试模式，确保测试用例开箱即用
        if not api_key:
            log.warning(f"[LLM Stream] No API Key configured for '{self.name}', running in Simulated Mode.")
            sim_text = f"[Simulated Cloud API Response via {self.name}] You asked: '{prompt}'. Let's run a robust streaming pipeline!"
            TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Simulating cloud response"})

            words = sim_text.split(" ")
            for i, word in enumerate(words):
                if TaskManager.is_cancelled(task_id):
                    raise RuntimeError("任务已被用户手动停止")
                time.sleep(0.15)  # 模拟网络与生成延迟
                token = word + " " if i < len(words) - 1 else word
                simulated_line = json.dumps({"response": token, "done": False})
                output_queue.put(simulated_line)

            output_queue.put(json.dumps({"response": "", "done": True}))
            output_queue.put(None)

            TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result={"text": sim_text, "model": model})
            ws_manager.send(task_id, {"type": "task_complete", "status": "success", "text": sim_text, "model": model})
            return

        if not base_url:
            raise RuntimeError(f"Cloud provider '{self.name}' has no base_url configured")

        if TaskManager.is_cancelled(task_id):
            raise RuntimeError("任务已被用户手动停止")

        api_url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        req_data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "temperature": options.get("temperature") if options.get("temperature") is not None else 0.7,
            "max_tokens": options.get("max_tokens") or options.get("max_predict"),
        }
        req_data = {k: v for k, v in req_data.items() if v is not None and not k.startswith("_")}

        full_text = ""
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Cloud LLM streaming started"})

        resp = requests.post(api_url, json=req_data, headers=headers, stream=True, timeout=120)
        resp.raise_for_status()

        # 逐块解析 OpenAI Stream 格式 (SSE) 并将其转换为统一 of Ollama NDJSON 格式
        thinking = False
        for raw_line in resp.iter_lines():
            if TaskManager.is_cancelled(task_id):
                log.info(f"[LLM Stream] Task {task_id} was cancelled. Closing connection.")
                resp.close()
                raise RuntimeError("任务已被用户手动停止")
            if not raw_line:
                continue
            line = raw_line.decode('utf-8') if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data: "):
                continue

            data_str = line[len("data: "):].strip()
            if data_str == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})

                reasoning = delta.get("reasoning_content", "")
                token = delta.get("content", "")

                # 处理推理/思考内容
                if reasoning:
                    if not thinking:
                        thinking = True
                        # 放入 <think> 标签，与 Ollama 深度思考模式对齐
                        output_queue.put(json.dumps({"response": "<think>\n", "done": False}))
                    output_queue.put(json.dumps({"response": reasoning, "done": False}))
                    full_text += reasoning

                # 处理正式回答内容
                if token:
                    if thinking:
                        thinking = False
                        # 放入 </think> 标签
                        output_queue.put(json.dumps({"response": "\n</think>\n\n", "done": False}))
                    full_text += token
                    simulated_line = json.dumps({"response": token, "done": False})
                    output_queue.put(simulated_line)
            except json.JSONDecodeError:
                pass

        # 若以思考状态结束，安全闭合标签
        if thinking:
            output_queue.put(json.dumps({"response": "\n</think>\n\n", "done": False}))

        # 发送结束标记
        output_queue.put(json.dumps({"response": "", "done": True}))
        output_queue.put(None)

        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result={"text": full_text, "model": model})
        ws_manager.send(task_id, {"type": "task_complete", "status": "success", "text": full_text, "model": model})
