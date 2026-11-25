# -*- coding: utf-8 -*-
"""
系统硬件监控后台采集器 (SystemMonitor)
======================================
采用分层周期采样架构（静态启动一次 / 低频 60s / 中频 12s / 高频 3s），
在独立守护线程中将结构化快照常驻内存，供 /interface/system/stats 毫秒级零压力读取。
"""

import ctypes
import importlib.metadata
import os
import platform
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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
_GIB = 1024 * 1024 * 1024
_MBPS_DIVISOR = 1_000_000.0

# 过滤虚拟、容器、隧道、回环网卡关键字（仅采集物理实体网卡）
_VIRTUAL_NIC_KEYWORDS = (
    "loopback",
    "vethernet",
    "hyper-v",
    "vmware",
    "vmnet",
    "virtualbox",
    "vbox",
    "docker",
    "veth",
    "virbr",
    "singbox",
    "sing-box",
    "wireguard",
    "tailscale",
    "zerotier",
    "wsl",
    "teredo",
    "isatap",
    "bluetooth",
    "npcap",
    "pseudo",
)
_VIRTUAL_NIC_EXACT_OR_PREFIX = ("lo", "br-", "tun", "tap", "wg")

# Linux/容器常见虚拟文件系统类型，不计入物理磁盘空间统计
_IGNORED_FS_TYPES = frozenset({
    "squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs",
    "cgroup", "cgroup2", "devpts", "mqueue", "hugetlbfs", "debugfs",
    "tracefs", "securityfs", "pstore", "fusectl", "autofs", "nsfs",
})

_MAX_GPU_PROCESSES = 15


def _decode_str(val: Any) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace").strip()
    return str(val).strip() if val is not None else ""


def _safe_call(func: Callable[[], Any], default: Any = None) -> Any:
    try:
        return func()
    except Exception:
        return default


def _is_physical_nic(nic_name: str) -> bool:
    """判断网卡名是否属于物理实体网卡（排除回环、容器、虚拟交换机与 VPN 隧道）。"""
    lower = nic_name.strip().lower()
    if not lower:
        return False
    if lower in _VIRTUAL_NIC_EXACT_OR_PREFIX:
        return False
    if any(lower.startswith(prefix) and (len(lower) == len(prefix) or lower[len(prefix)].isdigit() or lower[len(prefix)] in "-_")
           for prefix in _VIRTUAL_NIC_EXACT_OR_PREFIX if prefix != "lo"):
        return False
    return not any(kw in lower for kw in _VIRTUAL_NIC_KEYWORDS)


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


def _detect_arch() -> str:
    """统一跨平台架构命名（Windows AMD64 -> x86_64）。"""
    raw = (platform.machine() or "").strip()
    upper = raw.upper()
    if upper in ("AMD64", "X86_64", "X64"):
        return "x86_64"
    if upper in ("ARM64", "AARCH64"):
        return "aarch64"
    return raw or "unknown"


