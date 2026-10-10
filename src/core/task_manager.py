import uuid
import copy
import time
import threading
from typing import Dict, Any, Optional

from src.logic.logger import log


class TaskManager:
    _tasks: Dict[str, Dict[str, Any]] = {}
    _lock = threading.Lock()
    _abort_events: Dict[str, threading.Event] = {}
    _abort_lock = threading.Lock()

    @classmethod
    def register_abort_event(cls, task_id: str) -> threading.Event:
        with cls._abort_lock:
            event = threading.Event()
            cls._abort_events[task_id] = event
            return event

    @classmethod
    def get_abort_event(cls, task_id: str) -> Optional[threading.Event]:
        with cls._abort_lock:
            return cls._abort_events.get(task_id)

    @classmethod
    def unregister_abort_event(cls, task_id: str):
        with cls._abort_lock:
            cls._abort_events.pop(task_id, None)

    @classmethod
    def is_cancelled(cls, task_id: str) -> bool:
        event = cls.get_abort_event(task_id)
        return event.is_set() if event else False

    @classmethod
    def set_cancel_metadata(cls, task_id: str, metadata: Dict[str, Any]):
        with cls._lock:
            if task_id in cls._tasks:
                if "cancel_metadata" not in cls._tasks[task_id]:
                    cls._tasks[task_id]["cancel_metadata"] = {}
                cls._tasks[task_id]["cancel_metadata"].update(metadata)

    @classmethod
    def get_cancel_metadata(cls, task_id: str) -> Dict[str, Any]:
        with cls._lock:
            if task_id in cls._tasks:
                return copy.deepcopy(cls._tasks[task_id].get("cancel_metadata", {}))
            return {}


    STATUS_PENDING = "pending"
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"

    DEFAULT_TTL_SECONDS = 3600  # 任务结果默认保留 1 小时

    @classmethod
    def create_task(cls, task_type: str, client_id: Optional[str] = None) -> str:
        task_id = str(uuid.uuid4())
        with cls._lock:
            cls._tasks[task_id] = {
                "status": cls.STATUS_PENDING,
                "task_type": task_type,
                "client_id": client_id,
                "result": {},
                "created_at": time.time(),
            }
        return task_id

    @classmethod
    def get_task(cls, task_id: str) -> Dict[str, Any]:
        with cls._lock:
            task_data = cls._tasks.get(task_id)
            if task_data:
                return copy.deepcopy(task_data)
            return {"status": "not_found", "result": None}

    @classmethod
    def list_tasks(cls, client_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """返回任务状态的深拷贝快照。若提供 client_id 则仅返回归属于该客户端的任务。"""
        with cls._lock:
            if not client_id:
                return copy.deepcopy(cls._tasks)
            filtered = {
                tid: task for tid, task in cls._tasks.items()
                if task.get("client_id") == client_id
            }
            return copy.deepcopy(filtered)

    @classmethod
    def update_task(cls, task_id: str, status: str, result: Any = None):
        with cls._lock:
            if task_id in cls._tasks:
                cls._tasks[task_id]["status"] = status
                if result is not None:
                    cls._tasks[task_id]["result"] = result

    @classmethod
    def cleanup_expired(cls, max_age_seconds: int = None):
        """清理超过 max_age_seconds 的已完成/失败任务，释放内存"""
        if max_age_seconds is None:
            max_age_seconds = cls.DEFAULT_TTL_SECONDS
        cutoff = time.time() - max_age_seconds
        with cls._lock:
            to_remove = []
            for task_id, task in cls._tasks.items():
                if task["status"] in (cls.STATUS_SUCCESS, cls.STATUS_FAILED):
                    if task.get("created_at", 0) < cutoff:
                        to_remove.append(task_id)
            for task_id in to_remove:
                del cls._tasks[task_id]
            if to_remove:
                log.info(f"[TaskManager] Cleaned up {len(to_remove)} expired tasks, {len(cls._tasks)} remaining")
