# -*- coding: utf-8 -*-
"""
KamlPro ComfyUI 图像生成 (Text-to-Image) 集成测试用例
===================================================
1. 提交 Image 工作流（基于 Qwen Image 2512 闪电模型生图）
2. 通过 WS 监听实时进度并显示
3. 将生成的概念图下载并保存至 test/20260527/outputs/comfyui/image/ 目录中
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import asyncio
import httpx
import websockets
import json
import uuid
import datetime
from pathlib import Path
from rich.console import Console

console = Console()

BASE_URL = "http://127.0.0.1:7000"
WS_URL = "ws://127.0.0.1:7000"
HEADERS = {"Authorization": "Bearer 123456"}
WS_HEADERS = [("Authorization", "Bearer 123456")]

WORKFLOW_DIR = Path(__file__).parent / "comfyui_workflows"
OUTPUT_DIR = Path(__file__).parent / "outputs" / "comfyui" / "image"

def get_timestamp() -> str:
    return datetime.datetime.now().strftime("[%H:%M:%S.%f]")[:-3]

def log_info(msg: str, style: str = None):
    ts = get_timestamp()
    if style:
        console.print(f"[dim]{ts}[/dim] [{style}]{msg}[/{style}]")
    else:
        console.print(f"[dim]{ts}[/dim] {msg}")

def load_workflow(filename: str) -> dict:
    path = WORKFLOW_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Workflow file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

async def listen_ws_progress(task_id: str, prefix: str = "[生图 WS]") -> dict:
    ws_uri = f"{WS_URL}/interface/tasks/{task_id}/ws"
    log_info(f"{prefix} 正在建立 WebSocket 实时监听通道: {ws_uri}", "yellow")
    
    start_time = asyncio.get_event_loop().time()
    try:
        async with websockets.connect(ws_uri, additional_headers=WS_HEADERS) as ws:
            log_info(f"{prefix} WebSocket 连接成功！开始捕捉 ComfyUI 进度帧...", "bold green")
            while True:
                msg_str = await ws.recv()
                msg = json.loads(msg_str)
                msg_type = msg.get("type", "")
                
                if msg_type == "execution_start":
                    pid = msg.get("data", {}).get("prompt_id", "")
                    log_info(f"{prefix} [执行启动] ComfyUI 已装载工作流，prompt_id: {pid}", "cyan")
                elif msg_type == "executing":
                    node = msg.get("data", {}).get("node")
                    if node:
                        log_info(f"{prefix} 正在运行节点 ID: {node}", "dim")
                elif msg_type == "progress":
                    val = msg.get("data", {}).get("value", 0)
                    mx = msg.get("data", {}).get("max", 0)
                    if mx:
                        pct = val / mx * 100
                        bar = "#" * int(pct / 5) + "-" * (20 - int(pct / 5))
                        console.print(f"\r   [dim]{get_timestamp()}[/dim] {prefix} [采样进度]: \\[{bar}] {pct:.0f}%  {val}/{mx}", end="")
                elif msg_type == "execution_error":
                    console.print()
                    log_info(f"{prefix} [ERROR] ComfyUI 内部节点执行错误: {msg}", "bold red")
                    raise RuntimeError(f"ComfyUI execution error: {msg}")
                elif msg_type == "task_complete":
                    console.print()
                    log_info(f"{prefix} 任务 {task_id} 执行成功！WS 侦听到完成信号！", "bold green")
                    return {"status": "success", "outputs": msg.get("outputs", msg.get("result", {}))}
                elif msg_type == "task_failed":
                    console.print()
                    err_msg = msg.get("message", "未知错误")
                    log_info(f"{prefix} 任务 {task_id} 执行失败: {err_msg}", "bold red")
                    raise RuntimeError(f"Task failed: {err_msg}")
    except websockets.exceptions.ConnectionClosed as e:
        log_info(f"{prefix} [ERROR] WebSocket 连接异常关闭: {e}", "bold red")
        raise e

async def main():
    log_info("==========================================================", "bold yellow")
    log_info("        Kalm ComfyUI 图像生成 (Text-to-Image) 测试用例        ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    workflow = load_workflow("image_qwen_Image_2512.json")
    
    positive_prompt = (
        "A gorgeous futuristic cyber-punk style city skyline at sunset, towering vertical mega-structures, "
        "flying vehicles weaving between holographic billboards, bright neon glows in pink, cyan and orange, "
        "detailed wet streets with clear reflections of neon lights, masterpiece, hyper-detailed, 8K, cinematic light"
    )
    workflow["268"]["inputs"]["text"] = positive_prompt
    output_node_id = "282"

    url = f"{BASE_URL}/interface/tasks/submit"
    payload = {
        "task_type": "comfyui",
        "payload": {
            "workflow": workflow,
            "final_output_node_id": output_node_id,
            "client_id": str(uuid.uuid4())[:16]
        }
    }

    log_info(f"[Image Stage] 发起生图任务提交: POST {url}", "bold blue")
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=10)
            resp.raise_for_status()
            task_id = resp.json().get("data", {}).get("task_id")
            log_info(f"[Image Stage] 任务提交成功！task_id: {task_id}", "bold green")

            ws_result = await listen_ws_progress(task_id, prefix="[生图 WS]")
            outputs = ws_result.get("outputs", {})

            saved_file_path = None
            for node_id, output in outputs.items():
                images = output.get("images", [])
                for img in images:
                    filename = img.get("filename", "generated_image.png")
                    subfolder = img.get("subfolder", "")
                    img_type = img.get("type", "temp")
                    
                    dl_url = f"{BASE_URL}/file/ComfyUI/api/view?filename={filename}&subfolder={subfolder}&type={img_type}"
                    log_info(f"[Image Stage] 正在下载生成的概念图: {dl_url}", "yellow")
                    
                    img_resp = await client.get(dl_url, headers=HEADERS, timeout=30)
                    if img_resp.status_code == 200:
                        saved_file_path = OUTPUT_DIR / filename
                        saved_file_path.write_bytes(img_resp.content)
                        log_info(f"[Image Stage] [OK] 生图成功保存 -> {saved_file_path} ({len(img_resp.content)/1024:.1f} KB)", "bold green")
                    else:
                        log_info(f"[Image Stage] 下载失败, HTTP {img_resp.status_code}", "bold red")
    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请先启动服务: run.bat / main.py", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 测试出现未预期异常: {e}", "bold red")

if __name__ == "__main__":
    asyncio.run(main())
