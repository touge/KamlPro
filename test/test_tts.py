# -*- coding: utf-8 -*-
"""
TTS 端到端测试 (IndexTTS V1.5)
===============================
HTTP 轮询模式：提交 → 轮询 status → 下载音频 → (可选) 生成字幕
"""
import sys
import time
from pathlib import Path

import requests

# ============================================================
# 可配置项 — 所有 URL 集中管理，方便修改
# ============================================================
KALM_HOST = "127.0.0.1"
KALM_PORT = 7000
KALM_BASE = f"http://{KALM_HOST}:{KALM_PORT}"
INTERFACE_BASE = f"{KALM_BASE}/interface"       # 任务提交 / 状态查询
FILE_PROXY_BASE = f"{KALM_BASE}/file"           # 文件代理
TTS_FILE_URL = f"{FILE_PROXY_BASE}/tts"         # TTS 音频下载
SUBTITLE_FILE_URL = f"{FILE_PROXY_BASE}/tts"    # 字幕下载 (同服务)

OUTPUT_DIR = Path(__file__).parent / "tts" / "files"

# 是否同时生成字幕 (True=语音+字幕, False=仅语音)
SUBTITLE_ENABLED = True


def wait_for_done(task_id, poll_interval=2, timeout=300):
    started = time.time()
    while time.time() - started < timeout:
        resp = requests.get(f"{INTERFACE_BASE}/tasks/{task_id}/status", timeout=5)
        sdata = resp.json()["data"]
        status = sdata["status"]
        elapsed = int(time.time() - started)

        if status == "success":
            print(f"\n   [{elapsed}s] 任务完成")
            return sdata
        elif status == "failed":
            print(f"\n   [{elapsed}s] 任务失败: {sdata.get('result', {}).get('message', '')}")
            return None
        elif status == "running":
            r = sdata.get("result", {})
            svc_status = r.get("status", "?")
            print(f"\r   [{elapsed}s] running | 后端: {svc_status}", end="", flush=True)
        else:
            print(f"\r   [{elapsed}s] {status}...", end="", flush=True)

        time.sleep(poll_interval)

    print(f"\n   等待超时 ({timeout}s)")
    return None


def download_audio(result):
    """从 result 中提取 download_url / file_url，通过代理下载音频到本地"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    backend = result.get("result", {})
    bdata = backend.get("data", {})
    file_url = bdata.get("file_url")           # 如 /files/xxx（持久24h，优先）
    download_url = bdata.get("download_url")  # 如 /download/xxx（自动清理）

    url_path = file_url or download_url
    if not url_path:
        print("   未找到下载链接 (data.download_url / data.file_url)")
        return 0, None

    # 通过 Kalm 文件代理下载到本地
    proxy_url = f"{TTS_FILE_URL}{url_path}"
    filename = "tts_output.wav"
    dest = OUTPUT_DIR / filename

    print(f"   下载音频到本地: {proxy_url}")

    r = requests.get(proxy_url, timeout=30)
    if r.status_code == 200:
        dest.write_bytes(r.content)
        size_kb = len(r.content) / 1024
        print(f"   [OK] 已保存: {dest} ({size_kb:.1f} KB)")
        return 1, dest  # 返回本地文件路径
    else:
        print(f"   [ERROR] HTTP {r.status_code}")
        return 0, None


def submit_subtitle(text, local_audio_path):
    """提交字幕生成任务。multipart 上传本地音频文件 + 文稿"""
    print(f"\n   提交字幕任务: audio={local_audio_path.name}, text={text[:30]}...")
    with open(local_audio_path, "rb") as f:
        resp = requests.post(
            f"{INTERFACE_BASE}/tasks/submit",
            files={"audio_file": (local_audio_path.name, f, "audio/wav")},
            data={"task_type": "subtitle", "text": text},
            timeout=10,
        )
    resp.raise_for_status()
    data = resp.json()["data"]
    print(f"   subtitle task_id: {data['task_id']}")
    print(f"   track_mode: {data['track_mode']}")
    print(f"   hint: {data['hint']}")
    return data["task_id"]


def download_subtitle(result):
    """从字幕任务的 result 中提取 subtitle_url / file_url，下载 SRT 文件"""
    backend = result.get("result", {})
    bdata = backend.get("data", {})

    subtitle_url = bdata.get("subtitle_url")   # /download/subtitle/xxx
    s_file_url = bdata.get("file_url")          # /files/xxx（备用）
    s_dl_url = bdata.get("download_url")        # /download/xxx（备用）

    url_path = s_file_url or subtitle_url or s_dl_url
    if not url_path:
        print("   未找到字幕下载链接 (subtitle_url / file_url / download_url)")
        return 0

    # 通过 Kalm 文件代理下载（stream 模式防断连）
    proxy_url = f"{SUBTITLE_FILE_URL}{url_path}"
    filename = "tts_subtitle.srt"

    print(f"   下载字幕: {proxy_url}")

    r = requests.get(proxy_url, stream=True, timeout=30)
    if r.status_code == 200:
        chunks = []
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                chunks.append(chunk)
        content = b"".join(chunks)
        dest = OUTPUT_DIR / filename
        dest.write_bytes(content)
        print(f"   [OK] 已保存: {dest} ({len(content)} bytes)")
        # 打印字幕预览
        text = content.decode("utf-8", errors="replace")
        print(f"   [预览] --------")
        for line in text.split("\n")[:12]:
            print(f"   {line}")
        print(f"   [预览] --------")
        return 1
    else:
        print(f"   [ERROR] HTTP {r.status_code}")
        return 0


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    tts_text = "第一个副作用叫视界问题。简单来说曲速飞船是一个盲人司机。当时空泡形成后，飞船内部就与外部宇宙完全隔绝了。"

    payload = {
        "path": "/v1.5/generate",
        "text": tts_text,
        "speaker": "解说-魔魔",
    }

    # === 阶段一：提交 TTS ===
    print("=" * 60)
    print("阶段一：提交 TTS 任务 (IndexTTS V1.5)")
    resp = requests.post(
        f"{INTERFACE_BASE}/tasks/submit",
        json={"task_type": "tts", "payload": payload},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    task_id = data["task_id"]
    track_mode = data["track_mode"]
    print(f"   task_id: {task_id}")
    print(f"   track_mode: {track_mode}")

    # === 阶段二：轮询 TTS ===
    print("\n阶段二：HTTP 轮询等待...")
    result = wait_for_done(task_id)

    if result is None:
        sys.exit(1)

    # === 阶段三：下载音频 ===
    print("\n阶段三：下载音频")
    n_audio, audio_file_path = download_audio(result)

    # === 阶段四：(可选) 生成字幕 ===
    n_subtitle = 0
    if SUBTITLE_ENABLED and audio_file_path:
        print(f"\n阶段四：生成字幕 {'=' * 40}")
        sub_task_id = submit_subtitle(tts_text, audio_file_path)

        print("   轮询字幕任务...")
        sub_result = wait_for_done(sub_task_id)

        if sub_result:
            n_subtitle = download_subtitle(sub_result)

    print(f"\n{'=' * 60}")
    print(f"[OK] 音频 {n_audio} 个 + 字幕 {n_subtitle} 个 → {OUTPUT_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
