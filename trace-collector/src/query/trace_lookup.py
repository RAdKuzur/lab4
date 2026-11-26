"""Поиск всех спанов трассы по её идентификатору.

Проверяет формат ``trace_id`` (32 hex-символа в любом регистре) и
возвращает все спаны трассы без ограничения на число результатов.
"""
import re
from typing import Any

from src.storage.span_store import SpanStore


TRACE_ID_RE: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{32}$")


def lookup(store: SpanStore, trace_id: str) -> list[dict[str, Any]]:
    """Возвращает все спаны трассы по её идентификатору.

    Идентификатор проверяется регулярным выражением (32 hex-символа,
    верхний или нижний регистр) и приводится к нижнему регистру перед
    поиском. Поиск выполняется с большим лимитом, чтобы вернуть трассу
    целиком.

    Args:
        store: Хранилище спанов.
        trace_id: Идентификатор трассы (32 hex-символа).

    Returns:
        Список спанов трассы; пустой список, если трасса не найдена.

    Raises:
        ValueError: Если ``trace_id`` не является строкой из 32
            hex-символов.
    """
    if not isinstance(trace_id, str) or not TRACE_ID_RE.fullmatch(trace_id):
        raise ValueError("trace_id must be 32 hexadecimal characters")

    spans, _, _ = store.search(
        trace_id=trace_id.lower(),
        limit=100000,
    )

    return spans