# -*- coding: utf-8 -*-
"""
KamlPro 本地离线大模型 (Ollama) 自适应追踪策略集成测试用例
============================================================
1. 根据协议，本地大模型任务属于本地 GPU 串行轨道。
2. 客户端提交任务后，不应该硬编码追踪方式，而是通过返回的 `track_mode` 智能决定追踪策略。
3. 如果 track_mode 为 "ws"：客户端自动连接统一的 WebSocket 实时推送接口监听任务进度与结果。
4. 如果 track_mode 为 "poll"：客户端降级使用常规 HTTP `/status` 接口进行周期性轮询获取结果。
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import asyncio
import httpx
import websockets
import json
import datetime
from rich.console import Console

# 显式创建 Console 实例，完美兼容 GBK 与 UTF-8 终端
console = Console()

BASE_URL = "http://127.0.0.1:7000"
WS_URL = "ws://127.0.0.1:7000"

HEADERS = {
    "Authorization": "Bearer 123456",
    "Content-Type": "application/json"
}
WS_HEADERS = [("Authorization", "Bearer 123456")]

# 默认测试本地已拥有的 gemma3:12b 模型
TEST_MODEL = "gemma3:12b"


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


async def listen_task_via_ws(task_id: str, prefix: str = "[LLM WS]") -> dict:
    """策略 1：建立 WebSocket 管道，接收网关推送的进度消息直到任务结束"""
    ws_uri = f"{WS_URL}/interface/tasks/{task_id}/ws"
    log_info(f"{prefix} 正在建立 WebSocket 实时监听通道: {ws_uri}", "yellow")
    
    start_time = asyncio.get_event_loop().time()
    
    try:
        async with websockets.connect(ws_uri, additional_headers=WS_HEADERS) as ws:
            log_info(f"{prefix} WebSocket 通道连接成功！开始接收网关推送数据...", "bold green")
            
            while True:
                msg_str = await ws.recv()
                msg = json.loads(msg_str)
                elapsed = asyncio.get_event_loop().time() - start_time
                
                # 打印接收到的 WS 推送消息帧，方便前端观察真实的伪装 WS 数据结构
                log_info(f"{prefix} 接收帧 ({elapsed:.1f}s): {msg}", "dim")
                
                msg_type = msg.get("type")
                
                # 兼容多种返回消息类型定义
                if msg_type == "task_complete" or (msg_type == "status_change" and msg.get("status") == "success"):
                    log_info(f"{prefix} 任务 {task_id} 执行成功！WS 监听到完成信号！", "bold green")
                    return msg
                elif msg_type == "task_failed" or (msg_type == "status_change" and msg.get("status") == "failed"):
                    err_msg = msg.get("message", "未知错误")
                    log_info(f"{prefix} 任务 {task_id} 执行失败: {err_msg}", "bold red")
                    raise RuntimeError(f"Task execution failed: {err_msg}")
                    
    except websockets.exceptions.ConnectionClosed as e:
        log_info(f"{prefix} [ERROR] WebSocket 连接异常关闭: {e}", "bold red")
        raise e


async def poll_task_status(task_id: str, prefix: str = "[LLM 轮询]") -> dict:
    """策略 2：异步轮询任务状态直到成功或失败 (降级策略)"""
    url = f"{BASE_URL}/interface/tasks/{task_id}/status"
    start_time = asyncio.get_event_loop().time()
    log_info(f"{prefix} 开始轮询任务 {task_id} 状态...", "yellow")
    
    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(url, headers=HEADERS)
            resp.raise_for_status()
            task_data = resp.json().get("data", {})
            status = task_data.get("status")
            
            elapsed = asyncio.get_event_loop().time() - start_time
            log_info(f"{prefix} 任务 {task_id} 当前状态: {status} | 已轮询: {elapsed:.1f}s", "dim")
            
            if status == "success":
                log_info(f"{prefix} 任务 {task_id} 执行成功！总耗时: {elapsed:.2f}s", "bold green")
                return task_data
            elif status == "failed":
                err_msg = task_data.get("result", {}).get("message", "未知错误")
                log_info(f"{prefix} 任务 {task_id} 执行失败: {err_msg}", "bold red")
                return task_data
                
            await asyncio.sleep(2.0)


async def main():
    log_info("==========================================================", "bold yellow")
    log_info("    Kalm 本地大模型 (Ollama / 自适应路由模式) 测试用例     ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    log_info(f"   目标模型     : {TEST_MODEL}")
    log_info(f"   网关地址     : {BASE_URL}")
    log_info(f"   WS 推送地址  : {WS_URL}")
    log_info(f"   说明         : 提交任务后，程序读取 track_mode 自动决定使用 WS 监听还是 HTTP 轮询")
    
    prompt = "请用一句非常简短的话解释什么是量子计算。"
    url = f"{BASE_URL}/interface/tasks/submit"
    payload = {
        "task_type": "llm",
        "payload": {
            "model": TEST_MODEL,
            "prompt": prompt,
            "options": {"temperature": 0.3, "max_tokens": 150}
        }
    }
    
    log_info(f"[Local Stage] 发起本地大模型任务提交: POST {url}", "bold blue")
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            resp_data = resp.json()
            
            if resp_data.get("status") != "success":
                log_info(f"[Local Stage] 提交失败: {resp_data}", "bold red")
                return
                
            task_id = resp_data.get("data", {}).get("task_id")
            track_mode = resp_data.get("data", {}).get("track_mode", "poll")
            log_info(f"[Local Stage] 任务提交成功！task_id: {task_id} | 路由 track_mode: {track_mode}", "bold green")
            
            # ---- 智能分流决定客户端的拉取策略 ----
            if track_mode == "ws":
                log_info("[Local Stage] 检测到 track_mode='ws'，启动 WebSocket 被动收听策略...", "cyan")
                ws_msg = await listen_task_via_ws(task_id, prefix="[LLM WS]")
                result = ws_msg.get("result", {})
            elif track_mode == "poll":
                log_info("[Local Stage] 检测到 track_mode='poll'，启动 HTTP 周期轮询降级策略...", "cyan")
                task_data = await poll_task_status(task_id, prefix="[LLM 轮询]")
                result = task_data.get("result", {})
            else:
                log_info(f"[ERROR] 无法识别的 track_mode: {track_mode}", "bold red")
                return
            
            # 提取最终推理出的结果
            text = result.get("text", "").strip()
            duration_ns = result.get("total_duration")
            duration_s = f"{duration_ns / 1e9:.2f}s" if duration_ns else "N/A"
            
            console.print(f"\n[bold yellow]─── 推理结果预览 (本地 Ollama / 通过 {track_mode.upper()} 追踪) ───[/bold yellow]")
            console.print(text)
            console.print(f"[dim]推理真实耗时 (Ollama 内核): {duration_s}[/dim]")
            console.print("[bold yellow]────────────────────────────────────────────────────────[/bold yellow]\n")
            log_info(f"[Local Stage] 本地大模型 {track_mode.upper()} 模式验证成功！", "bold green")
                
    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请先启动服务: run.bat / main.py", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 测试运行发生异常: {e}", "bold red")


if __name__ == "__main__":
    asyncio.run(main())
