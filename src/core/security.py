# -*- coding: utf-8 -*-
"""
API 安全认证
============
使用 FastAPI 原生 Security + Depends 机制验证 Bearer Token，
这样 /docs 页面会自动显示 Authorize 按钮。

同时保留 WebSocket 中间件认证（WebSocket 不支持 Depends）。

配置：config.yaml 中 api_server.tokens
- 留空则跳过认证
- HTTP 请求通过 Authorization: Bearer <token> 头验证
- WebSocket 请求通过 Authorization 头或 x-token 头验证
"""

from fastapi import Depends, HTTPException, Security, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.websockets import WebSocket
from starlette.responses import JSONResponse
from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.logger import log


class OptionalHTTPBearer(HTTPBearer):
    """
    HTTPBearer 的 WebSocket 兼容版本。
    WebSocket 路由没有 Request 对象，FastAPI 无法解析 request 参数，
    此时使用默认值 None 跳过 HTTP 认证（WebSocket 由中间件处理）。
    """

    async def __call__(self, request: Request = None):
        if request is None:
            return None
        return await super().__call__(request)


security_scheme = OptionalHTTPBearer(auto_error=True)


async def require_token(
    credentials: HTTPAuthorizationCredentials | None = Security(security_scheme)
):
    """
    全局认证依赖。使用 Security(security_scheme) 作为默认参数，
    这样 FastAPI / Swagger UI 会自动在 docs 页面显示 Authorize 按钮。

    关键：必须使用 Security() 而非 Depends() 作为参数默认值，
    Swagger UI 才能识别这个安全方案并显示认证输入框。

    WebSocket 连接时 credentials 为 None，认证由 WebSocketAuthMiddleware 处理。
    """
    if credentials is None:
        return True

    tokens = yaml_config_loader.get("api_server.tokens", [])
    if not tokens:
        return True

    if credentials.credentials not in tokens:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    return True


class WebSocketAuthMiddleware:
    """WebSocket 专用认证中间件 — 只处理 websocket 类型请求"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return

        # 非 Kalm 端点（如平台监控探活 /ws），静默关闭不触发认证
        if scope["path"] not in ("/interface/queue/ws",) and not scope["path"].startswith("/interface/tasks/"):
            ws = WebSocket(scope, receive=receive, send=send)
            await ws.accept()
            await ws.close(code=1000)
            return

        ws = WebSocket(scope, receive=receive, send=send)
        is_valid, auth_subprotocol = self._verify_ws_token(ws)
        if not is_valid:
            await ws.accept()
            await ws.close(code=4001, reason="Missing or invalid authentication token")
            return

        # 拦截 send 消息，自动在握手时回传 subprotocol，防止浏览器报错
        async def custom_send(message):
            if message["type"] == "websocket.accept" and auth_subprotocol:
                if "subprotocol" not in message or message.get("subprotocol") is None:
                    message["subprotocol"] = auth_subprotocol
            await send(message)

        await self.app(scope, receive, custom_send)

    def _verify_ws_token(self, ws) -> tuple[bool, str | None]:
        tokens = yaml_config_loader.get("api_server.tokens", [])
        # log.info(f"[WSAuth] Configured tokens: {tokens}")
        if not tokens:
            return True, None

        auth_header = ws.headers.get("authorization", "")
        x_token = ws.headers.get("x-token", "")
        # log.info(f"[WSAuth] Headers - authorization: '{auth_header[:20]}', x-token: '{x_token}'")

        # 方式1: 从 Authorization 头获取 (Authorization: Bearer xxx)
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token in tokens:
                return True, None

        # 方式2: 从 x-token 头获取（简化版，直接传 token 值）
        if x_token and x_token in tokens:
            return True, None

        # 方式3: 从 Sec-WebSocket-Protocol 头获取（兼容浏览器前端）
        # 浏览器传的 subprotocol 数组：['kalm-auth', 'token']
        # 会被拼接成请求头: Sec-WebSocket-Protocol: kalm-auth, token
        subprotocols_str = ws.headers.get("sec-websocket-protocol", "")
        if subprotocols_str:
            parts = [p.strip() for p in subprotocols_str.split(",")]
            if len(parts) >= 2 and parts[0] == "kalm-auth":
                token = parts[1]
                if token in tokens:
                    return True, "kalm-auth"

        log.info("[WSAuth] Token verification failed, closing with code 4001")
        return False, None
