# -*- coding: utf-8 -*-
"""
KamlPro 配音与字幕 (TTS & Subtitle) 自适应 WebSocket 实时推送集成测试用例
========================================================================
1. 根据协议，TTS 与 Subtitle 被动受本地 GPU 串行轨道调度。
2. 本示例为【WebSocket 优先自适应接入规范】。
3. 客户端在提交任务后，读取网关返回的 `track_mode`：
   - 若为 "ws"：直接建立 WebSocket 管道实时接收网关伪装的完成信号。
   - 若为 "poll"：智能自动降级采用 HTTP GET `/status` 周期轮询获取产物。
4. 本用例能完美抵御后端任务配置变更，下游厂商可直接参考本脚本编写集成 SDK。
"""

import asyncio
import httpx
import websockets
import json
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
AUDIO_FILE = TEST_ROOT / "file" / "test_subtitle.wav"
TEXT_FILE = TEST_ROOT / "file" / "test_subtitle.md"

OUTPUT_ROOT = Path(__file__).parent / "outputs"
TTS_OUTPUT_DIR = OUTPUT_ROOT / "tts"
SUBTITLE_OUTPUT_DIR = OUTPUT_ROOT / "subtitle"


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


async def listen_task_via_ws(task_id: str, prefix: str = "[WS监听]") -> dict:
    """策略 1：建立 WebSocket 管道，被动接收网关推送的进度消息直到任务结束"""
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


async def poll_task_status(task_id: str, prefix: str = "[HTTP轮询]") -> dict:
    """策略 2 (自动降级)：异步轮询任务状态直到成功或失败"""
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
                return {"result": task_data.get("result", {})}
            elif status == "failed":
                err_msg = task_data.get("result", {}).get("message", "未知错误")
                log_info(f"{prefix} 任务 {task_id} 执行失败: {err_msg}", "bold red")
                raise RuntimeError(f"Task execution failed: {err_msg}")
                
            await asyncio.sleep(2.0)


async def run_tts_scenario():
    """TTS 语音合成场景测试 (自适应路由)"""
    log_info("\n" + "=" * 60, "bold green")
    log_info("场景 1: 提交 TTS 语音合成，自适应策略接收网关进度并下载产物", "bold green")
    log_info("=" * 60, "bold green")

    # 创建输出目录
    TTS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    url = f"{BASE_URL}/interface/tasks/submit"
    payload = {
        "task_type": "tts",
        "payload": {
            "text": "欢迎使用 Kalm 智能配音平台，这是一款轻量而强大的 AIGC 调度网关系统。",
            "speaker": "解说-魔魔",
            "generate_subtitle": True
        }
    }

    log_info(f"[TTS] 发起任务提交请求: POST {url}", "bold blue")
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get("status") != "success":
            log_info(f"[TTS] 提交失败: {data}", "bold red")
            return

        task_id = data.get("data", {}).get("task_id")
        track_mode = data.get("data", {}).get("track_mode", "poll")
        log_info(f"[TTS] 任务提交成功！分配 task_id: {task_id} | 路由 track_mode: {track_mode}", "bold green")

        # ---- 自适应选择拉取策略 ----
        if track_mode == "ws":
            log_info("[TTS] 选择策略：通过 WebSocket 实时监听任务完成状态...", "cyan")
            ws_msg = await listen_task_via_ws(task_id, prefix="[TTS WS]")
            backend_info = ws_msg.get("result", {})
        else:
            log_info("[TTS] 选择策略：通过 HTTP /status 接口周期性轮询获取状态...", "cyan")
            poll_res = await poll_task_status(task_id, prefix="[TTS 轮询]")
            backend_info = poll_res.get("result", {})

        backend_data = backend_info.get("data", {})
        backend_tid = backend_data.get("task_id")

        if not backend_tid:
            log_info("[TTS] [ERROR] 未能从推送/轮询结果中获取到后端真正 task_id", "bold red")
            return

        log_info(f"[TTS] 最终获取后端 task_id: {backend_tid}", "cyan")

        # ---- 1. 下载配音 WAV 音频 ----
        file_url = backend_data.get("file_url") or backend_data.get("download_url")
        if file_url:
            audio_dl_url = f"{BASE_URL}/file/tts{file_url}"
            log_info(f"[TTS] 正在下载生成的配音音频: {audio_dl_url}", "yellow")
            try:
                audio_resp = await client.get(audio_dl_url, headers=HEADERS, timeout=30)
                if audio_resp.status_code == 200:
                    audio_dest = TTS_OUTPUT_DIR / f"{backend_tid}.wav"
                    audio_dest.write_bytes(audio_resp.content)
                    log_info(f"[TTS] [OK] 音频保存成功 -> {audio_dest} ({len(audio_resp.content)/1024:.1f} KB)", "bold green")
                else:
                    log_info(f"[TTS] [ERROR] 下载音频失败，状态码: {audio_resp.status_code}", "bold red")
            except Exception as e:
                log_info(f"[TTS] [ERROR] 下载音频发生异常: {e}", "bold red")

        # ---- 2. 下载同步生成的 SRT 字幕 ----
        subtitle_url = backend_data.get("subtitle_url")
        if subtitle_url:
            sub_dl_url = f"{BASE_URL}/file/tts{subtitle_url}"
            log_info(f"[TTS] 正在下载同步生成的字幕: {sub_dl_url}", "yellow")
            try:
                sub_resp = await client.get(sub_dl_url, headers=HEADERS, timeout=15)
                if sub_resp.status_code == 200:
                    sub_dest = TTS_OUTPUT_DIR / f"{backend_tid}.srt"
                    sub_dest.write_bytes(sub_resp.content)
                    log_info(f"[TTS] [OK] 字幕保存成功 -> {sub_dest} ({len(sub_resp.content)} bytes)", "bold green")
                else:
                    log_info(f"[TTS] [ERROR] 下载字幕失败，状态码: {sub_resp.status_code}", "bold red")
            except Exception as e:
                log_info(f"[TTS] [ERROR] 下载字幕发生异常: {e}", "bold red")


