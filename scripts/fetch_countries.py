"""Однократный фетч списка стран с restcountries.com в локальный JSON.

Запуск: `poetry run python scripts/fetch_countries.py`

Файл сохраняется в `app/services/countries.json` и коммитится в репо. Бот
читает его при старте, не делая сетевых запросов в рантайме. Чтобы обновить
данные — перезапустить скрипт.

Русские названия столиц поддерживаются вручную в RU_CAPITALS ниже —
restcountries.com даёт только английские. Страны без русского названия
столицы попадут в JSON с capital_ru = null и будут исключены из квиза
по столицам, но всё равно работают в флаговой викторине.
"""

import json
import sys
from pathlib import Path

import httpx

URL = "https://restcountries.com/v3.1/independent?status=true&fields=name,translations,cca2,region"
OUT_PATH = Path(__file__).resolve().parent.parent / "app" / "services" / "countries.json"

# cca2 → русское название столицы. Покрывает все ~195 независимых стран.
RU_CAPITALS: dict[str, str] = {
    # Europe
    "AD": "Андорра-ла-Велья",
    "AL": "Тирана",
    "AT": "Вена",
    "BA": "Сараево",
    "BE": "Брюссель",
    "BG": "София",
    "BY": "Минск",
    "CH": "Берн",
    "CZ": "Прага",
    "DE": "Берлин",
    "DK": "Копенгаген",
    "EE": "Таллин",
    "ES": "Мадрид",
    "FI": "Хельсинки",
    "FR": "Париж",
    "GB": "Лондон",
    "GR": "Афины",
    "HR": "Загреб",
    "HU": "Будапешт",
    "IE": "Дублин",
    "IS": "Рейкьявик",
    "IT": "Рим",
    "LI": "Вадуц",
    "LT": "Вильнюс",
    "LU": "Люксембург",
    "LV": "Рига",
    "MC": "Монако",
    "MD": "Кишинёв",
    "ME": "Подгорица",
    "MK": "Скопье",
    "MT": "Валлетта",
    "NL": "Амстердам",
    "NO": "Осло",
    "PL": "Варшава",
    "PT": "Лиссабон",
    "RO": "Бухарест",
    "RS": "Белград",
    "RU": "Москва",
    "SE": "Стокгольм",
    "SI": "Любляна",
    "SK": "Братислава",
    "SM": "Сан-Марино",
    "UA": "Киев",
    "VA": "Ватикан",
    "XK": "Приштина",
    # Asia
    "AE": "Абу-Даби",
    "AF": "Кабул",
    "AM": "Ереван",
    "AZ": "Баку",
    "BD": "Дакка",
    "BH": "Манама",
    "BN": "Бандар-Сери-Бегаван",
    "BT": "Тхимпху",
    "CN": "Пекин",
    "CY": "Никосия",
    "GE": "Тбилиси",
    "ID": "Джакарта",
    "IL": "Иерусалим",
    "IN": "Нью-Дели",
    "IQ": "Багдад",
    "IR": "Тегеран",
    "JO": "Амман",
    "JP": "Токио",
    "KG": "Бишкек",
    "KH": "Пномпень",
    "KP": "Пхеньян",
    "KR": "Сеул",
    "KW": "Эль-Кувейт",
    "KZ": "Астана",
    "LA": "Вьентьян",
    "LB": "Бейрут",
    "LK": "Шри-Джаяварденепура-Котте",
    "MM": "Нейпьидо",
    "MN": "Улан-Батор",
    "MV": "Мале",
    "MY": "Куала-Лумпур",
    "NP": "Катманду",
    "OM": "Маскат",
    "PH": "Манила",
    "PK": "Исламабад",
    "QA": "Доха",
    "SA": "Эр-Рияд",
    "SG": "Сингапур",
    "SY": "Дамаск",
    "TH": "Бангкок",
    "TJ": "Душанбе",
    "TL": "Дили",
    "TM": "Ашхабад",
    "TR": "Анкара",
    "UZ": "Ташкент",
    "VN": "Ханой",
    "YE": "Сана",
    # Africa
    "AO": "Луанда",
    "BF": "Уагадугу",
    "BI": "Гитега",
    "BJ": "Порто-Ново",
    "BW": "Габороне",
    "CD": "Киншаса",
    "CF": "Банги",
    "CG": "Браззавиль",
    "CI": "Ямусукро",
    "CM": "Яунде",
    "CV": "Прая",
    "DJ": "Джибути",
    "DZ": "Алжир",
    "EG": "Каир",
    "ER": "Асмэра",
    "ET": "Аддис-Абеба",
    "GA": "Либревиль",
    "GH": "Аккра",
    "GM": "Банжул",
    "GN": "Конакри",
    "GQ": "Малабо",
    "GW": "Бисау",
    "KE": "Найроби",
    "KM": "Морони",
    "LR": "Монровия",
    "LS": "Масеру",
    "LY": "Триполи",
    "MA": "Рабат",
    "MG": "Антананариву",
    "ML": "Бамако",
    "MR": "Нуакшот",
    "MU": "Порт-Луи",
    "MW": "Лилонгве",
    "MZ": "Мапуту",
    "NA": "Виндхук",
    "NE": "Ниамей",
    "NG": "Абуджа",
    "RW": "Кигали",
    "SC": "Виктория",
    "SD": "Хартум",
    "SL": "Фритаун",
    "SN": "Дакар",
    "SO": "Могадишо",
    "SS": "Джуба",
    "ST": "Сан-Томе",
    "SZ": "Мбабане",
    "TD": "Нджамена",
    "TG": "Ломе",
    "TN": "Тунис",
    "TZ": "Додома",
    "UG": "Кампала",
    "ZA": "Претория",
    "ZM": "Лусака",
    "ZW": "Хараре",
    # Americas
    "AG": "Сент-Джонс",
    "AR": "Буэнос-Айрес",
    "BB": "Бриджтаун",
    "BO": "Сукре",
    "BR": "Бразилиа",
    "BS": "Нассау",
    "BZ": "Бельмопан",
    "CA": "Оттава",
    "CL": "Сантьяго",
    "CO": "Богота",
    "CR": "Сан-Хосе",
    "CU": "Гавана",
    "DM": "Розо",
    "DO": "Санто-Доминго",
    "EC": "Кито",
    "GD": "Сент-Джорджес",
    "GT": "Гватемала",
    "GY": "Джорджтаун",
    "HN": "Тегусигальпа",
    "HT": "Порт-о-Пренс",
    "JM": "Кингстон",
    "KN": "Бастер",
    "LC": "Кастри",
    "MX": "Мехико",
    "NI": "Манагуа",
    "PA": "Панама",
    "PE": "Лима",
    "PY": "Асунсьон",
    "SR": "Парамарибо",
    "SV": "Сан-Сальвадор",
    "TT": "Порт-оф-Спейн",
    "US": "Вашингтон",
    "UY": "Монтевидео",
    "VC": "Кингстаун",
    "VE": "Каракас",
    # Oceania
    "AU": "Канберра",
    "FJ": "Сува",
    "FM": "Паликир",
    "KI": "Тарава",
    "MH": "Маджуро",
    "NR": "Ярен",
    "NZ": "Веллингтон",
    "PG": "Порт-Морсби",
    "PW": "Нгерулмуд",
    "SB": "Хониара",
    "TO": "Нукуалофа",
    "TV": "Фунафути",
    "VU": "Порт-Вила",
    "WS": "Апиа",
}


def main() -> int:
    print(f"Fetching {URL}…")
    try:
        r = httpx.get(URL, timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0))
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    data = r.json()
    out = []
    missing_capitals: list[str] = []
    for item in data:
        cca2 = (item.get("cca2") or "").strip().upper()
        rus = ((item.get("translations") or {}).get("rus") or {}).get("common")
        name_en = (item.get("name") or {}).get("common")
        region = item.get("region") or ""
        if not cca2 or not rus or not name_en:
            continue
        capital_ru = RU_CAPITALS.get(cca2)
        if capital_ru is None:
            missing_capitals.append(f"{cca2} ({rus})")
        out.append(
            {
                "cca2": cca2,
                "name_ru": rus,
                "name_en": name_en,
                "region": region,
                "capital_ru": capital_ru,
            }
        )

    out.sort(key=lambda x: x["cca2"])
    OUT_PATH.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with_caps = sum(1 for c in out if c["capital_ru"])
    print(f"OK: {len(out)} countries (with capital_ru: {with_caps}) → {OUT_PATH}")
    if missing_capitals:
        print(f"  missing RU capital for: {', '.join(missing_capitals)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
