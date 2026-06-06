import asyncio
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services.device import get_device_info
from app.services.version import get_version

router = Router(name="device")


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}д {h}ч {m}м {s}с"


@router.message(Command("device"))
async def cmd_device(message: Message) -> None:
    info = await asyncio.to_thread(get_device_info)

    # CPU-блок: модель/температура/частота показываем только если данные есть
    # (на облачных VM сенсоров и cpufreq может не быть — тогда строки опускаем).
    cpu_lines = [f"  ядер: {info.cpu_count}"]
    if info.cpu_model:
        cpu_lines.append(f"  модель: {escape(info.cpu_model)}")
    if info.cpu_temp_c is not None:
        cpu_lines.append(f"  температура: {info.cpu_temp_c} °C")
    if info.cpu_freq_mhz is not None:
        cpu_lines.append(f"  частота: {info.cpu_freq_mhz} МГц")
    cpu_lines.append(f"  load avg: {info.load_avg_1} / {info.load_avg_5} / {info.load_avg_15}")
    cpu_block = "\n".join(cpu_lines)

    text = (
        f"<b>{escape(info.model)}</b>\n"
        f"версия: <code>{escape(get_version().short())}</code>\n"
        f"hostname: <code>{escape(info.hostname)}</code>\n"
        f"ядро: <code>{escape(info.kernel)}</code>\n"
        f"uptime: {_format_uptime(info.uptime_seconds)}\n\n"
        f"<b>CPU</b>\n"
        f"{cpu_block}\n\n"
        f"<b>Память</b>\n"
        f"  {info.memory_used_mb} / {info.memory_total_mb} МБ "
        f"({info.memory_used_percent}%)\n\n"
        f"<b>Диск (/)</b>\n"
        f"  {info.disk_used_gb} / {info.disk_total_gb} ГБ "
        f"({info.disk_used_percent}%)"
    )
    await message.answer(text, parse_mode="HTML")
