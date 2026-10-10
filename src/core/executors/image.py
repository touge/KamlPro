# -*- coding: utf-8 -*-
"""
图像生成任务执行器
===================
纯透传模式：接收 workflow → 动态加载生图 Provider (如 ComfyUI) 并委托其执行生图流程。
不做任何下载、URL 改写等业务操作。文件访问走通用 /file 端点。
"""

from src.core.providers.image.manager import image_provider_manager
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.logger import log


def execute(task_id: str, **payload):
    """
    图像生成任务入口。由调度器通过 importlib 动态调用。

    payload 期望字段:
      - workflow: dict — ComfyUI 工作流 JSON
      - final_output_node_id: str — 输出节点 ID
      - client_id: str (可选) — WebSocket 客户端标识
      - provider: str (可选) — 图像生成服务提供商，默认为 'comfyui'
    """
    payload["task_id"] = task_id
    provider_type = payload.get("provider", "comfyui")

    try:
        log.info(f"[Image Executor] >>> task_id={task_id} 初始化生图任务...")
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Initializing image generation..."})
        
        provider = image_provider_manager.get_provider(provider_type)

        def progress_callback(raw_message: dict):
            """将生图实时进度消息透传到 TaskManager 和前端 WS (双轨兼容与单轨节流)"""
            TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, result=raw_message)
            ws_manager.send(task_id, raw_message)

            client_id = payload.get("client_id")
            progress_val = raw_message.get("overall_progress")
            if progress_val is None:
                step = raw_message.get("step")
                max_step = raw_message.get("max_step")
                if step is not None and max_step:
                    progress_val = int((step / max_step) * 100)
            if progress_val is not None:
                ws_manager.emit_progress_throttled(
                    task_id=task_id,
                    client_id=client_id,
                    message={
                        "type": "task_progress",
                        "task_id": task_id,
                        "client_id": client_id,
                        "progress": progress_val,
                        "node_name": raw_message.get("node_title") or raw_message.get("node"),
                        "message": f"正在生成中 ({progress_val}%)",
                    },
                    interval=0.3
                )

        log.info(f"[Image Executor] >>> task_id={task_id} 委托给提供商 '{provider_type}' 执行生图...")
        final_outputs = provider.generate_image(payload, progress_callback)
        log.info(f"[Image Executor] >>> task_id={task_id} 生图任务执行成功")

        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result={
            "message": "Image generation completed",
            "outputs": final_outputs,
        })
        ws_manager.send(task_id, {"type": "task_complete", "status": "success",
            "message": "Image generation completed", "outputs": final_outputs})

    except Exception as e:
        if "任务已被用户手动停止" in str(e):
            log.info(f"[Image Executor] >>> task_id={task_id} 任务已被用户手动停止。")
        else:
            log.error(f"[Image Executor] >>> task_id={task_id} 执行失败: {e}", exc_info=True)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        ws_manager.send(task_id, {"type": "task_failed", "message": str(e)})
