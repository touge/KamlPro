# -*- coding: utf-8 -*-
"""
LLM API (云端) 多任务取消测试
"""
import sys
import os
import asyncio
import httpx
import datetime
from pathlib import Path

sys.path.insert(0, os.path.abspath(Path(__file__).parent / ".." / ".."))

BASE_URL = "http://127.0.0.1:7000"
HEADERS = {"Authorization": "Bearer 123456"}

def get_timestamp() -> str:
    return datetime.datetime.now().strftime("[%H:%M:%S.%f]")[:-3]

def log_info(msg: str):
    print(f"{get_timestamp()} [CloudLLM多任务] {msg}")

async def main():
    async with httpx.AsyncClient() as client:
        try:
            task_ids = []
            # 提交 3 个 LLM 任务 (利用 API 并发轨道，均被分配为 RUNNING 运行或 QUEUED 排队)
            for i in range(1, 4):
                payload = {
                    "task_type": "llm",
                    "payload": {
                        "model": "deepseek-v4-flash",
                        "prompt": f"简短回答问题 {i}，什么是大数据？",
                        "options": {}
                    }
                }
                resp = await client.post(f"{BASE_URL}/interface/tasks/submit", json=payload, headers=HEADERS)
                task_id = resp.json()["data"]["task_id"]
                task_ids.append(task_id)
                log_info(f"已提交云端大模型任务 {i}，分配 task_id: {task_id}")

            log_info("立即发起针对任务 1 和任务 3 的取消请求...")
            cancel_1 = await client.post(f"{BASE_URL}/interface/tasks/{task_ids[0]}/cancel", headers=HEADERS)
            cancel_3 = await client.post(f"{BASE_URL}/interface/tasks/{task_ids[2]}/cancel", headers=HEADERS)
            log_info(f"任务 1 取消响应: {cancel_1.json()}")
            log_info(f"任务 3 取消响应: {cancel_3.json()}")

            log_info("等待 3.0 秒让未取消的任务 2 运行完毕...")
            await asyncio.sleep(3.0)

            # 打印最终状态
            for i, tid in enumerate(task_ids, start=1):
                status_resp = await client.get(f"{BASE_URL}/interface/tasks/{tid}/status", headers=HEADERS)
                data = status_resp.json()["data"]
                log_info(f"任务 {i} ({tid}) 状态: {data['status']} | 结果: {data['result']}")
        except httpx.ConnectError:
            log_info("无法连接到网关，请先启动网关服务器 (python main.py)")

if __name__ == "__main__":
    asyncio.run(main())
