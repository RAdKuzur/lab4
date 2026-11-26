"""Нагрузочный тест приёма спанов ``POST /v1/traces``.

Отправляет заданное число запросов с ограничением конкурентности,
собирает латентности и коды ответов, при наличии ``COLLECTOR_PID``
снимает потребление памяти процесса коллектора через ``psutil`` и
печатает сводку (RPS, среднее, P50/P95/P99, максимум, RAM).
"""
import asyncio
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import psutil
import pytest


pytestmark = pytest.mark.load

BASE_URL: str = os.getenv("COLLECTOR_URL", "http://trace-collector:4318")

PAYLOADS_PATH: Path = Path(__file__).parent / "payloads.jsonl"

TOTAL_REQUESTS: int = int(os.getenv("LOAD_TOTAL_REQUESTS", "1000"))
CONCURRENCY: int = int(os.getenv("LOAD_CONCURRENCY", "50"))
COLLECTOR_PID: Optional[str] = os.getenv("COLLECTOR_PID")


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


def load_payloads() -> list[dict[str, Any]]:
    """Загружает спаны из JSONL-файла ``payloads.jsonl``.

    Returns:
        Список словарей спанов; пустые строки пропускаются.
    """
    with PAYLOADS_PATH.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


@pytest.mark.asyncio
async def test_collector_ingest_load() -> None:
    """Выполняет нагрузочный прогон приёма спанов.

    Отправляет ``TOTAL_REQUESTS`` запросов с конкурентностью
    ``CONCURRENCY``, собирает латентности, коды ответов и счётчики
    вставок/дубликатов, печатает сводный отчёт и проверяет, что
    ошибок нет, а все запросы вернули 200.

    Raises:
        AssertionError: Если файл payloads пуст, есть ошибки запросов
            или не все запросы завершились кодом 200.
    """
    payloads = load_payloads()
    assert payloads, "Payload file is empty"

    semaphore = asyncio.Semaphore(CONCURRENCY)
    latencies: list[float] = []
    statuses: list[int] = []
    inserted = 0
    duplicates = 0
    errors: list[str] = []

    process: Optional[psutil.Process] = (
        psutil.Process(int(COLLECTOR_PID))
        if COLLECTOR_PID
        else None
    )

    memory_samples: list[float] = []

    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=30
    ) as client:

        health = await client.get("/healthz")
        assert health.status_code == 200

        initial_memory: Optional[float] = (
            process.memory_info().rss / 1024 / 1024
            if process else None
        )

        async def send_request(index: int) -> None:
            """Отправляет один спан и учитывает результат.

            Args:
                index: Индекс запроса, по модулю выбирающий payload.
            """
            nonlocal inserted, duplicates

            async with semaphore:
                payload = payloads[index % len(payloads)]
                started = time.perf_counter()

                try:
                    response = await client.post(
                        "/v1/traces",
                        json=payload
                    )

                    elapsed = (
                        time.perf_counter() - started
                    ) * 1000

                    latencies.append(elapsed)
                    statuses.append(response.status_code)

                    if response.status_code != 200:
                        errors.append(
                            f"HTTP {response.status_code}: "
                            f"{response.text[:200]}"
                        )
                        return

                    result = response.json().get("status")

                    if result == "ok":
                        inserted += 1
                    elif result == "duplicate":
                        duplicates += 1

                except Exception as exc:
                    errors.append(str(exc))

        started = time.perf_counter()

        tasks = [
            asyncio.create_task(send_request(i))
            for i in range(TOTAL_REQUESTS)
        ]

        await asyncio.gather(*tasks)

        duration = time.perf_counter() - started

        final_memory: Optional[float] = (
            process.memory_info().rss / 1024 / 1024
            if process else None
        )

    successful = sum(status == 200 for status in statuses)
    rps = successful / duration if duration else 0
    insert_rps = inserted / duration if duration else 0

    print("\n========== INGEST LOAD TEST ==========")
    print(f"Requests:       {TOTAL_REQUESTS}")
    print(f"Concurrency:    {CONCURRENCY}")
    print(f"Duration:       {duration:.2f} sec")
    print(f"HTTP successful:{successful}")
    print(f"Inserted spans: {inserted}")
    print(f"Duplicates:     {duplicates}")
    print(f"Errors:         {len(errors)}")
    print(f"Requests/sec:   {rps:.2f}")
    print(f"Inserted/sec:   {insert_rps:.2f}")

    if latencies:
        print(f"Average:        {statistics.mean(latencies):.2f} ms")
        print(f"P50:            {percentile(latencies, 50):.2f} ms")
        print(f"P95:            {percentile(latencies, 95):.2f} ms")
        print(f"P99:            {percentile(latencies, 99):.2f} ms")
        print(f"Max:            {max(latencies):.2f} ms")

    if process:
        print(f"Initial RAM:    {initial_memory:.2f} MB")
        print(f"Final RAM:      {final_memory:.2f} MB")

    print("======================================\n")

    assert not errors, errors[:10]
    assert successful == TOTAL_REQUESTS