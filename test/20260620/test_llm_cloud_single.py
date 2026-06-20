# -*- coding: utf-8 -*-
"""
LLM API (云端) 单任务流式取消与连接中止测试
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
    print(f"{get_timestamp()} [CloudLLM单任务] {msg}")

async def main():
    async with httpx.AsyncClient() as client:
        try:
            payload = {
                "task_type": "llm_streaming",
                "payload": {
                    "model": "deepseek-v4-flash",
                    "prompt": "写一篇 300 字的长文讨论未来城市科技。",
                    "options": {"temperature": 0.7}
                }
            }

            log_info("1. 提交云端大模型流式任务...")
            resp = await client.post(f"{BASE_URL}/interface/tasks/submit", json=payload, headers=HEADERS)
            resp.raise_for_status()
            task_id = resp.json()["data"]["task_id"]
            log_info(f"提交成功，分配 task_id: {task_id}")

            # 建立流式连接获取 Token
            stream_url = f"{BASE_URL}/interface/tasks/{task_id}/stream"
            log_info(f"2. 连接流式端点: {stream_url}")

            # 在后台延迟 1.5 秒后发送取消
            async def auto_cancel(tid: str):
                await asyncio.sleep(1.5)
                log_info(f"[后台线程] 触发取消操作，向网关发送 cancel 请求...")
                try:
                    cancel_resp = await client.post(f"{BASE_URL}/interface/tasks/{tid}/cancel", headers=HEADERS)
                    log_info(f"[后台线程] 取消响应: {cancel_resp.json()}")
                except Exception as e:
                    log_info(f"[后台线程] 取消错误: {e}")

            cancel_task = asyncio.create_task(auto_cancel(task_id))

            log_info("3. 开启 HTTP Stream 流读取接收帧 (观察断开事件)...")
            try:
                async with client.stream("GET", stream_url, headers=HEADERS, timeout=10.0) as stream_resp:
                    async for line in stream_resp.iter_lines():
                        if line:
                            print(f"  [SSE Frame] {line}")
                log_info("流式连接已正常结束。")
            except Exception as e:
                log_info(f"流式连接被异常截断 (正常现象，代表连接已被后端 resp.close()): {e}")

            await cancel_task

            # 检查最终状态
            status_resp = await client.get(f"{BASE_URL}/interface/tasks/{task_id}/status", headers=HEADERS)
            data = status_resp.json()["data"]
            log_info(f"任务最终状态: {data['status']} | 结果: {data['result']}")
        except httpx.ConnectError:
            log_info("无法连接到网关，请先启动网关服务器 (python main.py)")

if __name__ == "__main__":
    asyncio.run(main())
