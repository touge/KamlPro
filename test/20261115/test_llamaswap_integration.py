# -*- coding: utf-8 -*-
"""
LlamaSwap 动态发现与零配置集成测试
=================================
验证：
1. Provider 从 services.yaml 动态推导 base_url (零重复端口配置)
2. 在线与离线模型自发现 (自动解析视觉模态)
3. 调度器显存释放 (POST /api/models/unload)
4. GET /llm/models API 动态暴露
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath("."))

from src.core.service_controller import service_controller
from src.core.providers.llm.openai_compatible import OpenAICompatibleProvider
from src.core.providers.llm.manager import llm_provider_manager
from src.core.scheduler import scheduler


class TestLlamaSwapIntegration(unittest.TestCase):
    def setUp(self):
        llm_provider_manager.reload()

    def test_01_dynamic_base_url_derivation(self):
        """配置中无 base_url 时，自动从 service_name 关联的 services.yaml 推导"""
        prov = OpenAICompatibleProvider("test_prov", {"service_name": "LlamaSwap"})
        self.assertEqual(prov.get_base_url(), "http://127.0.0.1:11435/v1")
        self.assertEqual(prov.get_api_key(), "local")

    def test_02_dynamic_model_discovery_and_vision_modality(self):
        """自动从 llama-swap 发现 3 个模型，并精准识别 art-vision 模态"""
        prov = llm_provider_manager.get_provider_for_model("qwen3.8-code-fast")
        self.assertIsNotNone(prov)
        self.assertEqual(prov.name, "llamaswap")

        # 检查模态与层级
        self.assertEqual(llm_provider_manager.get_model_tier("qwen3.8-code-fast"), "local")
        self.assertEqual(llm_provider_manager.get_model_modalities("qwen3.8-code-fast"), ["text"])
        self.assertTrue(llm_provider_manager.supports_modality("qwen3.8-art-vision", "vision"))
        self.assertEqual(llm_provider_manager.get_service_name_for_model("qwen3.8-code-fast"), "LlamaSwap")

    def test_03_offline_model_discovery_fallback(self):
        """当服务不可访问时，优雅回退至本地 config.yaml 解析"""
        prov = OpenAICompatibleProvider("test_fallback", {
            "service_name": "LlamaSwap",
            "base_url": "http://127.0.0.1:65530/v1"
        })
        models = prov.discover_models()
        model_names = [m["name"] for m in models]
        self.assertIn("qwen3.8-code-fast", model_names)
        self.assertIn("qwen3.8-art-vision", model_names)
        vision_model = next(m for m in models if m["name"] == "qwen3.8-art-vision")
        self.assertIn("vision", vision_model["modalities"])

    def test_04_free_api_invokes_unload_endpoint(self):
        """验证调度器释放显存接口正确拼接 /api/models/unload"""
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = '{"msg":"ok"}'
            scheduler._free_service_resources("LlamaSwap")
            mock_post.assert_called_once()
            called_url = mock_post.call_args[0][0]
            self.assertEqual(called_url, "http://127.0.0.1:11435/api/models/unload")

    def test_05_api_list_models_includes_dynamic_llamaswap_models(self):
        """验证已注册模型包含 llama-swap 动态模型"""
        models = llm_provider_manager.get_available_models()
        discovered_names = [m["name"] for m in models]
        self.assertIn("qwen3.8-code-fast", discovered_names)
        self.assertIn("qwen3.8-code-128k", discovered_names)
        self.assertIn("qwen3.8-art-vision", discovered_names)


if __name__ == "__main__":
    unittest.main()
