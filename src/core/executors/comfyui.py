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
      - provider: str (可选) — 生成服务提供商，默认为 'comfyui'
    """
    payload["task_id"] = task_id
    provider_type = payload.get("provider", "comfyui")

    try:
        log.info(f"[Comfyui Executor] >>> task_id={task_id} 初始化Comfyui任务...")
        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Initializing comfyui generation..."})
        
        provider = image_provider_manager.get_provider(provider_type)

        def progress_callback(raw_message: dict):
            """将ComfyUI任务实时进度消息透传到 TaskManager 和前端 WS (双轨兼容与单轨节流)"""
            TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, result=raw_message)
            # 旧通道受 ws_manager.send 内部 300ms 节流保护
            ws_manager.send(task_id, raw_message)

            client_id = payload.get("client_id")
            msg_type = raw_message.get("type", "")
            data = raw_message.get("data", {}) if isinstance(raw_message.get("data"), dict) else {}

            progress_val = None
            node_name = None
            message_text = None

            # 单轨全局总线 task_progress 解析：
            # 对于 ComfyUI，进度必须由 overall_progress（高水位锁与拓扑加权）单调下发，
            # 严禁将底层单节点的局部步数（progress）作为任务总进度，否则多采样器工作流会导致进度倒退回跳！
            if msg_type == "overall_progress":
                progress_val = data.get("percent")
                message_text = data.get("message")
                node_name = data.get("node") or data.get("node_name")
            elif provider_type != "comfyui":
                if msg_type == "progress":
                    val = data.get("value", 0)
                    max_val = data.get("max", 1)
                    node_name = data.get("node")
                    if max_val > 0:
                        progress_val = int((val / max_val) * 100)
                elif "percent" in raw_message:
                    progress_val = raw_message.get("percent")
                elif "progress" in raw_message:
                    progress_val = raw_message.get("progress")

            # 仅在获取到有效百分比时触发单轨 300ms 节流推送
            if progress_val is not None:
                ws_manager.emit_progress_throttled(
                    task_id=task_id,
                    client_id=client_id,
                    message={
                        "type": "task_progress",
                        "task_id": task_id,
                        "client_id": client_id,
                        "progress": progress_val,
                        "node_name": node_name or raw_message.get("node_title") or raw_message.get("node"),
                        "message": message_text or f"正在生成中 ({progress_val}%)",
                    },
                    interval=0.3
                )

        log.info(f"[ComfyUI Executor] >>> task_id={task_id} 委托给提供商 '{provider_type}' 执行ComfyUI任务...")
        final_outputs = provider.generate_image(payload, progress_callback)
        log.info(f"[ComfyUI Executor] >>> task_id={task_id} ComfyUI任务执行成功")

        TaskManager.update_task(task_id, TaskManager.STATUS_SUCCESS, result={
            "message": "ComfyUI generation completed",
            "outputs": final_outputs,
        })
        ws_manager.send(task_id, {"type": "task_complete", "status": "success",
            "message": "ComfyUI generation completed", "outputs": final_outputs})

    except Exception as e:
        if "任务已被用户手动停止" in str(e):
            log.info(f"[ComfyUI Executor] >>> task_id={task_id} 任务已被用户手动停止。")
        else:
            log.error(f"[ComfyUI Executor] >>> task_id={task_id} 执行失败: {e}", exc_info=True)
        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
        ws_manager.send(task_id, {"type": "task_failed", "message": str(e)})
