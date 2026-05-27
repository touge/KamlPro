# -*- coding: utf-8 -*-
"""
KamlPro ComfyUI 图像与视频生成 (Image & Video) 级联集成测试用例
==========================================================
参考 test_hybrid_stream.py 风格，使用 websockets 与 httpx 实现全流程 WS 进度监听。

级联测试流程：
1. 提交 Image 工作流（基于 Qwen Image 2512 闪电模型生图），通过 WS 监听实时进度，下载生成图片。
2. 自动定位本地 ComfyUI 服务目录，将所生图片拷贝至 ComfyUI 默认 input/ 目录，实现数据级联。
3. 提交 Video 工作流（基于 LTX 2.3 图像转 5s 视频），通过 WS 监听进度，下载生成视频 MP4 产物。
4. 产物分类归档至 test/20260527/outputs/comfyui/ 目录中。
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

# 显式创建 Console 实例，完美兼容 GBK 与 UTF-8 终端
console = Console()

BASE_URL = "http://127.0.0.1:7000"
WS_URL = "ws://127.0.0.1:7000"

HEADERS = {
    "Authorization": "Bearer 123456"
}
WS_HEADERS = [("Authorization", "Bearer 123456")]

# 动态测试路径与输入、输出目录管理
TEST_ROOT = Path(__file__).parent.parent
WORKFLOW_DIR = Path(__file__).parent / "comfyui_workflows"
OUTPUT_ROOT = Path(__file__).parent / "outputs" / "comfyui"
IMAGE_OUTPUT_DIR = OUTPUT_ROOT / "image"
VIDEO_OUTPUT_DIR = OUTPUT_ROOT / "video"


def get_timestamp() -> str:
    """获取带毫秒的高精度时间戳"""
    return datetime.datetime.now().strftime("[%H:%M:%S.%f]")[:-3]


def log_info(msg: str, style: str = None):
    """带高精度时间戳的日志输出"""
    ts = get_timestamp()
    if style:
        console.print(f"[dim]{ts}[/dim] [{style}]{msg}[/{style}]")
    else:
        console.print(f"[dim]{ts}[/dim] {msg}")


def load_workflow(filename: str) -> dict:
    """读取指定工作流 JSON 文件"""
    path = WORKFLOW_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Workflow file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def listen_ws_progress(task_id: str, prefix: str = "[ComfyUI WS]") -> dict:
    """建立 WebSocket 管道，被动捕获 ComfyUI 的 K采样器步骤进度并实时彩显进度条"""
    ws_uri = f"{WS_URL}/interface/tasks/{task_id}/ws"
    log_info(f"{prefix} 正在建立 WebSocket 实时监听通道: {ws_uri}", "yellow")
    
    start_time = asyncio.get_event_loop().time()
    
    try:
        async with websockets.connect(ws_uri, additional_headers=WS_HEADERS) as ws:
            log_info(f"{prefix} WebSocket 连接成功！开始捕捉 ComfyUI 进度帧...", "bold green")
            
            while True:
                msg_str = await ws.recv()
                msg = json.loads(msg_str)
                elapsed = asyncio.get_event_loop().time() - start_time
                
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
                    console.print()  # 换行
                    log_info(f"{prefix} [ERROR] ComfyUI 内部节点执行错误: {msg}", "bold red")
                    raise RuntimeError(f"ComfyUI execution error: {msg}")
                    
                elif msg_type == "task_complete":
                    console.print()  # 换行
                    log_info(f"{prefix} 任务 {task_id} 执行成功！WS 侦听到完成信号！", "bold green")
                    # 返回 outputs 字典（包含节点输出图片/视频数据）
                    return {"status": "success", "outputs": msg.get("outputs", msg.get("result", {}))}
                    
                elif msg_type == "task_failed":
                    console.print()  # 换行
                    err_msg = msg.get("message", "未知错误")
                    log_info(f"{prefix} 任务 {task_id} 执行失败: {err_msg}", "bold red")
                    raise RuntimeError(f"Task failed: {err_msg}")
                    
    except websockets.exceptions.ConnectionClosed as e:
        log_info(f"{prefix} [ERROR] WebSocket 连接异常关闭: {e}", "bold red")
        raise e


async def run_stage1_image_generation() -> Path:
    """第一阶段：测试生成 Qwen Image 2512 闪电模型生图，并下载结果图片"""
    log_info("\n" + "=" * 80, "bold green")
    log_info("第一阶段: 提交 Qwen Image 生图工作流并进行 WS 被动进度跟踪", "bold green")
    log_info("=" * 80, "bold green")

    IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 准备工作流
    workflow = load_workflow("image_qwen_Image_2512.json")
    
    # 动态设定生图 Prompt 词 (科幻赛博朋克都市夕阳)
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
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=10)
        resp.raise_for_status()
        task_id = resp.json().get("data", {}).get("task_id")
        log_info(f"[Image Stage] 任务提交成功！task_id: {task_id}", "bold green")

        # 2. 建立 WS 进度跟踪并等待完成
        ws_result = await listen_ws_progress(task_id, prefix="[生图 WS]")
        outputs = ws_result.get("outputs", {})

        # 3. 下载生图结果图片
        saved_file_path = None
        for node_id, output in outputs.items():
            images = output.get("images", [])
            for img in images:
                filename = img.get("filename", "generated_image.png")
                subfolder = img.get("subfolder", "")
                img_type = img.get("type", "temp")
                
                # ComfyUI 的通用代理文件读取路径
                dl_url = f"{BASE_URL}/file/ComfyUI/api/view?filename={filename}&subfolder={subfolder}&type={img_type}"
                log_info(f"[Image Stage] 正在下载生成的概念图: {dl_url}", "yellow")
                
                img_resp = await client.get(dl_url, headers=HEADERS, timeout=30)
                if img_resp.status_code == 200:
                    saved_file_path = IMAGE_OUTPUT_DIR / filename
                    saved_file_path.write_bytes(img_resp.content)
                    log_info(f"[Image Stage] [OK] 生图成功保存 -> {saved_file_path} ({len(img_resp.content)/1024:.1f} KB)", "bold green")
                else:
                    log_info(f"[Image Stage] 下载失败, HTTP {img_resp.status_code}", "bold red")
        
        if not saved_file_path:
            raise RuntimeError("Failed to generate and download output image.")
        return saved_file_path


def copy_image_to_comfyui_input(src_path: Path):
    """自动定位本地 ComfyUI 配置目录，将所生图片安全拷贝至 ComfyUI input/"""
    log_info("\n" + "=" * 80, "bold green")
    log_info("级联数据桥接: 自动定位本地 ComfyUI 服务并将图片拷贝至 input/", "bold green")
    log_info("=" * 80, "bold green")

    # 从 configs/services.yaml 通过 API 控制器动态推断 ComfyUI 根目录
    try:
        from src.core.service_controller import service_controller
        comfy_config = service_controller.get_service_config("ComfyUI")
        
        if not comfy_config:
            log_info("[Bridge] [WARNING] 未能在 services.yaml 找到 ComfyUI 配置, 尝试使用系统默认路径 'D:/AIGC/ComfyUI'", "yellow")
            comfy_root = Path("D:/AIGC/ComfyUI")
        else:
            # 提取 Windows 下的启动脚本路径
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
        log_info("[Bridge] 请确认 ComfyUI 服务安装于 'D:/AIGC/ComfyUI'", "yellow")


async def run_stage2_video_generation(input_image_name: str):
    """第二阶段：提交 LTX 2.3 图像转 5s 视频工作流，并下载结果 MP4 产物"""
    log_info("\n" + "=" * 80, "bold green")
    log_info("第二阶段: 提交 LTX 2.3 图像转 5 秒视频工作流并进行 WS 被动进度跟踪", "bold green")
    log_info("=" * 80, "bold green")

    VIDEO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 准备工作流
    workflow = load_workflow("LTX2-i2v-api-no-audio.json")
    
    # 动态把刚才在 Stage 1 生成的图片设置为输入源 (Node 19)
    workflow["19"]["inputs"]["image"] = input_image_name
    
    # 动态把视频帧数设置为 81 帧 (约 5 秒)
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

    log_info(f"[Video Stage] 发起生视频任务提交: POST {url} | 载入图像={input_image_name}", "bold blue")
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=10)
        resp.raise_for_status()
        task_id = resp.json().get("data", {}).get("task_id")
        log_info(f"[Video Stage] 任务提交成功！task_id: {task_id}", "bold green")

        # 2. 建立 WS 进度跟踪并等待完成（视频生成采样通常较慢，支持优雅心跳）
        ws_result = await listen_ws_progress(task_id, prefix="[视频 WS]")
        outputs = ws_result.get("outputs", {})

        # 3. 下载生视频结果 MP4/GIF
        downloaded_count = 0
        for node_id, output in outputs.items():
            # 视频组件输出可能放在 gifs 或 videos 节点下
            items = output.get("gifs", []) or output.get("videos", []) or output.get("images", [])
            for item in items:
                filename = item.get("filename", "generated_video.mp4")
                subfolder = item.get("subfolder", "")
                item_type = item.get("type", "output")
                
                dl_url = f"{BASE_URL}/file/ComfyUI/api/view?filename={filename}&subfolder={subfolder}&type={item_type}"
                log_info(f"[Video Stage] 正在下载生成的 5 秒视频: {dl_url}", "yellow")
                
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


async def main():
    log_info("==========================================================", "bold yellow")
    log_info("   Kalm 图像与 5 秒视频生成级联并发集成测试客户端 (WS版)   ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    log_info(f"   目标网关地址 : {BASE_URL}")
    log_info(f"   WS 推送地址  : {WS_URL}")
    
    try:
        # 1. 运行生图任务并下载图片
        image_path = await run_stage1_image_generation()
        
        # 2. 跨进程级联：拷贝图片至 ComfyUI 输入端
        copy_image_to_comfyui_input(image_path)
        
        # 3. 运行图生视频任务并下载视频
        await run_stage2_video_generation(image_path.name)
        
        log_info("\n" + "=" * 80, "bold yellow")
        log_info("   测试完成！所有图像与视频生成结果均已安全归档至 outputs/comfyui/ 目录。   ", "bold yellow")
        log_info("=" * 80, "bold yellow")

    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请先启动服务: run.bat / main.py", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 级联测试出现未预期异常: {e}", "bold red")


if __name__ == "__main__":
    asyncio.run(main())
