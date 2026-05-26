# -*- coding: utf-8 -*-
"""
TTS + 字幕 一条龙测试
=====================
后端 TTS 服务支持 `generate_subtitle: True`，音频和字幕在一次调用中同时产出。
1. 提交 TTS 任务（文稿 + generate_subtitle: True）
2. 轮询等待完成，获取后端 task_id
3. 通过新静态端点 /static/{tid}/{tid}.wav / .srt 下载
"""
import sys
import time
from pathlib import Path

import requests

# ============================================================
# 可配置项
# ============================================================
KALM_HOST = "127.0.0.1"
KALM_PORT = 7000
INTERFACE_BASE = f"http://{KALM_HOST}:{KALM_PORT}/interface"
FILE_TTS = f"http://{KALM_HOST}:{KALM_PORT}/file/tts"

TTS_PATH = "/v1.5/generate"
TTS_SPEAKER = "解说-魔魔"
TTS_TEXT = (
    "第一个副作用叫视界问题。简单来说曲速飞船是一个盲人司机。"
    "当时空泡形成后，飞船内部就与外部宇宙完全隔绝了。"
)

OUTPUT_DIR = Path(__file__).parent / "output" / "test_tts_subtitle"


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ==================================================================
    # 1. 提交 TTS + 字幕
    # ==================================================================
    print("=" * 60)
    print("提交 TTS + 字幕 (generate_subtitle=True)")
    print(f"  文稿: {TTS_TEXT[:40]}...")
    print("=" * 60)

    r = requests.post(f"{INTERFACE_BASE}/tasks/submit", json={
        "task_type": "tts",
        "payload": {
            "path": TTS_PATH,
            "text": TTS_TEXT,
            "speaker": TTS_SPEAKER,
            "generate_subtitle": True,
        },
    }, timeout=10)
    r.raise_for_status()
    kalm_tid = r.json()["data"]["task_id"]
    print(f"  Kalm task_id: {kalm_tid}\n")

    # ==================================================================
    # 2. 轮询
    # ==================================================================
    print("轮询...")
    backend_tid = None
    started = time.time()
    while time.time() - started < 300:
        time.sleep(2)
        sdata = requests.get(
            f"{INTERFACE_BASE}/tasks/{kalm_tid}/status", timeout=5).json()["data"]
        st = sdata["status"]
        elapsed = int(time.time() - started)

        if st == "success":
            print(f"\n  [{elapsed}s] 完成")
            backend = sdata.get("result", {})
            bdata = backend.get("data", {})
            backend_tid = bdata.get("task_id", "")
            print(f"  backend task_id: {backend_tid}")
            print(f"  [DEBUG] {list(bdata.keys())}")
            break
        elif st == "failed":
            print(f"\n  [{elapsed}s] 失败: {sdata.get('result', {}).get('message', '')}")
            sys.exit(1)
        elif st == "running":
            svc = sdata.get("result", {}).get("status", "?")
            print(f"\r  [{elapsed}s] running (后端: {svc})", end="", flush=True)
        else:
            print(f"\r  [{elapsed}s] {st}", end="", flush=True)
    else:
        print("\n  超时"); sys.exit(1)

    if not backend_tid:
        print("[ERROR] 未获取到后端 task_id")
        sys.exit(1)

    # ==================================================================
    # 3. 下载（新静态端点 /static/{tid}/{tid}.wav / .srt）
    # ==================================================================

    # 下载音频
    print(f"\n── 下载音频 ──")
    audio_url = f"{FILE_TTS}/static/{backend_tid}/{backend_tid}.wav"
    print(f"   {audio_url}")
    r = requests.get(audio_url, timeout=30)
    r.raise_for_status()
    audio_file = OUTPUT_DIR / f"{backend_tid}.wav"
    audio_file.write_bytes(r.content)
    print(f"   [OK] {audio_file.name} ({len(r.content) / 1024:.1f} KB)")

    # 下载字幕
    print(f"\n── 下载字幕 ──")
    srt_url = f"{FILE_TTS}/static/{backend_tid}/{backend_tid}.srt"
    print(f"   {srt_url}")
    r = requests.get(srt_url, timeout=30)
    r.raise_for_status()
    srt_file = OUTPUT_DIR / f"{backend_tid}.srt"
    srt_file.write_bytes(r.content)
    print(f"   [OK] {srt_file.name} ({len(r.content)} bytes)")

    # 预览字幕
    # print(f"\n── 字幕预览 ──")
    # print(r.text.strip())
    # print(f"── 预览结束 ──")

    # ==================================================================
    print(f"\n{'=' * 60}")
    print(f"完成 -> {OUTPUT_DIR}")
    print(f"{'=' * 60}")
    a_size = audio_file.stat().st_size
    s_size = srt_file.stat().st_size
    print(f"  {audio_file.name}  ({a_size / 1024:.1f} KB)")
    print(f"  {srt_file.name}  ({s_size} bytes)")


if __name__ == "__main__":
    main()
