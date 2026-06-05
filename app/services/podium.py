"""Офлайн-рендер картинки с пьедесталом для итогов игр (Pillow, без нейросети).

Presentation-слой: принимает уже посчитанный топ (имя + метрики строками) и
рисует PNG с тумбами 2-1-3. Никакой зависимости от БД и формата конкретной
игры — и «Сделка», и блекджек отдают сюда свой топ через `PodiumEntry`.

Почему Pillow, а не headless-браузер: бот живёт на Raspberry Pi, Pillow уже в
зависимостях, а DejaVu Sans с кириллицей есть в системе. Рендер занимает
десятки миллисекунд и не тянет внешних сервисов.

Аватарок Telegram тут намеренно нет: медальон рисуется из инициалов игрока.
Реальные фото можно прикрутить позже, передав в `PodiumEntry.avatar` готовые
байты (см. заглушку в `_draw_medallion`).
"""

import io
import logging
from dataclasses import dataclass
from typing import cast

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("app")

__all__ = ["PodiumEntry", "render_podium"]


# --- Палитра -----------------------------------------------------------------

_BG_TOP = (28, 32, 56)  # верх вертикального градиента фона (тёмно-синий)
_BG_BOTTOM = (12, 14, 28)  # низ градиента
_TITLE = (245, 247, 255)
_SUBTITLE = (150, 158, 190)
_FOOTER = (150, 158, 190)
_NAME = (235, 238, 248)
_VALUE = (255, 255, 255)
_SUB = (185, 190, 210)

# Цвета медалей: золото, серебро, бронза. Индексируются местом (0,1,2).
_MEDAL = ((255, 198, 60), (197, 206, 220), (208, 138, 78))
_MEDAL_DARK = ((120, 84, 0), (78, 86, 100), (96, 56, 20))  # текст инициалов/места

# --- Геометрия ---------------------------------------------------------------

_W, _H = 1000, 720
_MARGIN_BOTTOM = 40
_BAR_W = 250
_BAR_GAP = 16
_BAR_HEIGHTS = (330, 250, 195)  # по местам: 1, 2, 3
_MEDALLION_R = 64
_RANK_BY_SLOT = (0, 1, 2)  # какое место стоит в визуальном слоте (лево-центр-право)
_SLOT_ORDER = (1, 0, 2)  # слева 2-е место, по центру 1-е, справа 3-е


@dataclass(frozen=True)
class PodiumEntry:
    """Одна строка пьедестала. value — главная метрика, sub — подпись под ней.

    avatar — байты фото профиля (любой формат, который читает Pillow). Если
    None или картинка битая, медальон рисуется из инициалов имени.
    """

    name: str
    value: str
    sub: str | None = None
    avatar: bytes | None = None


# --- Шрифты ------------------------------------------------------------------

# Порядок поиска: системные DejaVu (есть на Pi и в WSL) → дефолт Pillow.
_FONT_CANDIDATES_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)
_FONT_CANDIDATES_REG = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "DejaVuSans.ttf",
)

