# -*- coding: utf-8 -*-
"""
KamlPro 全方法透明代理、试听鉴权与 TTS free_api 调度验证测试
==============================================================
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient

from src.api.main import app
from src.core.executors import subtitle as subtitle_executor
from src.core.executors import tts as tts_executor
from src.core.scheduler import scheduler
from src.core.service_controller import service_controller
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager

AUTH_HEADERS = {"Authorization": "Bearer 123456"}


class _MockBackendHandler(BaseHTTPRequestHandler):
    captured_requests = []

    def log_message(self, format, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(length) if length > 0 else b""
        self.captured_requests.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": raw_body.decode("utf-8", errors="replace"),
        })

        # 模拟 FastAPI 尾部斜杠 307 重定向 (/speaker -> /speaker/)
        if self.path == "/speaker":
            host = self.headers.get("Host", "127.0.0.1")
            self.send_response(307)
            self.send_header("Location", f"http://{host}/speaker/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # 模拟静态音频分段 Range 试听
        if self.path.startswith("/static/voices/demo.wav"):
            payload = b"0123456789ABCDEF"
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start_s, end_s = rng.replace("bytes=", "").split("-", 1)
                start = int(start_s)
                end = int(end_s) if end_s else len(payload) - 1
                chunk = payload[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(chunk)
                return

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return

        resp_bytes = json.dumps({
            "status": "success",
            "method": self.command,
            "path": self.path,
            "body": raw_body.decode("utf-8", errors="replace"),
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp_bytes)

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_PATCH(self):
        self._handle()


class TestProxyAndTTSIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockBackendHandler)
        cls.port = cls.server.server_address[1]
        cls.backend_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _MockBackendHandler.captured_requests.clear()
        self.client = TestClient(app)

    def test_01_full_methods_transparent_proxy_and_header_cleaning(self):
        """/proxy/tts/... 透明支持 GET/POST/PUT/DELETE/PATCH，清洗浏览器头并注入后端 Token"""
        with patch.object(service_controller, "is_enabled", return_value=True), \
             patch.object(service_controller, "get_service_url", return_value=self.backend_url), \
             patch.object(service_controller, "get_service_config", return_value={"token": "backend_secret"}):

            # GET（含 query 参数透传与浏览器头清洗）
            r_get = self.client.get(
                "/proxy/tts/speaker/?foo=bar",
                headers={
                    **AUTH_HEADERS,
                    "Origin": "http://localhost:5173",
                    "Referer": "http://localhost:5173/",
                },
            )
            self.assertEqual(r_get.status_code, 200)
            self.assertEqual(r_get.json()["path"], "/speaker/?foo=bar")

            # PUT（如 /speaker/rename）
            r_put = self.client.put(
                "/proxy/tts/speaker/rename",
                headers=AUTH_HEADERS,
                json={"old_path": "a.wav", "new_path": "b.wav"},
            )
            self.assertEqual(r_put.status_code, 200)
            self.assertEqual(r_put.json()["method"], "PUT")

            # DELETE 带 JSON Body（如 DELETE /speaker/）
            r_del = self.client.request(
                "DELETE",
                "/proxy/tts/speaker/",
                headers=AUTH_HEADERS,
                json={"path": "voices/ref_audios/a.wav"},
            )
            self.assertEqual(r_del.status_code, 200)
            self.assertEqual(r_del.json()["method"], "DELETE")
            self.assertIn("voices/ref_audios/a.wav", r_del.json()["body"])

            # PATCH
            r_patch = self.client.patch("/proxy/tts/emo/item", headers=AUTH_HEADERS, json={"k": 1})
            self.assertEqual(r_patch.status_code, 200)
            self.assertEqual(r_patch.json()["method"], "PATCH")

            # 校验首条请求头清洗与后端 Token 注入
            req0 = _MockBackendHandler.captured_requests[0]
            self.assertEqual(req0["headers"].get("authorization"), "Bearer backend_secret")
            self.assertNotIn("origin", req0["headers"])
            self.assertNotIn("referer", req0["headers"])

    def test_02_audition_query_token_and_range_streaming(self):
        """GET/HEAD 兼容 ?token= 试听鉴权（转发前剥离 token），并支持 Range 206 流式播放；写操作禁用 ?token="""
        with patch.object(service_controller, "is_enabled", return_value=True), \
             patch.object(service_controller, "get_service_url", return_value=self.backend_url), \
             patch.object(service_controller, "get_service_config", return_value={"token": "123456"}):

            # 1. GET 带 ?token=123456 且带 Range 头
            r_range = self.client.get(
                "/proxy/tts/static/voices/demo.wav?v=2&token=123456",
                headers={"Range": "bytes=2-5"},
            )
            self.assertEqual(r_range.status_code, 206)
            self.assertEqual(r_range.content, b"2345")
            self.assertEqual(r_range.headers.get("content-range"), "bytes 2-5/16")
            self.assertEqual(_MockBackendHandler.captured_requests[-1]["path"], "/static/voices/demo.wav?v=2")

            # 2. 错误 ?token= 拒绝
            r_bad = self.client.get("/proxy/tts/static/voices/demo.wav?token=wrong")
            self.assertEqual(r_bad.status_code, 401)

            # 3. 写操作不允许只传 ?token=
            r_post_query = self.client.post("/proxy/tts/speaker/category?token=123456", json={"name": "test"})
            self.assertEqual(r_post_query.status_code, 403)

            # 4. 旧 /file/tts/... 向后兼容
            r_legacy = self.client.get("/file/tts/static/voices/demo.wav", headers=AUTH_HEADERS)
            self.assertEqual(r_legacy.status_code, 200)
            self.assertEqual(r_legacy.content, b"0123456789ABCDEF")

    def test_03_scheduler_free_service_resources_sends_bearer_token(self):
        """调度器调用 TTS free_api 时正确携带 services.yaml 配置的 Bearer Token"""
        with patch.object(
            service_controller,
            "get_service_config",
            return_value={"token": "123456", "free_api": {"path": "/free", "body": {"unload_models": True}}},
        ), patch.object(service_controller, "get_service_url", return_value=self.backend_url):
            scheduler._free_service_resources("TTS")

        self.assertEqual(len(_MockBackendHandler.captured_requests), 1)
        req = _MockBackendHandler.captured_requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/free")
        self.assertEqual(req["headers"].get("authorization"), "Bearer 123456")

    def test_04_tts_and_subtitle_executors_forward_progress_to_single_track(self):
        """TTS 与 Subtitle 执行器均能把进度透传到单轨 task_progress"""
        fake_provider = MagicMock()

        def _fake_tts(payload, cb):
            cb({"status": "processing", "message": "正在合成文本分段 (1/2)", "progress": 45, "data": {"progress": 45}})
            return {"status": "completed", "data": {"task_id": "svc-1", "file_url": "/files/svc-1"}}

        def _fake_sub(payload, cb):
            cb({"status": "processing", "message": "正在对齐字幕", "data": {"progress": 60}})
            return {"status": "completed", "data": {"task_id": "svc-2", "subtitle_url": "/download/subtitle/svc-2"}}

        fake_provider.generate_speech.side_effect = _fake_tts
        fake_provider.align_subtitle.side_effect = _fake_sub

        tid1 = TaskManager.create_task("tts", client_id="c_test")
        tid2 = TaskManager.create_task("subtitle", client_id="c_test")

        with patch("src.core.executors.tts.audio_provider_manager.get_provider", return_value=fake_provider), \
             patch("src.core.executors.subtitle.audio_provider_manager.get_provider", return_value=fake_provider), \
             patch.object(ws_manager, "emit_progress_throttled") as mock_emit, \
             patch.object(ws_manager, "send"):
            tts_executor.execute(tid1, client_id="c_test", text="hello", speaker="s1")
            subtitle_executor.execute(tid2, client_id="c_test", text="hello", audio_file_path="a.wav")

        self.assertEqual(mock_emit.call_count, 2)
        self.assertEqual(mock_emit.call_args_list[0].kwargs["message"]["progress"], 45)
        self.assertEqual(mock_emit.call_args_list[1].kwargs["message"]["progress"], 60)

    def test_05_openapi_schema_clean_and_307_redirect(self):
        """OpenAPI 文档不发生 operationId 冲突，隐藏 HEAD/PATCH/空路径，并自动跟随后端 307 斜杠重定向"""
        openapi = self.client.get("/openapi.json").json()
        paths = openapi.get("paths", {})
        self.assertNotIn("/proxy/{service_name}", paths)
        self.assertIn("/proxy/{service_name}/{path}", paths)

        proxy_ops = paths["/proxy/{service_name}/{path}"]
        self.assertEqual(set(proxy_ops.keys()), {"get", "post", "put", "delete"})

        operation_ids = [op["operationId"] for op in proxy_ops.values()]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))

        for method_name, op in proxy_ops.items():
            param_map = {p["name"]: p["in"] for p in op.get("parameters", [])}
            self.assertEqual(param_map.get("service_name"), "path", f"{method_name} service_name should be in path")
            self.assertEqual(param_map.get("path"), "path", f"{method_name} path should be in path")

        # 运行时仍兼容尾部斜杠 307 重定向与 HEAD 探测
        with patch.object(service_controller, "is_enabled", return_value=True), \
             patch.object(service_controller, "get_service_url", return_value=self.backend_url), \
             patch.object(service_controller, "get_service_config", return_value={"token": "backend_secret"}):
            r_no_slash = self.client.get("/proxy/tts/speaker", headers=AUTH_HEADERS)
            self.assertEqual(r_no_slash.status_code, 200)
            self.assertEqual(r_no_slash.json()["path"], "/speaker/")
            self.assertEqual([r["path"] for r in _MockBackendHandler.captured_requests], ["/speaker", "/speaker/"])

            r_head = self.client.head("/proxy/tts/speaker/", headers=AUTH_HEADERS)
            self.assertEqual(r_head.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)

