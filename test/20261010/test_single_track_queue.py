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
import json
import os
import sys
import time
import unittest
from pathlib import Path
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

    @classmethod
    def tearDownClass(cls):
        import shutil
        if Path("assets").exists():
            shutil.rmtree("assets", ignore_errors=True)

    def test_01_task_manager_client_id_isolation(self):
        """测试 1: TaskManager 对 client_id 的记录与强校验拦截（无 client_id 抛出异常中断）"""
        t1 = TaskManager.create_task("image", client_id="client_A")
        t2 = TaskManager.create_task("tts", client_id="client_B")
        t3 = TaskManager.create_task("llm", client_id="client_A")

        # 强校验：缺失或空 client_id 严厉抛出 ValueError 中断，拒绝任何弱智兜底！
        with self.assertRaises(ValueError):
            TaskManager.create_task("image", client_id=None)
        with self.assertRaises(ValueError):
            TaskManager.create_task("image", client_id="")
        with self.assertRaises(ValueError):
            TaskManager.create_task("image", client_id="   ")

        all_tasks = TaskManager.list_tasks()
        self.assertIn(t1, all_tasks)
        self.assertIn(t2, all_tasks)
        self.assertIn(t3, all_tasks)

        # 仅查询 client_A
        tasks_a = TaskManager.list_tasks(client_id="client_A")
        self.assertIn(t1, tasks_a)
        self.assertIn(t3, tasks_a)
        self.assertNotIn(t2, tasks_a)

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

        # 强校验：缺失 client_id 调用 subscribe_queue / send_to_client / emit_to_client 必须直接抛出 ValueError！
        with self.assertRaises(ValueError):
            ws_manager.subscribe_queue("ws_bad", mock_ws_a1, client_id=None)
        with self.assertRaises(ValueError):
            ws_manager.subscribe_queue("ws_bad", mock_ws_a1, client_id="")
        with self.assertRaises(ValueError):
            ws_manager.send_to_client("", {"type": "test"})
        with self.assertRaises(ValueError):
            ws_manager.emit_to_client(None, {"type": "test"})

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

        # 强校验：不带 client_id 提交任务必须直接返回 400 严厉拒绝！
        bad_resp = client.post("/interface/tasks/submit", headers=headers, json={
            "task_type": "comfyui",
            "payload": {"workflow": {}}
        })
        self.assertEqual(bad_resp.status_code, 400)
        self.assertIn("Missing required field 'client_id'", bad_resp.json()["message"])

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

    def test_07_jit_asset_upload_flow(self):
        """测试 7: JIT 素材延迟上传接口与文件回填"""
        from src.logic.yaml_config_loader import yaml_config_loader
        import io
        valid_tokens = yaml_config_loader.get("api_server.tokens", [])
        token = valid_tokens[0] if valid_tokens else ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        client = TestClient(app)

        # 1. 提交轻量任务（不带大文件），标记 asset_status="not_ready"
        resp = client.post("/interface/tasks/submit", headers=headers, json={
            "task_type": "comfyui",
            "client_id": "client_jit_test",
            "asset_status": "not_ready",
            "required_assets": ["image"],
            "payload": {
                "prompt": "a cute cat"
            }
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        task_id = data["task_id"]
        self.assertEqual(data["asset_status"], "not_ready")

        task_info = TaskManager.get_task(task_id)
        self.assertEqual(task_info["asset_status"], "not_ready")

        # 2. 模拟轮到任务后，客户端调用 POST /tasks/{task_id}/assets 补交素材
        fake_image = io.BytesIO(b"fake image bytes")
        upload_resp = client.post(
            f"/interface/tasks/{task_id}/assets",
            headers=headers,
            files={"image": ("cat.png", fake_image, "image/png")}
        )
        self.assertEqual(upload_resp.status_code, 200)
        up_data = upload_resp.json()["data"]
        self.assertEqual(up_data["asset_status"], "ready")
        self.assertIn("image", up_data["files"])

        # 3. 验证 TaskManager 中状态和 payload 均已动态补齐
        updated_task = TaskManager.get_task(task_id)
        self.assertEqual(updated_task["asset_status"], "ready")
        payload = TaskManager.get_task_payload(task_id)
        self.assertIn("image_path", payload)
        self.assertTrue(os.path.exists(payload["image_path"]))

    def test_08_purge_client_tasks_and_queue_removal(self):
        """测试 8: 连坐清理机制 (purge_client_tasks)"""
        # 创建属于 client_purge 的 3 个任务
        t1 = TaskManager.create_task("tts", client_id="client_purge")
        t2 = TaskManager.create_task("image", client_id="client_purge")
        t3 = TaskManager.create_task("comfyui", client_id="client_purge")
        TaskManager.update_task(t1, TaskManager.STATUS_QUEUED)
        TaskManager.update_task(t2, TaskManager.STATUS_AWAITING_ASSETS)
        TaskManager.update_task(t3, TaskManager.STATUS_QUEUED)

        # 模拟入队
        scheduler.task_queue.put({"type": "tts", "id": t1, "client_id": "client_purge", "payload": {}})
        scheduler.task_queue.put({"type": "image", "id": t2, "client_id": "client_purge", "payload": {}})
        scheduler.task_queue.put({"type": "comfyui", "id": t3, "client_id": "client_purge", "payload": {}})

        # 执行连坐清理
        purged = scheduler.purge_client_tasks("client_purge", reason="测试主动清理")
        self.assertIn(t1, purged)
        self.assertIn(t2, purged)
        self.assertIn(t3, purged)

        # 验证任务状态全为 failed
        for tid in (t1, t2, t3):
            t = TaskManager.get_task(tid)
            self.assertEqual(t["status"], TaskManager.STATUS_FAILED)
            self.assertEqual(t["result"]["message"], "测试主动清理")

        # 验证队列中该 client 的所有任务已被剔除
        with scheduler.task_queue.mutex:
            for item in scheduler.task_queue.queue:
                if item is not None:
                    self.assertNotEqual(item.get("client_id"), "client_purge")

    def test_09_offline_duration_detection(self):
        """测试 9: 客户端离线计时器与状态判定"""
        cid = "client_offline_watcher"
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()

        # 未连接过
        self.assertFalse(ws_manager.is_client_connected(cid))
        self.assertIsNone(ws_manager.get_client_offline_duration(cid))

        # 连接上线
        ws_manager.subscribe_queue("ws_off_1", mock_ws, client_id=cid)
        self.assertTrue(ws_manager.is_client_connected(cid))
        self.assertEqual(ws_manager.get_client_offline_duration(cid), 0.0)

        # 断开下线
        ws_manager.unsubscribe_queue("ws_off_1")
        self.assertFalse(ws_manager.is_client_connected(cid))
        dur = ws_manager.get_client_offline_duration(cid)
        self.assertIsNotNone(dur)
        self.assertGreaterEqual(dur, 0.0)

    def test_10_single_channel_progress_throttling(self):
        """测试 10: 单任务旧频道 (ws_manager.send) 的高频 progress/overall_progress 防洪节流"""
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()
        tid = "task_flood_test"
        ws_manager.register(tid, mock_ws)

        # 连续快速发送 10 条 progress/overall_progress
        for i in range(10):
            ws_manager.send(tid, {"type": "progress", "data": {"value": i, "max": 100}})
            ws_manager.send(tid, {"type": "overall_progress", "data": {"percent": i}})

        self.loop.run_until_complete(asyncio.sleep(0.01))
        # 300ms 内，progress 和 overall_progress 各自只能放行第 1 次，其余 18 条全部被平滑拦截！
        self.assertEqual(mock_ws.send_json.call_count, 2)

        # 当终态 100% 到达时，立即强制放行
        ws_manager.send(tid, {"type": "overall_progress", "data": {"percent": 100}})
        self.loop.run_until_complete(asyncio.sleep(0.01))
        self.assertEqual(mock_ws.send_json.call_count, 3)

        ws_manager.clear_task_progress(tid)
        ws_manager.unregister(tid)

    def test_11_pure_zero_byte_asset_reuse_json(self):
        """测试 11: 建议2场景B - 纯 JSON 免传开闸 (0 字节传输秒传开闸)"""
        from src.logic.yaml_config_loader import yaml_config_loader
        valid_tokens = yaml_config_loader.get("api_server.tokens", [])
        token = valid_tokens[0] if valid_tokens else ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        # 1. 模拟远端 assets/ 目录已存在的公共角色物料
        assets_dir = Path("assets")
        assets_dir.mkdir(exist_ok=True)
        sample_hero = assets_dir / "lin_zhi_wei.jpeg"
        sample_hero.write_bytes(b"MOCK_HERO_IMAGE_BYTES")

        client = TestClient(app)

        # 2. 提交任务，初始 asset_status="not_ready"
        resp = client.post("/interface/tasks/submit", headers=headers, json={
            "task_type": "comfyui",
            "client_id": "client_drama_producer",
            "asset_status": "not_ready",
            "required_assets": ["lin_zhi_wei.jpeg"],
            "payload": {
                "shot_index": "Shot_01"
            }
        })
        self.assertEqual(resp.status_code, 200)
        task_id = resp.json()["data"]["task_id"]

        # 3. 客户端直接发起纯 JSON 免传开闸声明 (无需 multipart，0 字节大文件网络传输)
        reuse_resp = client.post(
            f"/interface/tasks/{task_id}/assets",
            headers=headers,
            json={
                "reuse_assets": ["lin_zhi_wei.jpeg"]
            }
        )
        self.assertEqual(reuse_resp.status_code, 200)
        data = reuse_resp.json()["data"]
        self.assertEqual(data["asset_status"], "ready")
        self.assertIn("lin_zhi_wei.jpeg", data["reused_files"])

        # 4. 验证任务状态已被置为 ready，并且 payload 中已注入文件路径
        updated = TaskManager.get_task(task_id)
        self.assertEqual(updated["asset_status"], "ready")
        payload = TaskManager.get_task_payload(task_id)
        self.assertIn("lin_zhi_wei_path", payload)
        self.assertTrue(os.path.exists(payload["lin_zhi_wei_path"]))

    def test_12_mixed_upload_and_reuse_assets(self):
        """测试 12: 建议2场景A - 混合表单模式 (上传 1 个新分镜文件 + 复用 1 个已有物料)"""
        from src.logic.yaml_config_loader import yaml_config_loader
        import io
        valid_tokens = yaml_config_loader.get("api_server.tokens", [])
        token = valid_tokens[0] if valid_tokens else ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        # 确保公共物料存在
        assets_dir = Path("assets")
        assets_dir.mkdir(exist_ok=True)
        sample_voice = assets_dir / "lin_voice.flac"
        sample_voice.write_bytes(b"MOCK_HERO_AUDIO_BYTES")

        client = TestClient(app)

        # 1. 提交分镜任务
        resp = client.post("/interface/tasks/submit", headers=headers, json={
            "task_type": "comfyui",
            "client_id": "client_drama_producer",
            "asset_status": "not_ready",
            "payload": {
                "shot_index": "Shot_02"
            }
        })
        self.assertEqual(resp.status_code, 200)
        task_id = resp.json()["data"]["task_id"]

        # 2. 混合表单上传：上传当前镜头特有的草图，并声明复用已有的音色
        fake_sketch = io.BytesIO(b"MOCK_NEW_SKETCH_BYTES")
        mixed_resp = client.post(
            f"/interface/tasks/{task_id}/assets",
            headers=headers,
            files={"shot_sketch": ("sketch_02.png", fake_sketch, "image/png")},
            data={"reuse_assets": json.dumps({"voice_ref": "lin_voice.flac"})}
        )
        self.assertEqual(mixed_resp.status_code, 200)
        data = mixed_resp.json()["data"]
        self.assertEqual(data["asset_status"], "ready")
        self.assertIn("shot_sketch", data["uploaded_files"])
        self.assertIn("voice_ref", data["reused_files"])

        # 3. 验证任务就绪且所有物料均映射成功
        updated = TaskManager.get_task(task_id)
        self.assertEqual(updated["asset_status"], "ready")
        payload = TaskManager.get_task_payload(task_id)
        self.assertIn("shot_sketch_path", payload)
        self.assertIn("voice_ref_path", payload)
        self.assertTrue(os.path.exists(payload["shot_sketch_path"]))
        self.assertTrue(os.path.exists(payload["voice_ref_path"]))

    def test_13_queue_positions_updated_lifecycle(self):
        """测试 13: 验证轻量顺位流转广播 queue_positions_updated 全生命周期流转 (动作A, B, C)"""
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()
        ws_manager.subscribe_queue("ws_flow_test", mock_ws, client_id="client_shot_flow")

        try:
            # 清理调度器旧队列以准备干净测试环境
            with scheduler.task_queue.mutex:
                scheduler.task_queue.queue.clear()
                scheduler.task_queue.unfinished_tasks = 0

            # 模拟入队 4 个分镜任务
            t1 = TaskManager.create_task("comfyui", client_id="client_shot_flow")
            t2 = TaskManager.create_task("comfyui", client_id="client_shot_flow")
            t3 = TaskManager.create_task("comfyui", client_id="client_shot_flow")
            t4 = TaskManager.create_task("comfyui", client_id="client_shot_flow")
            for tid in (t1, t2, t3, t4):
                scheduler.task_queue.put({"id": tid, "type": "comfyui", "client_id": "client_shot_flow"})

            scheduler.broadcast_queue_positions()
            self.loop.run_until_complete(asyncio.sleep(0.01))

            # 初始状态：4 个任务全在队列中
            pos_initial = scheduler.get_queue_positions(client_id="client_shot_flow")
            self.assertEqual(pos_initial, {t1: 1, t2: 2, t3: 3, t4: 4})

            # 🌟 动作 A：任务 1 出队并开始执行 (task_started)
            popped_t1 = scheduler.task_queue.get()
            self.assertEqual(popped_t1["id"], t1)
            scheduler.broadcast_queue_positions()
            self.loop.run_until_complete(asyncio.sleep(0.01))

            # 验证剩余队列顺位自动向前递减：t2->1, t3->2, t4->3
            pos_after_a = scheduler.get_queue_positions(client_id="client_shot_flow")
            self.assertEqual(pos_after_a, {t2: 1, t3: 2, t4: 3})

            # 🌟 动作 B：任务 1 执行完毕，任务 2 出队开始执行
            popped_t2 = scheduler.task_queue.get()
            self.assertEqual(popped_t2["id"], t2)
            scheduler.broadcast_queue_positions()
            self.loop.run_until_complete(asyncio.sleep(0.01))

            # 验证此时队列仅剩 t3 和 t4：t3->1, t4->2
            pos_after_b = scheduler.get_queue_positions(client_id="client_shot_flow")
            self.assertEqual(pos_after_b, {t3: 1, t4: 2})

            # 🌟 动作 C：用户手动取消队列中的任务 3 (task_cancelled)
            removed = scheduler.remove_from_queue(t3)
            self.assertTrue(removed)
            self.loop.run_until_complete(asyncio.sleep(0.01))

            # 验证取消出队后，t4 顺位从 2 瞬时变为 1
            pos_after_c = scheduler.get_queue_positions(client_id="client_shot_flow")
            self.assertEqual(pos_after_c, {t4: 1})

            # 任务 4 最终也出队执行
            popped_t4 = scheduler.task_queue.get()
            self.assertEqual(popped_t4["id"], t4)
            scheduler.broadcast_queue_positions()
            self.loop.run_until_complete(asyncio.sleep(0.01))

            # 队列排队任务全部清空，返回空字典 {}
            pos_empty = scheduler.get_queue_positions(client_id="client_shot_flow")
            self.assertEqual(pos_empty, {})

            # 检查 mock_ws 收到的一系列 queue_positions_updated 数据包
            sent_msgs = [call.args[0] for call in mock_ws.send_json.call_args_list if call.args[0].get("type") == "queue_positions_updated"]
            self.assertTrue(len(sent_msgs) >= 4)
            self.assertEqual(sent_msgs[-1]["data"], {})
            self.assertEqual(sent_msgs[-2]["data"], {t4: 1})

        finally:
            ws_manager.unsubscribe_queue("ws_flow_test")

    def test_14_cancel_task_api_removes_from_queue_and_emits_cancelled(self):
        """测试 14: 验证取消接口 POST /tasks/{id}/cancel 能够主动移出队列并广播 task_cancelled"""
        from src.logic.yaml_config_loader import yaml_config_loader
        valid_tokens = yaml_config_loader.get("api_server.tokens", [])
        token = valid_tokens[0] if valid_tokens else ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        # 1. 订阅队列广播
        client = TestClient(app)
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()
        ws_manager.subscribe_queue("ws_cancel_test", mock_ws, client_id="client_cancel_test")

        try:
            # 提交排队任务 (声明素材未就绪以避免调度器在测试中断言前瞬间出队)
            resp = client.post("/interface/tasks/submit", headers=headers, json={
                "task_type": "comfyui",
                "client_id": "client_cancel_test",
                "asset_status": "not_ready",
                "required_assets": ["sketch.png"],
                "payload": {"prompt": "test cancel queued"}
            })
            self.assertEqual(resp.status_code, 200)
            task_id = resp.json()["data"]["task_id"]

            # 2. 调用取消接口
            cancel_resp = client.post(f"/interface/tasks/{task_id}/cancel", headers=headers)
            self.assertEqual(cancel_resp.status_code, 200)

            # 3. 验证任务状态置为 FAILED
            task_info = TaskManager.get_task(task_id)
            self.assertEqual(task_info["status"], TaskManager.STATUS_FAILED)

            # 4. 验证任务已不在调度器队列中
            positions = scheduler.get_queue_positions("client_cancel_test")
            self.assertNotIn(task_id, positions)

            # 5. 验证 WS 广播捕获到了 task_cancelled 和 queue_positions_updated
            self.loop.run_until_complete(asyncio.sleep(0.01))
            msg_types = [call.args[0].get("type") for call in mock_ws.send_json.call_args_list]
            self.assertIn("task_cancelled", msg_types)
            self.assertIn("queue_positions_updated", msg_types)
        finally:
            ws_manager.unsubscribe_queue("ws_cancel_test")


if __name__ == "__main__":
    unittest.main()