_font_cache: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (bold, size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            _font_cache[key] = font
            return font
        except OSError:
            continue
    # Последний рубеж: дефолт Pillow (в 10+ это TrueType-DejaVu, кириллица есть).
    font = cast(ImageFont.FreeTypeFont, ImageFont.load_default(size))
    _font_cache[key] = font
    return font


# --- Примитивы ---------------------------------------------------------------


def _vertical_gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    """Фон с плавным вертикальным переходом цвета. Рисуем по строкам пикселей."""
    w, h = size
    base = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t = y / max(1, h - 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line(((0, y), (w, y)), fill=color)
    return base


def _fit_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int
) -> str:
    """Обрезать текст с многоточием, чтобы влез в max_w пикселей."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    while text and draw.textlength(text + ell, font=font) > max_w:
        text = text[:-1]
    return (text + ell) if text else ell


def _initials(name: str) -> str:
    """1-2 буквы из имени для медальона: «Иван Петров» → «ИП», «deal» → «D»."""
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[1][0]).upper()


def _load_avatar(data: bytes, diameter: int) -> Image.Image | None:
    """Декодировать фото, центрированно обрезать в квадрат и масштабировать.

    None если байты не открылись как картинка — вызывающий откатится на инициалы.
    """
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        log.info("podium: bad avatar bytes — fallback to initials")
        return None
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    im = im.crop((left, top, left + side, top + side))
    return im.resize((diameter, diameter), Image.Resampling.LANCZOS)


def _draw_medallion(
    img: Image.Image, center: tuple[int, int], rank: int, entry: PodiumEntry
) -> None:
    """Кружок-аватар с цветной обводкой по медали места.

    Если у записи есть валидное фото — кладём его круглым кропом, иначе рисуем
    инициалы на тёмном круге.
    """
    cx, cy = center
    r = _MEDALLION_R
    draw = ImageDraw.Draw(img)
    box = (cx - r, cy - r, cx + r, cy + r)
    # Обводка-кольцо цветом медали.
    draw.ellipse(box, fill=_MEDAL[rank])

    inner_r = r - 7
    diameter = inner_r * 2
    avatar = _load_avatar(entry.avatar, diameter) if entry.avatar else None
    if avatar is not None:
        # Круглая маска по размеру фото — вставляем поверх кольца.
        mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
        img.paste(avatar, (cx - inner_r, cy - inner_r), mask)
        return
    # Фолбэк: тёмный круг + инициалы цветом медали.
    inner = (cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r)
    draw.ellipse(inner, fill=(22, 26, 44))
    draw.text(
        (cx, cy), _initials(entry.name), font=_font(46, bold=True), fill=_MEDAL[rank], anchor="mm"
    )


def _draw_bar(img: Image.Image, slot_x: int, rank: int, entry: PodiumEntry) -> None:
    """Одна тумба пьедестала плюс всё, что над и на ней."""
    draw = ImageDraw.Draw(img)
    bar_h = _BAR_HEIGHTS[rank]
    bar_top = _H - _MARGIN_BOTTOM - bar_h
    bar_left = slot_x
    bar_right = slot_x + _BAR_W

    # Сама тумба: лёгкий двухтоновый блок со скруглённым верхом.
    face = tuple(min(255, c + 18) for c in _MEDAL_DARK[rank])
    draw.rounded_rectangle(
        (bar_left, bar_top, bar_right, _H - _MARGIN_BOTTOM),
        radius=14,
        fill=face,
    )
    # Цветная «крышка» цветом медали — визуально отделяет верх тумбы.
    draw.rounded_rectangle(
        (bar_left, bar_top, bar_right, bar_top + 12),
        radius=6,
        fill=_MEDAL[rank],
    )

    cx = (bar_left + bar_right) // 2

    # Крупная цифра места по центру тумбы.
    draw.text(
        (cx, bar_top + bar_h // 2 + 18),
        str(rank + 1),
        font=_font(96, bold=True),
        fill=_MEDAL[rank],
        anchor="mm",
    )

    # Подпись (кол-во игр и т.п.) — на верхней части тумбы, под крышкой.
    if entry.sub:
        sub = _fit_text(draw, entry.sub, _font(22), _BAR_W - 16)
        draw.text((cx, bar_top + 36), sub, font=_font(22), fill=_SUB, anchor="mm")

    # Над тумбой стопкой вверх (фикс. отступы, не зависят от высоты тумбы):
    # значение → имя → медальон. Так блоки не наезжают при разной высоте.
    value_y = bar_top - 30
    name_y = bar_top - 66
    med_cy = name_y - 30 - _MEDALLION_R

    value = _fit_text(draw, entry.value, _font(30, bold=True), _BAR_W - 8)
    draw.text((cx, value_y), value, font=_font(30, bold=True), fill=_VALUE, anchor="mm")

    name = _fit_text(draw, entry.name, _font(28, bold=True), _BAR_W + 4)
    draw.text((cx, name_y), name, font=_font(28, bold=True), fill=_NAME, anchor="mm")

    _draw_medallion(img, (cx, med_cy), rank, entry)


def render_podium(
    entries: list[PodiumEntry],
    *,
    title: str,
    period: str | None = None,
    footer: str | None = None,
) -> bytes:
    """Отрисовать пьедестал и вернуть PNG-байты.

    entries — топ по убыванию места (1-е, 2-е, 3-е). Хватит и одной записи:
    пустые слоты просто не рисуются. Лишние записи (4+) игнорируются —
    пьедестал всегда максимум на три места.
    """
    img = _vertical_gradient((_W, _H), _BG_TOP, _BG_BOTTOM)
    draw = ImageDraw.Draw(img)

    # Заголовок и период по центру сверху.
    draw.text(
        (_W // 2, 56),
        _fit_text(draw, title, _font(44, bold=True), _W - 80),
        font=_font(44, bold=True),
        fill=_TITLE,
        anchor="mm",
    )
    if period:
        draw.text(
            (_W // 2, 104),
            _fit_text(draw, period, _font(26), _W - 80),
            font=_font(26),
            fill=_SUBTITLE,
            anchor="mm",
        )

    # Три слота по горизонтали, центрируем блок из трёх тумб.
    total_w = _BAR_W * 3 + _BAR_GAP * 2
    start_x = (_W - total_w) // 2
    for slot, rank in enumerate(_SLOT_ORDER):
        if rank >= len(entries):
            continue
        slot_x = start_x + slot * (_BAR_W + _BAR_GAP)
        _draw_bar(img, slot_x, rank, entries[rank])

    if footer:
        draw.text(
            (_W // 2, _H - 16),
            _fit_text(draw, footer, _font(22), _W - 80),
            font=_font(22),
            fill=_FOOTER,
            anchor="mm",
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
