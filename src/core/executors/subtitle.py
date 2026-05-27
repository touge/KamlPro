# -*- coding: utf-8 -*-
"""
字幕生成任务执行器
==================
接收前端上传的本地音频文件 + 文稿 → 动态路由至配音 Audio Provider 并委托其执行对齐流程。
与 TTS 语音生成共享同一 TTS 服务进程，调度器自动复用，无需重启。
"""

from src.core.providers.audio.manager import audio_provider_manager
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.logger import log


def execute(task_id: str, **payload):
    """
    字幕生成任务入口。由调度器动态调用。

    payload 期望字段:
      - text: str — 字幕对齐文本
      - audio_file_path: str — 本地音频文件绝对路径（前端上传后由 Kalm 保存）
      - output_filename: str (可选) — 输出字幕文件名
      - provider: str (可选) — 配音服务提供商，默认为 'index_tts'
    """
    payload["task_id"] = task_id
    provider_type = payload.get("provider", "index_tts")

    try:
        log.info(f"[Subtitle Executor] >>> task_id={task_id} 初始化强制字幕对齐任务...")
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING,
                                {"message": "Submitting to subtitle alignment..."})

        provider = audio_provider_manager.get_provider(provider_type)

        def progress_callback(status_data: dict):
            """将字幕对齐实时进度消息透传到 TaskManager 和前端 WS"""
            TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, result=status_data)
            ws_manager.send(task_id, status_data)

        log.info(f"[Subtitle Executor] >>> task_id={task_id} 委托给提供商 '{provider_type}' 执行字幕对齐...")
        status_data = provider.align_subtitle(payload, progress_callback)
        log.info(f"[Subtitle Executor] >>> task_id={task_id} 字幕对齐执行成功")

        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result=status_data)
        ws_manager.send(task_id, {"type": "task_complete", "status": "success", "result": status_data})

    except Exception as e:
        log.error(f"[Subtitle Executor] >>> Task {task_id} 字幕对齐失败: {e}", exc_info=True)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        ws_manager.send(task_id, {"type": "task_failed", "message": str(e)})
