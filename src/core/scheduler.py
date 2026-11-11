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

import asyncio
import threading
import queue
import time
import importlib
import subprocess
import requests
from typing import Dict, Any, Optional, Callable

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
        self.is_local_busy = False

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
            if not task_config.get("enabled", True):
                continue

            service_name = task_config.get("service")
            if service_name and not service_controller.is_enabled(service_name):
                continue

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

        # 资产就绪等待事件池 (task_id -> threading.Event)
        self._asset_events: Dict[str, threading.Event] = {}
        self._asset_events_lock = threading.Lock()

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

    def notify_asset_ready(self, task_id: str):
        """客户端通过接口上传完成素材后调用，解除调度器的等待阻塞"""
        with self._asset_events_lock:
            event = self._asset_events.get(task_id)
            if event:
                event.set()
                log.info(f"[Scheduler] Asset ready event signaled for task {task_id}")

    def purge_client_tasks(self, client_id: str, reason: str = "客户端失联或超时，任务已连坐清理") -> list:
        """
        连坐清理指定 client_id 的所有排队/等待中任务：
        1. 调用 TaskManager.purge_client_tasks 批量更新状态为 failed；
        2. 从 self.task_queue 中剔除对应任务；
        3. 解除该客户端可能正在等待资产的 event；
        4. 广播 task_completed (failed) 通知。
        """
        if not client_id:
            return []
        purged_ids = TaskManager.purge_client_tasks(client_id, reason)
        if not purged_ids:
            return []
        log.warning(f"[Scheduler] Purging {len(purged_ids)} tasks for client '{client_id}': {purged_ids}. Reason: {reason}")

        # 唤醒可能正在等待资产上传的 event，让调度循环能够迅速脱身
        with self._asset_events_lock:
            for tid in purged_ids:
                evt = self._asset_events.get(tid)
                if evt:
                    evt.set()

        # 从 task_queue 中剔除属于该 client_id 的未运行任务
        import collections
        with self.task_queue.mutex:
            old_len = len(self.task_queue.queue)
            remaining = collections.deque(
                item for item in self.task_queue.queue 
                if item is not None and (self._is_internal_item(item) or item.get("client_id") != client_id)
            )
            removed_count = old_len - len(remaining)
            self.task_queue.queue = remaining
            self.task_queue.unfinished_tasks -= removed_count

        # 广播连坐清理通知
        for tid in purged_ids:
            ws_manager.clear_task_progress(tid)
            ws_manager.emit_to_client(client_id, {
                "type": "task_completed",
                "task_id": tid,
                "client_id": client_id,
                "status": TaskManager.STATUS_FAILED,
                "result": {"message": reason},
                "next_task_id": None,
                "next_task_type": None,
                "next_track_mode": None,
                "message": reason,
            })
        self.broadcast_queue_positions()
        return purged_ids

    def submit_task(self, task_type: str, task_id: str, payload: Dict[str, Any],
                    output_queue=None, started_event=None, client_id: Optional[str] = None,
                    asset_status: str = "ready", required_assets: Optional[list] = None) -> bool:
        """
        统一提交任务入口。支持双轨分流与 JIT 按需延迟素材：
        - 本地 GPU 任务：进入原本的 FIFO 串行队列。
        - 云端 API 任务：直接进入并行 ThreadPoolExecutor 并发处理。
        返回 can_stream_immediately: bool，表示该任务是否可以免 WS 握手直接发起流式连接。
        """
        if not client_id or not str(client_id).strip():
            raise ValueError(f"[Scheduler] Task {task_id} is missing required client_id! 严禁无 client_id 提交任务，程序中断。")

        track_mode = self.task_track_modes.get(task_type, "poll")
        model_name = payload.get("model", "")

        # 判定是否为云端 API 任务 (根据大模型 tier 属性判断："api" 走云端并发，"local" 走本地串行)
        is_cloud_task = False
        if task_type in ("llm", "llm_streaming") and model_name:
            from src.core.providers.llm.manager import llm_provider_manager
            model_tier = llm_provider_manager.get_model_tier(model_name)
            if model_tier == "api":
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
            log.info(f"[Scheduler] >>> submit_task (云端 API): {task_id} ({task_type}), model={model_name}, client_id={client_id}")
            
            with self.api_lock:
                can_stream = self.active_cloud_tasks < self.max_concurrent_api_tasks
            
            # 广播入队通知（云端并发无本地队列顺位排队，position=0）
            ws_manager.emit_to_client(client_id, {
                "type": "task_enqueued",
                "task_id": task_id,
                "task_type": task_type,
                "client_id": client_id,
                "track_mode": track_mode,
                "queue_position": 0,
                "total_queued": self.task_queue.qsize(),
                "asset_status": asset_status,
                "message": "云端 API 任务已提交，准备执行",
            })

            # 提交到 ThreadPoolExecutor 线程池运行！
            self.api_executor.submit(
                self._run_cloud_task, task_type, task_id, payload, output_queue, started_event, client_id
            )
            
            log.info(f"[Scheduler] Cloud task {task_id} submitted. can_stream_immediately={can_stream}")
            return can_stream

        # 本地 GPU 串行轨道
        with self.api_lock:
            # 当队列为空且当前本地 worker 空闲时，本地流式任务也支持免 WS 握手即时建流
            can_stream = self.task_queue.empty() and not self.is_local_busy

        log.info(f"[Scheduler] >>> submit_task (本地 GPU): {task_id} ({task_type}), asset_status={asset_status}, 当前队列大小={self.task_queue.qsize()}, can_stream_immediately={can_stream}")
        task_item = {
            "type": task_type, "id": task_id, "payload": payload,
            "output_queue": output_queue, "started_event": started_event,
            "track_mode": track_mode, "client_id": client_id,
            "asset_status": asset_status, "required_assets": required_assets or [],
        }
        self.task_queue.put(task_item)
        q_size = self.task_queue.qsize()
        log.info(f"[Scheduler] >>> submit_task 已入队: {task_id} ({task_type}), 新队列大小={q_size}")
        log.info(f"[Scheduler] Task {task_id} ({task_type}) enqueued. Queue size: {q_size}")
        
        # 广播入队通知（携带当前在队列中的排位与资产状态）
        ws_manager.emit_to_client(client_id, {
            "type": "task_enqueued",
            "task_id": task_id,
            "task_type": task_type,
            "client_id": client_id,
            "track_mode": track_mode,
            "queue_position": q_size,
            "total_queued": q_size,
            "asset_status": asset_status,
            "message": f"任务已排入队列，前方排队 {q_size - 1} 人" if q_size > 1 else "任务已排入队列，即将出队执行",
        })
        return can_stream

    def _run_cloud_task(self, task_type: str, task_id: str, payload: Dict[str, Any],
                        output_queue, started_event, client_id: Optional[str] = None):
        """
        云端 API 并发执行线程。
        """
        track_mode = self.task_track_modes.get(task_type, "poll")
        if not client_id or not str(client_id).strip():
            raise ValueError(f"[Scheduler] Cloud task {task_id} is missing required client_id! 程序中断。")
        
        # 检查是否需要更新为 STATUS_RUNNING。
        task_info = TaskManager.get_task(task_id)
        if task_info and task_info.get("status") == TaskManager.STATUS_FAILED:
            log.info(f"[Scheduler] Cloud task {task_id} was cancelled before starting. Skipping.")
            if output_queue is not None:
                output_queue.put(None)
            ws_manager.clear_task_progress(task_id)
            ws_manager.emit_to_client(client_id, {
                "type": "task_completed",
                "task_id": task_id,
                "task_type": task_type,
                "client_id": client_id,
                "status": TaskManager.STATUS_FAILED,
                "result": task_info.get("result", {}),
                "next_task_id": None,
                "next_task_type": None,
                "next_track_mode": None,
            })
            return

        if task_info and task_info.get("status") == TaskManager.STATUS_QUEUED:
            TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Cloud task out of queue, starting..."})
            # 频道 A: 推送至单任务专有临时连接 /interface/tasks/{id}/ws (兼容老版多轨客户端，未连接时不发包)
            ws_manager.send(task_id, {
                "type": "task_started",
                "status": TaskManager.STATUS_RUNNING,
                "task_id": task_id,
                "message": "云端 API 任务开始执行"
            })

        with self.api_lock:
            self.active_cloud_tasks += 1
            log.info(f"[Scheduler] Cloud API task {task_id} executing. Active: {self.active_cloud_tasks}/{self.max_concurrent_api_tasks}")

        # 频道 B: 推送至单轨全局长连接 /interface/queue/ws?client_id={id} (《下游接入契约 V4》新版单轨规范，严格定向)
        ws_manager.emit_to_client(client_id, {
            "type": "task_started",
            "task_id": task_id,
            "task_type": task_type,
            "client_id": client_id,
            "track_mode": track_mode,
            "message": "云端 API 任务开始执行",
        })

        try:
            if TaskManager.get_task(task_id).get("status") == TaskManager.STATUS_FAILED:
                raise RuntimeError("任务已被用户手动停止")
            executor = self.task_executors.get(task_type)
            if executor:
                log.info(f"[Scheduler] >>> 开始执行云端任务: {task_id} ({task_type}), executor={executor.__module__}.{executor.__name__}")
                if output_queue is not None:
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
            
            # 清理节流并广播单轨任务完成通知（携带最新终态与产物）
            ws_manager.clear_task_progress(task_id)
            final_task_info = TaskManager.get_task(task_id)
            ws_manager.emit_to_client(client_id, {
                "type": "task_completed",
                "task_id": task_id,
                "task_type": task_type,
                "client_id": client_id,
                "status": final_task_info.get("status", "success"),
                "result": final_task_info.get("result", {}),
                "next_task_id": None,
                "next_task_type": None,
                "next_track_mode": None,
                "message": "云端 API 任务已完成",
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

                if self._is_internal_item(task):
                    self._execute_internal_service_task(task)
                    self.task_queue.task_done()
                    continue

                task_type = task["type"]
                task_id = task["id"]
                client_id = task.get("client_id")
                if not client_id or not str(client_id).strip():
                    raise ValueError(f"[Scheduler] Local task {task_id} in queue is missing required client_id! 程序中断。")
                payload = task["payload"]
                output_queue = task.get("output_queue")
                started_event = task.get("started_event")

                log.info(f"[Scheduler] >>> 从队列取出: {task_id} ({task_type}), 剩余队列大小={self.task_queue.qsize()}")

                log.info(f"[Scheduler] Processing task {task_id} ({task_type})")

                # 1. 检查客户端离线状态（失联检测）
                if client_id:
                    offline_dur = ws_manager.get_client_offline_duration(client_id)
                    if offline_dur is not None and offline_dur > 30.0:
                        log.warning(f"[Scheduler] Client '{client_id}' offline for {offline_dur:.1f}s (>30s). Purging client tasks.")
                        self.purge_client_tasks(client_id, reason="客户端离线超过30秒，任务已连坐清理")
                        self.task_queue.task_done()
                        continue

                self.is_local_busy = True
                try:
                    task_info = TaskManager.get_task(task_id)
                    if task_info and task_info.get("status") == TaskManager.STATUS_FAILED:
                        log.info(f"[Scheduler] Task {task_id} was cancelled before execution. Skipping.")
                        self.task_queue.task_done()
                        peek = self._peek_next_user_task()
                        next_info = {
                            "type": "task_completed",
                            "task_id": task_id,
                            "task_type": task_type,
                            "client_id": client_id,
                            "status": TaskManager.STATUS_FAILED,
                            "result": task_info.get("result", {}),
                            "next_task_id": None,
                            "next_task_type": None,
                            "next_track_mode": None,
                        }
                        if peek:
                            next_type = peek["type"]
                            next_info["next_task_id"] = peek["id"]
                            next_info["next_task_type"] = next_type
                            next_info["next_track_mode"] = peek.get("track_mode", self.task_track_modes.get(next_type, "poll"))
                        ws_manager.clear_task_progress(task_id)
                        ws_manager.emit_to_client(client_id, next_info)
                        self.broadcast_queue_positions()
                        continue

                    # 2. 按需延迟资产等待 (JIT Asset Whistling)
                    initial_asset_status = task.get("asset_status", "ready")
                    initial_required_assets = task.get("required_assets", [])
                    current_asset_status = task_info.get("asset_status", initial_asset_status) if task_info else initial_asset_status
                    whistled_assets = False
                    if current_asset_status != "ready":
                        whistled_assets = True
                        log.info(f"[Scheduler] Task {task_id} requires assets (status='{current_asset_status}'). Whistling task_asset_required...")
                        TaskManager.update_task(task_id, TaskManager.STATUS_AWAITING_ASSETS, {"message": "正在等待客户端上传资产素材"})
                        
                        asset_event = threading.Event()
                        with self._asset_events_lock:
                            self._asset_events[task_id] = asset_event

                        track_mode = task.get("track_mode", self.task_track_modes.get(task_type, "poll"))
                        ws_manager.emit_to_client(client_id, {
                            "type": "task_asset_required",
                            "task_id": task_id,
                            "task_type": task_type,
                            "client_id": client_id,
                            "track_mode": track_mode,
                            "required_assets": task_info.get("required_assets", initial_required_assets) if task_info else initial_required_assets,
                            "upload_url": f"/interface/tasks/{task_id}/assets",
                            "timeout": 30,
                            "message": "轮到您的任务了！请在30秒内上传素材文件以开闸运行",
                        })
                        # 🌟 唯一时机 1A：任务出队进入素材吹哨，有且仅广播 1 次剩余排队顺位
                        self.broadcast_queue_positions()

                        # 等待客户端响应
                        asset_timeout = yaml_config_loader.get("scheduler.asset_upload_timeout", 30)
                        uploaded = asset_event.wait(timeout=asset_timeout)

                        # 防误杀检查：若超时唤醒但处于正在上传中 (STATUS_ASSET_UPLOADING)，给予流式传输宽限期
                        check_task = TaskManager.get_task(task_id) or {}
                        if not uploaded and check_task.get("status") == TaskManager.STATUS_ASSET_UPLOADING:
                            log.info(f"[Scheduler] Task {task_id} is actively uploading files. Extending wait up to 300s...")
                            uploaded = asset_event.wait(timeout=300)
                            check_task = TaskManager.get_task(task_id) or {}

                        with self._asset_events_lock:
                            self._asset_events.pop(task_id, None)

                        # 检查等待期间是否被取消
                        if check_task.get("status") == TaskManager.STATUS_FAILED:
                            log.warning(f"[Scheduler] Task {task_id} was cancelled during asset upload wait.")
                            self.task_queue.task_done()
                            continue

                        # 超时未上传完成判定
                        if not uploaded or check_task.get("asset_status") != "ready":
                            reason = f"任务素材上传超时({asset_timeout}s)，已连坐取消所有排队任务"
                            log.warning(f"[Scheduler] Task {task_id} asset upload timed out. Purging client '{client_id}'.")
                            self.purge_client_tasks(client_id, reason=reason)
                            self.task_queue.task_done()
                            continue

                        # 素材已就绪，合并最新 payload
                        extra_payload = TaskManager.get_task_payload(task_id)
                        if extra_payload:
                            payload.update(extra_payload)
                        log.info(f"[Scheduler] Task {task_id} assets READY! Proceeding to execution.")

                    # 如果任务还在排队或等待资产状态，正式起跑变更为 RUNNING，并向 WS 广播 task_running 起跑信号
                    latest_status = TaskManager.get_task(task_id).get("status")
                    if latest_status in (TaskManager.STATUS_QUEUED, TaskManager.STATUS_AWAITING_ASSETS):
                        TaskManager.update_task(task_id, TaskManager.STATUS_RUNNING, {"message": "Local task out of queue, starting..."})
                        # 频道 A: 推送至单任务专有临时连接 /interface/tasks/{id}/ws (兼容老版多轨客户端，未连接时不发包)
                        ws_manager.send(task_id, {
                            "type": "task_started",
                            "status": TaskManager.STATUS_RUNNING,
                            "task_id": task_id,
                            "message": "本地 GPU 任务开始执行"
                        })

                    # 通知流式端点：任务已开始执行
                    if started_event:
                        started_event.set()

                    # 频道 B: 推送至单轨全局长连接 /interface/queue/ws?client_id={id} (《下游接入契约 V4》新版单轨规范，严格定向)
                    track_mode = task.get("track_mode", self.task_track_modes.get(task_type, "poll"))
                    ws_manager.emit_to_client(client_id, {
                        "type": "task_started",
                        "task_id": task_id,
                        "task_type": task_type,
                        "client_id": client_id,
                        "track_mode": track_mode,
                        "message": "本地 GPU 任务开始执行",
                    })
                    # 🌟 唯一时机 1B：任务出队直接起跑，有且仅广播 1 次（若在吹哨时已广播则不再重复广播）
                    if not whistled_assets:
                        self.broadcast_queue_positions()

                    # 服务管理：大模型任务根据模型动态获取物理微服务名（如 "Ollama"），其他任务从配置表映射
                    if task_type in ("llm", "llm_streaming"):
                        from src.core.providers.llm.manager import llm_provider_manager
                        model_name = payload.get("model", "")
                        required_service = llm_provider_manager.get_service_name_for_model(model_name)
                    else:
                        required_service = self.task_service_map.get(task_type)

                    if required_service:
                        if self.current_service_name != required_service:
                            if self.current_service_name:
                                log.info(f"[Scheduler] Switching service: '{self.current_service_name}' -> '{required_service}'")
                                self._stop_current_service()
                            self._start_service(required_service)
                        else:
                            log.info(f"[Scheduler] Reusing active service '{self.current_service_name}'")

                    # 执行任务（传入服务名与 started_event，executor 不需要硬编码）
                    self._execute_task_logic(task_type, task_id, payload, output_queue, required_service, started_event)
                    self.task_queue.task_done()
                finally:
                    self.is_local_busy = False

                # 广播任务完成（含下一任务信息与结果详情，带 client_id 定向推送）
                ws_manager.clear_task_progress(task_id)
                task_info = TaskManager.get_task(task_id)
                peek = self._peek_next_user_task()
                next_info = {
                    "type": "task_completed",
                    "task_id": task_id,
                    "task_type": task_type,
                    "client_id": client_id,
                    "status": task_info.get("status", "success"),
                    "result": task_info.get("result", {}),
                    "next_task_id": None,
                    "next_task_type": None,
                    "next_track_mode": None,
                    "message": "本地 GPU 任务执行完成",
                }
                if peek:
                    next_type = peek["type"]
                    next_info["next_task_id"] = peek["id"]
                    next_info["next_task_type"] = next_type
                    next_info["next_track_mode"] = peek.get("track_mode", self.task_track_modes.get(next_type, "poll"))
                ws_manager.emit_to_client(client_id, next_info)

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

    @staticmethod
    def _is_internal_item(item: Any) -> bool:
        """判断队列项是否为调度器内部同步任务（不计入用户排队序列）。"""
        return isinstance(item, dict) and bool(item.get("_internal"))

    def run_with_service(
        self,
        service_name: str,
        action: Callable[[], Any],
        stop_if_started: bool = True,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        在调度器串行通道中按需激活目标服务并执行同步操作，避免与正在运行的 GPU 任务冲突。
        若服务此前未运行且非 auto_start，操作完成后按需关闭（若队首下一项仍需该服务则保留）。
        """
        if not self.is_running:
            raise RuntimeError("TaskScheduler is not running")
        if not service_controller.is_enabled(service_name):
            raise RuntimeError(f"Service '{service_name}' is disabled in services.yaml")

        done_event = threading.Event()
        cancelled_event = threading.Event()
        result_box: Dict[str, Any] = {}

        self.task_queue.put({
            "_internal": True,
            "service_name": service_name,
            "action": action,
            "stop_if_started": stop_if_started,
            "done_event": done_event,
            "cancelled_event": cancelled_event,
            "result_box": result_box,
        })

        wait_timeout = timeout if timeout is not None else (self.startup_timeout + 60)
        if not done_event.wait(timeout=wait_timeout):
            cancelled_event.set()
            raise TimeoutError(f"Timed out waiting for service '{service_name}' action after {wait_timeout}s")

        if "error" in result_box:
            raise result_box["error"]
        return result_box.get("value")

    def _execute_internal_service_task(self, task: Dict[str, Any]):
        """在调度器工作线程中执行内部按需服务探测/查询操作。"""
        done_event: threading.Event = task["done_event"]
        cancelled_event: threading.Event = task["cancelled_event"]
        result_box: Dict[str, Any] = task["result_box"]

        if cancelled_event.is_set():
            done_event.set()
            return

        service_name: str = task["service_name"]
        action: Callable[[], Any] = task["action"]
        stop_if_started: bool = task.get("stop_if_started", True)

        self.is_local_busy = True
        started_by_probe = False
        try:
            was_port_running = bool(service_controller.status(service_name).get("running"))
            if not was_port_running:
                if self.current_service_name and self.current_service_name != service_name:
                    log.info(f"[Scheduler] Switching service: '{self.current_service_name}' -> '{service_name}'")
                    self._stop_current_service()
                self._start_service(service_name)
                started_by_probe = True

            result_box["value"] = action()
        except Exception as e:
            result_box["error"] = e
        finally:
            try:
                if (
                    stop_if_started
                    and started_by_probe
                    and not service_controller.is_auto_start(service_name)
                    and self.current_service_name == service_name
                    and self._peek_next_service_name() != service_name
                ):
                    log.info(f"[Scheduler] Stopping temporary service '{service_name}' after on-demand action.")
                    self._stop_current_service()
            finally:
                self.is_local_busy = False
                done_event.set()

    def _peek_next_user_task(self) -> Optional[Dict[str, Any]]:
        """查看队列中下一个真实用户任务（跳过内部同步探测任务）。"""
        with self.task_queue.mutex:
            for item in self.task_queue.queue:
                if isinstance(item, dict) and not self._is_internal_item(item):
                    return item
        return None

    def _peek_next_task_type(self) -> str | None:
        """查看队列中下一个任务的类型（不取出），用于决策是否释放资源。"""
        nxt = self._peek_next_user_task()
        return nxt.get("type") if nxt else None

    def _peek_next_service_name(self) -> str | None:
        """查看队列中下一个排队项所需的后端服务名称。"""
        with self.task_queue.mutex:
            for item in self.task_queue.queue:
                if not isinstance(item, dict):
                    continue
                if self._is_internal_item(item):
                    return item.get("service_name")
                return self.task_service_map.get(item.get("type"))
        return None

    def _maybe_free_resources(self, service_name: str, current_task_type: str):
        """
        根据队列中下一个任务类型决定是否释放当前服务资源。
        下一个任务是同服务/同类型（如 llm 与 llm_streaming 均为大模型） → 跳过释放，模型常驻，避免重复加载。
        下一个任务是不同类型或队列为空 → 释放资源。
        """
        next_type = self._peek_next_task_type()
        is_same_type = (next_type == current_task_type) or (
            next_type in ("llm", "llm_streaming") and current_task_type in ("llm", "llm_streaming")
        )
        if is_same_type:
            log.info(f"[Scheduler] Next task '{next_type}' shares service with '{current_task_type}', skipping free.")
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
        headers = {}
        token = svc_config.get("token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        gpu_before = self._get_gpu_memory()
        log.info(f"[Scheduler] Calling free API: POST {free_url} body={body}")
        try:
            resp = requests.post(free_url, json=body, headers=headers, timeout=10)
            if resp.status_code == 200:
                gpu_after = self._get_gpu_memory()
                log.success(f"[Scheduler] Freed resources for {service_name} | GPU: {gpu_before} -> {gpu_after} | response: {resp.text[:200]}")
            else:
                log.warning(f"[Scheduler] Free returned {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            log.warning(f"[Scheduler] Free failed (non-critical): {e}")

    def _execute_task_logic(self, task_type: str, task_id: str, payload: Dict[str, Any],
                            output_queue=None, service_name=None, started_event=None):
        try:
            executor = self.task_executors.get(task_type)
            if executor:
                # 注入服务名到 payload，executor 不硬编码服务名
                if service_name:
                    payload["_service_name"] = service_name
                log.info(f"[Scheduler] >>> 开始执行: {task_id} ({task_type}), executor={executor.__module__}.{executor.__name__}, service={service_name}")
                if output_queue is not None:
                    executor(task_id, output_queue, started_event, **payload)
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
                    if self._is_internal_item(task):
                        task["result_box"]["error"] = RuntimeError("Service shutting down, request cancelled.")
                        task["done_event"].set()
                    else:
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

    def get_queue_status(self, client_id: Optional[str] = None) -> Dict[str, Any]:
        """返回当前调度器队列的快照信息。若提供 client_id 则仅筛选归属于该客户端的排队任务。"""
        with self.api_lock:
            with self.task_queue.mutex:
                user_items = [
                    item for item in list(self.task_queue.queue)
                    if isinstance(item, dict) and not self._is_internal_item(item)
                ]
            total_queued = len(user_items)
            is_busy = self.is_local_busy or (self.active_cloud_tasks > 0)
            queued_items = []
            for idx, item in enumerate(user_items, 1):
                item_cid = item.get("client_id")
                if client_id and item_cid != client_id:
                    continue
                queued_items.append({
                    "position": idx,
                    "task_id": item.get("id"),
                    "task_type": item.get("type"),
                    "client_id": item_cid,
                })
            return {
                "total_queued": total_queued,
                "is_busy": is_busy,
                "current_running_service": self.current_service_name,
                "active_cloud_tasks": self.active_cloud_tasks,
                "queue_list": queued_items,
            }

    def get_queue_positions(self, client_id: Optional[str] = None) -> Dict[str, int]:
        """
        获取当前排队等待中任务的 1-based 物理排位字典 { task_id: position }。
        - position: 1 表示排在队伍第 1 位即将开始执行；2 表示前方还有 1 人；以此类推。
        - 若指定 client_id，则仅返回属于该 client_id 的排队任务映射（保持全局物理顺位）；
        - 若未指定 client_id，则返回所有排队任务的排位映射。
        """
        positions = {}
        with self.task_queue.mutex:
            user_items = [
                item for item in self.task_queue.queue
                if isinstance(item, dict) and not self._is_internal_item(item)
            ]
            for idx, item in enumerate(user_items, 1):
                tid = item.get("id")
                if not tid:
                    continue
                item_cid = item.get("client_id")
                if client_id and item_cid != client_id:
                    continue
                positions[tid] = idx
        return positions

    def broadcast_queue_positions(self):
        """
        向所有 WebSocket 队列订阅者广播最新排队顺位流转事件：
        {
            "type": "queue_positions_updated",
            "data": { [task_id]: position }
        }
        根据订阅者 client_id 分流隔离，保护隐私并准确保留真实物理顺位。
        """
        try:
            if not ws_manager.loop or ws_manager.loop.is_closed():
                return

            with ws_manager._queue_lock:
                subscribers = list(ws_manager._queue_subscribers.items())
                subscriber_clients = dict(ws_manager._subscriber_clients)

            # 兜底：若无任何 WebSocket 订阅者在线，避免空跑计算与调度
            if not subscribers:
                return

            all_positions = self.get_queue_positions(client_id=None)
            client_positions_cache: Dict[str, Dict[str, int]] = {}

            for ws_id, ws in subscribers:
                cid = subscriber_clients.get(ws_id)
                if cid:
                    if cid not in client_positions_cache:
                        client_positions_cache[cid] = self.get_queue_positions(client_id=cid)
                    data = client_positions_cache[cid]
                else:
                    data = all_positions

                msg = {
                    "type": "queue_positions_updated",
                    "data": data,
                }
                asyncio.run_coroutine_threadsafe(
                    ws_manager._send_queue_async(ws_id, ws, msg), ws_manager.loop
                )
        except Exception as e:
            log.warning(f"[Scheduler] Failed to broadcast queue positions: {e}")

    def remove_from_queue(self, task_id: str) -> bool:
        """从本地 FIFO 队列中移出指定的排队任务（如被用户手动取消）。若成功移除则触发广播并返回 True。"""
        import collections
        if not task_id:
            return False
        removed = False
        with self.task_queue.mutex:
            remaining = collections.deque()
            for item in self.task_queue.queue:
                if isinstance(item, dict) and item.get("id") == task_id:
                    removed = True
                else:
                    remaining.append(item)
            if removed:
                removed_count = len(self.task_queue.queue) - len(remaining)
                self.task_queue.queue = remaining
                self.task_queue.unfinished_tasks -= removed_count
        if removed:
            self.broadcast_queue_positions()
        return removed

    def abort_asset_wait(self, task_id: str) -> bool:
        """若任务正在等待素材上传，提前解除等待信号以便调度器快速流转。"""
        if not task_id:
            return False
        with self._asset_events_lock:
            evt = self._asset_events.get(task_id)
            if evt:
                evt.set()
                return True
        return False


scheduler = TaskScheduler()
