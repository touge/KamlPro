# -*- coding: utf-8 -*-
"""
通用透明代理与文件代理端点
============================
1. /proxy/{service_name}/{path:path}
   全方法透明 HTTP 反向代理（GET / HEAD / POST / PUT / DELETE / PATCH）：
   前端直接按后端服务原生路径调用（如 /proxy/tts/speaker/、/proxy/tts/emo/、
   /proxy/tts/static/voices/...?token=xxx、/proxy/comfyui/object_info 等），
   网关仅负责服务寻址、跨域头清洗、后端 Token 注入与流式/Range 透传，不耦合任何具体业务接口。

2. /file/{service_name}/{file_path:path}
   向后兼容的文件代理端点（GET / HEAD / POST），与 /proxy 共用核心转发逻辑。
"""

import asyncio
from typing import Optional
from urllib.parse import urlencode
from weakref import WeakKeyDictionary

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from src.core.response import error
from src.core.service_controller import service_controller
from src.logic.logger import log
from src.logic.yaml_config_loader import yaml_config_loader

router = APIRouter(tags=["Proxy"])

_PASSTHROUGH_RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "content-disposition",
    "etag",
    "cache-control",
    "last-modified",
)

_http_clients: WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = WeakKeyDictionary()


def _get_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _http_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0),
            trust_env=False,
            follow_redirects=True,
        )
        _http_clients[loop] = client
    return client


async def close_client():
    loop = asyncio.get_running_loop()
    client = _http_clients.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()


def _clean_headers(headers: dict, token: str = "") -> dict:
    """清洗请求头：去除浏览器跨域限制头、主机头、长度头与前端专用鉴权头，防止后端返回 403 Forbidden"""
    strip_keys = {
        "host",
        "content-length",
        "origin",
        "referer",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "authorization",
        "connection",
        "accept-encoding",
    }
    clean = {k: v for k, v in headers.items() if k.lower() not in strip_keys}
    if token:
        clean["Authorization"] = f"Bearer {token}"
    return clean


def _build_backend_url(service_url: str, target_path: str, request: Request) -> str:
    """拼接后端真实 URL，并剥离仅供网关鉴权使用的 query 参数 ?token="""
    clean_path = target_path.lstrip("/")
    backend_url = f"{service_url}/{clean_path}" if clean_path else f"{service_url}/"
    filtered_query = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
    if filtered_query:
        backend_url += f"?{urlencode(filtered_query, doseq=True)}"
    return backend_url


def _extract_response_headers(upstream_headers: httpx.Headers) -> dict:
    result = {}
    for key in _PASSTHROUGH_RESPONSE_HEADERS:
        val = upstream_headers.get(key)
        if val is not None:
            result[key] = val
    return result


async def _proxy_request(service_name: str, target_path: str, request: Request):
    """共享的透明 HTTP 反向代理核心逻辑"""
    tasks_config = yaml_config_loader.get("tasks", {})
    actual_service = tasks_config.get(service_name, {}).get("service", service_name)

    if not service_controller.is_enabled(actual_service):
        log.warning(f"[Proxy] 🚫 目标服务 '{actual_service}' 已禁用，拒绝代理 {request.method} {target_path}")
        return error(f"Service '{actual_service}' is disabled", 503)

    service_url = service_controller.get_service_url(actual_service)
    if not service_url:
        log.warning(f"[Proxy] ❓ 未找到目标服务 '{service_name}' (mapped='{actual_service}')")
        return error(f"Service '{service_name}' not found", 404)

    backend_url = _build_backend_url(service_url, target_path, request)
    svc = service_controller.get_service_config(actual_service)
    backend_token = svc.get("token", "") if svc else ""
    clean_headers = _clean_headers(dict(request.headers), backend_token)
    range_header = request.headers.get("range", "")
    auth_via_query = bool(request.query_params.get("token"))

    client = _get_client()
    download_tracked = False
    try:
        service_controller.download_begin(actual_service)
        download_tracked = True

        if request.method in ("GET", "HEAD"):
            upstream_req = client.build_request(request.method, backend_url, headers=clean_headers)
            upstream_resp = await client.send(upstream_req, stream=True)

            if upstream_resp.status_code >= 400 or request.method == "HEAD":
                try:
                    content = await upstream_resp.aread() if request.method != "HEAD" else b""
                    resp_headers = _extract_response_headers(upstream_resp.headers)
                    if upstream_resp.status_code >= 400:
                        log.warning(
                            f"[Proxy] ❌ {request.method} {actual_service}:{target_path} -> HTTP {upstream_resp.status_code} "
                            f"| resp={content[:200]!r}"
                        )
                    else:
                        log.info(f"[Proxy] {request.method} {actual_service}:{target_path} -> HTTP {upstream_resp.status_code}")
                    return Response(
                        content=content,
                        status_code=upstream_resp.status_code,
                        headers=resp_headers,
                        media_type=resp_headers.get("content-type"),
                    )
                finally:
                    await upstream_resp.aclose()
                    service_controller.download_end(actual_service)
                    download_tracked = False

            resp_headers = _extract_response_headers(upstream_resp.headers)
            extra_flags = []
            if range_header:
                extra_flags.append(f"range={range_header}")
            if auth_via_query:
                extra_flags.append("query_token=stripped")
            flag_str = f" ({', '.join(extra_flags)})" if extra_flags else ""
            log.info(f"[Proxy] {request.method} {actual_service}:{target_path} -> HTTP {upstream_resp.status_code}{flag_str}")

            async def _stream_body():
                try:
                    async for chunk in upstream_resp.aiter_bytes(chunk_size=65536):
                        yield chunk
                finally:
                    await upstream_resp.aclose()
                    service_controller.download_end(actual_service)

            download_tracked = False  # 生命周期移交到流式生成器 finally 块中收尾
            return StreamingResponse(
                _stream_body(),
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type=resp_headers.get("content-type"),
            )

        # POST / PUT / DELETE / PATCH
        body = await request.body()
        log.info(
            f"[Proxy] 📤 {request.method} {actual_service}:{target_path} -> {backend_url} "
            f"| body={len(body)} bytes | content-type={request.headers.get('content-type', '')}"
        )

        upstream_resp = await client.request(
            request.method,
            backend_url,
            headers=clean_headers,
            content=body,
        )
        resp_headers = _extract_response_headers(upstream_resp.headers)
        if upstream_resp.status_code < 400:
            log.info(
                f"[Proxy] ✅ {request.method} {actual_service}:{target_path} -> HTTP {upstream_resp.status_code} "
                f"| resp={len(upstream_resp.content)} bytes"
            )
        else:
            log.warning(
                f"[Proxy] ❌ {request.method} {actual_service}:{target_path} -> HTTP {upstream_resp.status_code} "
                f"| resp={upstream_resp.content[:300]!r}"
            )
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=resp_headers.get("content-type"),
        )

    except httpx.ConnectError:
        log.error(f"[Proxy] ❌ 无法连接后端服务: {backend_url}")
        return error(f"Backend service '{service_name}' is not reachable", 502)
    except httpx.TimeoutException as e:
        log.error(f"[Proxy] ⏱️ 请求后端服务超时: {backend_url} -> {e}")
        return error(f"Backend service '{service_name}' timeout", 504)
    except Exception as e:
        log.error(f"[Proxy] Error proxying {backend_url}: {e}", exc_info=True)
        return error(f"Proxy error: {str(e)}", 500)
    finally:
        if download_tracked:
            service_controller.download_end(actual_service)


