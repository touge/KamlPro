# -*- coding: utf-8 -*-
"""
KamlPro 配音与字幕 (TTS & Subtitle) 整合调度集成测试用例
======================================================
参考 test_hybrid_stream.py 风格，使用 httpx 与 asyncio 实现高精度并发/轮询日志打印。

测试流程：
1. 提交并轮询 TTS 任务（语音合成）直到成功，下载合成的语音 WAV 和字幕 SRT 产物。
2. 提交并轮询 Subtitle 任务（音频-文本字幕强制对齐）直到成功，下载对齐的字幕 SRT 产物。
3. 产物将分类下载并归入 test/20260527/outputs/ 目录中对应的子目录里。
"""

import asyncio
import httpx
import datetime
from pathlib import Path
from rich.console import Console

# 显式创建 Console 实例，完美兼容 GBK 与 UTF-8 终端
console = Console()

BASE_URL = "http://127.0.0.1:7000"
HEADERS = {
    "Authorization": "Bearer 123456"
}

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


async def poll_task_status(task_id: str, prefix: str = "[轮询]") -> dict:
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


async def run_tts_scenario():
    """TTS 语音合成场景测试并下载文件"""
    log_info("\n" + "=" * 60, "bold green")
    log_info("场景 1: 提交 TTS 语音合成任务、轮询状态并下载音频与字幕文件", "bold green")
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
        log_info(f"[TTS] 任务提交成功！分配 task_id: {task_id}", "bold green")

        # 轮询状态
        result = await poll_task_status(task_id, prefix="[TTS 轮询]")
        backend_info = result.get("result", {})
        backend_data = backend_info.get("data", {})
        backend_tid = backend_data.get("task_id")

        if not backend_tid:
            log_info("[TTS] [ERROR] 未获取到后端执行的真正 task_id", "bold red")
            return

        log_info(f"[TTS] 后端成功运行 task_id: {backend_tid}", "cyan")

        # ---- 1. 下载音频文件 ----
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

        # ---- 2. 下载同步生成的字幕文件 ----
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
    """Subtitle 强制字幕时间戳对齐场景测试并下载文件"""
    log_info("\n" + "=" * 60, "bold green")
    log_info("场景 2: 上传本地 WAV 并提交 Subtitle 对齐任务，轮询并下载字幕文件", "bold green")
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
        log_info(f"[Subtitle] 任务提交成功！分配 task_id: {task_id}", "bold green")

        # 轮询状态
        result = await poll_task_status(task_id, prefix="[Subtitle 轮询]")
        
        # 轮询成功后，提取后端 ID 并执行文件保存
        backend_data = result.get("result", {}).get("data", {})
        backend_tid = backend_data.get("task_id")
        if backend_tid:
            # 使用统一的静态文件访问 URL 进行下载与持久化保存
            srt_url = f"{BASE_URL}/file/tts/static/{backend_tid}/subtitle.srt"
            log_info(f"[Subtitle] 正在获取并下载生成的字幕文件: {srt_url}", "yellow")
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
    log_info("   Kalm 音频与字幕集成测试客户端 (基于协程 HTTPX)    ", "bold yellow")
    log_info("==========================================================", "bold yellow")
    log_info(f"   目标网关地址 : {BASE_URL}")
    log_info(f"   认证 Token   : Bearer 123456")
    
    try:
        # 1. 执行场景 1: TTS 并自动下载产物至 outputs/tts/
        await run_tts_scenario()
        
        # 2. 执行场景 2: Subtitle 并自动下载产物至 outputs/subtitle/
        await run_subtitle_scenario()
        
    except httpx.ConnectError:
        log_info("\n[ERROR] 无法连接到 Kalm 网关，请先启动服务: run.bat / main.py", "bold red")
    except Exception as e:
        log_info(f"\n[ERROR] 测试出现未预期异常: {e}", "bold red")


if __name__ == "__main__":
    asyncio.run(main())
