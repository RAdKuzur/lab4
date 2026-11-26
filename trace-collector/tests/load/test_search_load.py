"""Нагрузочный тест поиска спанов по ``trace_id`` (``GET /spans``).

Отправляет заданное число запросов с ограничением конкурентности,
собирает латентности и ошибки и печатает сводку (RPS, среднее,
P50/P95/P99, максимум). Если ``LOAD_TRACE_ID`` не задан, тест
пропускается.
"""
import asyncio
import os
import statistics
import time
from typing import Optional

import httpx
import pytest


pytestmark = pytest.mark.load

BASE_URL: str = os.getenv("COLLECTOR_URL", "http://trace-collector:4318")
TRACE_ID: Optional[str] = os.getenv("LOAD_TRACE_ID")

TOTAL_REQUESTS: int = int(os.getenv("LOAD_SEARCH_REQUESTS", "500"))
CONCURRENCY: int = int(os.getenv("LOAD_SEARCH_CONCURRENCY", "50"))


def percentile(values: list[float], percent: float) -> float:
    """Вычисляет перцентиль по списку значений.

    Args:
        values: Список числовых значений.
        percent: Перцентиль в диапазоне 0–100.

    Returns:
        Значение перцентиля; 0, если список пуст.
    """
    values = sorted(values)
    if not values:
        return 0

    index = round((len(values) - 1) * percent / 100)
    return values[index]


@pytest.mark.asyncio
async def test_collector_trace_lookup_load() -> None:
    """Выполняет нагрузочный прогон поиска по ``trace_id``.

    Отправляет ``TOTAL_REQUESTS`` запросов с конкурентностью
    ``CONCURRENCY``, собирает латентности и ошибки, печатает сводный
    отчёт и проверяет отсутствие ошибок и полноту выборки латентностей.

    Raises:
        pytest.skip.Exception: Если ``LOAD_TRACE_ID`` не задан.
        AssertionError: Если есть ошибки запросов или число собранных
            латентностей не совпало с ``TOTAL_REQUESTS``.
    """
    if not TRACE_ID:
        pytest.skip("Set LOAD_TRACE_ID to an existing trace_id")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    latencies: list[float] = []
    errors: list[str] = []

    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=30
    ) as client:

        health = await client.get("/healthz")
        assert health.status_code == 200

        async def search() -> None:
            """Отправляет один поисковый запрос и учитывает результат."""
            async with semaphore:
                started = time.perf_counter()

                try:
                    response = await client.get(
                        "/spans",
                        params={"trace_id": TRACE_ID}
                    )

                    elapsed = (
                        time.perf_counter() - started
                    ) * 1000

                    latencies.append(elapsed)

                    if response.status_code != 200:
                        errors.append(
                            f"HTTP {response.status_code}"
                        )

                except httpx.HTTPError as exc:
                    errors.append(str(exc))

        started = time.perf_counter()

        await asyncio.gather(
            *[search() for _ in range(TOTAL_REQUESTS)]
        )

        duration = time.perf_counter() - started

    rps = TOTAL_REQUESTS / duration if duration else 0

    print("\n========== SEARCH LOAD TEST ==========")
    print(f"Requests:     {TOTAL_REQUESTS}")
    print(f"Concurrency:  {CONCURRENCY}")
    print(f"Duration:     {duration:.2f} sec")
    print(f"Requests/sec: {rps:.2f}")

    if latencies:
        print(f"Average:      {statistics.mean(latencies):.2f} ms")
        print(f"P50:          {percentile(latencies, 50):.2f} ms")
        print(f"P95:          {percentile(latencies, 95):.2f} ms")
        print(f"P99:          {percentile(latencies, 99):.2f} ms")
        print(f"Max:          {max(latencies):.2f} ms")

    print(f"Errors:       {len(errors)}")
    print("======================================\n")

    assert not errors, errors[:10]
    assert len(latencies) == TOTAL_REQUESTS