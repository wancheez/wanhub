from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services.telemt import TelemtUnavailable, get_telemt_snapshot

router = Router(name="telemt")


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}д {h}ч {m}м {s}с"


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024**2:
        return f"{n / 1024:.1f} КБ"
    if n < 1024**3:
        return f"{n / 1024**2:.2f} МБ"
    return f"{n / 1024**3:.2f} ГБ"


@router.message(Command("telemt"))
async def cmd_telemt(message: Message) -> None:
    try:
        s = await get_telemt_snapshot()
    except TelemtUnavailable as e:
        await message.answer(
            f"⚠️ Метрики telemt недоступны: <code>{escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return

    lines = [
        f"<b>telemt</b> {escape(s.version or '?')} · аптайм {_format_uptime(s.uptime_seconds)}",
        "",
        "<b>Соединения</b>",
        f"  принято: {s.connections_total} (bad: {s.connections_bad_total})",
        f"  hs timeouts: {s.handshake_timeouts_total}",
        "",
        "<b>Upstream</b>",
        f"  ok / fail: {s.upstream_connect_success_total} / {s.upstream_connect_fail_total}",
        "",
        "<b>ME-пул</b>",
        f"  writers: {s.me_writers_active} active / {s.me_writers_target} target / {s.me_writers_warm} warm",
        f"  reconnect: {s.me_reconnect_success_total}/{s.me_reconnect_attempts_total}",
        f"  карантины: {s.me_endpoint_quarantine_total} (unexpected: {s.me_endpoint_quarantine_unexpected_total})",
        "",
        "<b>Здоровье</b>",
        f"  desync: {s.desync_total}, padding: {s.secure_padding_invalid_total}",
        f"  crc/seq: {s.crc_mismatch_total}/{s.seq_mismatch_total}",
        f"  drops no_conn/queue_full: {s.route_drop_no_conn_total}/{s.route_drop_queue_full_total}",
    ]

    if s.users:
        lines.append("")
        lines.append("<b>Пользователи</b>")
        for u in s.users:
            lines.append(
                f"  <code>{escape(u.user)}</code>: "
                f"{u.connections_current} активных, "
                f"↓{_format_bytes(u.octets_to_client)} "
                f"↑{_format_bytes(u.octets_from_client)}, "
                f"IP {u.unique_ips_current}/{u.unique_ips_limit or '∞'}"
            )

    await message.answer("\n".join(lines), parse_mode="HTML")
