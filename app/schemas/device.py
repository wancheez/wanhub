from pydantic import BaseModel


class DeviceInfo(BaseModel):
    hostname: str
    model: str
    kernel: str
    uptime_seconds: float
    cpu_count: int
    cpu_model: str | None
    cpu_temp_c: float | None
    cpu_freq_mhz: float | None
    load_avg_1: float
    load_avg_5: float
    load_avg_15: float
    memory_total_mb: int
    memory_used_mb: int
    memory_used_percent: float
    disk_total_gb: float
    disk_used_gb: float
    disk_used_percent: float
