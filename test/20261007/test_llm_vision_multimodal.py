# -*- coding: utf-8 -*-
"""
多模态大模型视觉 (Vision) 支持及图片加载单元与集成测试
======================================================
验证:
1. Manager 模态元数据读取与 supports_modality 逻辑
2. media_loader 对 Base64、Data URL 与本地文件的清洗和转换
3. LLM 执行器对非 vision 模型的防呆拦截
4. Ollama Provider 对 top-level images 数组的正确注入
5. OpenAI Compatible Provider 对多模态消息体 (image_url) 的正确构建
"""

import sys
import os
import json
import base64
import queue
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# 确保项目根路径加入 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.core.providers.llm.manager import llm_provider_manager
from src.core.utils.media_loader import normalize_image, normalize_images
from src.core.executors.llm import execute, execute_stream
from src.core.providers.llm.ollama import OllamaProvider
from src.core.providers.llm.openai_compatible import OpenAICompatibleProvider
from src.core.task_manager import TaskManager


class TestLLMVisionMultimodal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        llm_provider_manager.reload()

    def test_01_modalities_configuration(self):
        """测试 1: 验证 configs/llm_config.yaml 中各模型的模态解析"""
        vision_model = "orcarouter/Qwen3.8-27B-Uncensored:q3_K_M"
        text_model = "gemma3:12b"
        cloud_model = "deepseek-v4-pro"

        # 视觉模型支持 text 与 vision
        self.assertTrue(llm_provider_manager.supports_modality(vision_model, "vision"))
        self.assertTrue(llm_provider_manager.supports_modality(vision_model, "text"))
        self.assertIn("vision", llm_provider_manager.get_model_modalities(vision_model))

        # 纯文本模型不支持 vision
        self.assertFalse(llm_provider_manager.supports_modality(text_model, "vision"))
        self.assertTrue(llm_provider_manager.supports_modality(text_model, "text"))
        self.assertEqual(llm_provider_manager.get_model_modalities(text_model), ["text"])

        # 云端纯文本模型
        self.assertFalse(llm_provider_manager.supports_modality(cloud_model, "vision"))

    def test_02_media_loader_normalization(self):
        """测试 2: 验证 media_loader 的 Base64/Data URL/本地图片文件转码"""
        raw_bytes = b"fake-png-binary-data-for-testing"
        expected_b64 = base64.b64encode(raw_bytes).decode("utf-8")

        # 1. 纯 Base64
        self.assertEqual(normalize_image(expected_b64), expected_b64)

        # 2. Data URL 格式
        data_url = f"data:image/png;base64,{expected_b64}"
        self.assertEqual(normalize_image(data_url), expected_b64)

        # 3. 本地磁盘真实文件
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(raw_bytes)
            temp_path = f.name

        try:
            res_b64 = normalize_image(temp_path)
            self.assertEqual(res_b64, expected_b64)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        # 4. 批量处理 normalize_images
        batch = normalize_images([expected_b64, data_url])
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0], expected_b64)
        self.assertEqual(batch[1], expected_b64)

        # 5. 空列表与空值安全
        self.assertEqual(normalize_images([]), [])
        self.assertEqual(normalize_images(None), [])

    def test_03_executor_modality_guardrail(self):
        """测试 3: 验证执行器遇到纯文本模型传图时实施防呆拦截"""
        text_model = "gemma3:12b"
        task_id = TaskManager.create_task("llm_text")

        # 故意传入一张图片
        fake_b64 = base64.b64encode(b"sample").decode("utf-8")
        execute(task_id, model=text_model, prompt="描述这张图", images=[fake_b64])

        task = TaskManager.get_task(task_id)
        self.assertEqual(task["status"], TaskManager.STATUS_FAILED)
        self.assertIn("不支持视觉多模态", task["result"]["message"])

    def test_04_ollama_provider_images_injection(self):
        """测试 4: 验证 OllamaProvider 正确将 images 注入顶层请求体"""
        provider = OllamaProvider("ollama", {
            "type": "ollama",
            "tier": "local",
            "service_name": "Ollama",
            "base_url": "http://127.0.0.1:11434"
        })

        fake_b64 = base64.b64encode(b"ollama-test-image").decode("utf-8")
        captured_payloads = []

        def mock_post(url, **kwargs):
            req_json = kwargs.get("json", {})
            captured_payloads.append(req_json)
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            if req_json.get("stream"):
                resp.iter_lines.return_value = [
                    json.dumps({"response": "检测到", "done": False}).encode("utf-8"),
                    json.dumps({"response": "图片内容", "done": True}).encode("utf-8")
                ]
            else:
                resp.json.return_value = {
                    "response": "这是一张测试图片",
                    "total_duration": 12345
                }
            return resp

        with patch("requests.post", side_effect=mock_post), \
             patch("src.core.service_controller.service_controller.get_service_url", return_value="http://127.0.0.1:11434"):
            
            # 1. 测试非流式 generate
            res = provider.generate(
                model="orcarouter/Qwen3.8-27B-Uncensored:q3_K_M",
                prompt="请分析图片",
                options={"temperature": 0.5, "images": [fake_b64]}
            )
            self.assertEqual(res["text"], "这是一张测试图片")
            self.assertEqual(len(captured_payloads), 1)
            req1 = captured_payloads[0]
            self.assertIn("images", req1)
            self.assertEqual(req1["images"], [fake_b64])
            self.assertNotIn("images", req1["options"])  # 验证 options 中已被清除
            self.assertEqual(req1["options"]["temperature"], 0.5)

            # 2. 测试流式 generate_stream
            out_q = queue.Queue()
            task_id = TaskManager.create_task("llm_streaming")
            
            provider.generate_stream(
                task_id=task_id,
                model="orcarouter/Qwen3.8-27B-Uncensored:q3_K_M",
                prompt="请分析图片",
                options={"temperature": 0.8, "images": [fake_b64]},
                output_queue=out_q
            )
            self.assertEqual(len(captured_payloads), 2)
            req2 = captured_payloads[1]
            self.assertTrue(req2["stream"])
            self.assertIn("images", req2)
            self.assertEqual(req2["images"], [fake_b64])
            self.assertNotIn("images", req2["options"])

    def test_05_openai_compatible_multimodal_messages(self):
        """测试 5: 验证 OpenAICompatibleProvider 的 _build_messages 多模态格式"""
        fake_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        
        # 纯文本
        text_msgs = OpenAICompatibleProvider._build_messages("你好", {})
        self.assertEqual(text_msgs, [{"role": "user", "content": "你好"}])

        # 多模态
        vision_msgs = OpenAICompatibleProvider._build_messages("识别这张图", {"images": [fake_b64]})
        self.assertEqual(len(vision_msgs), 1)
        content = vision_msgs[0]["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0], {"type": "text", "text": "识别这张图"})
        self.assertEqual(content[1], {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{fake_b64}"}
        })


if __name__ == "__main__":
    unittest.main()
