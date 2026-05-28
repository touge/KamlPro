# -*- coding: utf-8 -*-
"""
KamlPro 云端大模型 (DeepSeek) 纯 HTTP 轮询集成测试用例
=====================================================
云端模型同样支持打包轮询模式（Poll）。提交 task_type="llm"，通过 HTTP /status
轮询获取完整文本。适用于不需要流式输出的简单集成场景。
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import asyncio
import httpx
import datetime
from rich.console import Console

console = Console()

BASE_URL = "http://127.0.0.1:7000"
HEADERS = {
    "Authorization": "Bearer 123456",
    "Content-Type": "application/json"
}

# 默认使用云端极速 deepseek-v4-flash 模型
TEST_MODEL = "deepseek-v4-flash"

def get_timestamp() -> str:
    return datetime.datetime.now().strftime("[%H:%M:%S.%f]")[:-3]

def log_info(msg: str, style: str = None):
    ts = get_timestamp()
    if style:
        console.print(f"[dim]{ts}[/dim] [{style}]{msg}[/{style}]")
    else:
        console.print(f"[dim]{ts}[/dim] {msg}")

async def poll_task_status(task_id: str, prefix: str = "[云端轮询]") -> dict:
    """异步轮询任务状态直到成功或失败"""
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

            await asyncio.sleep(1.0)

async def main():
    log_info("==========================================================", "bold yellow")
    log_info("      Kalm 云端大模型 (DeepSeek / 纯 HTTP 轮询模式) 测试用例    ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    log_info(f"   目标模型     : {TEST_MODEL}")
    log_info(f"   说明         : 云端模型纯 HTTP 轮询，提交 task_type='llm' 后轮询 /status")

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

    log_info(f"[Cloud Poll] 发起云端大模型任务提交: POST {url}", "bold blue")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            resp_data = resp.json()
            task_id = resp_data.get("data", {}).get("task_id")
            track_mode = resp_data.get("data", {}).get("track_mode", "poll")

            log_info(f"[Cloud Poll] 任务提交成功！task_id: {task_id} | 推荐 track_mode: {track_mode}", "bold green")

            # 纯 HTTP 轮询获取返回结果
            task_data = await poll_task_status(task_id)
            result = task_data.get("result", {})

            if task_data.get("status") == "success":
                text = result.get("text", "").strip()
                console.print(f"\n[bold yellow]─── 推理结果预览 (云端 DeepSeek / HTTP 轮询模式) ───[/bold yellow]")
                console.print(text)
                console.print("[bold yellow]──────────────────────────────────────────[/bold yellow]\n")
                log_info("[Cloud Poll] 云端大模型纯 HTTP 轮询模式验证成功！", "bold green")
            else:
                log_info("[Cloud Poll] 云端大模型任务执行失败", "bold red")

    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请先启动服务: run.bat / main.py", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 测试运行发生异常: {e}", "bold red")

if __name__ == "__main__":
    asyncio.run(main())
