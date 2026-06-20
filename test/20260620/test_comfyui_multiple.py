# -*- coding: utf-8 -*-
"""
ComfyUI 多任务排队取消测试
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
    print(f"{get_timestamp()} [ComfyUI多任务] {msg}")

async def main():
    async with httpx.AsyncClient() as client:
        try:
            workflow_path = Path(__file__).parent / "comfyui_workflows" / "image_qwen_Image_2512.json"
            if not workflow_path.exists():
                log_info(f"错误: 未能找到工作流文件 {workflow_path}")
                return
            with open(workflow_path, "r", encoding="utf-8") as f:
                workflow = json.load(f)

            task_ids = []
            # 提交 3 个任务以触发排队
            for i in range(1, 4):
                payload = {
                    "task_type": "comfyui",
                    "payload": {
                        "workflow": workflow,
                        "final_output_node_id": ["282"],
                        "client_id": str(uuid.uuid4())[:16],
                        "task_label": f"multi_task_{i}"
                    }
                }
                resp = await client.post(f"{BASE_URL}/interface/tasks/submit", json=payload, headers=HEADERS)
                task_id = resp.json()["data"]["task_id"]
                task_ids.append(task_id)
                log_info(f"已提交任务 {i}，分配 task_id: {task_id}")

            log_info("等待 2.0 秒（确保任务 1 运行，任务 2/3 排队）...")
            await asyncio.sleep(2.0)

            # 取消运行中的任务 1 和排队中的任务 3
            log_info(f"取消运行中的任务 1 (task_id: {task_ids[0]})...")
            cancel_1 = await client.post(f"{BASE_URL}/interface/tasks/{task_ids[0]}/cancel", headers=HEADERS)
            log_info(f"任务 1 取消响应: {cancel_1.json()}")

            log_info(f"取消排队中的任务 3 (task_id: {task_ids[2]})...")
            cancel_3 = await client.post(f"{BASE_URL}/interface/tasks/{task_ids[2]}/cancel", headers=HEADERS)
            log_info(f"任务 3 取消响应: {cancel_3.json()}")

            log_info("开始轮询任务 2 的状态，直到其执行完毕...")
            outputs_dir = Path(__file__).parent / "outputs"
            while True:
                status_resp = await client.get(f"{BASE_URL}/interface/tasks/{task_ids[1]}/status", headers=HEADERS)
                data = status_resp.json()["data"]
                status = data["status"]
                if status in ("success", "failed"):
                    log_info(f"任务 2 ({task_ids[1]}) 执行结束，最终状态: {status}")
                    break
                
                percent = 0
                res = data.get("result", {})
                if isinstance(res, dict):
                    percent = res.get("data", {}).get("percent", 0)
                log_info(f"任务 2 正在执行中，当前进度: {percent}%... 5.0 秒后再次查询")
                await asyncio.sleep(5.0)

            # 打印最终状态并下载生成物
            for i, tid in enumerate(task_ids, start=1):
                status_resp = await client.get(f"{BASE_URL}/interface/tasks/{tid}/status", headers=HEADERS)
                data = status_resp.json()["data"]
                log_info(f"任务 {i} ({tid}) 状态: {data['status']} | 结果: {data['result']}")
                if data['status'] == "success":
                    await download_comfyui_outputs(client, data, outputs_dir)

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
