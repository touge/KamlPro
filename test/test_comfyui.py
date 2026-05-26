# -*- coding: utf-8 -*-
"""
ComfyUI 端到端测试
===================
1. 提交任务
2. HTTP 轮询等到服务启动、任务开始执行 (status: running)
3. 连接 WebSocket 实时跟踪进度
4. 下载结果图片
"""
import json
import sys
import uuid
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

OUTPUT_DIR = Path(__file__).parent / "comfyui" / "files"


def load_workflow():
    wf_path = Path(__file__).parent / "workflows" / "image_qwen_Image_2512.json"
    with open(wf_path, encoding="utf-8") as f:
        return json.load(f)


def set_prompt(workflow, positive_prompt, negative_prompt=None):
    for node_id, node in workflow.items():
        if node.get("class_type") == "CLIPTextEncode":
            title = node.get("_meta", {}).get("title", "")
            if "Positive" in title:
                node["inputs"]["text"] = positive_prompt
            elif "Negative" in title and negative_prompt:
                node["inputs"]["text"] = negative_prompt


def find_output_node(workflow):
    for node_id, node in workflow.items():
        if node.get("class_type") in ("SaveImage", "PreviewImage"):
            return str(node_id)
    return list(workflow.keys())[-1]


def wait_for_running(task_id, poll_interval=2, timeout=120):
    """HTTP 轮询，等到任务进入 running 状态（服务已启动、任务开始执行）"""
    import time
    started = time.time()
    while time.time() - started < timeout:
        resp = requests.get(f"{INTERFACE_BASE}/tasks/{task_id}/status", timeout=5)
        sdata = resp.json()["data"]
        status = sdata["status"]
        elapsed = int(time.time() - started)

        if status == "running":
            print(f"   [{elapsed}s] 服务就绪，任务开始执行")
            return True
        elif status == "failed":
            print(f"   [{elapsed}s] 启动失败: {sdata['result'].get('message', '')}")
            return False
        else:
            print(f"   [{elapsed}s] 等待中... (status: {status})", end="\r")
            time.sleep(poll_interval)

    print(f"\n   等待超时 ({timeout}s)")
    return False


def download_images(outputs):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for node_id, output in outputs.items():
        images = output.get("images", [])
        for img in images:
            filename = img.get("filename", "unknown.png")
            img_url = (
                f"{COMFYUI_FILE_URL}"
                f"?filename={filename}&subfolder={img.get('subfolder', '')}&type={img.get('type', 'temp')}"
            )
            print(f"   下载: {filename}")
            print(f"        {img_url}")
            r = requests.get(img_url, timeout=30)
            if r.status_code == 200:
                dest = OUTPUT_DIR / filename
                dest.write_bytes(r.content)
                size_kb = len(r.content) / 1024
                print(f"   [OK] 已保存 ({size_kb:.1f} KB)")
                downloaded += 1
            else:
                print(f"   [ERROR] HTTP {r.status_code}")
    return downloaded


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # === 阶段一：准备 workflow ===
    print("=" * 60)
    print("阶段一：准备 workflow")
    workflow = load_workflow()
    set_prompt(
        workflow,
        "A orange tabby cat lying lazily on a wooden park bench, warm afternoon sunlight filtering through green trees, bokeh background, photorealistic, 8K, shallow depth of field",
        "low quality, blurry, distorted, ugly, deformed, bad anatomy",
    )
    output_node_id = find_output_node(workflow)
    print(f"   输出节点: {output_node_id}")
    print(f"   workflow 节点数: {len(workflow)}")

    # === 阶段二：提交任务 ===
    print("\n阶段二：提交任务")
    resp = requests.post(
        f"{INTERFACE_BASE}/tasks/submit",
        json={
            "task_type": "comfyui",
            "payload": {
                "workflow": workflow,
                "final_output_node_id": output_node_id,
                "client_id": str(uuid.uuid4())[:16],
            },
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    task_id = data["task_id"]
    track_mode = data["track_mode"]
    print(f"   task_id: {task_id}")
    print(f"   track_mode: {track_mode}")

    # === 阶段三：HTTP 轮询等启动 ===
    print("\n阶段三：等待服务启动...")
    if not wait_for_running(task_id):
        sys.exit(1)

    # === 阶段四：WebSocket 实时跟踪 ===
    print("\n阶段四：WebSocket 实时进度")
    ws_url = f"{WS_BASE}/interface/tasks/{task_id}/ws"
    ws = websocket.create_connection(ws_url, timeout=30)
    final_result = None

    while True:
        raw = ws.recv()
        if not raw:
            break
        msg = json.loads(raw)
        msg_type = msg.get("type", "")

        if msg_type == "execution_start":
            pid = msg.get("data", {}).get("prompt_id", "")
            print(f"   ComfyUI 开始执行, prompt_id: {pid}")

        elif msg_type == "executing":
            node = msg.get("data", {}).get("node")
            if node:
                print(f"   正在执行节点: {node}")

        elif msg_type == "progress":
            val = msg.get("data", {}).get("value", 0)
            mx = msg.get("data", {}).get("max", 0)
            if mx:
                pct = val / mx * 100
                bar = "#" * int(pct / 5) + "-" * (20 - int(pct / 5))
                print(f"\r   进度: [{bar}] {pct:.0f}%  {val}/{mx}", end="", flush=True)

        elif msg_type == "execution_error":
            print(f"\n   [ERROR] 执行错误: {json.dumps(msg, ensure_ascii=False)}")
            final_result = {"status": "failed", "result": msg}
            break

        elif msg_type == "task_complete":
            print(f"\n   [DEBUG] task_complete keys: {list(msg.keys())}")
            print(f"   [DEBUG] task_complete raw: {json.dumps(msg, ensure_ascii=False)[:500]}")
            final_result = {"status": "success", "outputs": msg.get("outputs", msg.get("result", {}))}
            break

        elif msg_type == "task_failed":
            print(f"\n   [ERROR] 任务失败: {msg.get('message', '')}")
            final_result = {"status": "failed", "result": msg}
            break

    ws.close()
    print()

    if final_result is None or final_result["status"] != "success":
        print(f"\n[ERROR] 任务未成功完成")
        sys.exit(1)

    # === 阶段五：下载结果 ===
    print("\n阶段五：下载结果图片")
    outputs = final_result.get("outputs", {})
    print(f"   输出节点数: {len(outputs)}")

    n = download_images(outputs)

    print(f"\n{'=' * 60}")
    print(f"[OK] 共下载 {n} 张图片到 {OUTPUT_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
