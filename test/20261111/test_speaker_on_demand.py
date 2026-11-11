# -*- coding: utf-8 -*-
"""
TTS 发音人代理按需拉起与内存缓存自动化测试
==========================================
验证：
1. TTS 服务禁用 (enabled: false) 时返回 503 且不拉起进程
2. 无缓存且未运行时：通过调度器串行拉起 TTS（必要时先停止旧空闲服务）-> 抓取列表 -> 写入内存缓存 -> 自动停止临时 TTS
3. 命中内存缓存：毫秒级直接返回，不触发进程启停与后端 HTTP 请求
4. 强制刷新 (?refresh=true)：绕过缓存重新抓取并刷新缓存
5. auto_start: true 或服务已运行：获取并缓存后不强杀 TTS 进程
6. 后端失败或启动异常：不写入错误缓存，且临时拉起的 TTS 仍能正常回收
7. 调度器队列隔离：内部探测任务不污染 get_queue_status / get_queue_positions；若队首已有真实 tts 任务则保留服务复用
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import speaker as speaker_route
from src.core.scheduler import scheduler
from src.core.service_controller import service_controller


AUTH_HEADERS = {"Authorization": "Bearer 123456"}


class TestSpeakerOnDemand(unittest.TestCase):
    def setUp(self):
        speaker_route.clear_speaker_cache()
        self._orig_current = scheduler.current_service_name
        scheduler.current_service_name = None
        self.client = TestClient(app)

    def tearDown(self):
        speaker_route.clear_speaker_cache()
        scheduler.current_service_name = self._orig_current

    def test_01_disabled_service_returns_503(self):
        """TTS 服务禁用时直接返回 503，不触发任何网络或启动动作"""
        with patch.object(service_controller, "is_enabled", return_value=False), \
             patch.object(service_controller, "safe_start") as mock_start, \
             patch("src.api.routes.speaker.sync_requests.get") as mock_get:
            resp = self.client.get("/interface/tts/speakers", headers=AUTH_HEADERS)

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["status"], "error")
        mock_start.assert_not_called()
        mock_get.assert_not_called()

    def test_02_on_demand_start_cache_and_stop(self):
        """无缓存且未运行时：经调度器切换/拉起 TTS -> 缓存结果 -> 停止临时进程 -> 二次命中缓存"""
        scheduler.current_service_name = "ComfyUI"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "status": "success",
            "data": {"categories": [{"name": "默认", "speakers": ["解说-魔魔"]}]},
        }

        with patch.object(service_controller, "is_enabled", return_value=True), \
             patch.object(service_controller, "get_service_url", return_value="http://127.0.0.1:7012"), \
             patch.object(service_controller, "get_service_config", return_value={"token": "123456"}), \
             patch.object(service_controller, "status", return_value={"running": False}), \
             patch.object(service_controller, "is_auto_start", return_value=False), \
             patch.object(service_controller, "safe_start") as mock_start, \
             patch.object(service_controller, "stop") as mock_stop, \
             patch("src.api.routes.speaker.sync_requests.get", return_value=mock_resp) as mock_get:

            # 首次请求：触发切换停止 ComfyUI -> 拉起 TTS -> 获取并缓存 -> 停止 TTS
            r1 = self.client.get("/interface/tts/speakers", headers=AUTH_HEADERS)
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(
                r1.json()["data"],
                {"categories": [{"name": "默认", "speakers": ["解说-魔魔"]}]},
            )
            mock_start.assert_called_once_with("TTS", timeout=scheduler.startup_timeout)
            # 先停旧服务 ComfyUI，再停临时拉起的 TTS
            self.assertEqual(mock_stop.call_args_list[0].args[0], "ComfyUI")
            self.assertEqual(mock_stop.call_args_list[1].args[0], "TTS")
            self.assertEqual(mock_get.call_count, 1)
            self.assertIsNone(scheduler.current_service_name)

            # 第二次请求：直接命中内存缓存，不再启动也不再发 HTTP 请求
            r2 = self.client.get("/interface/tts/speakers", headers=AUTH_HEADERS)
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.json()["data"], r1.json()["data"])
            self.assertEqual(mock_start.call_count, 1)
            self.assertEqual(mock_get.call_count, 1)

    def test_03_refresh_bypasses_cache(self):
        """传入 ?refresh=true 时绕过内存缓存并更新缓存"""
        speaker_route._set_cached_speakers({"categories": [{"name": "旧", "speakers": ["A"]}]})
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"data": {"categories": [{"name": "新", "speakers": ["B"]}]}}

        with patch.object(service_controller, "is_enabled", return_value=True), \
             patch.object(service_controller, "get_service_url", return_value="http://127.0.0.1:7012"), \
             patch.object(service_controller, "get_service_config", return_value={"token": "123456"}), \
             patch.object(service_controller, "status", return_value={"running": False}), \
             patch.object(service_controller, "is_auto_start", return_value=False), \
             patch.object(service_controller, "safe_start") as mock_start, \
             patch.object(service_controller, "stop") as mock_stop, \
             patch("src.api.routes.speaker.sync_requests.get", return_value=mock_resp):
            r = self.client.get("/interface/tts/speakers?refresh=true", headers=AUTH_HEADERS)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"], {"categories": [{"name": "新", "speakers": ["B"]}]})
        self.assertEqual(speaker_route.get_cached_speakers(), r.json()["data"])
        mock_start.assert_called_once()
        mock_stop.assert_called_once_with("TTS")

    def test_04_auto_start_or_already_running_is_not_stopped(self):
        """auto_start: true 或服务原本已在运行时，获取后不停止 TTS 进程"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"data": {"categories": []}}

        # 场景 A: 服务原本已在运行
        with patch.object(service_controller, "is_enabled", return_value=True), \
             patch.object(service_controller, "get_service_url", return_value="http://127.0.0.1:7012"), \
             patch.object(service_controller, "get_service_config", return_value={}), \
             patch.object(service_controller, "status", return_value={"running": True}), \
             patch.object(service_controller, "safe_start") as mock_start, \
             patch.object(service_controller, "stop") as mock_stop, \
             patch("src.api.routes.speaker.sync_requests.get", return_value=mock_resp):
            r1 = self.client.get("/interface/tts/speakers", headers=AUTH_HEADERS)
        self.assertEqual(r1.status_code, 200)
        mock_start.assert_not_called()
        mock_stop.assert_not_called()

        # 场景 B: auto_start=True 且未运行时被拉起，完成后不强杀
        speaker_route.clear_speaker_cache()
        with patch.object(service_controller, "is_enabled", return_value=True), \
             patch.object(service_controller, "get_service_url", return_value="http://127.0.0.1:7012"), \
             patch.object(service_controller, "get_service_config", return_value={}), \
             patch.object(service_controller, "status", return_value={"running": False}), \
             patch.object(service_controller, "is_auto_start", return_value=True), \
             patch.object(service_controller, "safe_start") as mock_start, \
             patch.object(service_controller, "stop") as mock_stop, \
             patch("src.api.routes.speaker.sync_requests.get", return_value=mock_resp):
            r2 = self.client.get("/interface/tts/speakers", headers=AUTH_HEADERS)
        self.assertEqual(r2.status_code, 200)
        mock_start.assert_called_once()
        mock_stop.assert_not_called()
        scheduler.current_service_name = None

    def test_05_error_not_cached_and_temp_process_cleaned_up(self):
        """后端返回异常时不写入缓存，且临时拉起的 TTS 仍会被关闭"""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.text = "internal error"

        with patch.object(service_controller, "is_enabled", return_value=True), \
             patch.object(service_controller, "get_service_url", return_value="http://127.0.0.1:7012"), \
             patch.object(service_controller, "get_service_config", return_value={}), \
             patch.object(service_controller, "status", return_value={"running": False}), \
             patch.object(service_controller, "is_auto_start", return_value=False), \
             patch.object(service_controller, "safe_start") as mock_start, \
             patch.object(service_controller, "stop") as mock_stop, \
             patch("src.api.routes.speaker.sync_requests.get", return_value=mock_resp):
            r = self.client.get("/interface/tts/speakers", headers=AUTH_HEADERS)

        self.assertEqual(r.status_code, 500)
        self.assertIsNone(speaker_route.get_cached_speakers())
        mock_start.assert_called_once()
        mock_stop.assert_called_once_with("TTS")

    def test_06_internal_item_hidden_from_queue_views(self):
        """内部探测任务不污染用户队列快照与排位字典，且队首有真实 tts 任务时不误停服务"""
        import threading

        gate_started = threading.Event()
        gate_release = threading.Event()

        def _hold_worker():
            gate_started.set()
            gate_release.wait(timeout=5)

        blocker = {
            "_internal": True,
            "service_name": "TTS",
            "action": _hold_worker,
            "stop_if_started": False,
            "done_event": threading.Event(),
            "cancelled_event": threading.Event(),
            "result_box": {},
        }
        internal_item = {
            "_internal": True,
            "service_name": "TTS",
            "action": lambda: None,
            "done_event": threading.Event(),
            "cancelled_event": threading.Event(),
            "result_box": {},
        }
        user_item = {
            "type": "comfyui",
            "id": "task_user_1",
            "client_id": "client_1",
            "payload": {},
        }

        with patch.object(service_controller, "status", return_value={"running": True}):
            scheduler.task_queue.put(blocker)
            self.assertTrue(gate_started.wait(timeout=3))
            try:
                scheduler.task_queue.put(internal_item)
                scheduler.task_queue.put(user_item)

                status = scheduler.get_queue_status()
                positions = scheduler.get_queue_positions()
                peek = scheduler._peek_next_user_task()
                self.assertEqual(status["total_queued"], 1)
                self.assertEqual(len(status["queue_list"]), 1)
                self.assertEqual(status["queue_list"][0]["position"], 1)
                self.assertEqual(positions, {"task_user_1": 1})
                self.assertEqual(peek["id"], "task_user_1")
            finally:
                with scheduler.task_queue.mutex:
                    scheduler.task_queue.queue.clear()
                    scheduler.task_queue.unfinished_tasks = 1
                gate_release.set()
                blocker["done_event"].wait(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

