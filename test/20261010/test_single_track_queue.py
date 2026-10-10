# -*- coding: utf-8 -*-
"""
单轨机制与客户端定向隔离自动化验证测试
====================================
验证：
1. TaskManager client_id 支持与任务列表过滤
2. WebSocketManager 按 client_id 物理隔离与定向推送 (send_to_client vs broadcast_queue)
3. WebSocketManager 进度节流器 (emit_progress_throttled) 窗口平滑性
4. 调度器排队顺位与大盘快照 (get_queue_status)
5. 统一提交接口 POST /tasks/submit 携带 client_id 与响应
6. 队列大盘接口 GET /interface/queue/status 正确性
7. WebSocket /interface/queue/ws 首帧 queue_snapshot 快照下发
"""

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, AsyncMock

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient

from src.api.main import app
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.core.scheduler import scheduler


class TestSingleTrackQueue(unittest.TestCase):
    def setUp(self):
        # 确保后台事件循环已给 ws_manager 准备好模拟环境
        self.loop = asyncio.new_event_loop()
        ws_manager.loop = self.loop

    def tearDown(self):
        self.loop.close()

    def test_01_task_manager_client_id_isolation(self):
        """测试 1: TaskManager 对 client_id 的记录与多租户过滤"""
        t1 = TaskManager.create_task("image", client_id="client_A")
        t2 = TaskManager.create_task("tts", client_id="client_B")
        t3 = TaskManager.create_task("llm", client_id="client_A")
        t_anon = TaskManager.create_task("image", client_id=None)

        all_tasks = TaskManager.list_tasks()
        self.assertIn(t1, all_tasks)
        self.assertIn(t2, all_tasks)
        self.assertIn(t3, all_tasks)
        self.assertIn(t_anon, all_tasks)

        # 仅查询 client_A
        tasks_a = TaskManager.list_tasks(client_id="client_A")
        self.assertIn(t1, tasks_a)
        self.assertIn(t3, tasks_a)
        self.assertNotIn(t2, tasks_a)
        self.assertNotIn(t_anon, tasks_a)

        # 仅查询 client_B
        tasks_b = TaskManager.list_tasks(client_id="client_B")
        self.assertIn(t2, tasks_b)
        self.assertNotIn(t1, tasks_b)
        self.assertNotIn(t3, tasks_b)

    def test_02_ws_manager_client_grouping_and_send(self):
        """测试 2: WebSocketManager 按 client_id 注册、注销与定向投递"""
        mock_ws_a1 = MagicMock()
        mock_ws_a1.send_json = AsyncMock()
        mock_ws_a2 = MagicMock()
        mock_ws_a2.send_json = AsyncMock()
        mock_ws_b = MagicMock()
        mock_ws_b.send_json = AsyncMock()

        ws_manager.subscribe_queue("ws_1", mock_ws_a1, client_id="user_alpha")
        ws_manager.subscribe_queue("ws_2", mock_ws_a2, client_id="user_alpha")
        ws_manager.subscribe_queue("ws_3", mock_ws_b, client_id="user_beta")

        # 验证注册结构
        self.assertEqual(len(ws_manager._client_subscribers["user_alpha"]), 2)
        self.assertEqual(len(ws_manager._client_subscribers["user_beta"]), 1)

        # 测试定向向 user_alpha 推送
        ws_manager.send_to_client("user_alpha", {"msg": "hello_alpha"})

        # 在模拟循环中跑一步
        self.loop.run_until_complete(asyncio.sleep(0.01))

        mock_ws_a1.send_json.assert_called_once_with({"msg": "hello_alpha"})
        mock_ws_a2.send_json.assert_called_once_with({"msg": "hello_alpha"})
        mock_ws_b.send_json.assert_not_called()

        # 注销 ws_1
        ws_manager.unsubscribe_queue("ws_1")
        self.assertEqual(len(ws_manager._client_subscribers["user_alpha"]), 1)
        ws_manager.unsubscribe_queue("ws_2")
        self.assertNotIn("user_alpha", ws_manager._client_subscribers)
        ws_manager.unsubscribe_queue("ws_3")
        self.assertNotIn("user_beta", ws_manager._client_subscribers)

    def test_03_ws_manager_progress_throttling(self):
        """测试 3: 进度节流器 (高频调用时按窗口合并下发)"""
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()
        ws_manager.subscribe_queue("ws_throttled", mock_ws, client_id="user_throttle")

        task_id = "test_task_throttle"

        # 连续发送 5 次微小步数
        ws_manager.emit_progress_throttled(task_id, "user_throttle", {"type": "task_progress", "progress": 10}, interval=0.2)
        ws_manager.emit_progress_throttled(task_id, "user_throttle", {"type": "task_progress", "progress": 11}, interval=0.2)
        ws_manager.emit_progress_throttled(task_id, "user_throttle", {"type": "task_progress", "progress": 12}, interval=0.2)
        ws_manager.emit_progress_throttled(task_id, "user_throttle", {"type": "task_progress", "progress": 13}, interval=0.2)

        self.loop.run_until_complete(asyncio.sleep(0.01))
        # 0.2 秒内仅能发射第 1 次
        self.assertEqual(mock_ws.send_json.call_count, 1)

        # 当进度达到 100% 终态时立即强制放行
        ws_manager.emit_progress_throttled(task_id, "user_throttle", {"type": "task_progress", "progress": 100}, interval=0.2)
        self.loop.run_until_complete(asyncio.sleep(0.01))
        self.assertEqual(mock_ws.send_json.call_count, 2)

        ws_manager.clear_task_progress(task_id)
        ws_manager.unsubscribe_queue("ws_throttled")

    def test_04_scheduler_queue_status_api(self):
        """测试 4: 调度器状态快照 get_queue_status"""
        status = scheduler.get_queue_status()
        self.assertIn("total_queued", status)
        self.assertIn("is_busy", status)
        self.assertIn("queue_list", status)

    def test_05_http_endpoints_submit_and_queue_status(self):
        """测试 5: HTTP 端点 POST /tasks/submit 与 GET /interface/queue/status"""
        from src.logic.yaml_config_loader import yaml_config_loader
        valid_tokens = yaml_config_loader.get("api_server.tokens", [])
        token = valid_tokens[0] if valid_tokens else ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        client = TestClient(app)

        # 提交带 client_id 的测试任务
        resp = client.post("/interface/tasks/submit", headers=headers, json={
            "task_type": "comfyui",
            "client_id": "client_test_suite",
            "payload": {
                "workflow": {},
                "final_output_node_id": "9"
            }
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["client_id"], "client_test_suite")
        task_id = data["task_id"]

        # 查询队列大盘端点
        q_resp = client.get("/interface/queue/status?client_id=client_test_suite", headers=headers)
        self.assertEqual(q_resp.status_code, 200)
        q_data = q_resp.json()["data"]
        self.assertIn("total_queued", q_data)
        self.assertIn("queue_list", q_data)

        # 查询专属任务列表端点
        t_resp = client.get("/interface/tasks?client_id=client_test_suite", headers=headers)
        self.assertEqual(t_resp.status_code, 200)
        t_data = t_resp.json()["data"]["tasks"]
        self.assertIn(task_id, t_data)
        self.assertEqual(t_data[task_id]["client_id"], "client_test_suite")

    def test_06_ws_queue_snapshot_handshake(self):
        """测试 6: WebSocket 连接即送 queue_snapshot 初始帧"""
        from src.logic.yaml_config_loader import yaml_config_loader
        valid_tokens = yaml_config_loader.get("api_server.tokens", [])
        token = valid_tokens[0] if valid_tokens else ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        client = TestClient(app)
        with client.websocket_connect("/interface/queue/ws?client_id=tester_ws_snap", headers=headers) as ws:
            first_frame = ws.receive_json()
            self.assertEqual(first_frame["type"], "queue_snapshot")
            self.assertIn("data", first_frame)
            self.assertEqual(first_frame["data"]["client_id"], "tester_ws_snap")


if __name__ == "__main__":
    unittest.main()
