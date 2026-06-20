# -*- coding: utf-8 -*-
"""
ComfyUI 单任务取消测试
"""
import sys
import os
import json
import asyncio
import httpx
import uuid
import datetime
from pathlib import Path

sys.path.insert(0, os.path.abspath(Path(__file__).parent / ".." / ".."))

BASE_URL = "http://127.0.0.1:7000"
HEADERS = {"Authorization": "Bearer 123456"}

def get_timestamp() -> str:
    return datetime.datetime.now().strftime("[%H:%M:%S.%f]")[:-3]

def log_info(msg: str):
    print(f"{get_timestamp()} [ComfyUI单任务] {msg}")

async def main():
    async with httpx.AsyncClient() as client:
        try:
            workflow_path = Path(__file__).parent / "comfyui_workflows" / "image_qwen_Image_2512.json"
            if not workflow_path.exists():
                log_info(f"错误: 未能找到工作流文件 {workflow_path}")
                return
            with open(workflow_path, "r", encoding="utf-8") as f:
                workflow = json.load(f)

            payload = {
                "task_type": "comfyui",
                "payload": {
                    "workflow": workflow,
                    "final_output_node_id": ["282"],
                    "client_id": str(uuid.uuid4())[:16]
                }
            }

            log_info("1. 提交单个 ComfyUI 绘图任务...")
            resp = await client.post(f"{BASE_URL}/interface/tasks/submit", json=payload, headers=HEADERS)
            resp.raise_for_status()
            task_id = resp.json()["data"]["task_id"]
            log_info(f"提交成功，分配 task_id: {task_id}")

            log_info("等待 2.0 秒确保其启动并注册到 ComfyUI...")
            await asyncio.sleep(2.0)

            log_info(f"2. 发送取消请求: {task_id}...")
            cancel_resp = await client.post(f"{BASE_URL}/interface/tasks/{task_id}/cancel", headers=HEADERS)
            cancel_resp.raise_for_status()
            log_info(f"取消响应: {cancel_resp.json()}")

            # 验证状态
            status_resp = await client.get(f"{BASE_URL}/interface/tasks/{task_id}/status", headers=HEADERS)
            status_data = status_resp.json()["data"]
            log_info(f"任务最终状态: {status_data['status']} | 结果: {status_data['result']}")
            if status_data['status'] == "success":
                outputs_dir = Path(__file__).parent / "outputs"
                await download_comfyui_outputs(client, status_data, outputs_dir)
        except httpx.ConnectError:
            log_info("无法连接到网关，请先启动网关服务器 (python main.py)")

async def download_comfyui_outputs(client, result_data, outputs_dir: Path):
    outputs_dir.mkdir(parents=True, exist_ok=True)
    outputs = result_data.get("result", {}).get("outputs", {})
    if not outputs:
        return
    
    for node_id, node_output in outputs.items():
        for file_type in ["images", "gifs"]:
            for item in node_output.get(file_type, []):
                filename = item.get("filename")
                subfolder = item.get("subfolder", "")
                type_val = item.get("type", "output")
                if not filename:
                    continue
                
                download_url = f"{BASE_URL}/file/comfyui/view?filename={filename}&subfolder={subfolder}&type={type_val}"
                try:
                    resp = await client.get(download_url, headers=HEADERS)
                    if resp.status_code == 200:
                        save_path = outputs_dir / filename
                        save_path.write_bytes(resp.content)
                        log_info(f"成功保存 ComfyUI 生成物到: {save_path}")
                    else:
                        log_info(f"下载 ComfyUI 生成物失败 {filename}, HTTP 状态码: {resp.status_code}")
                except Exception as e:
                    log_info(f"下载 ComfyUI 生成物出错 {filename}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
