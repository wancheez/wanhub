import os
import shutil
import socket
from pathlib import Path

from app.schemas.device import DeviceInfo


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text().strip().rstrip("\x00")
    except OSError:
        return None


def _cpu_temp_c() -> float | None:
    raw = _read_text("/sys/class/thermal/thermal_zone0/temp")
    return round(int(raw) / 1000, 1) if raw else None


def _cpu_freq_mhz() -> float | None:
    raw = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    return round(int(raw) / 1000, 1) if raw else None


def _model() -> str:
    return _read_text("/proc/device-tree/model") or "unknown"


def _uptime_seconds() -> float:
    raw = _read_text("/proc/uptime")
    return float(raw.split()[0]) if raw else 0.0


def _memory_mb() -> tuple[int, int]:
    """Return (total_mb, used_mb) parsed from /proc/meminfo."""
    info: dict[str, int] = {}
    text = _read_text("/proc/meminfo") or ""
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        info[key.strip()] = int(rest.strip().split()[0])  # kB
    total_kb = info.get("MemTotal", 0)
    available_kb = info.get("MemAvailable", info.get("MemFree", 0))
    used_kb = max(total_kb - available_kb, 0)
    return total_kb // 1024, used_kb // 1024


def get_device_info() -> DeviceInfo:
    load_1, load_5, load_15 = os.getloadavg()
    memory_total_mb, memory_used_mb = _memory_mb()
    memory_used_percent = (
        round(memory_used_mb / memory_total_mb * 100, 1) if memory_total_mb else 0.0
    )

    disk = shutil.disk_usage("/")
    disk_total_gb = round(disk.total / 1024**3, 2)
    disk_used_gb = round(disk.used / 1024**3, 2)
    disk_used_percent = round(disk.used / disk.total * 100, 1) if disk.total else 0.0

    return DeviceInfo(
        hostname=socket.gethostname(),
        model=_model(),
        kernel=os.uname().release,
        uptime_seconds=round(_uptime_seconds(), 1),
        cpu_count=os.cpu_count() or 0,
        cpu_temp_c=_cpu_temp_c(),
        cpu_freq_mhz=_cpu_freq_mhz(),
        load_avg_1=round(load_1, 2),
        load_avg_5=round(load_5, 2),
        load_avg_15=round(load_15, 2),
        memory_total_mb=memory_total_mb,
        memory_used_mb=memory_used_mb,
        memory_used_percent=memory_used_percent,
        disk_total_gb=disk_total_gb,
        disk_used_gb=disk_used_gb,
        disk_used_percent=disk_used_percent,
    )
