"""Тесты поиска спанов по трассе ``lookup``.

Проверяют успешный поиск по корректному ``trace_id``, отказ при
неверной длине идентификатора и пустой результат для отсутствующей
трассы.
"""
from typing import Any

import pytest

from src.query.trace_lookup import lookup
from src.storage.span_store import SpanStore


def test_lookup_valid(tmp_store: SpanStore, sample_span: dict[str, Any]) -> None:
    """Проверяет поиск спанов по корректному ``trace_id``.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
        sample_span: Эталонный спан.
    """
    tmp_store.add(sample_span)
    result = lookup(tmp_store, sample_span["trace_id"])
    assert len(result) == 1


def test_lookup_short_trace_id(tmp_store: SpanStore) -> None:
    """Проверяет отказ при неверной длине ``trace_id``.

    Ожидается ``ValueError`` с упоминанием ``32 hex``.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
    """
    with pytest.raises(ValueError, match="32 hex"):
        lookup(tmp_store, "abc")


def test_lookup_not_found(tmp_store: SpanStore) -> None:
    """Проверяет пустой результат для отсутствующего ``trace_id``.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
    """
    assert lookup(tmp_store, "c"*32) == []