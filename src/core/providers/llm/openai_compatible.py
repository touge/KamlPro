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
    def get_base_url(self) -> str:
        """获取 base_url。若未显式配置，自动从绑定的 service_name 动态推导并拼接 /v1。"""
        base_url = self.config.get("base_url", "")
        if not base_url:
            service_name = self.config.get("service_name")
            if service_name:
                from src.core.service_controller import service_controller
                svc_url = service_controller.get_service_url(service_name)
                if svc_url:
                    base_url = f"{svc_url.rstrip('/')}/v1"
        return base_url

    def get_api_key(self) -> str:
        """获取 API Key。本地微服务缺省免 Key，默认使用 local 以免误入模拟模式。"""
        api_key = self._resolve_env_var(self.config.get("api_key", ""))
        if not api_key and self.config.get("service_name"):
            return "local"
        return api_key

    def discover_models(self) -> list[dict]:
        """
        动态发现当前提供商支持的模型列表：
        1. 优先尝试从在线 API (GET {base_url}/models) 实时发现。
        2. 若服务未启动或连接受限，且配置了本地微服务 (service_name)，回退至读取该服务根目录下的配置文件 (config.yaml)。
        """
        base_url = self.get_base_url()
        api_key = self.get_api_key()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        # ---- 策略 1: 在线 API 探测 ----
        if base_url:
            api_url = f"{base_url.rstrip('/')}/models"
            try:
                resp = requests.get(api_url, headers=headers, timeout=2.5)
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    discovered = []
                    for item in data:
                        m_id = item.get("id")
                        if not m_id:
                            continue
                        modalities = ["text"]
                        arch = item.get("architecture", {})
                        in_mods = arch.get("input_modalities", [])
                        if "image" in in_mods or "vision" in in_mods or "vision" in m_id.lower() or "art" in m_id.lower():
                            modalities.append("vision")
                        discovered.append({
                            "name": m_id,
                            "display_name": item.get("name") or m_id,
                            "modalities": modalities,
                            "enabled": True,
                        })
                    if discovered:
                        log.info(f"[LLM] Auto-discovered {len(discovered)} models online from '{api_url}'")
                        return discovered
            except Exception as e:
                log.debug(f"[LLM] Online model discovery failed for '{self.name}' ({api_url}): {e}")

        # ---- 策略 2: 本地配置文件回退读取 ----
        service_name = self.config.get("service_name")
        if service_name:
            from src.core.service_controller import service_controller
            svc_cfg = service_controller.get_service_config(service_name)
            if svc_cfg:
                import os
                import yaml
                script_path = None
                platforms = svc_cfg.get("platforms")
                if isinstance(platforms, dict):
                    # 优先取 windows 平台，缺省取第一个平台脚本
                    win_cfg = platforms.get("windows")
                    if isinstance(win_cfg, dict):
                        script_path = win_cfg.get("path")
                    elif platforms:
                        first_plat = next(iter(platforms.values()))
                        if isinstance(first_plat, dict):
                            script_path = first_plat.get("path")
                if not script_path:
                    script_path = svc_cfg.get("path")

                if script_path:
                    svc_dir = os.path.dirname(os.path.abspath(script_path))
                    for cfg_name in ("config.yaml", "config.yml"):
                        cfg_file = os.path.join(svc_dir, cfg_name)
                        if os.path.exists(cfg_file):
                            try:
                                with open(cfg_file, "r", encoding="utf-8") as f:
                                    local_cfg = yaml.safe_load(f) or {}
                                models_dict = local_cfg.get("models", {})
                                if isinstance(models_dict, dict) and models_dict:
                                    discovered = []
                                    for m_id, m_info in models_dict.items():
                                        modalities = ["text"]
                                        cmd_str = str(m_info.get("cmd", "")) if isinstance(m_info, dict) else ""
                                        if "vision" in m_id.lower() or "art" in m_id.lower() or "--mmproj" in cmd_str:
                                            modalities.append("vision")
                                        discovered.append({
                                            "name": m_id,
                                            "display_name": m_id,
                                            "modalities": modalities,
                                            "enabled": True,
                                        })
                                    log.info(f"[LLM] Auto-discovered {len(discovered)} models offline from '{cfg_file}'")
                                    return discovered
                            except Exception as e:
                                log.warning(f"[LLM] Failed to parse local service config '{cfg_file}': {e}")
        return []

    @staticmethod
    def _build_messages(prompt: str, options: dict) -> list:
        """根据是否包含图片，构建单轮或多模态消息体"""
        images = options.get("images")
        if not images:
            return [{"role": "user", "content": prompt}]
        content_list = [{"type": "text", "text": prompt}]
        for img in images:
            img_url = img if str(img).startswith("data:") else f"data:image/jpeg;base64,{img}"
            content_list.append({
                "type": "image_url",
                "image_url": {"url": img_url}
            })
        return [{"role": "user", "content": content_list}]

    def generate(self, model: str, prompt: str, options: dict) -> dict:
        base_url = self.get_base_url()
        api_key = self.get_api_key()

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
            "messages": self._build_messages(prompt, options),
            "stream": False,
            "temperature": options.get("temperature") if options.get("temperature") is not None else 0.7,
            "max_tokens": options.get("max_tokens") or options.get("max_predict"),
        }
        req_data = {k: v for k, v in req_data.items() if v is not None and not k.startswith("_") and k != "images"}

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

        base_url = self.get_base_url()
        api_key = self.get_api_key()

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
            "messages": self._build_messages(prompt, options),
            "stream": True,
            "temperature": options.get("temperature") if options.get("temperature") is not None else 0.7,
            "max_tokens": options.get("max_tokens") or options.get("max_predict"),
        }
        req_data = {k: v for k, v in req_data.items() if v is not None and not k.startswith("_") and k != "images"}

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
