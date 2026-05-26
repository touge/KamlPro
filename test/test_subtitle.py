# -*- coding: utf-8 -*-
"""
字幕独立测试
============
上传本地音频 + 文稿 → 生成 SRT 字幕
通过 Kalm 任务系统提交，产物下载到本地。
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
FILE_PROXY_BASE = f"http://{KALM_HOST}:{KALM_PORT}/file"

# 输入文件
AUDIO_FILE = Path(__file__).parent / "file" / "test_subtitle.wav"
TEXT_FILE = Path(__file__).parent / "file" / "test_subtitle.md"

# 输出目录
OUTPUT_DIR = Path(__file__).parent / "output" / "test_subtitle"


def main():
    if not AUDIO_FILE.exists():
        print(f"[ERROR] 音频文件不存在: {AUDIO_FILE}")
        sys.exit(1)
    if not TEXT_FILE.exists():
        print(f"[ERROR] 文稿文件不存在: {TEXT_FILE}")
        sys.exit(1)

    text = TEXT_FILE.read_text(encoding="utf-8").strip()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ==================================================================
    # 阶段一：multipart 上传音频 + 文稿
    # ==================================================================
    print("=" * 60)
    print("提交字幕任务")
    print(f"  音频: {AUDIO_FILE.name}  ({AUDIO_FILE.stat().st_size / 1024:.1f} KB)")
    print(f"  文稿: {text}")
    print("=" * 60)

    with open(AUDIO_FILE, "rb") as f:
        resp = requests.post(
            f"{INTERFACE_BASE}/tasks/submit",
            files={"audio_file": (AUDIO_FILE.name, f, "audio/wav")},
            data={"task_type": "subtitle", "text": text},
            timeout=10,
        )
    resp.raise_for_status()
    kalm_tid = resp.json()["data"]["task_id"]
    print(f"  Kalm task_id: {kalm_tid}")
    print()

    # ==================================================================
    # 阶段二：轮询
    # ==================================================================
    print("轮询等待...")
    backend_tid = None
    started = time.time()
    while time.time() - started < 300:
        resp = requests.get(f"{INTERFACE_BASE}/tasks/{kalm_tid}/status", timeout=5)
        sdata = resp.json()["data"]
        status = sdata["status"]
        elapsed = int(time.time() - started)

        if status == "success":
            print(f"\n  [{elapsed}s] 完成")
            backend = sdata.get("result", {})
            bdata = backend.get("data", {})
            backend_tid = bdata.get("task_id", "")
            print(f"  backend task_id: {backend_tid}")
            break
        elif status == "failed":
            msg = sdata.get("result", {}).get("message", "")
            print(f"\n  [{elapsed}s] 失败: {msg}")
            sys.exit(1)
        elif status == "running":
            r = sdata.get("result", {})
            svc = r.get("status", "?")
            print(f"\r  [{elapsed}s] running (后端: {svc})", end="", flush=True)
        else:
            print(f"\r  [{elapsed}s] {status}", end="", flush=True)
        time.sleep(2)
    else:
        print("\n  超时 (300s)")
        sys.exit(1)

    if not backend_tid:
        print("[ERROR] 未获取到后端 task_id")
        sys.exit(1)

    # ==================================================================
    # 阶段三：下载字幕（新静态端点，独立字幕产出文件名为 subtitle.srt）
    # ==================================================================
    srt_filename = "subtitle.srt"
    srt_static = f"/static/{backend_tid}/{srt_filename}"
    print(f"\n下载字幕 {srt_static}...")
    srt_url = f"{FILE_PROXY_BASE}/tts{srt_static}"
    print(f"  {srt_url}")
    r = requests.get(srt_url, timeout=10)
    r.raise_for_status()

    srt_file = OUTPUT_DIR / f"{backend_tid}.srt"
    srt_file.write_bytes(r.content)
    print(f"  [OK] {srt_file.name} ({len(r.content)} bytes)")

    # 预览
    print(f"\n── 字幕预览 ──")
    print(r.text.strip())
    print(f"── 预览结束 ──")

    print(f"\n{'=' * 60}")
    print(f"完成 -> {OUTPUT_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
