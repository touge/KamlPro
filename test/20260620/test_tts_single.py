# -*- coding: utf-8 -*-
"""
TTS 单任务取消测试
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
    print(f"{get_timestamp()} [TTS单任务] {msg}")

async def main():
    async with httpx.AsyncClient() as client:
        try:
            payload = {
                "task_type": "tts",
                "payload": {
                    "text": "语音配音任务单个取消测试。",
                    "speaker": "解说-魔魔",
                    "generate_subtitle": True
                }
            }

            log_info("1. 提交单个 TTS 语音合成任务...")
            resp = await client.post(f"{BASE_URL}/interface/tasks/submit", json=payload, headers=HEADERS)
            resp.raise_for_status()
            task_id = resp.json()["data"]["task_id"]
            log_info(f"提交成功，分配 task_id: {task_id}")

            log_info("等待 0.5 秒模拟状态查询中途被取消...")
            await asyncio.sleep(0.5)

            log_info(f"2. 发送取消请求: {task_id}...")
            cancel_resp = await client.post(f"{BASE_URL}/interface/tasks/{task_id}/cancel", headers=HEADERS)
            cancel_resp.raise_for_status()
            log_info(f"取消响应: {cancel_resp.json()}")

            # 验证最终状态
            status_resp = await client.get(f"{BASE_URL}/interface/tasks/{task_id}/status", headers=HEADERS)
            data = status_resp.json()["data"]
            log_info(f"任务最终状态: {data['status']} | 结果: {data['result']}")
            if data['status'] == "success":
                outputs_dir = Path(__file__).parent / "outputs"
                await download_tts_outputs(client, data, outputs_dir)
        except httpx.ConnectError:
            log_info("无法连接到网关，请先启动网关服务器 (python main.py)")

async def download_tts_outputs(client, result_data, outputs_dir: Path):
    outputs_dir.mkdir(parents=True, exist_ok=True)
    result = result_data.get("result", {})
    if not result:
        return
    
    data = result.get("data", {})
    for key in ["audio", "subtitle"]:
        file_path = data.get(key)
        if not file_path:
            continue
        
        filename = Path(file_path).name
        download_url = f"{BASE_URL}/files/{filename}"
        try:
            resp = await client.get(download_url, headers=HEADERS)
            if resp.status_code == 200:
                save_path = outputs_dir / filename
                save_path.write_bytes(resp.content)
                log_info(f"成功保存 TTS 生成物到: {save_path}")
            else:
                log_info(f"下载 TTS 生成物失败 {filename}, HTTP 状态码: {resp.status_code}")
        except Exception as e:
            log_info(f"下载 TTS 生成物出错 {filename}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
