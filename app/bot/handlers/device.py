import asyncio
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services.device import get_device_info

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
    cpu_temp = f"{info.cpu_temp_c} °C" if info.cpu_temp_c is not None else "—"
    cpu_freq = f"{info.cpu_freq_mhz} МГц" if info.cpu_freq_mhz is not None else "—"
    text = (
        f"<b>{escape(info.model)}</b>\n"
        f"hostname: <code>{escape(info.hostname)}</code>\n"
        f"uptime: {_format_uptime(info.uptime_seconds)}\n\n"
        f"<b>CPU</b>\n"
        f"  ядер: {info.cpu_count}\n"
        f"  температура: {cpu_temp}\n"
        f"  частота: {cpu_freq}\n"
        f"  load avg: {info.load_avg_1} / {info.load_avg_5} / {info.load_avg_15}\n\n"
        f"<b>Память</b>\n"
        f"  {info.memory_used_mb} / {info.memory_total_mb} МБ "
        f"({info.memory_used_percent}%)\n\n"
        f"<b>Диск (/)</b>\n"
        f"  {info.disk_used_gb} / {info.disk_total_gb} ГБ "
        f"({info.disk_used_percent}%)"
    )
    await message.answer(text, parse_mode="HTML")
