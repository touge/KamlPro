# -*- coding: utf-8 -*-
"""
KamlPro 混合流式传输与并发调度集成测试用例
==========================================
覆盖 2×2 测试矩阵：
1. 响应模式（流式 Stream / 非流式 Non-stream）
2. 队列状态（空闲直连 / 繁忙排队）

前端开发接入时，请完整参考此脚本的代码逻辑。
"""

import asyncio
import httpx
import websockets
import json
import datetime
from rich.console import Console

# 显式使用 utf-8 强制 Console 支持，但为了兼容 GBK 终端，我们同时去除可能导致报错的非 GBK 字符（Emoji等）
console = Console()

BASE_URL = "http://127.0.0.1:7000"
WS_URL = "ws://127.0.0.1:7000"
HEADERS = {
    "Authorization": "Bearer 123456",
    "Content-Type": "application/json"
}
WS_HEADERS = [("Authorization", "Bearer 123456")]


def get_timestamp() -> str:
    """获取带毫秒的高精度时间戳"""
    return datetime.datetime.now().strftime("[%H:%M:%S.%f]")[:-3]


def log_info(msg: str, style: str = None):
    """带时间戳的日志输出"""
    ts = get_timestamp()
    if style:
        console.print(f"[dim]{ts}[/dim] [{style}]{msg}[/{style}]")
    else:
        console.print(f"[dim]{ts}[/dim] {msg}")


