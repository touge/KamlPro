# -*- coding: utf-8 -*-
"""
ComfyUI 图像生成服务提供商实现
=================================
封装了与 ComfyUI 后端交互的完整逻辑，包含 WebSocket 连接、
任务队列推送、进度监控以及路径翻译等逻辑。
"""

import time
import urllib.request
import urllib.parse
import urllib.error
import json
import websocket
from typing import Dict, Any

from src.core.service_controller import service_controller
from src.logic.logger import log
from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.comfy_progress_interceptor import ComfyProgressInterceptor
from src.utils import translate_workflow_paths
from src.core.providers.image.base import BaseImageProvider, register_image_provider


def log_cancel_debug(msg: str):
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    try:
        with open("cancel_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] [ComfyUI_Client] {msg}\n")
    except Exception:
        pass


@register_image_provider("comfyui")
class ComfyUIProvider(BaseImageProvider):
    def generate_image(self, payload: Dict[str, Any], progress_callback: callable) -> Dict[str, Any]:
        task_id = payload.get("task_id", "")
        workflow = payload.get("workflow")
        final_output_node_id = payload.get("final_output_node_id")
        client_id = payload.get("client_id", task_id[:8])

        if not workflow or not final_output_node_id:
            log.error(f"[ComfyUI Provider] >>> task_id={task_id} 缺少 workflow 或 final_output_node_id")
            raise ValueError("Missing 'workflow' or 'final_output_node_id' in payload")

        # 统一规范化: 支持单节点字符串 "9" 或多节点数组 ["264","266","296"]
        if isinstance(final_output_node_id, (list, tuple)):
            output_node_ids = [str(nid) for nid in final_output_node_id]
        else:
            output_node_ids = [str(final_output_node_id)]
        log.info(f"[ComfyUI Provider] >>> task_id={task_id} 输出节点ID列表: {output_node_ids}")

        service_name = payload.get("_service_name", "ComfyUI")
        log.info(f"[ComfyUI Provider] >>> task_id={task_id} 获取服务地址, service_name={service_name}...")
        service_url = service_controller.get_service_url(service_name)
        if not service_url:
            log.error(f"[ComfyUI Provider] >>> task_id={task_id} 服务地址获取失败, service_name={service_name}")
            raise RuntimeError(f"ComfyUI service '{service_name}' not available")

        log.info(f"[ComfyUI Provider] >>> task_id={task_id} ComfyUI 地址={service_url}")

        # 平台路径级联翻译：自适应获取目标平台系统并执行完整翻译与盘符基准映射
        svc_config = service_controller.get_service_config(service_name)
        if svc_config:
            target_os = svc_config.get("target_os")
            # 兜底推断：如果未配置 target_os，通过启动脚本类型推断目标系统的类型
            if not target_os:
                svc_type = svc_config.get("type")
                if svc_type == "sh":
                    target_os = "linux"
                elif svc_type in ("ps1", "cmd"):
                    target_os = "windows"
            
            if target_os:
                # 从 config.yaml 中获取全局基准路径映射表（如 {"D:/AIGC": "/root/workspace"}）
                path_mappings = yaml_config_loader.get("path_mappings", {})
                workflow = translate_workflow_paths(workflow, target_os=target_os, base_mappings=path_mappings)
                log.info(f"[ComfyUI Provider] >>> task_id={task_id} 级联路径已翻译为 {target_os.upper()} 格式")

        if "://" not in service_url:
            service_url = f"http://{service_url}"
        parsed = urllib.parse.urlparse(service_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 7001

        client = _ComfyUIClient(host, port)

        log.info(f"[ComfyUI Provider] >>> task_id={task_id} 开始提交 workflow 到 ComfyUI...")
        final_outputs = client.run_workflow(task_id, workflow, client_id, progress_callback, output_node_ids)
        log.info(f"[ComfyUI Provider] >>> task_id={task_id} workflow 执行完成")
        return final_outputs


class _ComfyUIClient:
    """轻量 ComfyUI 客户端：提交 Prompt + WebSocket 进度 + 获取输出信息"""

    def __init__(self, server_address, port):
        self.server_address = server_address
        self.port = port
        self.base_url = f"http://{server_address}:{port}"
        self.ws_url = f"ws://{server_address}:{port}/ws"

    def run_workflow(self, task_id: str, workflow: dict, client_id: str, message_callback: callable, output_node_ids: list = None) -> dict:
        from src.core.task_manager import TaskManager

        if TaskManager.is_cancelled(task_id):
            raise RuntimeError("任务已被用户手动停止")

        log.info(f"[ComfyUI Client] >>> _queue_prompt 开始...")
        prompt_id = self._queue_prompt(workflow, client_id)
        log.info(f"[ComfyUI Client] >>> _queue_prompt 完成, prompt_id={prompt_id}")

        if TaskManager.is_cancelled(task_id):
            self._delete_from_queue(prompt_id)
            self._interrupt()
            raise RuntimeError("任务已被用户手动停止")

        # 注册 ComfyUI 任务元数据，便于取消时查找对应的 prompt_id 和服务端口
        TaskManager.set_cancel_metadata(task_id, {
            "engine_type": "comfyui",
            "prompt_id": prompt_id,
            "server_address": self.server_address,
            "port": self.port
        })

        message_callback({"status": "queued", "prompt_id": prompt_id})

        total_nodes = len(workflow.keys()) if isinstance(workflow, dict) else 1
        interceptor = ComfyProgressInterceptor(message_callback, total_nodes)

        log.info(f"[ComfyUI Client] >>> _track_progress 开始, prompt_id={prompt_id}, total_nodes={total_nodes}")
        self._track_progress(task_id, prompt_id, client_id, interceptor)
        log.info(f"[ComfyUI Client] >>> _track_progress 完成, prompt_id={prompt_id}")
        
        # 兜底收尾：全部执行完成，发送最终的 100%
        message_callback({
            "type": "overall_progress",
            "data": {
                "percent": 100,
                "message": "执行完成 (100%)"
            }
        })

        log.info(f"[ComfyUI Client] >>> _get_history 开始, prompt_id={prompt_id}")
        history = self._get_history(prompt_id)
        all_outputs = history.get(prompt_id, {}).get("outputs", {})
        log.info(f"[ComfyUI Client] >>> _get_history 完成, all_outputs keys={list(all_outputs.keys())}")

        # 按指定节点 ID 过滤输出；未指定时返回全部
        if output_node_ids:
            outputs = {}
            for nid in output_node_ids:
                if nid in all_outputs:
                    outputs[nid] = all_outputs[nid]
                else:
                    log.warning(f"[ComfyUI Client] >>> 指定输出节点 {nid} 不在 ComfyUI 返回的 outputs 中，可用节点: {list(all_outputs.keys())}")
            if not outputs:
                log.error(f"[ComfyUI Client] >>> 所有指定节点 {output_node_ids} 均未找到输出！回退返回全部 outputs")
                outputs = all_outputs
        else:
            outputs = all_outputs

        log.info(f"[ComfyUI Client] >>> 最终 outputs keys={list(outputs.keys())}")
        message_callback({"type": "awaiting_delivery", "status": "completed", "prompt_id": prompt_id, "output_nodes": list(outputs.keys())})
        return outputs

    def _queue_prompt(self, prompt: dict, client_id: str) -> str:
        data = json.dumps({"prompt": prompt, "client_id": client_id}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/prompt",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())["prompt_id"]
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="ignore")
            detailed_msg = raw_err
            try:
                err_json = json.loads(raw_err)
                if "node_errors" in err_json and err_json["node_errors"]:
                    err_lines = []
                    for node_id, node_info in err_json["node_errors"].items():
                        class_type = node_info.get("class_type", "未知节点")
                        for err in node_info.get("errors", []):
                            err_lines.append(f"[节点 {node_id} ({class_type})]: {err.get('message', '')} {err.get('details', '')}".strip())
                    if err_lines:
                        detailed_msg = " | ".join(err_lines)
                elif isinstance(err_json.get("error"), dict):
                    detailed_msg = err_json["error"].get("message") or str(err_json["error"])
                elif err_json.get("error"):
                    detailed_msg = str(err_json["error"])
            except Exception:
                pass

            log.error(f"[ComfyUI Client] >>> 提交工作流被拒绝 (HTTP 400 Bad Request): {detailed_msg}")
            raise RuntimeError(f"ComfyUI 校验失败: {detailed_msg}") from e
        except Exception as e:
            log.error(f"[ComfyUI Client] >>> 提交 Prompt 发生网络异常: {e}")
            raise

    def _delete_from_queue(self, prompt_id: str):
        log_cancel_debug(f"_delete_from_queue called for prompt_id={prompt_id}")
        try:
            data = json.dumps({"action": "delete", "delete": [prompt_id]}).encode("utf-8")
            req = urllib.request.Request(f"{self.base_url}/queue", data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as resp:
                resp_data = resp.read()
                log.info(f"[ComfyUI Client] Queue delete prompt {prompt_id} response: {resp_data}")
                log_cancel_debug(f"_delete_from_queue success: response={resp_data}")
        except Exception as e:
            log.warning(f"[ComfyUI Client] Failed to delete prompt {prompt_id} from queue: {e}")
            log_cancel_debug(f"_delete_from_queue failed: {e}")

    def _interrupt(self):
        log_cancel_debug(f"_interrupt called")
        try:
            req = urllib.request.Request(f"{self.base_url}/interrupt", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as resp:
                resp_data = resp.read()
                log.info(f"[ComfyUI Client] Interrupt response: {resp_data}")
                log_cancel_debug(f"_interrupt success: response={resp_data}")
        except Exception as e:
            log.warning(f"[ComfyUI Client] Failed to interrupt execution: {e}")
            log_cancel_debug(f"_interrupt failed: {e}")

    def _get_history(self, prompt_id: str) -> dict:
        with urllib.request.urlopen(f"{self.base_url}/history/{prompt_id}") as resp:
            return json.loads(resp.read().decode())

    def _track_progress(self, task_id: str, prompt_id: str, client_id: str, message_callback: callable):
        from src.core.task_manager import TaskManager
        ws = websocket.create_connection(f"{self.ws_url}?clientId={client_id}", timeout=30)
        ws.settimeout(30)
        try:
            last_msg_time = time.time()
            max_idle_seconds = 300  # 5 分钟无新消息视为超时（大模型加载可能需要数分钟）
            while True:
                if TaskManager.is_cancelled(task_id):
                    log.info(f"[ComfyUI Client] Task {task_id} was cancelled during tracking. Closing WS, deleting queue, interrupting, and raising exception.")
                    log_cancel_debug(f"TaskManager.is_cancelled detected in _track_progress for task_id={task_id}, prompt_id={prompt_id}")
                    self._delete_from_queue(prompt_id)
                    self._interrupt()
                    raise RuntimeError("任务已被用户手动停止")
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if time.time() - last_msg_time > max_idle_seconds:
                        raise RuntimeError(f"WebSocket idle timeout after {max_idle_seconds}s")
                    continue
                if not msg:
                    break
                if isinstance(msg, bytes):
                    continue
                last_msg_time = time.time()
                data = json.loads(msg)
                msg_type = data.get("type", "")
                if msg_type in ("progress", "execution_start", "execution_cached"):
                    message_callback(data)
                if msg_type == "executing":
                    message_callback(data)
                    # ComfyUI 全部节点执行完毕时发送 executing + node:null
                    if data.get("data", {}).get("node") is None:
                        log.info(f"[ComfyUI Client] >>> _track_progress 全部节点执行完毕, prompt_id={prompt_id}")
                        break
                if msg_type == "executed":
                    log.info(f"[ComfyUI Client] >>> _track_progress 收到 executed, node={data.get('data', {}).get('node')}")
                    # 不 break — 多输出工作流会有多个 executed，需等全部完成
                if msg_type == "execution_error":
                    message_callback(data)
                    raise RuntimeError(f"ComfyUI execution error: {json.dumps(data.get('data', {}))}")
        finally:
            ws.close()
            log.info(f"[ComfyUI Client] >>> _track_progress WS 已关闭, prompt_id={prompt_id}")

