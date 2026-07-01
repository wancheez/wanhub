"""Единый лог потребления токенов Claude для всех вызовов Anthropic.

`log_usage(op, response, elapsed)` пишет одну строку INFO: модель, latency,
stop_reason и разбивку токенов из `response.usage` (вход, выход, чтение и
запись кэша), плюс грубую оценку стоимости по тарифу модели. Для модели с
неизвестным тарифом стоимость не печатаем (est_cost=n/a), токены — всегда.

Вызывать из каждого места, где дёргается Claude, передавая короткую метку `op`
(«chat», «llm_quiz», …), сам ответ SDK и затраченное время.
"""

import logging

log = logging.getLogger("app")

__all__ = ["log_usage"]

# $ за 1M токенов: (input, output). Кэш считаем производно стандартными
# множителями Anthropic: чтение кэша = 0.1×input, запись (5-мин) = 1.25×input.
# Сопоставление по префиксу модели, чтобы «claude-haiku-4-5-20251001» попадал
# под «claude-haiku-4-5».
_RATES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
}


def _rate(model: str) -> tuple[float, float] | None:
    for key, rate in _RATES.items():
        if model.startswith(key):
            return rate
    return None


def _estimate_cost(
    model: str, inp: int, out: int, cache_read: int, cache_write: int
) -> float | None:
    rate = _rate(model)
    if rate is None:
        return None
    in_rate, out_rate = rate
    return (
        inp / 1e6 * in_rate
        + out / 1e6 * out_rate
        + cache_read / 1e6 * in_rate * 0.1
        + cache_write / 1e6 * in_rate * 1.25
    )


def log_usage(op: str, response: object, elapsed: float) -> None:
    """Залогировать токены и метаинформацию одного вызова Claude.

    `response` — объект Message из Anthropic SDK (берём поля защитно через
    getattr, чтобы хелпер не падал на неожиданной форме ответа).
    """
    model = getattr(response, "model", "?")
    stop = getattr(response, "stop_reason", "?")
    usage = getattr(response, "usage", None)
    if usage is None:
        log.info("llm %s: model=%s elapsed=%.2fs stop=%s (no usage)", op, model, elapsed, stop)
        return

    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

    cost = _estimate_cost(model, inp, out, cache_read, cache_write)
    cost_s = f"~${cost:.4f}" if cost is not None else "n/a"
    log.info(
        "llm %s: model=%s elapsed=%.2fs stop=%s "
        "tokens[in=%d out=%d cache_read=%d cache_write=%d] est_cost=%s",
        op,
        model,
        elapsed,
        stop,
        inp,
        out,
        cache_read,
        cache_write,
        cost_s,
    )
