# -*- coding: utf-8 -*-
"""
KamlPro ComfyUI 集成测试用例 - 测试 Cover3Sizes 多采样器工作流的全局进度条
=============================================================================
1. 提交 cover3sizes 工作流（包含3个KSampler，专门用来验证进度条防回跳）
2. 监听 WS，打印我们刚刚新加的 `overall_progress` 进度
3. 将最终的 3 张不同尺寸的封面下载并保存
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
OUTPUT_DIR = Path(__file__).parent / "outputs" / "comfyui" / "cover3sizes"

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

async def listen_ws_progress(task_id: str, prefix: str = "[进度测试 WS]") -> dict:
    ws_uri = f"{WS_URL}/interface/tasks/{task_id}/ws"
    log_info(f"{prefix} 正在建立 WebSocket 通道: {ws_uri}", "yellow")
    
    last_printed_pct = -1
    
    try:
        async with websockets.connect(ws_uri, additional_headers=WS_HEADERS) as ws:
            log_info(f"{prefix} WebSocket 连接成功！开始捕捉进度帧...", "bold green")
            while True:
                msg_str = await ws.recv()
                msg = json.loads(msg_str)
                msg_type = msg.get("type", "")
                
                # --- 核心：拦截并打印我们后端的 overall_progress ---
                if msg_type == "overall_progress":
                    pct = msg.get("data", {}).get("percent", 0)
                    if pct != last_printed_pct:
                        message = msg.get("data", {}).get("message", "")
                        bar = "█" * int(pct / 5) + "-" * (20 - int(pct / 5))
                        console.print(f"   [dim]{get_timestamp()}[/dim] {prefix} [合成全局进度]: [{bar}] {pct}% {message}")
                        last_printed_pct = pct
                    
                # 保留原生 progress 的接收，但我们不打印成进度条，只作为后台对照
                elif msg_type == "progress":
                    pass 

                elif msg_type == "execution_start":
                    pid = msg.get("data", {}).get("prompt_id", "")
                    log_info(f"\n{prefix} [执行启动] prompt_id: {pid}", "cyan")
                elif msg_type == "executing":
                    node = msg.get("data", {}).get("node")
                    if node:
                        log_info(f"{prefix} 正在运行底层节点 ID: {node}", "dim")
                elif msg_type == "execution_error":
                    log_info(f"{prefix} [ERROR] 执行错误: {msg}", "bold red")
                    raise RuntimeError(f"ComfyUI execution error: {msg}")
                elif msg_type == "task_complete":
                    console.print()
                    log_info(f"{prefix} 任务 {task_id} 执行成功！", "bold green")
                    return {"status": "success", "outputs": msg.get("outputs", msg.get("result", {}))}
                elif msg_type == "task_failed":
                    console.print()
                    err_msg = msg.get("message", "未知错误")
                    log_info(f"{prefix} 任务失败: {err_msg}", "bold red")
                    raise RuntimeError(f"Task failed: {err_msg}")
    except websockets.exceptions.ConnectionClosed as e:
        log_info(f"\n{prefix} [ERROR] WebSocket 断开: {e}", "bold red")
        raise e

async def main():
    log_info("==========================================================", "bold yellow")
    log_info("   KamlPro 测试：Cover3Sizes 多重采样工作流进度防跳回测试   ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    workflow = load_workflow("cover3sizes.json")
    
    # 稍微改一下文字以证明是我们发起的测试
    workflow["313"]["inputs"]["value"] = "全局进度条测试\n再也不会横跳了！"
    
    # 根据 JSON 文件，有 3 个最终输出节点
    output_node_ids = ["264", "266", "296"]

    url = f"{BASE_URL}/interface/tasks/submit"
    payload = {
        "task_type": "comfyui",
        "payload": {
            "workflow": workflow,
            "final_output_node_id": output_node_ids,
            "client_id": str(uuid.uuid4())[:16]
        }
    }

    log_info(f"提交任务到: {url}", "bold blue")
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=10)
            resp.raise_for_status()
            task_id = resp.json().get("data", {}).get("task_id")
            log_info(f"提交成功！task_id: {task_id}", "bold green")

            ws_result = await listen_ws_progress(task_id)
            outputs = ws_result.get("outputs", {})

            # 下载 3 张图片
            for node_id, output in outputs.items():
                images = output.get("images", [])
                for img in images:
                    filename = img.get("filename", "generated_image.png")
                    subfolder = img.get("subfolder", "")
                    img_type = img.get("type", "temp")
                    
                    dl_url = f"{BASE_URL}/file/ComfyUI/api/view?filename={filename}&subfolder={subfolder}&type={img_type}"
                    log_info(f"正在下载生成的封面图 (Node {node_id}): {dl_url}", "yellow")
                    
                    img_resp = await client.get(dl_url, headers=HEADERS, timeout=30)
                    if img_resp.status_code == 200:
                        saved_file_path = OUTPUT_DIR / f"{node_id}_{filename}"
                        saved_file_path.write_bytes(img_resp.content)
                        log_info(f"[OK] 成功保存 -> {saved_file_path} ({len(img_resp.content)/1024:.1f} KB)", "bold green")
                    else:
                        log_info(f"下载失败, HTTP {img_resp.status_code}", "bold red")
    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请确保 main.py 服务已经启动并且 ComfyUI 在运行！", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 异常: {e}", "bold red")

if __name__ == "__main__":
    asyncio.run(main())
