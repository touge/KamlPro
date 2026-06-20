# -*- coding: utf-8 -*-
"""
LLM 本地 (Ollama) 单任务取消测试
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
    print(f"{get_timestamp()} [Ollama单任务] {msg}")

async def main():
    async with httpx.AsyncClient() as client:
        try:
            payload = {
                "task_type": "llm",
                "payload": {
                    "model": "gemma3:12b",
                    "prompt": "写一个 200 字的故事。",
                    "options": {}
                }
            }

            log_info("1. 提交本地 Ollama LLM 任务...")
            resp = await client.post(f"{BASE_URL}/interface/tasks/submit", json=payload, headers=HEADERS)
            resp.raise_for_status()
            task_id = resp.json()["data"]["task_id"]
            log_info(f"提交成功，分配 task_id: {task_id}")

            # 延迟 1 秒后尝试取消
            log_info("等待 1.0 秒模拟中途取消...")
            await asyncio.sleep(1.0)

            log_info(f"2. 发送取消请求: {task_id}...")
            cancel_resp = await client.post(f"{BASE_URL}/interface/tasks/{task_id}/cancel", headers=HEADERS)
            cancel_resp.raise_for_status()
            log_info(f"取消响应: {cancel_resp.json()}")

            # 验证状态
            status_resp = await client.get(f"{BASE_URL}/interface/tasks/{task_id}/status", headers=HEADERS)
            data = status_resp.json()["data"]
            log_info(f"任务最终状态: {data['status']} | 结果: {data['result']}")
        except httpx.ConnectError:
            log_info("无法连接到网关，请先启动网关服务器 (python main.py)")

if __name__ == "__main__":
    asyncio.run(main())
