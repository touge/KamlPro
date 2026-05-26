# -*- coding: utf-8 -*-
"""
交叉测试：每 500ms 提交 → 自适应轮询 + 单 WS
"""
import json, time, uuid
from pathlib import Path
import requests, websocket

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

OUT = Path(__file__).parent / "test1"
WF = Path(__file__).parent / "workflows" / "image_qwen_Image_2512.json"

PROMPTS = [
    "A orange tabby cat sleeping on a sunny park bench, warm golden hour light, photorealistic, 8K",
    "A golden retriever puppy playing in a field of wildflowers, soft morning light, photorealistic",
    "A snowy mountain peak at sunset, alpine landscape, dramatic clouds, 8K nature photography",
]
TTS_TEXTS = [
    "今天天气真好，阳光洒在窗台上，让人感到格外温暖。",
    "人工智能正在改变我们的生活方式，从语音助手到自动驾驶，每天都在进步。",
    "很久很久以前，在一座遥远的城堡里，住着一位勇敢的骑士。",
]

TYPES = [
    "comfyui", 
    # "tts", 
    # "tts",
    "comfyui", 
    "tts", 
    # "comfyui"
]


def interval(pos):
    return [2, 4, 6, 8, 10, 10][pos] if pos < 6 else 10


OUT.mkdir(parents=True, exist_ok=True)

# ============================================================
# 阶段一：每 500ms 提交一个
# ============================================================
print("=" * 60)
print("阶段一：每 500ms 提交一个")
print("=" * 60)

tasks = []      # [(type, task_id)]
ci = ti = 0
for tp in TYPES:
    if tp == "comfyui":
        wf = json.loads(WF.read_text(encoding="utf-8"))
        for n in wf.values():
            if n.get("class_type") == "CLIPTextEncode" and "Positive" in n.get("_meta", {}).get("title", ""):
                n["inputs"]["text"] = PROMPTS[ci % len(PROMPTS)]
        p = {"workflow": wf, "final_output_node_id": "264", "client_id": str(uuid.uuid4())[:16]}
        ci += 1
    else:
        p = {"path": "/v1.5/generate", "text": TTS_TEXTS[ti % len(TTS_TEXTS)], "speaker": "解说-魔魔"}
        ti += 1

    r = requests.post(f"{INTERFACE_BASE}/tasks/submit", json={"task_type": tp, "payload": p}, timeout=10)
    tid = r.json()["data"]["task_id"]
    tasks.append((tp, tid))
    print(f"  [{len(tasks)*0.5:.1f}s] {tp:14s} -> {tid[:8]}")
    time.sleep(0.5)

print(f"  共 {len(tasks)} 个\n")

# ============================================================
# 阶段二：自适应轮询 + 单 WS
# ============================================================
print("=" * 60)
print("阶段二：自适应轮询")
print("=" * 60)

last = {}       # task_id → 上次轮询时间
done = set()
ws = None
ws_tid = None
step = 0
round_n = 0     # 轮次计数
t0 = time.time()

