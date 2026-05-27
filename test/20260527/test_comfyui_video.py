# -*- coding: utf-8 -*-
"""
KamlPro ComfyUI 图像转视频 (Image-to-Video) 集成测试用例
=====================================================
1. 自动定位 test/20260527/outputs/comfyui/image/ 目录下最新生成的概念图文件作为输入源。
2. 自动定位本地 ComfyUI 服务目录，将图片拷贝至 ComfyUI 默认 input/ 目录。
3. 提交 Video 工作流（基于 GGUF/分体 LTX 2.3 工作流），支持 WS 进度监听与实时采样条显示。
4. 动态在内存中为 node 622 设置 sage_attention="disabled" 以规避 sageattention 库依赖。
5. 将生成结果 MP4 产物下载并保存至 test/20260527/outputs/comfyui/video/ 目录。
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import asyncio
import httpx
import websockets
import json
import uuid
import shutil
import datetime
from pathlib import Path
from rich.console import Console

console = Console()

BASE_URL = "http://127.0.0.1:7000"
WS_URL = "ws://127.0.0.1:7000"
HEADERS = {"Authorization": "Bearer 123456"}
WS_HEADERS = [("Authorization", "Bearer 123456")]

WORKFLOW_DIR = Path(__file__).parent / "comfyui_workflows"
IMAGE_INPUT_DIR = Path(__file__).parent / "outputs" / "comfyui" / "image"
VIDEO_OUTPUT_DIR = Path(__file__).parent / "outputs" / "comfyui" / "video"

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

def copy_image_to_comfyui_input(src_path: Path):
    """自动定位本地 ComfyUI 配置目录，将所生图片安全拷贝至 ComfyUI input/"""
    log_info("\n" + "=" * 80, "bold green")
    log_info("级联数据桥接: 自动定位本地 ComfyUI 服务并将图片拷贝至 input/", "bold green")
    log_info("=" * 80, "bold green")

    try:
        from src.core.service_controller import service_controller
        comfy_config = service_controller.get_service_config("ComfyUI")
        
        if not comfy_config:
            log_info("[Bridge] [WARNING] 未能在 services.yaml 找到 ComfyUI 配置, 尝试使用系统默认路径 'D:/AIGC/ComfyUI'", "yellow")
            comfy_root = Path("D:/AIGC/ComfyUI")
        else:
            script_path = comfy_config.get("platforms", {}).get("windows", {}).get("path")
            if script_path:
                comfy_root = Path(script_path).parent
            else:
                comfy_root = Path("D:/AIGC/ComfyUI")

        comfy_input_dir = comfy_root / "input"
        comfy_input_dir.mkdir(parents=True, exist_ok=True)
        
        dest_path = comfy_input_dir / src_path.name
        shutil.copy2(src_path, dest_path)
        log_info(f"[Bridge] [OK] 级联成功！概念图已复制到 ComfyUI 输入端 -> {dest_path}", "bold green")
        
    except Exception as e:
        log_info(f"[Bridge] [ERROR] 概念图跨目录复制失败 (非阻断性): {e}", "bold red")

async def listen_ws_progress(task_id: str, prefix: str = "[视频 WS]") -> dict:
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
    log_info("        Kalm ComfyUI 图像转视频 (Image-to-Video) 测试用例        ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    
    VIDEO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 查找最新生成的图像文件
    input_image_path = None
    if IMAGE_INPUT_DIR.exists():
        files = [f for f in IMAGE_INPUT_DIR.glob("*.png") if f.is_file()]
        if files:
            files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            input_image_path = files[0]
            log_info(f"[Video Stage] 自动扫描到最新生成的概念图: {input_image_path.name}", "bold green")
            
    if not input_image_path:
        log_info("[Video Stage] [ERROR] 未能在 outputs/comfyui/image/ 目录下找到可供级联的图像产物，请先运行 test_comfyui_image.py！", "bold red")
        return

    # 数据桥接：拷贝图片到 ComfyUI input 文件夹
    copy_image_to_comfyui_input(input_image_path)
    
    # 载入工作流并配置参数
    workflow = load_workflow("LTX2-i2v-api-no-audio.json")
    
    # 动态把刚才在 Stage 1 生成的图片设置为输入源 (Node 19)
    workflow["19"]["inputs"]["image"] = input_image_path.name
    
    # 动态把视频帧数设置为 121 帧
    workflow["12"]["inputs"]["value"] = 121
    
    # 动态把 node 622 (PathchSageAttentionKJ) 的 sage_attention 设为 disabled 以适应本地无该依赖的环境
    if "622" in workflow and "inputs" in workflow["622"]:
        workflow["622"]["inputs"]["sage_attention"] = "disabled"
    
    # 动态设定视频生成 Prompt 词 (Node 26)
    video_prompt = (
        "High-definition video, the futuristic cyber-punk skyline comes to life, flying neon-lit cars "
        "slowly glide through the vertical structures, giant holographic ads shimmer dynamically, "
        "camera slowly pans across the futuristic street with reflection detail, ultra photorealistic, 8K"
    )
    workflow["26"]["inputs"]["text"] = video_prompt
    
    output_node_id = "53"

    url = f"{BASE_URL}/interface/tasks/submit"
    payload = {
        "task_type": "comfyui",
        "payload": {
            "workflow": workflow,
            "final_output_node_id": output_node_id,
            "client_id": str(uuid.uuid4())[:16]
        }
    }

    log_info(f"[Video Stage] 发起生视频任务提交: POST {url} | 载入图像={input_image_path.name}", "bold blue")
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=10)
            resp.raise_for_status()
            task_id = resp.json().get("data", {}).get("task_id")
            log_info(f"[Video Stage] 任务提交成功！task_id: {task_id}", "bold green")

            # 进度监听
            ws_result = await listen_ws_progress(task_id, prefix="[视频 WS]")
            outputs = ws_result.get("outputs", {})

            # 下载 MP4 产物
            downloaded_count = 0
            for node_id, output in outputs.items():
                items = output.get("gifs", []) or output.get("videos", []) or output.get("images", [])
                for item in items:
                    filename = item.get("filename", "generated_video.mp4")
                    subfolder = item.get("subfolder", "")
                    item_type = item.get("type", "output")
                    
                    dl_url = f"{BASE_URL}/file/ComfyUI/api/view?filename={filename}&subfolder={subfolder}&type={item_type}"
                    log_info(f"[Video Stage] 正在下载生成的视频: {dl_url}", "yellow")
                    
                    vid_resp = await client.get(dl_url, headers=HEADERS, timeout=60)
                    if vid_resp.status_code == 200:
                        saved_path = VIDEO_OUTPUT_DIR / filename
                        saved_path.write_bytes(vid_resp.content)
                        log_info(f"[Video Stage] [OK] 视频保存成功 -> {saved_path} ({len(vid_resp.content)/1024/1024:.2f} MB)", "bold green")
                        downloaded_count += 1
                    else:
                        log_info(f"[Video Stage] 下载失败, HTTP {vid_resp.status_code}", "bold red")
            
            if downloaded_count == 0:
                log_info("[Video Stage] [WARNING] 未能从 outputs 中匹配并下载到 MP4/GIF 视频产物", "yellow")
    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请先启动服务: run.bat / main.py", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 测试出现未预期异常: {e}", "bold red")

if __name__ == "__main__":
    asyncio.run(main())
