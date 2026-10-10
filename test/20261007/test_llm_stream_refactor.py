# -*- coding: utf-8 -*-
"""
本地模型流式与 tier/type 职责解耦集成验证测试
=============================================
测试范围：
1. 模型 tier 继承与动态解析验证 (local vs api)
2. 微服务名称动态解析验证 (Ollama vs None)
3. 调度器任务分流路由测试 (本地 GPU 队列 vs 云端并发池)
4. 本地大模型流式任务 (llm_streaming) 入队与即时/排队标志验证
5. 执行器 execute_stream 针对本地模型的流式数据队列推送能力验证
"""

import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.abspath("."))

import unittest
import queue
import threading
import json
from unittest.mock import MagicMock, patch

from src.core.providers.llm.manager import llm_provider_manager
from src.core.task_manager import TaskManager
from src.core.scheduler import scheduler
from src.core.executors.llm import execute, execute_stream


class TestLLMStreamAndTierRefactor(unittest.TestCase):
    def test_01_tier_inheritance(self):
        """测试 1: 验证 tier 从 Provider 配置段正确继承到各模型"""
        # 本地 Ollama 组
        self.assertEqual(llm_provider_manager.get_model_tier("gemma3:12b"), "local")
        self.assertEqual(llm_provider_manager.get_model_tier("qwen3:14b"), "local")

        # 云端 DeepSeek 组
        self.assertEqual(llm_provider_manager.get_model_tier("deepseek-v4-flash"), "api")
        self.assertEqual(llm_provider_manager.get_model_tier("deepseek-v4-pro"), "api")

        # 云端/代理 Gemini 组
        self.assertEqual(llm_provider_manager.get_model_tier("gemini-3-flash"), "api")
        self.assertEqual(llm_provider_manager.get_model_tier("gemini-3-pro"), "api")
        self.assertEqual(llm_provider_manager.get_model_tier("gemini-3-flash-thinking"), "api")

        # 未知模型兜底回退至 ollama (tier=local)
        self.assertEqual(llm_provider_manager.get_model_tier("non_existent_model"), "local")

    def test_02_service_name_resolution(self):
        """测试 2: 验证物理微服务名称根据模型所属 Provider 动态解析"""
        # 本地 Ollama 模型 -> 物理服务 "Ollama"
        self.assertEqual(llm_provider_manager.get_service_name_for_model("gemma3:12b"), "Ollama")
        self.assertEqual(llm_provider_manager.get_service_name_for_model("qwen3:14b"), "Ollama")

        # 云端 API 模型 -> None (不依赖本地物理微服务)
        self.assertIsNone(llm_provider_manager.get_service_name_for_model("deepseek-v4-flash"))
        self.assertIsNone(llm_provider_manager.get_service_name_for_model("gemini-3-flash"))

    def test_03_local_llm_streaming_routing(self):
        """测试 3: 本地大模型流式任务 (llm_streaming) 正确进入本地 GPU 队列并注册 stream_events"""
        task_id = TaskManager.create_task("llm_streaming")
        output_q = queue.Queue(maxsize=256)
        started_evt = threading.Event()

        # 模拟提交本地模型的流式任务
        can_stream = scheduler.submit_task(
            "llm_streaming",
            task_id,
            {"model": "qwen3:14b", "prompt": "你好"},
            output_queue=output_q,
            started_event=started_evt
        )

        # 验证已成功在 stream_events 注册
        self.assertIn(task_id, scheduler.stream_events)
        self.assertEqual(scheduler.stream_events[task_id]["event"], started_evt)
        self.assertEqual(scheduler.stream_events[task_id]["queue"], output_q)

        # 清理该任务
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": "test cleanup"})

    def test_04_cloud_llm_streaming_routing(self):
        """测试 4: 云端大模型流式任务 (llm_streaming) 走云端并发通道"""
        task_id = TaskManager.create_task("llm_streaming")
        output_q = queue.Queue(maxsize=256)
        started_evt = threading.Event()

        with patch.object(scheduler.api_executor, "submit") as mock_submit:
            can_stream = scheduler.submit_task(
                "llm_streaming",
                task_id,
                {"model": "deepseek-v4-flash", "prompt": "测试并发"},
                output_queue=output_q,
                started_event=started_evt
            )
            # 云端任务必定提交至 ThreadPoolExecutor
            mock_submit.assert_called_once()

    def test_05_local_stream_executor_data_flow(self):
        """测试 5: 执行器 execute_stream 针对本地模型的流式数据消费链路"""
        task_id = TaskManager.create_task("llm_streaming")
        output_q = queue.Queue(maxsize=256)
        started_evt = threading.Event()
        started_evt.set()  # 模拟客户端已握手连接

        # Mock OllamaProvider.generate_stream
        ollama_prov = llm_provider_manager.get_provider_for_model("qwen3:14b")
        def mock_generate_stream(t_id, model, prompt, options, out_q, s_evt):
            out_q.put(json.dumps({"response": "你", "done": False}))
            out_q.put(json.dumps({"response": "好", "done": False}))
            out_q.put(json.dumps({"response": "！", "done": True}))
            out_q.put(None)
            TaskManager.update_task(t_id, TaskManager.STATUS_SUCCESS, {"text": "你好！"})

        with patch.object(ollama_prov, "generate_stream", side_effect=mock_generate_stream):
            execute_stream(task_id, output_q, started_evt, model="qwen3:14b", prompt="你好")

            # 验证队列中收到了 NDJSON token
            chunk1 = json.loads(output_q.get(timeout=2))
            self.assertEqual(chunk1["response"], "你")
            chunk2 = json.loads(output_q.get(timeout=2))
            self.assertEqual(chunk2["response"], "好")
            chunk3 = json.loads(output_q.get(timeout=2))
            self.assertEqual(chunk3["response"], "！")
            self.assertTrue(chunk3["done"])
            self.assertIsNone(output_q.get(timeout=2))

            task_info = TaskManager.get_task(task_id)
            self.assertEqual(task_info["status"], TaskManager.STATUS_SUCCESS)


if __name__ == "__main__":
    unittest.main()
