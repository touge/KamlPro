# -*- coding: utf-8 -*-
"""
TTS 任务执行器
==============
纯透传模式：接收前端 payload → 动态路由至配音 Audio Provider 并委托其执行合成流程。
支持 generate_subtitle: True 同步生成字幕，产物通过 /static/{task_id}/{filename} 下载。
"""

from src.core.providers.audio.manager import audio_provider_manager
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.logger import log


def execute(task_id: str, **payload):
    """
    TTS 生成任务入口。由调度器动态调用。

    payload 期望字段:
      - path: str — 后端生成接口路径，如 "/v1.5/generate"
      - text: str — 要合成的文本
      - speaker: str — 音色名称
      - generate_subtitle: bool (可选) — 是否同步生成 SRT 字幕
      - provider: str (可选) — 配音服务提供商，默认为 'index_tts'
      - 其余字段原样 POST 给后端
    """
    payload["task_id"] = task_id
    provider_type = payload.get("provider", "index_tts")

    try:
        log.info(f"[TTS Executor] >>> task_id={task_id} 初始化配音/语音合成任务...")
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING,
            {"message": "Submitting to TTS service..."})

        provider = audio_provider_manager.get_provider(provider_type)

        def progress_callback(status_data: dict):
            """将 TTS 实时生成进度消息透传到 TaskManager 和前端 WS"""
            TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, result=status_data)
            ws_manager.send(task_id, status_data)

        log.info(f"[TTS Executor] >>> task_id={task_id} 委托给提供商 '{provider_type}' 执行语音合成...")
        status_data = provider.generate_speech(payload, progress_callback)
        log.info(f"[TTS Executor] >>> task_id={task_id} 语音合成执行成功")

        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result=status_data)
        ws_manager.send(task_id, {"type": "task_complete", "status": "success", "result": status_data})

    except Exception as e:
        if "任务已被用户手动停止" in str(e):
            log.info(f"[TTS Executor] >>> Task {task_id} 任务已被用户手动停止。")
        else:
            log.error(f"[TTS Executor] >>> Task {task_id} 语音合成失败: {e}", exc_info=True)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        ws_manager.send(task_id, {"type": "task_failed", "message": str(e)})
