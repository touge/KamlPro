# -*- coding: utf-8 -*-
"""
发音人列表代理端点
=================
GET /interface/tts/speakers
前端通过此端点获取 TTS 服务支持的发音人列表，无需知道后端实际地址。
支持：
1. 内存缓存：命中缓存直接返回，避免重复拉起 TTS；支持 ?refresh=true 强制刷新。
2. 按需拉起与自动回收：无缓存且 TTS 未运行时，通过 TaskScheduler 串行拉起 TTS 获取列表，
   并在完成后自动停止临时拉起的进程（auto_start: true 或原本已运行的服务除外）。
"""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import requests as sync_requests
from fastapi import APIRouter

from src.core.response import success, error
from src.core.scheduler import scheduler
from src.core.service_controller import service_controller
from src.logic.logger import log
from src.logic.yaml_config_loader import yaml_config_loader

router = APIRouter(tags=["TTS"])

_executor = ThreadPoolExecutor(max_workers=2)
_cache_lock = threading.Lock()
_fetch_lock = threading.Lock()
_speaker_cache: Optional[Any] = None


def get_cached_speakers() -> Optional[Any]:
    """读取内存中的发音人列表缓存。"""
    with _cache_lock:
        return _speaker_cache


def clear_speaker_cache() -> None:
    """清空内存中的发音人列表缓存。"""
    global _speaker_cache
    with _cache_lock:
        _speaker_cache = None


def _set_cached_speakers(data: Any) -> None:
    global _speaker_cache
    with _cache_lock:
        _speaker_cache = data


def _extract_speaker_data(body: Any) -> Any:
    """后端响应格式: {status, code, message, data: {categories: [...]}}，提取 data 避免双重嵌套。"""
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _fetch_speakers_sync(service_name: str, backend_url: str, headers: dict, refresh: bool) -> tuple[int, Any]:
    """在同步锁保护下获取发音人列表：无缓存时优先复用运行中服务，否则通过调度器按需拉起。"""
    with _fetch_lock:
        if not refresh:
            cached = get_cached_speakers()
            if cached is not None:
                return 200, cached

        def _http_fetch() -> tuple[int, Any]:
            resp = sync_requests.get(backend_url, headers=headers, timeout=10)
            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                return resp.status_code, resp.json()
            return resp.status_code, resp.text

        status_code = None
        body = None

        # 若服务已在运行（如 auto_start 常驻或正有 TTS 任务活跃），直接查询且不干预生命周期
        if service_controller.status(service_name).get("running"):
            try:
                status_code, body = _http_fetch()
            except sync_requests.ConnectionError:
                log.warning(f"[Speaker] Service '{service_name}' port closed during direct fetch, falling back to scheduler.")

        if status_code is None:
            status_code, body = scheduler.run_with_service(
                service_name,
                _http_fetch,
                stop_if_started=True,
            )

        if status_code >= 400:
            return status_code, body

        result = _extract_speaker_data(body)
        _set_cached_speakers(result)
        return status_code, result


@router.get("/tts/speakers")
async def list_speakers(refresh: bool = False):
    """
    获取 TTS 服务支持的发音人列表。
    优先读取内存缓存；未命中时协调调度器按需拉起 TTS 获取并缓存，非自启动服务随后自动停止。
    """
    tasks_config = yaml_config_loader.get("tasks", {})
    tts_config = tasks_config.get("tts", {})
    service_name = tts_config.get("service", "TTS")

    if not service_controller.is_enabled(service_name):
        log.warning(f"[Speaker] TTS service '{service_name}' is disabled in services.yaml")
        return error(f"TTS service '{service_name}' is disabled", 503)

    service_url = service_controller.get_service_url(service_name)
    if not service_url:
        return error(f"TTS service '{service_name}' not found", 404)

    if not refresh:
        cached = get_cached_speakers()
        if cached is not None:
            log.info(f"[Speaker] Returned cached speaker list for {service_name}")
            return success(data=cached)

    svc = service_controller.get_service_config(service_name)
    headers = {}
    if svc:
        token = svc.get("token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

    backend_url = f"{service_url}/speakers"

    try:
        loop = asyncio.get_running_loop()
        status_code, result = await loop.run_in_executor(
            _executor,
            _fetch_speakers_sync,
            service_name,
            backend_url,
            headers,
            refresh,
        )

        if status_code >= 400:
            log.warning(f"[Speaker] Backend returned {status_code}: {backend_url}")
            return error(f"Backend returned {status_code}", status_code)

        log.info(f"[Speaker] Returned and cached speaker list from {service_name}")
        return success(data=result)

    except sync_requests.ConnectionError as e:
        log.error(f"[Speaker] TTS service '{service_name}' is not reachable at {backend_url}: {e}")
        return error("TTS service is not reachable", 502)
    except (TimeoutError, sync_requests.Timeout) as e:
        log.error(f"[Speaker] Timeout fetching speakers from {backend_url}: {e}")
        return error(f"TTS service timeout: {str(e)}", 504)
    except RuntimeError as e:
        log.error(f"[Speaker] Failed to start or access TTS service '{service_name}': {e}")
        return error(f"TTS service unavailable: {str(e)}", 502)
    except Exception as e:
        log.error(f"[Speaker] Error proxying {backend_url}: {e}", exc_info=True)
        return error(f"Proxy error: {str(e)}", 500)
