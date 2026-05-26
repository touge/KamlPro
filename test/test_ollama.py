# -*- coding: utf-8 -*-
"""
Ollama / LLM 端到端测试（通过 Kalm 网关）
==========================================
测试逻辑:
  测试脚本 → Kalm（任务提交）→ Kalm 拉起 Ollama → Ollama 推理 → 结果返回

Ollama 的启停完全由 Kalm 的 service_controller 负责，
本脚本不直接与 Ollama 通信，也不手动启动 Ollama 进程。
"""

import sys
import time
import requests

# ============================================================
# 配置项
# ============================================================
KALM_HOST = "127.0.0.1"
KALM_PORT = 7000
KALM_BASE = f"http://{KALM_HOST}:{KALM_PORT}"
INTERFACE_BASE = f"{KALM_BASE}/interface"

KALM_TOKEN = "123456"       # 对应 config.yaml → api_server.tokens
HEADERS = {"Authorization": f"Bearer {KALM_TOKEN}"}

# 指定要测试的模型（对应 configs/llm_config.yaml 中的 name 字段）
# 留空则自动从 /interface/ping 返回的可用服务中选第一个 ollama 模型
TEST_MODEL = "qwen3:8b"

# 推理内容
TEST_PROMPTS = [
    "Why is the sky blue? Answer in one sentence.",
    "用一句话解释什么是量子计算。",
]


# ============================================================
# 工具函数
# ============================================================
def check_kalm():
    """确认 Kalm 网关在线，并打印下游服务状态。"""
    print("=" * 60)
    print("Step 1: Check Kalm Gateway & Downstream Service Status")
    print("=" * 60)
    try:
        resp = requests.get(f"{INTERFACE_BASE}/ping", headers=HEADERS, timeout=5)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        print(f"[OK] Kalm is active on {KALM_BASE}")

        services = data.get("downstream_services", [])
        if services:
            print("     Downstream services:")
            for svc in services:
                status = "RUNNING" if svc.get("running") else "STOPPED"
                pid = svc.get("pid") or "-"
                print(f"       - {svc['name']:12s}  port={svc.get('port', '?')}  pid={pid}  [{status}]")
        else:
            print("     (No downstream services configured)")
        return True
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] Cannot connect to Kalm on {KALM_BASE}.")
        print("        Please start Kalm first: run.bat")
        return False
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] Kalm ping returned error: {e}")
        return False


def submit_llm_task(model, prompt):
    """向 Kalm 提交一个 LLM 推理任务，返回 task_id。"""
    payload = {
        "task_type": "llm",
        "payload": {
            "model": model,
            "prompt": prompt,
            "options": {"temperature": 0.3},
        },
    }
    resp = requests.post(
        f"{INTERFACE_BASE}/tasks/submit",
        headers=HEADERS,
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]["task_id"]


def poll_until_done(task_id, timeout=300, poll_interval=2):
    """轮询任务状态直到成功/失败，返回 result dict 或 None。"""
    url = f"{INTERFACE_BASE}/tasks/{task_id}/status"
    started = time.time()

    while time.time() - started < timeout:
        resp = requests.get(url, headers=HEADERS, timeout=5)
        resp.raise_for_status()
        s_data = resp.json()["data"]
        status = s_data["status"]
        elapsed = int(time.time() - started)

        if status == "success":
            print(f"\n   [{elapsed}s] 任务完成")
            return s_data.get("result", {})
        elif status == "failed":
            msg = s_data.get("result", {}).get("message", "unknown")
            print(f"\n   [{elapsed}s] 任务失败: {msg}")
            return None
        else:
            print(f"\r   [{elapsed}s] status={status}...", end="", flush=True)

        time.sleep(poll_interval)

    print(f"\n   [ERROR] 等待超时 ({timeout}s)")
    return None


# ============================================================
# 测试阶段
# ============================================================
def run_tests():
    success_count = 0
    total = len(TEST_PROMPTS)

    for idx, prompt in enumerate(TEST_PROMPTS, 1):
        print(f"\n{'=' * 60}")
        print(f"Step {idx + 1}: LLM Task #{idx}  (model={TEST_MODEL})")
        print(f"{'=' * 60}")
        print(f"   Prompt: \"{prompt}\"")
        print(f"   Submitting to Kalm → Kalm will start Ollama if needed...")

        try:
            task_id = submit_llm_task(TEST_MODEL, prompt)
            print(f"   task_id: {task_id}")
        except Exception as e:
            print(f"[ERROR] 提交失败: {e}")
            continue

        result = poll_until_done(task_id)
        if result:
            text = result.get("text", "").strip()
            duration_ns = result.get("total_duration")
            duration_s = f"{duration_ns / 1e9:.2f}s" if duration_ns else "N/A"
            print(f"   Response : \"{text}\"")
            print(f"   Duration : {duration_s}")
            success_count += 1
        else:
            print(f"[FAILED] Task #{idx} did not complete successfully.")

    return success_count, total


def main():
    print("==========================================================")
    print("   Kalm LLM / Ollama End-to-End Test (via Kalm Gateway)  ")
    print("==========================================================")
    print(f"   Target model : {TEST_MODEL}")
    print(f"   Note: Ollama will be started automatically by Kalm")
    print()

    # Step 1: 确认 Kalm 在线
    if not check_kalm():
        sys.exit(1)

    # Step 2+: 逐条发送推理任务
    success_count, total = run_tests()

    print(f"\n{'=' * 60}")
    print(f"  TEST SUMMARY: {success_count}/{total} tasks succeeded")
    print(f"{'=' * 60}")

    if success_count < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
