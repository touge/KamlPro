# -*- coding: utf-8 -*-
"""
系统硬件监控后台采集器 (SystemMonitor)
======================================
以独立守护线程每 3 秒周期采样 CPU、内存/虚拟内存、GPU（pynvml）与网络吞吐，
将结构化快照常驻内存，供 /interface/system/stats 毫秒级读取。
"""

import ctypes
import os
import platform
import socket
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil

from src.logic.logger import log
from src.logic.yaml_config_loader import yaml_config_loader

try:
    import pynvml
    _PYNVML_AVAILABLE = True
except ImportError:
    pynvml = None
    _PYNVML_AVAILABLE = False

_MIB = 1024 * 1024
_MBPS_DIVISOR = 1_000_000.0


def _decode_str(val: Any) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace").strip()
    return str(val).strip() if val is not None else ""


def _safe_call(func: Callable[[], Any], default: Any = None) -> Any:
    try:
        return func()
    except Exception:
        return default


def _detect_cpu_model() -> str:
    """一次性检测可读性较好的 CPU 型号。"""
    system = platform.system()
    if system == "Windows":
        try:
            import winreg
            key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                if name and str(name).strip():
                    return str(name).strip()
        except Exception:
            pass
    elif system == "Linux":
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        _, val = line.split(":", 1)
                        if val.strip():
                            return val.strip()
        except Exception:
            pass

    return platform.processor() or platform.machine() or "Unknown CPU"


