# -*- coding: utf-8 -*-
"""诊断：通过 Kalm 测试 TTS + 字幕一条龙"""
import time
from pathlib import Path
import requests

KALM = "http://127.0.0.1:7000"
TASKS = f"{KALM}/interface/tasks"
FILE_TTS = f"{KALM}/file/tts"
OUT = Path(__file__).parent / "output" / "_diag_subtitle"
OUT.mkdir(parents=True, exist_ok=True)

TEXT = "第一个副作用叫视界问题。简单来说曲速飞船是一个盲人司机。当时空泡形成后，飞船内部就与外部宇宙完全隔绝了"

# 1. 提交
print("1. 提交 TTS + subtitle (通过 Kalm)...")
r = requests.post(f"{TASKS}/submit", json={
    "task_type": "tts",
    "payload": {
        "path": "/v1.5/generate",
        "text": TEXT,
        "speaker": "解说-魔魔",
        "generate_subtitle": True,
    },
}, timeout=10)
r.raise_for_status()
kalm_tid = r.json()["data"]["task_id"]
print(f"   Kalm task_id: {kalm_tid}")

# 2. 轮询
print("2. 轮询...")
backend_tid = None
for i in range(120):
    time.sleep(2)
    s = requests.get(f"{TASKS}/{kalm_tid}/status", timeout=5).json()["data"]
    st = s["status"]
    print(f"   [{i*2}s] {st}", end="")
    if st == "success":
        bdata = s.get("result", {}).get("data", {})
        backend_tid = bdata.get("task_id", "")
        print(f"  backend_tid={backend_tid}")
        print(f"   data keys: {list(bdata.keys())}")
        break
    elif st == "failed":
        print(f" -> {s.get('result', {}).get('message', '')}")
        exit(1)
    print()
else:
    print("超时"); exit(1)

if not backend_tid:
    print("未获取到 backend_tid"); exit(1)

# 3. 下载音频（新静态端点）
print(f"3. 下载音频 /static/{backend_tid}/{backend_tid}.wav...")
r = requests.get(f"{FILE_TTS}/static/{backend_tid}/{backend_tid}.wav", timeout=10)
print(f"   HTTP {r.status_code}, {len(r.content)} bytes")
(OUT / f"{backend_tid}.wav").write_bytes(r.content)

# 4. 下载字幕
print(f"4. 下载字幕 /static/{backend_tid}/{backend_tid}.srt...")
r = requests.get(f"{FILE_TTS}/static/{backend_tid}/{backend_tid}.srt", timeout=10)
print(f"   HTTP {r.status_code}, {len(r.content)} bytes")
(OUT / f"{backend_tid}.srt").write_bytes(r.content)
print(f"   内容:\n{r.text}")

print(f"\n=== 产物: {OUT} ===")
for f in sorted(OUT.iterdir(), key=lambda x: x.stat().st_mtime):
    print(f"  {f.name}  ({f.stat().st_size / 1024:.1f} KB)")