while len(done) < len(tasks):
    now = time.time()
    pending = [(tp, tid) for tp, tid in tasks if tid not in done]

    # ---- WS 非阻塞读取 ----
    if ws:
        try:
            ws.settimeout(0.01)
            msg = json.loads(ws.recv())
            t = msg.get("type", "")
            if t == "progress":
                v, m = msg["data"]["value"], msg["data"]["max"]
                if m: print(f"\r    [WS] 进度: {v*100//m}%", end="", flush=True)
            elif t == "task_complete" and ws_tid not in done:
                step += 1
                print(f"\r    [WS] step{step} 生成完成")
                for _, out in msg.get("outputs", {}).items():
                    for img in out.get("images", []):
                        url = (f"{COMFYUI_FILE_URL}"
                               f"?filename={img['filename']}&subfolder={img.get('subfolder','')}&type={img.get('type','temp')}")
                        r = requests.get(url, timeout=30)
                        if r.status_code == 200:
                            fn = f"step{step}_{img['filename']}"
                            (OUT / fn).write_bytes(r.content)
                            print(f"    [下载] {fn} ({len(r.content)//1024}KB)")
                        else:
                            print(f"    [下载失败] HTTP {r.status_code}")
                done.add(ws_tid)
                ws.close(); ws = None; ws_tid = None
            elif t == "task_failed":
                step += 1
                done.add(ws_tid)
                print(f"\r  [step{step}] 生成失败: {msg.get('message','')}")
                ws.close(); ws = None; ws_tid = None
        except websocket.WebSocketTimeoutException:
            pass
        except websocket.WebSocketConnectionClosedException:
            ws = None; ws_tid = None
        except Exception:
            try: ws.close()
            except: pass
            ws = None; ws_tid = None

    # ---- HTTP 自适应轮询 ----
    round_printed = False  # 本轮是否已打印分隔线
    for pos, (tp, tid) in enumerate(pending):
        iv = interval(pos)
        if tid in last and now - last[tid] < iv:
            continue
        last[tid] = now

        try:
            s = requests.get(f"{INTERFACE_BASE}/tasks/{tid}/status", timeout=5).json()["data"]
        except Exception as e:
            print(f"  [!] {tid[:8]} 请求失败: {e}")
            time.sleep(2)
            continue

        st = s["status"]
        if not round_printed:
            round_n += 1
            t_str = time.strftime("%H:%M:%S")
            print(f"\n---- 第{round_n}轮 ({t_str}) ----")
            round_printed = True

        track = "ws" if (tp == "comfyui" and st == "running" and ws is not None) else "http"
        mark = {"running": "R", "queued": "Q", "pending": "P",
                "success": "OK", "failed": "FAIL"}.get(st, st)
        print(f"  [{mark}] {tid[:8]} | {tp:14s} | {track:4s} | pos={pos} intv={iv}s | {st}")

        if st == "running" and tp == "comfyui" and ws is None:
            try:
                ws = websocket.create_connection(
                    f"{WS_BASE}/interface/tasks/{tid}/ws", timeout=10)
                ws_tid = tid
                print(f"         └ WS 已连接")
            except Exception as e:
                print(f"         └ WS 失败: {e}")

        elif st == "success" and tid not in done:
            step += 1
            if tp == "tts":
                backend = s.get("result", {})
                bdata = backend.get("data", {})
                file_url = bdata.get("file_url", "")
                dl = file_url or bdata.get("download_url", "")
                if dl:
                    fn = f"step{step}_tts.wav"
                    r = requests.get(f"{TTS_FILE_URL}{dl}", timeout=30)
                    if r.status_code == 200:
                        (OUT / fn).write_bytes(r.content)
                        print(f"    [下载] {fn} ({len(r.content)//1024}KB)")
                    else:
                        print(f"    [下载失败] HTTP {r.status_code}")

            elif tp == "comfyui":
                # WS 没处理到的话，这里兜底下载
                for _, out in s.get("result", {}).get("outputs", {}).items():
                    for img in out.get("images", []):
                        url = (f"{COMFYUI_FILE_URL}"
                               f"?filename={img['filename']}&subfolder={img.get('subfolder','')}&type={img.get('type','temp')}")
                        r = requests.get(url, timeout=30)
                        if r.status_code == 200:
                            fn = f"step{step}_{img['filename']}"
                            (OUT / fn).write_bytes(r.content)
                            print(f"    [下载] {fn} ({len(r.content)//1024}KB)")
                        else:
                            print(f"    [下载失败] HTTP {r.status_code}")
            done.add(tid)

        elif st == "failed" and tid not in done:
            step += 1
            done.add(tid)

    time.sleep(0.5)

if ws:
    try: ws.close()
    except: pass

elapsed = time.time() - t0
print(f"\n{'=' * 60}")
print(f"完成! 耗时 {elapsed:.0f}s | 产物: {OUT}")
print("=" * 60)
for f in sorted(OUT.iterdir(), key=lambda x: x.stat().st_mtime):
    print(f"  {f.name}  ({f.stat().st_size/1024:.0f} KB)")
