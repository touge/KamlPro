# -*- coding: utf-8 -*-
"""
发音人列表代理端点
=================
GET /interface/speakers
前端通过此端点获取 TTS 服务支持的发音人列表，无需知道后端实际地址。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import requests as sync_requests
from fastapi import APIRouter

from src.core.service_controller import service_controller
from src.core.response import success, error
from src.logic.logger import log
from src.logic.yaml_config_loader import yaml_config_loader

router = APIRouter(tags=["TTS"])

_executor = ThreadPoolExecutor(max_workers=2)


@router.get("/tts/speakers")
async def list_speakers():
    """
    获取 TTS 服务支持的发音人列表。
    代理到后端 TTS 服务的 /speaker/ 端点，透传认证 token。
    """
    tasks_config = yaml_config_loader.get("tasks", {})
    tts_config = tasks_config.get("tts", {})
    service_name = tts_config.get("service", "TTS")

    service_url = service_controller.get_service_url(service_name)
    if not service_url:
        return error(f"TTS service '{service_name}' not found", 404)

    svc = service_controller.get_service_config(service_name)
    headers = {}
    if svc:
        token = svc.get("token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

    backend_url = f"{service_url}/speaker/"

    try:
        loop = asyncio.get_event_loop()

        def _fetch():
            resp = sync_requests.get(backend_url, headers=headers, timeout=10)
            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                return resp.status_code, resp.json()
            return resp.status_code, resp.text

        status_code, body = await loop.run_in_executor(_executor, _fetch)

        if status_code >= 400:
            log.warning(f"[Speaker] Backend returned {status_code}: {backend_url}")
            return error(f"Backend returned {status_code}", status_code)

        # 后端响应格式: {status, code, message, data: {categories: [...]}}
        # 提取 data 字段放入 Kalm 统一响应信封，避免双重嵌套
        if isinstance(body, dict) and "data" in body:
            result = body["data"]
        else:
            result = body

        log.info(f"[Speaker] Returned speaker list from {service_name}")
        return success(data=result)

    except sync_requests.ConnectionError:
        return error("TTS service is not reachable", 502)
    except Exception as e:
        log.error(f"[Speaker] Error proxying {backend_url}: {e}")
        return error(f"Proxy error: {str(e)}", 500)
