"""Разбор заголовка W3C Trace Context ``traceparent``.

Заголовок имеет формат ``версия-trace_id-span_id-flags``, где версия и
flags — по 2 hex-символа, trace_id — 32, span_id — 16. Пробелы по краям
допускаются и обрезаются перед проверкой.
"""
import re
from typing import Optional

TRACEPARENT_RE: re.Pattern[str] = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)


def parse_traceparent(header: Optional[str]):
    """Разбирает заголовок ``traceparent`` в словарь полей.

    Args:
        header: Значение заголовка ``traceparent`` или None.

    Returns:
        Словарь ``{"trace_id": ..., "span_id": ..., "flags": ...}``
        при корректном формате, иначе None. Пустая строка и строка,
        не соответствующая шаблону, дают None; пробелы по краям
        обрезаются перед проверкой.
    """
    if not header:
        return None
    m = TRACEPARENT_RE.match(header.strip())
    if not m:
        return None
    _version, trace_id, span_id, flags = m.groups()
    return {"trace_id": trace_id, "span_id": span_id, "flags": flags}