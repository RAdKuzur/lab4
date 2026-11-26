"""Тесты хранилища спанов ``SpanStore``.

Проверяют добавление и поиск, защиту от дубликатов по составному
первичному ключу, фильтры по сервису, статусу и длительности,
пагинацию с признаком усечения, подсчёт записей и ретеншн.
"""
from typing import Any

from src.storage.span_store import SpanStore


def test_add_and_search(tmp_store: SpanStore, sample_span: dict[str, Any]) -> None:
    """Проверяет добавление спана и его поиск по ``trace_id``.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
        sample_span: Эталонный спан.
    """
    assert tmp_store.add(sample_span) is True
    spans, total, truncated = tmp_store.search(trace_id=sample_span["trace_id"])
    assert len(spans) == 1
    assert total == 1
    assert truncated is False
    assert spans[0]["service_name"] == "service-b"
    assert spans[0]["attributes"]["http.method"] == "GET"


def test_duplicate_rejected(tmp_store: SpanStore, sample_span: dict[str, Any]) -> None:
    """Проверяет отклонение дубликата по первичному ключу.

    Повторное добавление того же спана возвращает ``False``, а в базе
    остаётся одна запись.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
        sample_span: Эталонный спан.
    """
    assert tmp_store.add(sample_span) is True
    assert tmp_store.add(sample_span) is False  # PRIMARY KEY (trace_id, span_id)
    spans, total, _ = tmp_store.search(trace_id=sample_span["trace_id"])
    assert total == 1


def test_search_by_service(tmp_store: SpanStore, sample_span: dict[str, Any]) -> None:
    """Проверяет фильтр по имени сервиса.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
        sample_span: Эталонный спан.
    """
    other = dict(sample_span, span_id="f"*16, trace_id="a"*32, service_name="service-a")
    tmp_store.add(sample_span)
    tmp_store.add(other)
    spans, total, _ = tmp_store.search(service="service-a")
    assert total == 1
    assert spans[0]["service_name"] == "service-a"


def test_search_by_status(tmp_store: SpanStore, sample_span: dict[str, Any]) -> None:
    """Проверяет фильтр по статусу спана.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
        sample_span: Эталонный спан.
    """
    err = dict(sample_span, span_id="e"*16, trace_id="b"*32, status="ERROR")
    tmp_store.add(sample_span)
    tmp_store.add(err)
    spans, total, _ = tmp_store.search(status="ERROR")
    assert total == 1
    assert spans[0]["status"] == "ERROR"


def test_search_by_min_duration(tmp_store: SpanStore, sample_span: dict[str, Any]) -> None:
    """Проверяет фильтр по минимальной длительности с границей.

    Эталонный спан длится ~10,3 мс: порог 5 мс проходит, 50 мс — нет.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
        sample_span: Эталонный спан.
    """
    tmp_store.add(sample_span)
    _, total, _ = tmp_store.search(min_duration_ns=5_000_000)   # 5 мс
    assert total == 1
    _, total, _ = tmp_store.search(min_duration_ns=50_000_000)  # 50 мс
    assert total == 0


def test_search_limit_truncated(tmp_store: SpanStore, sample_span: dict[str, Any]) -> None:
    """Проверяет пагинацию и признак усечения.

    При пяти записях и ``limit=3`` возвращается три спана, ``total=5``
    и ``truncated=True``.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
        sample_span: Эталонный спан.
    """
    for i in range(5):
        s = dict(sample_span, span_id=f"{i:016x}", trace_id=f"{i:032x}")
        tmp_store.add(s)
    spans, total, truncated = tmp_store.search(limit=3)
    assert len(spans) == 3
    assert total == 5
    assert truncated is True


def test_count(tmp_store: SpanStore, sample_span: dict[str, Any]) -> None:
    """Проверяет счётчик записей: 0 на пустой базе, 1 после добавления.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
        sample_span: Эталонный спан.
    """
    assert tmp_store.count() == 0
    tmp_store.add(sample_span)
    assert tmp_store.count() == 1


def test_retention_removes_old(tmp_store: SpanStore) -> None:
    """Проверяет удаление спанов старше заданной глубины хранения.

    Спан возрастом 8 дней при ``hot_days=7`` должен быть удалён;
    ``apply_retention`` возвращает число удалённых записей.

    Args:
        tmp_store: Изолированное хранилище на временной базе.
    """
    import time
    old = {
        "trace_id": "a"*32, "span_id": "b"*16,
        "parent_span_id": None, "service_name": "s", "name": "n",
        "kind": "SERVER",
        "start_time_unix_nano": int((time.time() - 8*86400) * 1e9),  # 8 дней назад
        "end_time_unix_nano": int((time.time() - 8*86400) * 1e9) + 1000,
        "status": "OK",
    }
    tmp_store.add(old)
    assert tmp_store.count() == 1
    removed = tmp_store.apply_retention(hot_days=7)
    assert removed == 1
    assert tmp_store.count() == 0