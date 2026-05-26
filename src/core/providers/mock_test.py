# -*- coding: utf-8 -*-
"""
自发现演示用 Mock 提供商
========================
用于证明“拖放即插即用”功能的插件类。
"""

import queue
import threading
import time
import json

from src.core.providers.base import BaseLLMProvider, register_provider
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.logger import log

@register_provider("mock_test")
class MockTestProvider(BaseLLMProvider):
    def generate(self, model: str, prompt: str, options: dict) -> dict:
        log.success(f"[Mock Provider] generate called for model '{model}'")
        return {
            "text": f"[Mock Autodiscovered Provider] Prompt received: '{prompt}'",
            "model": model
        }

    def generate_stream(self, task_id: str, model: str, prompt: str, options: dict,
                        output_queue: queue.Queue, started_event: threading.Event = None) -> None:
        log.success(f"[Mock Provider] generate_stream called for model '{model}'")
        
        if started_event is not None and not started_event.is_set():
            started_event.wait(timeout=8.0)

        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Mock streaming"})
        
        sim_text = f"[Mock Auto-Discovered Stream] Prompt: '{prompt}'"
        for word in sim_text.split(" "):
            time.sleep(0.1)
            output_queue.put(json.dumps({"response": word + " ", "done": False}))
            
        output_queue.put(json.dumps({"response": "", "done": True}))
        output_queue.put(None)
        
        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result={"text": sim_text, "model": model})
        ws_manager.send(task_id, {"type": "task_complete", "status": "success", "text": sim_text, "model": model})