def _detect_os_info() -> Tuple[str, str]:
    """一次性检测 (os_name, os_version)，例如 ('Windows 11', 'Windows 11 25H2')。"""
    system = platform.system()
    if system == "Linux":
        pretty = ""
        version_id = ""
        try:
            with open("/etc/os-release", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        pretty = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("VERSION_ID="):
                        version_id = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
        os_name = pretty or f"{system} {platform.release()}".strip()
        os_ver = pretty or (f"{system} {version_id}".strip() if version_id else os_name)
        return os_name, os_ver

    if system == "Windows":
        os_name = f"Windows {platform.release()}".strip()
        try:
            build = int(platform.version().split(".")[-1])
            if build >= 22000:
                os_name = "Windows 11"
        except Exception:
            pass

        disp_ver = ""
        try:
            import winreg
            key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                disp_ver = str(winreg.QueryValueEx(key, "DisplayVersion")[0]).strip()
        except Exception:
            pass
        os_version = f"{os_name} {disp_ver}".strip() if disp_ver else os_name
        return os_name, os_version

    release = platform.release()
    os_name = f"{system} {release}".strip() if release else (system or "Unknown OS")
    return os_name, os_name


def _detect_pytorch_version() -> Optional[str]:
    """仅读取包元数据获取 torch 版本，绝不 import torch 以免占用内存或触发 CUDA。"""
    try:
        return importlib.metadata.version("torch")
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


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


def _read_physical_net_bytes(physical_nics: Optional[Set[str]] = None) -> Tuple[int, int]:
    """读取物理实体网卡的累计 (bytes_recv, bytes_sent)。"""
    try:
        per_nic = psutil.net_io_counters(pernic=True)
        if physical_nics:
            filtered = [c for nic, c in per_nic.items() if nic in physical_nics]
        else:
            filtered = [c for nic, c in per_nic.items() if _is_physical_nic(nic)]

        if not filtered:
            filtered = [
                c for nic, c in per_nic.items()
                if nic.lower() != "lo" and "loopback" not in nic.lower()
            ]
        if filtered:
            return int(sum(c.bytes_recv for c in filtered)), int(sum(c.bytes_sent for c in filtered))
    except Exception:
        pass

    try:
        total = psutil.net_io_counters(pernic=False)
        if total:
            return int(total.bytes_recv), int(total.bytes_sent)
    except Exception:
        pass
    return 0, 0


def _read_linux_cpu_temp() -> Optional[float]:
    """在 Linux 下读取 CPU 封装温度，Windows 或不可用时返回 None。"""
    if platform.system() == "Windows" or not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            entries = temps.get(key)
            if entries:
                # 优先取 Package / Tctl / 首个有效读数
                for entry in entries:
                    label = (entry.label or "").lower()
                    if any(tag in label for tag in ("package", "tctl", "tdie", "cpu")):
                        return round(float(entry.current), 1)
                return round(float(entries[0].current), 1)
        first_group = next(iter(temps.values()))
        if first_group:
            return round(float(first_group[0].current), 1)
    except Exception:
        pass
    return None


class SystemMonitor:
    """后台硬件指标分层采集单例。"""

    _instance = None
    _singleton_lock = threading.Lock()

    def __new__(cls):
        if not cls._instance:
            with cls._singleton_lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        sample_interval: float = 3.0,
        low_freq_interval: float = 60.0,
        proc_interval: float = 12.0,
    ):
        if self._initialized:
            return

        self.sample_interval = sample_interval
        self.low_freq_interval = low_freq_interval
        self.proc_interval = proc_interval

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # ---- 静态宿主机属性（启动一次性采集，零后续开销） ----
        self._start_monotonic = time.monotonic()
        self._hostname = socket.gethostname()
        self._os_name, self._os_version = _detect_os_info()
        self._arch = _detect_arch()
        self._python_version = platform.python_version()
        self._pytorch_version = _detect_pytorch_version()

        self._cpu_model = _detect_cpu_model()
        self._cores_physical = psutil.cpu_count(logical=False) or os.cpu_count() or 1
        self._cores_logical = psutil.cpu_count(logical=True) or self._cores_physical

        self._nvml_ready = False

        # ---- 分层缓存状态 ----
        self._physical_nics: Set[str] = set()
        self._ip_addresses: List[str] = []
        self._link_speed_mbps: Optional[int] = None
        self._cached_disks: List[Dict[str, Any]] = []
        self._last_low_freq_at: float = 0.0

        self._cached_gpu_processes: Dict[int, List[Dict[str, Any]]] = {}
        self._last_proc_at: float = 0.0

        self._initial_net_bytes: Optional[Tuple[int, int]] = None
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
                "freq_mhz": None,
                "freq_max_mhz": None,
                "temperature_c": None,
                "load_avg": None,
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
            "disks": [],
            "network": {
                "rx_mbps": 0.0,
                "tx_mbps": 0.0,
                "rx_total_gb": 0.0,
                "tx_total_gb": 0.0,
                "link_speed_mbps": None,
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

    def _refresh_low_freq_metrics(self):
        """每 60 秒采集一次磁盘分区、物理网卡 IP 列表及协商速率。"""
        # 1. 磁盘分区
        disks: List[Dict[str, Any]] = []
        seen_mounts: Set[str] = set()
        for part in _safe_call(lambda: psutil.disk_partitions(all=False), []) or []:
            fstype = (part.fstype or "").strip()
            opts = (part.opts or "").lower()
            if "cdrom" in opts or fstype.lower() in _IGNORED_FS_TYPES:
                continue
            if not part.mountpoint or part.mountpoint in seen_mounts:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
                if usage.total <= 0:
                    continue
                seen_mounts.add(part.mountpoint)
                disks.append({
                    "mount": part.mountpoint,
                    "fs_type": fstype or "unknown",
                    "total_gb": round(usage.total / _GIB, 1),
                    "used_gb": round(usage.used / _GIB, 1),
                    "free_gb": round(usage.free / _GIB, 1),
                    "usage_percent": round(float(usage.percent), 1),
                })
            except Exception:
                continue
        self._cached_disks = disks

        # 2. 物理实体网卡 IP 与协商速率
        if_stats = _safe_call(psutil.net_if_stats, {}) or {}
        if_addrs = _safe_call(psutil.net_if_addrs, {}) or {}
        physical_nics: Set[str] = set()
        ips: List[str] = []
        max_speed: Optional[int] = None

        for nic, st in if_stats.items():
            if not _is_physical_nic(nic):
                continue
            physical_nics.add(nic)
            if not st.isup:
                continue
            if st.speed and st.speed > 0:
                speed_val = int(st.speed)
                if max_speed is None or speed_val > max_speed:
                    max_speed = speed_val
            for addr in if_addrs.get(nic, []):
                if addr.family == socket.AF_INET and addr.address and not addr.address.startswith("127."):
                    if addr.address not in ips:
                        ips.append(addr.address)

        self._physical_nics = physical_nics
        self._ip_addresses = ips
        self._link_speed_mbps = max_speed

    def _collect_cpu(self) -> Dict[str, Any]:
        usage = _safe_call(lambda: round(float(psutil.cpu_percent(interval=None)), 1), 0.0)

        freq_info = _safe_call(psutil.cpu_freq)
        freq_mhz = round(float(freq_info.current), 1) if (freq_info and freq_info.current) else None
        freq_max_mhz = round(float(freq_info.max), 1) if (freq_info and freq_info.max) else None

        raw_load = _safe_call(psutil.getloadavg)
        load_avg = [round(float(x), 2) for x in raw_load[:3]] if raw_load else None

        return {
            "model": self._cpu_model,
            "cores_physical": self._cores_physical,
            "cores_logical": self._cores_logical,
            "usage_percent": usage,
            "freq_mhz": freq_mhz,
            "freq_max_mhz": freq_max_mhz,
            "temperature_c": _read_linux_cpu_temp(),
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
        rx_bytes, tx_bytes = _read_physical_net_bytes(self._physical_nics)
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

        base_rx, base_tx = self._initial_net_bytes or (rx_bytes, tx_bytes)
        rx_total_gb = round(max(0, rx_bytes - base_rx) / _GIB, 3)
        tx_total_gb = round(max(0, tx_bytes - base_tx) / _GIB, 3)

        return {
            "rx_mbps": rx_mbps,
            "tx_mbps": tx_mbps,
            "rx_total_gb": rx_total_gb,
            "tx_total_gb": tx_total_gb,
            "link_speed_mbps": self._link_speed_mbps,
        }

    def _collect_gpu_processes(self, handle: Any) -> List[Dict[str, Any]]:
        """轻量收集单卡占卡进程列表（使用 NVML 底层查询并去重，限制上限避免开销）。"""
        merged: Dict[int, Optional[int]] = {}
        for getter_name in ("nvmlDeviceGetComputeRunningProcesses", "nvmlDeviceGetGraphicsRunningProcesses"):
            getter = getattr(pynvml, getter_name, None)
            if not getter:
                continue
            entries = _safe_call(lambda fn=getter: fn(handle), []) or []
            for proc in entries:
                pid = int(proc.pid)
                mem_mb = int(proc.usedGpuMemory // _MIB) if proc.usedGpuMemory is not None else None
                prev = merged.get(pid)
                if pid not in merged or (mem_mb is not None and (prev is None or mem_mb > prev)):
                    merged[pid] = mem_mb

        if not merged:
            return []

        results: List[Dict[str, Any]] = []
        for pid, mem_mb in merged.items():
            raw_name = _safe_call(lambda p=pid: pynvml.nvmlSystemGetProcessName(p))
            proc_name = os.path.basename(_decode_str(raw_name)) if raw_name else ""
            if not proc_name:
                # Windows WDDM 下系统保护进程既无显存值又无权限读取进程名时直接跳过，避免无意义噪声
                if mem_mb is None:
                    continue
                proc_name = _safe_call(lambda p=pid: psutil.Process(p).name(), "unknown")
            results.append({
                "pid": pid,
                "name": proc_name,
                "used_vram_mb": mem_mb,
            })

        results.sort(key=lambda item: (item["used_vram_mb"] is not None, item["used_vram_mb"] or 0), reverse=True)
        return results[:_MAX_GPU_PROCESSES]

    def _collect_gpus(self, refresh_processes: bool = False) -> List[Dict[str, Any]]:
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
            mem_util_pct = round(float(util.memory), 1) if util else 0.0

            temp = _safe_call(
                lambda h=handle: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU),
                0,
            )

            pwr_mw = _safe_call(lambda h=handle: pynvml.nvmlDeviceGetPowerUsage(h), 0)
            limit_mw = _safe_call(lambda h=handle: pynvml.nvmlDeviceGetEnforcedPowerLimit(h))
            if limit_mw is None:
                limit_mw = _safe_call(lambda h=handle: pynvml.nvmlDeviceGetPowerManagementLimit(h), 0)

            fan_pct = _safe_call(lambda h=handle: pynvml.nvmlDeviceGetFanSpeed(h))
            clk_graphics = _safe_call(
                lambda h=handle: pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS),
            )
            clk_memory = _safe_call(
                lambda h=handle: pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_MEM),
            )

            if refresh_processes or idx not in self._cached_gpu_processes:
                self._cached_gpu_processes[idx] = self._collect_gpu_processes(handle)

            gpus.append({
                "index": idx,
                "name": name,
                "vram_used_mb": vram_used_mb,
                "vram_total_mb": vram_total_mb,
                "vram_free_mb": vram_free_mb,
                "vram_usage_percent": vram_pct,
                "utilization_percent": util_pct,
                "memory_utilization_percent": mem_util_pct,
                "temperature_c": int(temp or 0),
                "power_draw_w": round(float(pwr_mw or 0) / 1000.0, 1),
                "power_limit_w": round(float(limit_mw or 0) / 1000.0, 1),
                "fan_speed_percent": int(fan_pct) if fan_pct is not None else None,
                "clock_graphics_mhz": int(clk_graphics) if clk_graphics is not None else None,
                "clock_memory_mhz": int(clk_memory) if clk_memory is not None else None,
                "driver_version": driver_ver,
                "cuda_version": cuda_ver,
                "processes": self._cached_gpu_processes.get(idx, []),
            })

        return gpus

    def sample_once(self, force_all: bool = False):
        """按分层周期执行一次采样并原子更新内存快照。"""
        now = time.monotonic()

        if force_all or (now - self._last_low_freq_at >= self.low_freq_interval):
            self._refresh_low_freq_metrics()
            self._last_low_freq_at = now

        refresh_procs = force_all or (now - self._last_proc_at >= self.proc_interval)
        if refresh_procs:
            self._last_proc_at = now

        snapshot = {
            "cpu": self._collect_cpu(),
            "memory": self._collect_memory(),
            "gpus": self._collect_gpus(refresh_processes=refresh_procs),
            "disks": self._cached_disks,
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

        # 先刷新物理网卡列表，再记录网关启动流量基线与 cpu_percent 预热
        self._refresh_low_freq_metrics()
        self._last_low_freq_at = time.monotonic()
        _safe_call(lambda: psutil.cpu_percent(interval=None))
        base_net = _read_physical_net_bytes(self._physical_nics)
        self._initial_net_bytes = base_net
        self._prev_net_bytes = base_net
        self._prev_net_time = time.monotonic()

        self.sample_once()

        self._thread = threading.Thread(
            target=self._run_loop,
            name="SystemMonitorThread",
            daemon=True,
        )
        self._running = True
        self._thread.start()
        log.info(
            f"[SystemMonitor] Background sampler started "
            f"(fast={self.sample_interval}s, proc={self.proc_interval}s, slow={self.low_freq_interval}s)."
        )

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
                "gpus": [
                    {**g, "processes": [dict(p) for p in g.get("processes", [])]}
                    for g in self._snapshot["gpus"]
                ],
                "disks": [dict(d) for d in self._snapshot.get("disks", [])],
                "network": dict(self._snapshot["network"]),
            }

        return {
            "server": {
                "hostname": self._hostname,
                "os": self._os_name,
                "os_version": self._os_version,
                "arch": self._arch,
                "ip_addresses": list(self._ip_addresses),
                "python_version": self._python_version,
                "pytorch_version": self._pytorch_version,
                "uptime_sec": int(time.monotonic() - self._start_monotonic),
                "gateway_version": str(yaml_config_loader.get("app.version", "1.0.0")),
            },
            **snap,
        }


system_monitor = SystemMonitor()