async def run_subtitle_scenario():
    """Subtitle 强制字幕时间戳对齐场景测试 (自适应路由)"""
    log_info("\n" + "=" * 60, "bold green")
    log_info("场景 2: 上传本地 WAV 并提交对齐任务，自适应路由接收并下载字幕", "bold green")
    log_info("=" * 60, "bold green")

    # 创建输出目录
    SUBTITLE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not AUDIO_FILE.exists() or not TEXT_FILE.exists():
        log_info(f"[Subtitle] [ERROR] 缺少测试素材，请确保存在以下路径：\n音频: {AUDIO_FILE}\n文稿: {TEXT_FILE}", "bold red")
        return

    text = TEXT_FILE.read_text(encoding="utf-8").strip()
    log_info(f"[Subtitle] 读取音频: {AUDIO_FILE.name} ({AUDIO_FILE.stat().st_size / 1024:.1f} KB)")
    log_info(f"[Subtitle] 读取文稿: \"{text}\"")

    url = f"{BASE_URL}/interface/tasks/submit"
    
    # 模拟 multipart/form-data 格式上传音频
    files = {
        "audio_file": (AUDIO_FILE.name, open(AUDIO_FILE, "rb"), "audio/wav")
    }
    data = {
        "task_type": "subtitle",
        "text": text
    }

    log_info(f"[Subtitle] 发起 Multipart 上传任务请求: POST {url}", "bold blue")
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=HEADERS, files=files, data=data, timeout=15)
        resp.raise_for_status()
        resp_data = resp.json()
        
        if resp_data.get("status") != "success":
            log_info(f"[Subtitle] 提交失败: {resp_data}", "bold red")
            return

        task_id = resp_data.get("data", {}).get("task_id")
        track_mode = resp_data.get("data", {}).get("track_mode", "poll")
        log_info(f"[Subtitle] 任务提交成功！分配 task_id: {task_id} | 路由 track_mode: {track_mode}", "bold green")

        # ---- 自适应选择对齐拉取策略 ----
        if track_mode == "ws":
            log_info("[Subtitle] 选择策略：通过 WebSocket 实时听取对齐进度...", "cyan")
            ws_msg = await listen_task_via_ws(task_id, prefix="[Subtitle WS]")
            backend_info = ws_msg.get("result", {})
        else:
            log_info("[Subtitle] 选择策略：通过 HTTP /status 接口周期性轮询...", "cyan")
            poll_res = await poll_task_status(task_id, prefix="[Subtitle 轮询]")
            backend_info = poll_res.get("result", {})
        
        backend_data = backend_info.get("data", {})
        backend_tid = backend_data.get("task_id")
        
        if backend_tid:
            # 下载对齐的 SRT 字幕
            srt_url = f"{BASE_URL}/file/tts/static/{backend_tid}/subtitle.srt"
            log_info(f"[Subtitle] 正在获取并下载生成的对齐字幕: {srt_url}", "yellow")
            try:
                sub_resp = await client.get(srt_url, headers=HEADERS, timeout=15)
                if sub_resp.status_code == 200:
                    # 1. 打印预览
                    console.print(f"\n[bold yellow]─── 字幕预览 ({backend_tid}) ───[/bold yellow]")
                    console.print(sub_resp.text.strip())
                    console.print("[bold yellow]────────────────────────────────────[/bold yellow]\n")

                    # 2. 持久化保存到 outputs/subtitle/ 目录下
                    sub_dest = SUBTITLE_OUTPUT_DIR / f"{backend_tid}.srt"
                    sub_dest.write_bytes(sub_resp.content)
                    log_info(f"[Subtitle] [OK] 字幕文件对齐保存成功 -> {sub_dest} ({len(sub_resp.content)} bytes)", "bold green")
                else:
                    log_info(f"[Subtitle] [ERROR] 字幕获取失败, 状态码: {sub_resp.status_code}", "bold red")
            except Exception as e:
                log_info(f"[Subtitle] [ERROR] 下载字幕出错: {e}", "bold red")


async def main():
    log_info("==========================================================", "bold yellow")
    log_info("   Kalm 音频与字幕 WS-自适应集成测试客户端 (基于 websockets)  ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    log_info(f"   目标网关地址 : {BASE_URL}")
    log_info(f"   WS 推送地址  : {WS_URL}")
    
    try:
        # 1. 执行场景 1: TTS 并下载产物
        await run_tts_scenario()
        
        # 2. 执行场景 2: Subtitle 并下载产物
        await run_subtitle_scenario()
        
    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请先启动服务: run.bat / main.py", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 测试出现未预期异常: {e}", "bold red")


if __name__ == "__main__":
    asyncio.run(main())
