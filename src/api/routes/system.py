# -*- coding: utf-8 -*-
from fastapi import APIRouter
from src.core.response import success
from src.core.service_controller import service_controller
from src.logic.system_monitor import system_monitor

router = APIRouter(tags=["System"])


@router.get("/ping")
async def ping():
    """
    健康检查：返回中转站自身状态 + 所有下游服务连通性。
    """
    services_status = service_controller.status_all()
    all_running = all(s["running"] for s in services_status)

    return success(data={
        "ping": "pong",
        "service": "Kalm",
        "downstream_services": services_status,
        "all_healthy": all_running,
    })


@router.get("/system/stats")
async def get_system_stats():
    """
    获取宿主机与显卡实时监控快照（由后台线程每 3 秒采样并常驻内存，直接读取响应）。
    """
    return success(data=system_monitor.get_stats())
