# -*- coding: utf-8 -*-
"""
Today's Refactor (2026-05-27) Unit and Mock Tests
==================================================
This test script validates:
1. Dynamic discovery and class registry of LLM providers.
2. Dynamic discovery and class registry of Image providers (ComfyUI).
3. Dynamic discovery and class registry of Audio/Dubbing providers (IndexTTS).
4. Executor mock delegation for Image (ComfyUI) execution routing.
5. Executor mock delegation for Audio (TTS & Subtitle) execution routing.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.core.providers.llm.manager import llm_provider_manager
from src.core.providers.image.manager import image_provider_manager
from src.core.providers.audio.manager import audio_provider_manager

from src.core.executors.comfyui import execute as execute_image
from src.core.executors.tts import execute as execute_tts
from src.core.executors.subtitle import execute as execute_subtitle


class TestProvidersRefactor(unittest.TestCase):

    def test_01_llm_provider_registry(self):
        """Verify LLM provider manager dynamic registry loads successfully."""
        print("\n[Test 1] Verifying LLM Provider dynamic registries...")
        # Check managers successfully loaded and cached Ollama and OpenAICompatible
        self.assertIn("ollama", llm_provider_manager.providers)
        self.assertIn("deepseek", llm_provider_manager.providers)
        
        # Verify provider instances
        ollama_provider = llm_provider_manager.providers["ollama"]
        self.assertEqual(ollama_provider.name, "ollama")
        
        print("[Test 1] SUCCESS: LLM providers loaded correctly.")

    def test_02_image_provider_registry(self):
        """Verify Image provider manager dynamic registry loads successfully."""
        print("\n[Test 2] Verifying Image Provider dynamic registries...")
        # Get comfyui provider
        comfyui_provider = image_provider_manager.get_provider("comfyui")
        self.assertIsNotNone(comfyui_provider)
        self.assertEqual(comfyui_provider.name, "comfyui")
        
        print("[Test 2] SUCCESS: Image provider (ComfyUI) loaded correctly.")

    def test_03_audio_provider_registry(self):
        """Verify Audio provider manager dynamic registry loads successfully."""
        print("\n[Test 3] Verifying Audio Provider dynamic registries...")
        # Get index_tts provider
        index_tts_provider = audio_provider_manager.get_provider("index_tts")
        self.assertIsNotNone(index_tts_provider)
        self.assertEqual(index_tts_provider.name, "index_tts")
        
        print("[Test 3] SUCCESS: Audio provider (IndexTTS) loaded correctly.")

    @patch("src.core.providers.image.manager.image_provider_manager.get_provider")
    @patch("src.core.task_manager.TaskManager.update_task")
    @patch("src.core.ws_manager.ws_manager.send")
    def test_04_image_executor_delegation(self, mock_ws_send, mock_update_task, mock_get_provider):
        """Verify image executor dynamically fetches ComfyUIProvider and routes execution."""
        print("\n[Test 4] Verifying image executor dynamic routing...")
        
        mock_provider = MagicMock()
        mock_provider.generate_image.return_value = {"node_id": "image_result.png"}
        mock_get_provider.return_value = mock_provider

        task_id = "test_image_task_123"
        payload = {
            "workflow": {"1": {"class_type": "KSampler"}},
            "final_output_node_id": "9",
            "provider": "comfyui"
        }

        # Run the executor
        execute_image(task_id, **payload)

        # Assert provider manager was queried
        mock_get_provider.assert_called_once_with("comfyui")
        # Assert generate_image was executed with expected arguments
        mock_provider.generate_image.assert_called_once()
        
        # Verify payload elements passed down
        called_args = mock_provider.generate_image.call_args[0][0]
        self.assertEqual(called_args["task_id"], task_id)
        self.assertEqual(called_args["final_output_node_id"], "9")

        print("[Test 4] SUCCESS: Image executor delegated execution successfully.")

    @patch("src.core.providers.audio.manager.audio_provider_manager.get_provider")
    @patch("src.core.task_manager.TaskManager.update_task")
    @patch("src.core.ws_manager.ws_manager.send")
    def test_05_tts_executor_delegation(self, mock_ws_send, mock_update_task, mock_get_provider):
        """Verify TTS executor dynamically fetches AudioProvider and routes execution."""
        print("\n[Test 5] Verifying TTS executor dynamic routing...")
        
        mock_provider = MagicMock()
        mock_provider.generate_speech.return_value = {"status": "completed", "output": "audio.wav"}
        mock_get_provider.return_value = mock_provider

        task_id = "test_tts_task_123"
        payload = {
            "text": "Hello world",
            "speaker": "female_1",
            "provider": "index_tts"
        }

        # Run the executor
        execute_tts(task_id, **payload)

        # Assert provider manager was queried
        mock_get_provider.assert_called_once_with("index_tts")
        # Assert generate_speech was executed
        mock_provider.generate_speech.assert_called_once()

        print("[Test 5] SUCCESS: TTS executor delegated execution successfully.")

    @patch("src.core.providers.audio.manager.audio_provider_manager.get_provider")
    @patch("src.core.task_manager.TaskManager.update_task")
    @patch("src.core.ws_manager.ws_manager.send")
    def test_06_subtitle_executor_delegation(self, mock_ws_send, mock_update_task, mock_get_provider):
        """Verify subtitle executor dynamically fetches AudioProvider and routes execution."""
        print("\n[Test 6] Verifying subtitle executor dynamic routing...")
        
        mock_provider = MagicMock()
        mock_provider.align_subtitle.return_value = {"status": "completed", "output": "subtitles.srt"}
        mock_get_provider.return_value = mock_provider

        task_id = "test_sub_task_123"
        payload = {
            "text": "Hello world",
            "audio_file_path": "dummy.wav",
            "provider": "index_tts"
        }

        # Run the executor
        execute_subtitle(task_id, **payload)

        # Assert provider manager was queried
        mock_get_provider.assert_called_once_with("index_tts")
        # Assert align_subtitle was executed
        mock_provider.align_subtitle.assert_called_once()

        print("[Test 6] SUCCESS: Subtitle executor delegated execution successfully.")


if __name__ == "__main__":
    unittest.main()
