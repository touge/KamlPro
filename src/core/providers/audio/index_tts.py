# -*- coding: utf-8 -*-
"""
IndexTTS 语音合成与字幕对齐提供商实现
=====================================
封装了与本地 TTS / Subtitle 后端交互的完整逻辑，包含
任务提交、字幕 multipart 音频上传，以及统一的轮询监听机制。
"""

import time
import requests
from pathlib import Path
from typing import Dict, Any

from src.core.service_controller import service_controller
from src.logic.logger import log
from src.core.providers.audio.base import BaseAudioProvider, register_audio_provider

POLL_INTERVAL = 2
MAX_POLL_SECONDS = 600


@register_audio_provider("index_tts")
class IndexTTSProvider(BaseAudioProvider):
    def generate_speech(self, payload: Dict[str, Any], progress_callback: callable) -> Dict[str, Any]:
        task_id = payload.get("task_id", "")
        path = payload.pop("path", "/v1.5/generate")
        service_name = payload.pop("_service_name", "TTS")

        svc = service_controller.get_service_config(service_name)
        if not svc:
            raise RuntimeError(f"Service '{service_name}' not configured")

        base_url = f"{svc['host']}:{svc['port']}"
        token = svc.get("token", "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        submit_url = f"{base_url}{path}"

        log.info(f"[IndexTTS Provider] Task {task_id}: submit to {submit_url}")

        # 1. 提交 TTS 语音合成任务
        resp = requests.post(submit_url, json=payload, headers=headers, timeout=(5, 10))
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(f"TTS rejected: {resp.status_code} | {detail}")

        result = resp.json()
        svc_task_id = result.get("data", {}).get("task_id")
        if not svc_task_id:
            raise RuntimeError("TTS returned no task_id")

        log.info(f"[IndexTTS Provider] Task {task_id} submitted -> {svc_task_id}")

        # 注册取消元数据，便于中途取消轮询和物理取消任务
        from src.core.task_manager import TaskManager
        TaskManager.set_cancel_metadata(task_id, {
            "engine_type": "tts",
            "svc_task_id": svc_task_id,
            "base_url": base_url,
            "headers": headers
        })

        # 2. 轮询状态直到成功或失败
        return self._poll_status(task_id, base_url, svc_task_id, headers, progress_callback)

    def align_subtitle(self, payload: Dict[str, Any], progress_callback: callable) -> Dict[str, Any]:
        task_id = payload.get("task_id", "")
        text = payload.get("text", "")
        audio_file_path = payload.get("audio_file_path", "")
        output_filename = payload.get("output_filename")
        service_name = payload.get("_service_name", "TTS")

        if not text:
            raise ValueError("Missing 'text' in payload")

        if not audio_file_path or not Path(audio_file_path).is_file():
            raise ValueError(f"Missing or invalid 'audio_file_path': {audio_file_path}")

        svc = service_controller.get_service_config(service_name)
        if not svc:
            raise RuntimeError(f"Service '{service_name}' not configured")

        base_url = f"{svc['host']}:{svc['port']}"
        token = svc.get("token", "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        # 读取本地音频文件
        audio_bytes = Path(audio_file_path).read_bytes()
        audio_name = Path(audio_file_path).name
        log.info(f"[IndexTTS Provider] Task {task_id}: read local audio {audio_name} ({len(audio_bytes)} bytes)")

        # 1. multipart 提交到 TTS 后端字幕端点
        files = {"audio_file": (audio_name, audio_bytes, "audio/wav")}
        data = {"text": text}
        if output_filename:
            data["output_filename"] = output_filename

        submit_url = f"{base_url}/subtitle/generate"
        log.info(f"[IndexTTS Provider] Task {task_id}: POST {submit_url}")

        resp = requests.post(submit_url, headers=headers, files=files, data=data, timeout=(5, 30))
        resp.raise_for_status()

        svc_task_id = resp.json().get("task_id")
        if not svc_task_id:
            raise RuntimeError("Subtitle endpoint returned no task_id")

        log.info(f"[IndexTTS Provider] Task {task_id} alignment submitted -> {svc_task_id}")

        # 注册取消元数据，便于中途取消轮询和物理取消任务
        from src.core.task_manager import TaskManager
        TaskManager.set_cancel_metadata(task_id, {
            "engine_type": "tts",
            "svc_task_id": svc_task_id,
            "base_url": base_url,
            "headers": headers
        })

        # 2. 轮询状态直到成功或失败
        return self._poll_status(task_id, base_url, svc_task_id, headers, progress_callback)

    def _poll_status(self, task_id: str, base_url: str, svc_task_id: str, headers: dict, progress_callback: callable) -> dict:
        from src.core.task_manager import TaskManager
        elapsed = 0
        while elapsed < MAX_POLL_SECONDS:
            if TaskManager.is_cancelled(task_id):
                log.info(f"[IndexTTS Provider] Task {task_id} was cancelled during polling. Aborting.")
                raise RuntimeError("任务已被用户手动停止")
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            try:
                sr = requests.get(
                    f"{base_url}/status/{svc_task_id}",
                    headers=headers, timeout=(5, 10),
                )
                if sr.status_code != 200:
                    continue

                status_data = sr.json()
                svc_status = status_data.get("status")

                # 执行进度回调
                progress_callback(status_data)

                if svc_status == "failed":
                    log.error(f"[IndexTTS Provider] Task {task_id} failed: {status_data}")
                    raise RuntimeError(f"Dubbing service returned status failed: {status_data}")

                if svc_status == "completed":
                    log.info(f"[IndexTTS Provider] Task {task_id} completed successfully.")
                    return status_data

            except requests.RequestException as e:
                log.warning(f"[IndexTTS Provider] Task {task_id}: poll error: {e}")

        raise TimeoutError(f"Polling timeout after {MAX_POLL_SECONDS}s")
