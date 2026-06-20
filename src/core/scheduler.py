# -*- coding: utf-8 -*-
"""
Kalm 全局任务调度器 (TaskScheduler)
==========================================
纯中转控制站的核心：FIFO 队列 + 动态执行器加载 + 服务生命周期管理。

设计原则：
  - 单线程串行消费，适配单 GPU 场景（4090 + 64GB）。
  - 服务智能启停：任务到达时自动启动，空闲超时后回收。
  - 执行器由 config.yaml 驱动，通过 importlib 动态加载。
  - 不包含任何业务逻辑——只做排队、分发、透传。
"""

import threading
import queue
import time
import importlib
import subprocess
import requests
from typing import Dict, Any, Optional

from src.logic.logger import log
from src.core.service_controller import service_controller
from src.core.task_manager import TaskManager
from src.core.ws_manager import ws_manager
from src.logic.yaml_config_loader import yaml_config_loader

DEFAULT_SERVICE_IDLE_TIMEOUT = 3


class TaskScheduler:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(TaskScheduler, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.task_queue: queue.Queue = queue.Queue()
        self.current_service_name: Optional[str] = None
        self.is_running = False
        self.worker_thread = None

        self.idle_timeout = yaml_config_loader.get("scheduler.idle_timeout", DEFAULT_SERVICE_IDLE_TIMEOUT)
        self.startup_timeout = yaml_config_loader.get("scheduler.startup_timeout", 300)
        self.download_idle_timeout = yaml_config_loader.get("scheduler.download_idle_timeout", 10)

        tasks_config = yaml_config_loader.get("tasks", {})
        if not tasks_config:
            log.warning("[Scheduler] 'tasks' configuration is empty or missing.")

        self.task_service_map: Dict[str, Optional[str]] = {}
        self.task_executors: Dict[str, callable] = {}
        self.task_track_modes: Dict[str, str] = {}
        self.task_has_outputs: Dict[str, bool] = {}  # 任务是否有可下载的产出文件

        for task_type, task_config in tasks_config.items():
            service_name = task_config.get("service")
            self.task_service_map[task_type] = service_name

            track_mode = task_config.get("track_mode", "poll")
            self.task_track_modes[task_type] = track_mode

            # 默认有产出文件（TTS/ComfyUI），显式配置 false 则跳过下载等待（LLM 等）
            self.task_has_outputs[task_type] = task_config.get("has_outputs", True)

            executor_config = task_config.get("executor", {})
            module_path = executor_config.get("module")
            function_name = executor_config.get("function")

            if module_path and function_name:
                try:
                    module = importlib.import_module(module_path)
                    executor_func = getattr(module, function_name)
                    self.task_executors[task_type] = executor_func
                    log.info(f"[Scheduler] Registered '{task_type}': service={service_name}")
                except Exception as e:
                    log.error(f"[Scheduler] Failed to load executor for '{task_type}': {e}", exc_info=True)
            else:
                log.warning(f"[Scheduler] Task '{task_type}' has incomplete executor config")

        # 云端大模型并发限制与线程池初始化
        from concurrent.futures import ThreadPoolExecutor
        self.max_concurrent_api_tasks = yaml_config_loader.get("llm_providers.api_concurrency_limit", 2)
        self.api_executor = ThreadPoolExecutor(max_workers=self.max_concurrent_api_tasks, thread_name_prefix="CloudAPIWorker")
        self.active_cloud_tasks = 0
        self.api_lock = threading.Lock()
        self.stream_events = {}  # task_id -> {"event": started_event, "queue": output_queue}

        # 初始化大模型底座提供商管理器
        from src.core.providers.llm.manager import llm_provider_manager
        log.info(f"[Scheduler] LLM Provider Gateway initialized. Concurrency limit: {self.max_concurrent_api_tasks}")

        self._start_worker()
        self._initialized = True

    def _start_worker(self):
        if self.is_running:
            return
        self.is_running = True
        self.worker_thread = threading.Thread(
            target=self._scheduler_loop, daemon=True, name="TaskSchedulerWorker"
        )
        self.worker_thread.start()
        log.info(f"[Scheduler] Worker thread started. Idle timeout: {self.idle_timeout}s")

    def submit_task(self, task_type: str, task_id: str, payload: Dict[str, Any],
                    output_queue=None, started_event=None) -> bool:
        """
        统一提交任务入口。支持双轨分流：
        - 本地 GPU 任务：进入原本的 FIFO 串行队列。
        - 云端 API 任务：直接进入并行 ThreadPoolExecutor 并发处理。
        返回 can_stream_immediately: bool，表示该任务是否可以免 WS 握手直接发起流式连接。
        """
        track_mode = self.task_track_modes.get(task_type, "poll")
        model_name = payload.get("model", "")

        # 判定是否为云端 API 任务 (通过大模型底座提供商路由判断，若不是 'ollama' 均路由至云端并行轨道)
        is_cloud_task = False
        if task_type in ("llm", "llm_streaming") and model_name:
            from src.core.providers.llm.manager import llm_provider_manager
            provider_instance = llm_provider_manager.get_provider_for_model(model_name)
            if provider_instance and provider_instance.name != "ollama":
                is_cloud_task = True

        # 对所有流式任务，在调度器中注册其等待信号和数据队列以供统一的透明代理端点连接
        if task_type == "llm_streaming":
            import queue
            if started_event is None:
                started_event = threading.Event()
            if output_queue is None:
                output_queue = queue.Queue(maxsize=256)
            
            with self.api_lock:
                self.stream_events[task_id] = {
                    "event": started_event,
                    "queue": output_queue
                }

        if is_cloud_task:
            log.info(f"[Scheduler] >>> submit_task (云端 API): {task_id} ({task_type}), model={model_name}")
            
            with self.api_lock:
                can_stream = self.active_cloud_tasks < self.max_concurrent_api_tasks
            
            # 提交到 ThreadPoolExecutor 线程池运行！
            self.api_executor.submit(
                self._run_cloud_task, task_type, task_id, payload, output_queue, started_event
            )
            
            log.info(f"[Scheduler] Cloud task {task_id} submitted. can_stream_immediately={can_stream}")
            return can_stream

        # 本地 GPU 串行轨道
        log.info(f"[Scheduler] >>> submit_task (本地 GPU): {task_id} ({task_type}), 当前队列大小={self.task_queue.qsize()}")
        task_item = {
            "type": task_type, "id": task_id, "payload": payload,
            "output_queue": output_queue, "started_event": started_event,
            "track_mode": track_mode,
        }
        self.task_queue.put(task_item)
        log.info(f"[Scheduler] >>> submit_task 已入队: {task_id} ({task_type}), 新队列大小={self.task_queue.qsize()}")
        log.info(f"[Scheduler] Task {task_id} ({task_type}) enqueued. Queue size: {self.task_queue.qsize()}")
        
        # 广播入队通知
        ws_manager.broadcast_queue({
            "type": "task_enqueued",
            "task_id": task_id,
            "task_type": task_type,
            "track_mode": track_mode,
        })
        return False

    def _run_cloud_task(self, task_type: str, task_id: str, payload: Dict[str, Any],
                        output_queue, started_event):
        """
        云端 API 并发执行线程。
        """
        track_mode = self.task_track_modes.get(task_type, "poll")
        
        # 检查是否需要更新为 STATUS_RUNNING。
        # 如果是闲时即时流，状态早已在 API 路由层被更新为 RUNNING。
        # 如果是忙时排队，当前任务仍为 QUEUED，线程被调度执行时代表“轮到它了”，我们需要将它变更为 RUNNING，并向 WS 广播“活来了！”
        task_info = TaskManager.get_task(task_id)
        if task_info and task_info.get("status") == TaskManager.STATUS_FAILED:
            log.info(f"[Scheduler] Cloud task {task_id} was cancelled before starting. Skipping.")
            if output_queue is not None:
                output_queue.put(None)
            ws_manager.broadcast_queue({
                "type": "task_completed",
                "task_id": task_id,
                "next_task_id": None,
                "next_task_type": None,
                "next_track_mode": None,
            })
            return

        if task_info and task_info.get("status") == TaskManager.STATUS_QUEUED:
            TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Cloud task out of queue, starting..."})
            # 向前端 WS 管道广播：任务正在执行（前端收到此消息即可瞬间触发 GET /stream 连接！）
            ws_manager.send(task_id, {
                "type": "task_running",
                "status": TaskManager.STATUS_RUNNING,
                "task_id": task_id
            })

        with self.api_lock:
            self.active_cloud_tasks += 1
            log.info(f"[Scheduler] Cloud API task {task_id} executing. Active: {self.active_cloud_tasks}/{self.max_concurrent_api_tasks}")

        # 广播全局队列任务启动通知
        ws_manager.broadcast_queue({
            "type": "task_started",
            "task_id": task_id,
            "task_type": task_type,
            "track_mode": track_mode,
        })

        try:
            if TaskManager.get_task(task_id).get("status") == TaskManager.STATUS_FAILED:
                raise RuntimeError("任务已被用户手动停止")
            executor = self.task_executors.get(task_type)
            if executor:
                log.info(f"[Scheduler] >>> 开始执行云端任务: {task_id} ({task_type}), executor={executor.__module__}.{executor.__name__}")
                if output_queue is not None:
                    # 云端流式执行器，支持开始事件同步
                    executor(task_id, output_queue, started_event, **payload)
                else:
                    executor(task_id, **payload)
                log.info(f"[Scheduler] >>> 云端任务执行完成: {task_id} ({task_type})")
            else:
                log.error(f"[Scheduler] No executor registered for cloud task: '{task_type}'")
                TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {
                    "message": f"No executor for cloud task {task_type}"
                })
                if output_queue:
                    output_queue.put(None)
        except Exception as e:
            if "任务已被用户手动停止" in str(e):
                log.info(f"[Scheduler] Cloud task {task_id} was cancelled by user.")
            else:
                error_msg = f"Cloud task execution failed: {str(e)}"
                log.error(f"[Scheduler] {error_msg}", exc_info=True)
            TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
            if output_queue:
                output_queue.put(None)
        finally:
            with self.api_lock:
                self.active_cloud_tasks -= 1
                self.stream_events.pop(task_id, None)
                log.info(f"[Scheduler] Cloud API task {task_id} completed. Active: {self.active_cloud_tasks}/{self.max_concurrent_api_tasks}")
            
            # 广播全局队列任务完成通知
            ws_manager.broadcast_queue({
                "type": "task_completed",
                "task_id": task_id,
                "next_task_id": None,
                "next_task_type": None,
                "next_track_mode": None,
            })

    def _scheduler_loop(self):
        while self.is_running:
            try:
                timeout = self.idle_timeout if self.current_service_name else None

                try:
                    task = self.task_queue.get(timeout=timeout)
                except queue.Empty:
                    if self.current_service_name:
                        if not service_controller.is_auto_start(self.current_service_name):
                            log.info(f"[Scheduler] Service '{self.current_service_name}' idle for {self.idle_timeout}s, stopping.")
                            self._stop_current_service()
                    continue

                if task is None:
                    break

                task_type = task["type"]
                task_id = task["id"]
                payload = task["payload"]
                output_queue = task.get("output_queue")
                started_event = task.get("started_event")

                log.info(f"[Scheduler] >>> 从队列取出: {task_id} ({task_type}), 剩余队列大小={self.task_queue.qsize()}")

                log.info(f"[Scheduler] Processing task {task_id} ({task_type})")

                task_info = TaskManager.get_task(task_id)
                if task_info and task_info.get("status") == TaskManager.STATUS_FAILED:
                    log.info(f"[Scheduler] Task {task_id} was cancelled before execution. Skipping.")
                    self.task_queue.task_done()
                    next_type = self._peek_next_task_type()
                    next_info = {
                        "type": "task_completed",
                        "task_id": task_id,
                        "next_task_id": None,
                        "next_task_type": None,
                        "next_track_mode": None,
                    }
                    if next_type:
                        peek = self.task_queue.queue[0]
                        next_info["next_task_id"] = peek["id"]
                        next_info["next_task_type"] = next_type
                        next_info["next_track_mode"] = peek.get("track_mode", self.task_track_modes.get(next_type, "poll"))
                    ws_manager.broadcast_queue(next_info)
                    continue

                # 通知流式端点：任务已开始执行
                if started_event:
                    started_event.set()

                # 广播任务开始
                track_mode = task.get("track_mode", self.task_track_modes.get(task_type, "poll"))
                ws_manager.broadcast_queue({
                    "type": "task_started",
                    "task_id": task_id,
                    "task_type": task_type,
                    "track_mode": track_mode,
                })

                # 服务管理
                required_service = self.task_service_map.get(task_type)
                if required_service:
                    if self.current_service_name != required_service:
                        if self.current_service_name:
                            log.info(f"[Scheduler] Switching service: '{self.current_service_name}' -> '{required_service}'")
                            self._stop_current_service()
                        self._start_service(required_service)
                    else:
                        log.info(f"[Scheduler] Reusing active service '{self.current_service_name}'")

                # 执行任务（传入服务名，executor 不需要硬编码）
                self._execute_task_logic(task_type, task_id, payload, output_queue, required_service)
                self.task_queue.task_done()

                # 广播任务完成（含下一任务信息）
                next_type = self._peek_next_task_type()
                next_info = {
                    "type": "task_completed",
                    "task_id": task_id,
                    "next_task_id": None,
                    "next_task_type": None,
                    "next_track_mode": None,
                }
                if next_type:
                    peek = self.task_queue.queue[0]
                    next_info["next_task_id"] = peek["id"]
                    next_info["next_task_type"] = next_type
                    next_info["next_track_mode"] = peek.get("track_mode", self.task_track_modes.get(next_type, "poll"))
                ws_manager.broadcast_queue(next_info)

                # 标记产出并等待客户端下载完成，才处理下一个任务
                # auto_start 常驻服务无需等待下载，服务不会关闭
                # has_outputs=false 的任务（如 LLM）结果直接在响应里，无文件可下载，跳过
                if required_service and not service_controller.is_auto_start(required_service):
                    if not self.task_has_outputs.get(task_type, True):
                        log.info(f"[Scheduler] Task type '{task_type}' has no downloadable outputs, skipping download wait.")
                    else:
                        task_info = TaskManager.get_task(task_id)
                        if task_info and task_info.get("status") == TaskManager.STATUS_FAILED:
                            log.info(f"[Scheduler] Task {task_id} failed, skipping download wait.")
                        else:
                            service_controller.mark_has_outputs(required_service)
                            service_controller._wait_downloads(required_service, timeout=self.download_idle_timeout)

                # 智能释放：同类型下一个任务跳过释放，不同类型或队列为空则释放
                if required_service:
                    self._maybe_free_resources(required_service, task_type)

            except Exception as e:
                log.error(f"[Scheduler] Critical error in scheduler loop: {e}", exc_info=True)
                time.sleep(1)

    def _start_service(self, service_name: str):
        log.info(f"[Scheduler] Starting service '{service_name}'...")
        try:
            service_controller.safe_start(service_name, timeout=self.startup_timeout)
            self.current_service_name = service_name
            log.info(f"[Scheduler] Service '{service_name}' is ready.")
        except Exception as e:
            log.error(f"[Scheduler] Failed to start service '{service_name}': {e}")
            self.current_service_name = None
            raise e

    def _stop_current_service(self):
        if not self.current_service_name:
            return
        if service_controller.is_auto_start(self.current_service_name):
            self.current_service_name = None
            return
        log.info(f"[Scheduler] Stopping service '{self.current_service_name}'...")
        try:
            service_controller.stop(self.current_service_name)
        except Exception as e:
            log.error(f"[Scheduler] Error stopping service '{self.current_service_name}': {e}")
        finally:
            self.current_service_name = None

    def _peek_next_task_type(self) -> str | None:
        """查看队列中下一个任务的类型（不取出），用于决策是否释放资源。"""
        if self.task_queue.empty():
            return None
        return self.task_queue.queue[0]["type"]

    def _maybe_free_resources(self, service_name: str, current_task_type: str):
        """
        根据队列中下一个任务类型决定是否释放当前服务资源。
        下一个任务是同类型 → 跳过释放，模型常驻，避免重复加载。
        下一个任务是不同类型或队列为空 → 释放资源。
        """
        next_type = self._peek_next_task_type()
        if next_type == current_task_type:
            log.info(f"[Scheduler] Next task is same type ({current_task_type}), skipping free.")
            return
        log.info(f"[Scheduler] Next task '{next_type}' != current '{current_task_type}', freeing resources...")
        self._free_service_resources(service_name)

    @staticmethod
    def _get_gpu_memory() -> str:
        """查询 GPU 显存用量，返回人类可读字符串。nvidia-smi 不可用时返回 'N/A'。"""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "N/A"

    def _free_service_resources(self, service_name: str):
        """调用后端服务的资源释放接口。由 services.yaml 中的 free_api 配置驱动。"""
        svc_config = service_controller.get_service_config(service_name)
        if not svc_config:
            return
        free_api = svc_config.get("free_api")
        if not free_api:
            log.info(f"[Scheduler] Service '{service_name}' has no free_api configured, skipping resource release.")
            return

        service_url = service_controller.get_service_url(service_name)
        if not service_url:
            return
        free_url = f"{service_url}{free_api['path']}"
        body = free_api.get("body", {})

        gpu_before = self._get_gpu_memory()
        log.info(f"[Scheduler] Calling free API: POST {free_url} body={body}")
        try:
            resp = requests.post(free_url, json=body, timeout=10)
            if resp.status_code == 200:
                gpu_after = self._get_gpu_memory()
                log.success(f"[Scheduler] Freed resources for {service_name} | GPU: {gpu_before} -> {gpu_after} | response: {resp.text[:200]}")
            else:
                log.warning(f"[Scheduler] Free returned {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            log.warning(f"[Scheduler] Free failed (non-critical): {e}")

    def _execute_task_logic(self, task_type: str, task_id: str, payload: Dict[str, Any],
                            output_queue=None, service_name=None):
        try:
            executor = self.task_executors.get(task_type)
            if executor:
                # 注入服务名到 payload，executor 不硬编码服务名
                if service_name:
                    payload["_service_name"] = service_name
                log.info(f"[Scheduler] >>> 开始执行: {task_id} ({task_type}), executor={executor.__module__}.{executor.__name__}, service={service_name}")
                if output_queue is not None:
                    executor(task_id, output_queue, **payload)
                else:
                    executor(task_id, **payload)
                log.info(f"[Scheduler] >>> 执行完成: {task_id} ({task_type})")
            else:
                log.error(f"[Scheduler] Unknown task type: '{task_type}'. No executor registered.")
                TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {
                    "message": f"Unknown task type: {task_type}"
                })
                if output_queue:
                    output_queue.put(None)
        except Exception as e:
            if "任务已被用户手动停止" in str(e):
                log.info(f"[Scheduler] Task {task_id} was cancelled by user.")
            else:
                error_msg = f"Task execution failed: {str(e)}"
                log.error(f"[Scheduler] {error_msg}", exc_info=True)
            try:
                TaskManager.update_task(task_id, TaskManager.STATUS_FAILED, {"message": str(e)})
            except Exception as update_error:
                log.error(f"[Scheduler] Failed to update task status for {task_id}: {update_error}")
            if output_queue:
                output_queue.put(None)

    def shutdown(self, wait: bool = True):
        if not self.is_running:
            return
        log.info("[Scheduler] Shutting down...")
        self.is_running = False
        try:
            self.task_queue.put(None)
        except Exception as e:
            log.warning(f"[Scheduler] Failed to enqueue poison pill: {e}")

        if wait and self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5)
            log.info("[Scheduler] Worker thread terminated.")

        while not self.task_queue.empty():
            try:
                task = self.task_queue.get_nowait()
                if task and task is not None:
                    task_id = task.get("id")
                    if task_id:
                        log.warning(f"[Scheduler] Cancelling pending task {task_id}")
                        TaskManager.update_task(task_id, TaskManager.STATUS_FAILED,
                            {"message": "Service shutting down, task cancelled."})
                self.task_queue.task_done()
            except queue.Empty:
                break

        if self.current_service_name:
            self._stop_current_service()
        log.info("[Scheduler] Shutdown complete.")


scheduler = TaskScheduler()