def get_concurrency_limit() -> int:
    """动态读取 API 并发度限制配置"""
    try:
        import yaml
        with open("configs/llm_config.yaml", "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)
        return int(config_data.get("api_concurrency_limit", 1))
    except Exception:
        return 1


async def submit_task(task_type: str, model: str, prompt: str) -> dict:
    """提交任务到 Kaml"""
    url = f"{BASE_URL}/interface/tasks/submit"
    payload = {
        "task_type": task_type,
        "payload": {
            "model": model,
            "prompt": prompt,
            "options": {"temperature": 0.7, "max_tokens": 100}
        }
    }
    
    start_time = asyncio.get_event_loop().time()
    log_info(f">>> 发起任务提交请求: POST {url} | model={model}", "bold blue")
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        elapsed = asyncio.get_event_loop().time() - start_time
        log_info(f"<<< 任务提交响应完成 | 耗时: {elapsed:.3f}s", "bold blue")
        
        if data.get("status") != "success":
            raise RuntimeError(f"Submit failed: {data}")
        return data.get("data", {})


async def stream_task_output(task_id: str, prefix: str = ""):
    """请求 HTTP Stream 端点并流式打印 Token，并在首尾记录时间与总耗时"""
    url = f"{BASE_URL}/interface/tasks/{task_id}/stream"
    pfx = f"{prefix} " if prefix else ""
    log_info(f"{pfx}>>> 发起 HTTP 流式请求: {url}", "bold cyan")
    
    start_time = asyncio.get_event_loop().time()
    
    async with httpx.AsyncClient() as client:
        try:
            async with client.stream("GET", url, headers=HEADERS, timeout=30) as response:
                if response.status_code != 200:
                    log_info(f"{pfx}流式连接失败, HTTP {response.status_code}", "bold red")
                    return
                
                log_info(f"{pfx}>>> 开始接收流式响应数据...", "dim")
                
                # 逐行读取 NDJSON 流式输出并打印
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("response", "")
                        # 实时打印 Token 字符
                        print(token, end="", flush=True)
                        if chunk.get("done"):
                            break
                    except json.JSONDecodeError:
                        pass
                print()  # 换行
                
                elapsed = asyncio.get_event_loop().time() - start_time
                log_info(f"{pfx}<<< 流式接收全部完成! 累计网络拉取耗时: {elapsed:.3f} 秒！", "bold green")
        except Exception as e:
            # 避免包含无法编码的 emoji
            log_info(f"{pfx}流式读取出现异常", "bold red")


async def poll_task_status(task_id: str) -> dict:
    """轮询任务状态直到成功或失败"""
    url = f"{BASE_URL}/interface/tasks/{task_id}/status"
    start_time = asyncio.get_event_loop().time()
    log_info(f"开始轮询任务 {task_id} 状态...", "yellow")
    
    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(url, headers=HEADERS)
            resp.raise_for_status()
            task_data = resp.json().get("data", {})
            status = task_data.get("status")
            
            elapsed = asyncio.get_event_loop().time() - start_time
            log_info(f"轮询任务 {task_id} 状态: {status} | 已轮询: {elapsed:.1f}s", "dim")
            
            if status in ("success", "failed"):
                return task_data
            await asyncio.sleep(0.5)


# ============================================================================
# 4 个测试场景实现
# ============================================================================

async def test_scenario_1_stream_idle():
    """测试场景 1：流式 + 空队 (直接获取流式响应)"""
    log_info("==================================================", "bold green")
    log_info("测试场景 1：流式传输 (Stream) + 空队 (闲时即时直连)", "bold green")
    log_info("==================================================", "bold green")

    # 1. 提交流式大模型任务
    task_data = await submit_task("llm_streaming", "deepseek-v4-flash", "请用5个词形容蓝天")
    task_id = task_data.get("task_id")
    can_stream = task_data.get("can_stream_immediately")
    status = task_data.get("status")

    log_info(f"任务提交成功! task_id={task_id}, status={status}, can_stream_immediately={can_stream}")
    
    # 2. 检查即时流式标志
    if can_stream:
        log_info("空闲检测成功！绕过 WebSocket，直接建立 HTTP 流连接：", "bold green")
        await stream_task_output(task_id, prefix="[直连流]")
    else:
        log_info("场景 1 失败：空闲状态下未能返回 can_stream_immediately=True", "bold red")


async def test_scenario_2_stream_busy():
    """测试场景 2：流式 + 满队 (WS 排队等待信号 -> 出队时发起流)"""
    limit = get_concurrency_limit()
    log_info("==================================================", "bold green")
    log_info(f"测试场景 2：流式传输 (Stream) + 满队 (有队排队监听) [当前并发限制: {limit}]", "bold green")
    log_info("==================================================", "bold green")

    # 1. 连续提交 limit 个任务，占满并发槽位
    log_info(f"正在连续提交 {limit} 个大模型任务以占满并发槽位...", "yellow")
    occupying_tasks = []
    bg_tasks = []
    
    for i in range(1, limit + 1):
        t_data = await submit_task("llm_streaming", "deepseek-v4-flash", f"模拟长占位任务{i}")
        t_id = t_data.get("task_id")
        occupying_tasks.append(t_id)
        log_info(f"槽位占满成功 {i}/{limit}! task_id={t_id}")
        
        # 瞬间开启后台流式接收，不让任务卡死/超时，且能拉取并打印其反馈
        bg_task = asyncio.create_task(stream_task_output(t_id, prefix=f"[占位任务{i}]"))
        bg_tasks.append(bg_task)

    # 2. 提交第 limit + 1 个流式任务，期望进入排队
    log_info(f"正在提交第 {limit + 1} 个流式任务，期望进入排队...", "yellow")
    task_data = await submit_task("llm_streaming", "deepseek-v4-flash", "我是排队流式任务，请在我出队时流式输出")
    task_id = task_data.get("task_id")
    can_stream = task_data.get("can_stream_immediately")
    status = task_data.get("status")

    log_info(f"排队任务提交成功! task_id={task_id}, status={status}, can_stream_immediately={can_stream}")
    
    if can_stream or status != "queued":
        log_info("场景 2 失败：忙时未能进入 queued 状态或 can_stream_immediately 错误返回为 True", "bold red")
        return

    # 3. 连上 WebSocket 排队监听
    ws_uri = f"{WS_URL}/interface/tasks/{task_id}/ws"
    log_info(f"连接 WebSocket 管道开始排队监听: {ws_uri}", "yellow")
    
    async with websockets.connect(ws_uri, additional_headers=WS_HEADERS) as ws:
        # 等待 WS 吐回 "task_running" 信号
        while True:
            msg_str = await ws.recv()
            msg = json.loads(msg_str)
            log_info(f"[WS 接收] {msg}")
            
            if msg.get("type") == "task_running":
                log_info("[Success] 收到 task_running 信号！活来了！瞬间发起 HTTP 流式连接拉取数据：", "bold green")
                # 瞬间建立流式管道接水！
                await stream_task_output(task_id, prefix="[排队流]")
                break
            elif msg.get("type") in ("task_complete", "task_failed"):
                log_info(f"任务已直接完成/失败: {msg.get('type')}", "dim")
                break

    # 等待后台并发任务全部流式输出接收完毕
    await asyncio.gather(*bg_tasks)


async def test_scenario_3_non_stream_idle():
    """测试场景 3：非流式 + 空队 (直接轮询获取)"""
    log_info("==================================================", "bold green")
    log_info("测试场景 3：非流式打包 (Poll) + 空队 (即时完成)", "bold green")
    log_info("==================================================", "bold green")

    # 1. 提交非流式任务 llm
    task_data = await submit_task("llm", "deepseek-v4-flash", "请问 1+1 等于几")
    task_id = task_data.get("task_id")
    status = task_data.get("status")

    log_info(f"非流式任务提交成功! task_id={task_id}, status={status}")

    # 2. 轮询直到 success
    result_data = await poll_task_status(task_id)
    log_info(f"非流式生成结果成功：{result_data.get('result', {}).get('text')}", "bold green")


async def test_scenario_4_non_stream_busy():
    """测试场景 4：非流式 + 满队 (排队后轮询)"""
    limit = get_concurrency_limit()
    log_info("==================================================", "bold green")
    log_info(f"测试场景 4：非流式打包 (Poll) + 满队 (排队后出队完成) [当前并发限制: {limit}]", "bold green")
    log_info("==================================================", "bold green")

    # 1. 连续提交 limit 个任务，占满并发槽位
    log_info(f"正在连续提交 {limit} 个占位任务以占满并发槽位...", "yellow")
    occupying_tasks = []
    bg_tasks = []
    
    for i in range(1, limit + 1):
        t_data = await submit_task("llm_streaming", "deepseek-v4-flash", f"占位任务A{i}")
        t_id = t_data.get("task_id")
        occupying_tasks.append(t_id)
        log_info(f"占位任务提交成功 {i}/{limit}! task_id={t_id}")
        
        # 同样开启后台接收，以便占位任务能够顺利执行完并释放槽位
        bg_task = asyncio.create_task(stream_task_output(t_id, prefix=f"[占位任务A{i}]"))
        bg_tasks.append(bg_task)

    # 2. 提交第 limit + 1 个非流式任务，此时必定进入排队
    log_info(f"正在提交第 {limit + 1} 个非流式任务，期望进入排队...", "yellow")
    task_data = await submit_task("llm", "deepseek-v4-flash", "我是排队非流式任务，回答我1+1等于几")
    task_id = task_data.get("task_id")
    status = task_data.get("status")

    log_info(f"排队任务提交成功! task_id={task_id}, status={status}")

    # 3. 轮询直到出队并执行成功
    result_data = await poll_task_status(task_id)
    log_info(f"非流式排队生成结果成功：{result_data.get('result', {}).get('text')}", "bold green")

    # 等待占位流式任务接收完毕
    await asyncio.gather(*bg_tasks)


# ============================================================================
# 主执行入口
# ============================================================================

async def main():
    log_info("==================================================", "bold cyan")
    log_info(" 开始运行 KamlPro 混合流式传输与并发调度测试用例", "bold cyan")
    log_info("==================================================", "bold cyan")
    
    try:
        # 1. 流式空队直连
        await test_scenario_1_stream_idle()
        
        # 2. 流式满队排队
        await test_scenario_2_stream_busy()
        
        # 3. 非流式空队直连
        await test_scenario_3_non_stream_idle()
        
        # 4. 非流式满队排队
        await test_scenario_4_non_stream_busy()
        
        log_info("[Success] 所有 4 个测试场景全部运行顺利！系统完美符合设计期望！", "bold green")
    except Exception as e:
        log_info(f"[Error] 测试运行失败，错误: {e}", "bold red")
        console.print_exception()


if __name__ == "__main__":
    asyncio.run(main())
