# -*- coding: utf-8 -*-
"""
KamlPro 本地离线大模型 (Ollama) 集成测试用例
===========================================
1. 根据协议，本地大模型推理仅支持打包轮询模式（Poll）。
2. 提交非流式任务 (task_type="llm")，模型使用本地 "gemma3:12b"。
3. 通过轮询 "/interface/tasks/{id}/status" 获取执行状态，展示本地冷启动与推理时间。
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import asyncio
import httpx
import time
import datetime
from rich.console import Console

console = Console()

BASE_URL = "http://127.0.0.1:7000"
HEADERS = {
    "Authorization": "Bearer 123456",
    "Content-Type": "application/json"
}

# 默认测试本地已拥有的 gemma3:12b 模型
TEST_MODEL = "gemma3:12b"

def get_timestamp() -> str:
    return datetime.datetime.now().strftime("[%H:%M:%S.%f]")[:-3]

def log_info(msg: str, style: str = None):
    ts = get_timestamp()
    if style:
        console.print(f"[dim]{ts}[/dim] [{style}]{msg}[/{style}]")
    else:
        console.print(f"[dim]{ts}[/dim] {msg}")

async def poll_task_status(task_id: str, prefix: str = "[本地轮询]") -> dict:
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
                
            await asyncio.sleep(2.0)

async def main():
    log_info("==========================================================", "bold yellow")
    log_info("        Kalm 本地离线大模型 (Ollama / Poll模式) 测试用例        ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    log_info(f"   目标模型     : {TEST_MODEL}")
    log_info(f"   说明         : 本地模型仅支持 Poll 打包轮询模式")
    
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
            task_id = resp.json().get("data", {}).get("task_id")
            log_info(f"[Local Stage] 任务提交成功！task_id: {task_id}", "bold green")

            # 轮询获取非流式返回结果
            task_data = await poll_task_status(task_id)
            result = task_data.get("result", {})
            
            if task_data.get("status") == "success":
                text = result.get("text", "").strip()
                duration_ns = result.get("total_duration")
                duration_s = f"{duration_ns / 1e9:.2f}s" if duration_ns else "N/A"
                
                console.print(f"\n[bold yellow]─── 推理结果预览 (本地 Ollama) ───[/bold yellow]")
                console.print(text)
                console.print(f"[dim]推理真实耗时 (Ollama 内核): {duration_s}[/dim]")
                console.print("[bold yellow]──────────────────────────────────────────[/bold yellow]\n")
                log_info("[Local Stage] 本地大模型 Poll 模式验证成功！", "bold green")
            else:
                log_info("[Local Stage] 本地大模型任务执行失败", "bold red")
                
    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请先启动服务: run.bat / main.py", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 测试运行发生异常: {e}", "bold red")

if __name__ == "__main__":
    asyncio.run(main())