_PROXY_BODY_OPENAPI = {
    "requestBody": {
        "required": False,
        "content": {
            "application/json": {
                "schema": {"type": "object"},
                "example": {},
            },
            "multipart/form-data": {
                "schema": {"type": "object"},
            },
        },
    }
}


@router.get("/proxy/{service_name}/{path:path}")
async def proxy_get(service_name: str, path: str, request: Request):
    """
    透明代理 GET 请求到指定下游服务。
    例如：`service_name=tts`，`path=speakers`、`emotions`（兼容 `emos`、`speaker/`、`emo/`）、`model/status`、`static/voices/...`。
    """
    return await _proxy_request(service_name, path, request)


@router.post("/proxy/{service_name}/{path:path}", openapi_extra=_PROXY_BODY_OPENAPI)
async def proxy_post(service_name: str, path: str, request: Request):
    """
    透明代理 POST 请求到指定下游服务（如 `tts` + `speaker/category`、`model/config`、`free`）。
    """
    return await _proxy_request(service_name, path, request)


@router.put("/proxy/{service_name}/{path:path}", openapi_extra=_PROXY_BODY_OPENAPI)
async def proxy_put(service_name: str, path: str, request: Request):
    """
    透明代理 PUT 请求到指定下游服务（如 `tts` + `speaker/rename`、`emo/rename`）。
    """
    return await _proxy_request(service_name, path, request)


@router.delete("/proxy/{service_name}/{path:path}", openapi_extra=_PROXY_BODY_OPENAPI)
async def proxy_delete(service_name: str, path: str, request: Request):
    """
    透明代理 DELETE 请求到指定下游服务（如 `tts` + `speaker/`、`emo/`）。
    """
    return await _proxy_request(service_name, path, request)


@router.api_route(
    "/proxy/{service_name}/{path:path}",
    methods=["HEAD", "PATCH"],
    include_in_schema=False,
)
async def proxy_extra_methods(service_name: str, path: str, request: Request):
    """运行时补充透传 HEAD（试听探测）与 PATCH，不在 Swagger /docs 展开"""
    return await _proxy_request(service_name, path, request)


@router.api_route(
    "/proxy/{service_name}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"],
    include_in_schema=False,
)
async def proxy_root(service_name: str, request: Request):
    """运行时透传服务根路径，不在 Swagger /docs 重复展开"""
    return await _proxy_request(service_name, "", request)


@router.get("/file/{service_name}/{file_path:path}")
async def proxy_file_get(service_name: str, file_path: str, request: Request):
    """通用文件下载代理（向后兼容入口）"""
    return await _proxy_request(service_name, file_path, request)


@router.post("/file/{service_name}/{file_path:path}", openapi_extra=_PROXY_BODY_OPENAPI)
async def proxy_file_post(service_name: str, file_path: str, request: Request):
    """通用文件上传代理（向后兼容入口）"""
    return await _proxy_request(service_name, file_path, request)


@router.head("/file/{service_name}/{file_path:path}", include_in_schema=False)
async def proxy_file_head(service_name: str, file_path: str, request: Request):
    """文件 HEAD 探测兼容入口"""
    return await _proxy_request(service_name, file_path, request)
