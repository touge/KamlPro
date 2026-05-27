# -*- coding: utf-8 -*-
"""
KamlPro 云端大模型 (DeepSeek) 集成测试用例
==========================================
1. 根据协议，云端大模型同时支持打包轮询模式（Poll）和流式输出模式（Stream）。
2. 第一部分测试：打包轮询模式。提交任务类型为 "llm"，轮询 "/status" 获取完整文本。
3. 第二部分测试：流式输出模式。提交任务类型为 "llm_streaming"，通过 "/stream" 拉取 SSE 流式输出，实时逐词打字机打印。
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import asyncio
import httpx
import json
import datetime
from pathlib import Path
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

async def submit_llm_task(task_type: str, model: str, prompt: str) -> str:
    url = f"{BASE_URL}/interface/tasks/submit"
    payload = {
        "task_type": task_type,
        "payload": {
            "model": model,
            "prompt": prompt,
            "options": {"temperature": 0.3, "max_tokens": 150}
        }
    }
    log_info(f"[Submit] 提交任务: POST {url} | 模型={model} | 任务类型={task_type}", "bold blue")
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Submit failed: {data}")
        return data["data"]["task_id"]

async def poll_task_status(task_id: str, prefix: str = "[云端轮询]") -> dict:
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
            log_info(f"{prefix} 任务 {task_id} 状态: {status} | 已轮询: {elapsed:.1f}s", "dim")
            
            if status == "success":
                log_info(f"{prefix} 任务 {task_id} 执行成功！总耗时: {elapsed:.2f}s", "bold green")
                return task_data
            elif status == "failed":
                err_msg = task_data.get("result", {}).get("message", "未知错误")
                log_info(f"{prefix} 任务 {task_id} 执行失败: {err_msg}", "bold red")
                return task_data
                
            await asyncio.sleep(1.0)

async def stream_output(task_id: str, prefix: str = ""):
    url = f"{BASE_URL}/interface/tasks/{task_id}/stream"
    pfx = f"{prefix} " if prefix else ""
    log_info(f"{pfx}>>> 发起 HTTP 流式请求: {url}", "bold cyan")
    
    start_time = asyncio.get_event_loop().time()
    first_token_time = None
    
    async with httpx.AsyncClient() as client:
        try:
            async with client.stream("GET", url, headers=HEADERS, timeout=30) as response:
                if response.status_code != 200:
                    log_info(f"{pfx}流式连接失败, HTTP {response.status_code}", "bold red")
                    return
                
                log_info(f"{pfx}>>> 成功建立流式通道，正在接收推理 Token...", "dim")
                console.print(f"\n[bold yellow]─── 推理流输出 ({prefix}) ───[/bold yellow]")
                
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("response", "")
                        
                        if first_token_time is None and token.strip():
                            first_token_time = asyncio.get_event_loop().time() - start_time
                            
                        print(token, end="", flush=True)
                        if chunk.get("done"):
                            break
                    except json.JSONDecodeError:
                        pass
                print()
                console.print("[bold yellow]──────────────────────────────────────────[/bold yellow]\n")
                
                total_time = asyncio.get_event_loop().time() - start_time
                ttft_str = f"首字延迟 (TTFT): {first_token_time:.2f}s | " if first_token_time else ""
                log_info(f"{pfx}<<< 流式接收全部完成! {ttft_str}网络拉取总耗时: {total_time:.2f}s", "bold green")
        except Exception as e:
            log_info(f"{pfx}流式读取发生异常: {e}", "bold red")

async def main():
    log_info("==========================================================", "bold yellow")
    log_info("        Kalm 云端大模型 (DeepSeek / Poll与Stream双模式) 测试用例  ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    log_info(f"   目标模型     : {TEST_MODEL}")
    log_info(f"   说明         : 云端模型支持打包轮询模式（Poll）与流式模式（Stream）")
    
    try:
        # 🟡 第一阶段：打包轮询模式测试 (Poll)
        log_info("\n" + "=" * 80, "bold green")
        log_info("阶段 1: 测试云端大语言模型打包轮询模式 (Poll)", "bold green")
        log_info("=" * 80, "bold green")
        
        prompt_poll = "用一句话说明为什么天空是蓝色的。"
        poll_task_id = await submit_llm_task("llm", TEST_MODEL, prompt_poll)
        
        # 轮询获取返回结果
        task_data = await poll_task_status(poll_task_id)
        if task_data.get("status") == "success":
            text = task_data.get("result", {}).get("text", "").strip()
            console.print(f"\n[bold yellow]─── 打包轮询结果预览 (云端 DeepSeek) ───[/bold yellow]")
            console.print(text)
            console.print("[bold yellow]──────────────────────────────────────────[/bold yellow]\n")
            log_info("[Cloud Stage] 阶段 1 (Poll) 验证成功！", "bold green")
        else:
            log_info("[Cloud Stage] 阶段 1 (Poll) 任务执行失败", "bold red")
            
        # 🟡 第二阶段：流式输出模式测试 (Stream)
        log_info("\n" + "=" * 80, "bold green")
        log_info("阶段 2: 测试云端大语言模型流式输出模式 (Stream)", "bold green")
        log_info("=" * 80, "bold green")
        
        prompt_stream = "请用一句非常简短的话解释什么是量子计算。"
        stream_task_id = await submit_llm_task("llm_streaming", TEST_MODEL, prompt_stream)
        
        # 发起 HTTP SSE 流式拉取并在控制台流式渲染打印 Token
        await stream_output(stream_task_id, prefix="云端-DeepSeek")
        log_info("[Cloud Stage] 阶段 2 (Stream) 验证成功！", "bold green")
        
        log_info("\n" + "=" * 80, "bold yellow")
        log_info("   测试完成！云端大模型已通过 Poll 与 Stream 双协议模式验证成功！   ", "bold yellow")
        log_info("=" * 80, "bold yellow")

    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请先启动服务: run.bat / main.py", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 测试运行发生异常: {e}", "bold red")

if __name__ == "__main__":
    asyncio.run(main())