def _detect_os_name() -> str:
    """一次性检测跨平台友好的操作系统名称。"""
    system = platform.system()
    if system == "Linux":
        try:
            with open("/etc/os-release", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
        except Exception:
            pass
    elif system == "Windows":
        try:
            build = int(platform.version().split(".")[-1])
            if build >= 22000:
                return "Windows 11"
        except Exception:
            pass

    release = platform.release()
    return f"{system} {release}".strip() if release else (system or "Unknown OS")


def _windows_pagefile_total_mb() -> int:
    """当 Windows 性能计数器被禁用导致 psutil.swap_memory 失败时，读取页面文件总量。"""
    from ctypes import wintypes

    class _PerformanceInfo(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("CommitTotal", ctypes.c_size_t),
            ("CommitLimit", ctypes.c_size_t),
            ("CommitPeak", ctypes.c_size_t),
            ("PhysicalTotal", ctypes.c_size_t),
            ("PhysicalAvailable", ctypes.c_size_t),
            ("SystemCache", ctypes.c_size_t),
            ("KernelTotal", ctypes.c_size_t),
            ("KernelPaged", ctypes.c_size_t),
            ("KernelNonpaged", ctypes.c_size_t),
            ("PageSize", ctypes.c_size_t),
            ("HandleCount", wintypes.DWORD),
            ("ProcessCount", wintypes.DWORD),
            ("ThreadCount", wintypes.DWORD),
        ]

    info = _PerformanceInfo()
    info.cb = ctypes.sizeof(info)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        return 0
    page_size = info.PageSize
    total_commit_mb = (info.CommitLimit * page_size) // _MIB
    phys_total_mb = (info.PhysicalTotal * page_size) // _MIB
    return max(0, int(total_commit_mb - phys_total_mb))


def _read_swap_mb() -> Tuple[int, int]:
    """跨平台读取 (swap_used_mb, swap_total_mb)，保证永远返回整数元组。"""
    try:
        swap = psutil.swap_memory()
        return int(swap.used // _MIB), int(swap.total // _MIB)
    except Exception:
        if platform.system() == "Windows":
            try:
                return 0, _windows_pagefile_total_mb()
            except Exception:
                pass
        return 0, 0


def _read_non_loopback_net_bytes() -> Tuple[int, int]:
    """读取非回环网卡的累计 (bytes_recv, bytes_sent)。"""
    try:
        per_nic = psutil.net_io_counters(pernic=True)
        filtered = [
            counters for nic, counters in per_nic.items()
            if nic.lower() != "lo" and "loopback" not in nic.lower()
        ]
        if filtered:
            rx_bytes = sum(c.bytes_recv for c in filtered)
            tx_bytes = sum(c.bytes_sent for c in filtered)
            return int(rx_bytes), int(tx_bytes)
    except Exception:
        pass

    try:
        total = psutil.net_io_counters(pernic=False)
        if total:
            return int(total.bytes_recv), int(total.bytes_sent)
    except Exception:
        pass
    return 0, 0


class SystemMonitor:
    """后台硬件指标采集单例。"""

    _instance = None
    _singleton_lock = threading.Lock()

    def __new__(cls):
        if not cls._instance:
            with cls._singleton_lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, sample_interval: float = 3.0):
        if self._initialized:
            return

        self.sample_interval = sample_interval
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        self._start_monotonic = time.monotonic()
        self._hostname = socket.gethostname()
        self._os_name = _detect_os_name()
        self._cpu_model = _detect_cpu_model()
        self._cores_physical = psutil.cpu_count(logical=False) or os.cpu_count() or 1
        self._cores_logical = psutil.cpu_count(logical=True) or self._cores_physical

        self._nvml_ready = False
        self._prev_net_bytes: Optional[Tuple[int, int]] = None
        self._prev_net_time: Optional[float] = None

        self._snapshot: Dict[str, Any] = self._build_empty_snapshot()
        self._initialized = True

    def _build_empty_snapshot(self) -> Dict[str, Any]:
        return {
            "cpu": {
                "model": self._cpu_model,
                "cores_physical": self._cores_physical,
                "cores_logical": self._cores_logical,
                "usage_percent": 0.0,
                "load_avg": [0.0, 0.0, 0.0],
            },
            "memory": {
                "used_mb": 0,
                "total_mb": 0,
                "available_mb": 0,
                "usage_percent": 0.0,
                "swap_used_mb": 0,
                "swap_total_mb": 0,
            },
            "gpus": [],
            "network": {
                "rx_mbps": 0.0,
                "tx_mbps": 0.0,
            },
        }

    def _init_nvml(self):
        if not _PYNVML_AVAILABLE or self._nvml_ready:
            return
        try:
            pynvml.nvmlInit()
            self._nvml_ready = True
            gpu_count = _safe_call(pynvml.nvmlDeviceGetCount, 0)
            log.info(f"[SystemMonitor] NVML initialized ({gpu_count} GPU(s) detected).")
        except Exception as e:
            self._nvml_ready = False
            log.warning(f"[SystemMonitor] NVML unavailable, GPU metrics disabled: {e}")

    def _shutdown_nvml(self):
        if not self._nvml_ready:
            return
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
        finally:
            self._nvml_ready = False

    def _collect_cpu(self) -> Dict[str, Any]:
        usage = _safe_call(lambda: round(float(psutil.cpu_percent(interval=None)), 1), 0.0)
        raw_load = _safe_call(psutil.getloadavg, (0.0, 0.0, 0.0))
        load_avg = [round(float(x), 2) for x in raw_load[:3]] if raw_load else [0.0, 0.0, 0.0]
        return {
            "model": self._cpu_model,
            "cores_physical": self._cores_physical,
            "cores_logical": self._cores_logical,
            "usage_percent": usage,
            "load_avg": load_avg,
        }

    def _collect_memory(self) -> Dict[str, Any]:
        vm = psutil.virtual_memory()
        swap_used_mb, swap_total_mb = _read_swap_mb()
        return {
            "used_mb": int(vm.used // _MIB),
            "total_mb": int(vm.total // _MIB),
            "available_mb": int(vm.available // _MIB),
            "usage_percent": round(float(vm.percent), 1),
            "swap_used_mb": swap_used_mb,
            "swap_total_mb": swap_total_mb,
        }

    def _collect_network(self) -> Dict[str, Any]:
        now = time.monotonic()
        rx_bytes, tx_bytes = _read_non_loopback_net_bytes()
        rx_mbps = 0.0
        tx_mbps = 0.0

        if self._prev_net_bytes is not None and self._prev_net_time is not None:
            dt = now - self._prev_net_time
            if dt > 0:
                delta_rx = max(0, rx_bytes - self._prev_net_bytes[0])
                delta_tx = max(0, tx_bytes - self._prev_net_bytes[1])
                rx_mbps = round((delta_rx * 8.0) / (dt * _MBPS_DIVISOR), 2)
                tx_mbps = round((delta_tx * 8.0) / (dt * _MBPS_DIVISOR), 2)

        self._prev_net_bytes = (rx_bytes, tx_bytes)
        self._prev_net_time = now
        return {"rx_mbps": rx_mbps, "tx_mbps": tx_mbps}

    def _collect_gpus(self) -> List[Dict[str, Any]]:
        if not self._nvml_ready:
            return []

        driver_ver = _decode_str(_safe_call(pynvml.nvmlSystemGetDriverVersion, ""))
        raw_cuda = _safe_call(pynvml.nvmlSystemGetCudaDriverVersion_v2, 0)
        cuda_ver = f"{raw_cuda // 1000}.{(raw_cuda % 1000) // 10}" if raw_cuda else ""

        device_count = _safe_call(pynvml.nvmlDeviceGetCount, 0) or 0
        gpus: List[Dict[str, Any]] = []

        for idx in range(device_count):
            handle = _safe_call(lambda i=idx: pynvml.nvmlDeviceGetHandleByIndex(i))
            if handle is None:
                continue

            name = _decode_str(_safe_call(lambda h=handle: pynvml.nvmlDeviceGetName(h), f"GPU-{idx}"))
            mem = _safe_call(lambda h=handle: pynvml.nvmlDeviceGetMemoryInfo(h))
            vram_used_mb = int(mem.used // _MIB) if mem else 0
            vram_total_mb = int(mem.total // _MIB) if mem else 0
            vram_free_mb = int(mem.free // _MIB) if mem else 0
            vram_pct = round((mem.used / mem.total) * 100.0, 1) if (mem and mem.total > 0) else 0.0

            util = _safe_call(lambda h=handle: pynvml.nvmlDeviceGetUtilizationRates(h))
            util_pct = round(float(util.gpu), 1) if util else 0.0

            temp = _safe_call(
                lambda h=handle: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU),
                0,
            )

            pwr_mw = _safe_call(lambda h=handle: pynvml.nvmlDeviceGetPowerUsage(h), 0)
            limit_mw = _safe_call(lambda h=handle: pynvml.nvmlDeviceGetEnforcedPowerLimit(h))
            if limit_mw is None:
                limit_mw = _safe_call(lambda h=handle: pynvml.nvmlDeviceGetPowerManagementLimit(h), 0)

            gpus.append({
                "index": idx,
                "name": name,
                "vram_used_mb": vram_used_mb,
                "vram_total_mb": vram_total_mb,
                "vram_free_mb": vram_free_mb,
                "vram_usage_percent": vram_pct,
                "utilization_percent": util_pct,
                "temperature_c": int(temp or 0),
                "power_draw_w": round(float(pwr_mw or 0) / 1000.0, 1),
                "power_limit_w": round(float(limit_mw or 0) / 1000.0, 1),
                "driver_version": driver_ver,
                "cuda_version": cuda_ver,
            })

        return gpus

    def sample_once(self):
        """执行一次完整采集并原子更新内存快照。"""
        snapshot = {
            "cpu": self._collect_cpu(),
            "memory": self._collect_memory(),
            "gpus": self._collect_gpus(),
            "network": self._collect_network(),
        }
        with self._lock:
            self._snapshot = snapshot

    def _run_loop(self):
        while not self._stop_event.wait(self.sample_interval):
            try:
                self.sample_once()
            except Exception as e:
                log.warning(f"[SystemMonitor] Sample cycle error: {e}")

    def start(self):
        if self._running:
            return
        self._start_monotonic = time.monotonic()
        self._stop_event.clear()
        self._init_nvml()

        # 预热 cpu_percent 与网卡基线，确保首次快照可用
        _safe_call(lambda: psutil.cpu_percent(interval=None))
        self._prev_net_bytes = _read_non_loopback_net_bytes()
        self._prev_net_time = time.monotonic()
        self.sample_once()

        self._thread = threading.Thread(
            target=self._run_loop,
            name="SystemMonitorThread",
            daemon=True,
        )
        self._running = True
        self._thread.start()
        log.info(f"[SystemMonitor] Background sampler started (interval={self.sample_interval}s).")

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._shutdown_nvml()
        log.info("[SystemMonitor] Background sampler stopped.")

    def get_stats(self) -> Dict[str, Any]:
        """读取当前内存中的最新硬件快照，并拼接实时 server 运行信息。"""
        with self._lock:
            snap = {
                "cpu": dict(self._snapshot["cpu"]),
                "memory": dict(self._snapshot["memory"]),
                "gpus": [dict(g) for g in self._snapshot["gpus"]],
                "network": dict(self._snapshot["network"]),
            }

        return {
            "server": {
                "hostname": self._hostname,
                "os": self._os_name,
                "uptime_sec": int(time.monotonic() - self._start_monotonic),
                "gateway_version": str(yaml_config_loader.get("app.version", "1.0.0")),
            },
            **snap,
        }


system_monitor = SystemMonitor()
