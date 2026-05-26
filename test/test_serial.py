# -*- coding: utf-8 -*-
"""
串行测试：逐个提交，等待完成后再提交下一个
===========================================
顺序: comfyui → tts → tts → comfyui → tts → comfyui
"""
import json
import sys
import time
import uuid
import os
from pathlib import Path

import requests
import websocket

# ============================================================
# 可配置项 — 所有 URL 集中管理，方便修改
# ============================================================
KALM_HOST = "127.0.0.1"
KALM_PORT = 7000
KALM_BASE = f"http://{KALM_HOST}:{KALM_PORT}"
INTERFACE_BASE = f"{KALM_BASE}/interface"       # 任务提交 / 状态查询
FILE_PROXY_BASE = f"{KALM_BASE}/file"           # 文件代理
WS_BASE = f"ws://{KALM_HOST}:{KALM_PORT}"       # WebSocket

COMFYUI_FILE_URL = f"{FILE_PROXY_BASE}/ComfyUI/api/view"  # ComfyUI 图片下载
TTS_FILE_URL = f"{FILE_PROXY_BASE}/tts"                   # TTS 音频下载

OUTPUT_DIR = Path(__file__).parent / "test1"
WF_PATH = Path(__file__).parent / "workflows" / "image_qwen_Image_2512.json"

os.environ["PYTHONUTF8"] = "1"

COMFY_PROMPTS = [
    "A orange tabby cat sleeping on a sunny park bench, warm golden hour light, bokeh background, photorealistic, 8K",
    "A golden retriever puppy playing in a field of wildflowers, soft morning light, shallow depth of field, photorealistic",
    "A snowy mountain peak at sunset, alpine landscape, dramatic clouds, ultra detailed, 8K nature photography",
]

TTS_TEXTS = [
    "今天天气真好，阳光洒在窗台上，让人感到格外温暖。",
    "人工智能正在改变我们的生活方式，从语音助手到自动驾驶，每天都在进步。",
    "很久很久以前，在一座遥远的城堡里，住着一位勇敢的骑士。",
]

TTS_SPEAKER = "解说-魔魔"
STEPS = [
    ("comfyui", 1),
    ("tts", 2),
    ("tts", 3),
    ("comfyui", 4),
    ("tts", 5),
    ("comfyui", 6),
]


def load_workflow():
    with open(WF_PATH, encoding="utf-8") as f:
        return json.load(f)


def set_prompt(wf, positive):
    for node in wf.values():
        if node.get("class_type") == "CLIPTextEncode":
            title = node.get("_meta", {}).get("title", "")
            if "Positive" in title:
                node["inputs"]["text"] = positive


def find_output_node(wf):
    for nid, node in wf.items():
        if node.get("class_type") in ("SaveImage", "PreviewImage"):
            return str(nid)
    return list(wf.keys())[-1]


def run_comfyui(step, prompt):
    print(f"  [step{step}] ComfyUI: {prompt[:60]}...")

    wf = load_workflow()
    set_prompt(wf, prompt)
    output_node = find_output_node(wf)

    r = requests.post(f"{INTERFACE_BASE}/tasks/submit", json={
        "task_type": "comfyui",
        "payload": {
            "workflow": wf,
            "final_output_node_id": output_node,
            "client_id": str(uuid.uuid4())[:16],
        },
    }, timeout=10)
    task_id = r.json()["data"]["task_id"]

    for _ in range(120):
        time.sleep(2)
        s = requests.get(f"{INTERFACE_BASE}/tasks/{task_id}/status", timeout=5).json()
        st = s["data"]["status"]
        if st == "running":
            print(f"  [step{step}] 服务就绪，连 WS...")
            break
        elif st == "failed":
            print(f"  [step{step}] 失败: {s['data']['result'].get('message','')}")
            return 0
        else:
            print(f"\r  [step{step}] {st}...", end="", flush=True)
    else:
        print(f"\n  [step{step}] 启动超时")
        return 0

    ws = websocket.create_connection(
        f"{WS_BASE}/interface/tasks/{task_id}/ws", timeout=30,
    )
    outputs = {}
    while True:
        msg = json.loads(ws.recv())
        t = msg.get("type", "")
        if t == "progress":
            v, m = msg["data"].get("value", 0), msg["data"].get("max", 0)
            if m:
                print(f"\r  [step{step}] 生成: {v}/{m} ({v*100//m}%)", end="", flush=True)
        elif t == "task_complete":
            outputs = msg.get("outputs", {})
            break
        elif t == "task_failed":
            print(f"\n  [step{step}] 失败: {msg.get('message','')}")
            ws.close()
            return 0
    ws.close()

    n = 0
    for _, out in outputs.items():
        for img in out.get("images", []):
            fn = f"step{step}_{img.get('filename', 'img.png')}"
            url = (
                f"{COMFYUI_FILE_URL}"
                f"?filename={img['filename']}&subfolder={img.get('subfolder','')}&type={img.get('type','temp')}"
            )
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                (OUTPUT_DIR / fn).write_bytes(r.content)
                print(f"\n  [step{step}] 图片已保存: {fn} ({len(r.content)/1024:.0f}KB)")
                n += 1
    return n


def run_tts(step, text):
    print(f"  [step{step}] TTS: {text[:40]}...")

    r = requests.post(f"{INTERFACE_BASE}/tasks/submit", json={
        "task_type": "tts",
        "payload": {
            "path": "/v1.5/generate",
            "text": text,
            "speaker": TTS_SPEAKER,
        },
    }, timeout=10)
    task_id = r.json()["data"]["task_id"]

    for _ in range(120):
        time.sleep(2)
        s = requests.get(f"{INTERFACE_BASE}/tasks/{task_id}/status", timeout=5).json()
        st = s["data"]["status"]
        rs = s["data"].get("result", {})
        svc_st = rs.get("status", "?")

        if st == "running":
            print(f"\r  [step{step}] TTS 后端: {svc_st}   ", end="", flush=True)
        elif st == "success":
            print(f"\n  [step{step}] 完成")
            dl = rs.get("data", {}).get("download_url", "")
            if dl:
                url = f"{TTS_FILE_URL}{dl}"
                r = requests.get(url, timeout=30)
                if r.status_code == 200:
                    fn = f"step{step}_tts_output.wav"
                    (OUTPUT_DIR / fn).write_bytes(r.content)
                    print(f"  [step{step}] 音频已保存: {fn} ({len(r.content)/1024:.0f}KB)")
                    return 1
            return 0
        elif st == "failed":
            print(f"\n  [step{step}] 失败: {s['data']['result'].get('message','')}")
            return 0
        else:
            print(f"\r  [step{step}] {st}...", end="", flush=True)
    return 0


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    total = 0

    print("=" * 60)
    print("串行测试: 逐个提交，等待完成后再提交下一个")
    print("顺序: comfyui → tts → tts → comfyui → tts → comfyui")
    print("=" * 60)

    for task_type, step in STEPS:
        print(f"\n--- Step {step} ({task_type}) ---")
        if task_type == "comfyui":
            prompt = COMFY_PROMPTS[(step - 1) % len(COMFY_PROMPTS)]
            n = run_comfyui(step, prompt)
        else:
            text = TTS_TEXTS[(step - 1) % len(TTS_TEXTS)]
            n = run_tts(step, text)
        total += n

    print(f"\n{'=' * 60}")
    print(f"串行测试完成! 共获取 {total} 个文件 -> {OUTPUT_DIR}")
    print(f"{'=' * 60}")

    for f in sorted(OUTPUT_DIR.iterdir()):
        print(f"  {f.name}")


if __name__ == "__main__":
    main()
