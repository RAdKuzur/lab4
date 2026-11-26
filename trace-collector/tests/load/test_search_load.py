import asyncio
import os
import statistics
import time

import httpx
import pytest


pytestmark = pytest.mark.load

BASE_URL = os.getenv("COLLECTOR_URL", "http://trace-collector:4318")
TRACE_ID = os.getenv("LOAD_TRACE_ID")

TOTAL_REQUESTS = int(os.getenv("LOAD_SEARCH_REQUESTS", "500"))
CONCURRENCY = int(os.getenv("LOAD_SEARCH_CONCURRENCY", "50"))


def percentile(values, percent):
    values = sorted(values)
    if not values:
        return 0

    index = round((len(values) - 1) * percent / 100)
    return values[index]


@pytest.mark.asyncio
async def test_collector_trace_lookup_load():
    if not TRACE_ID:
        pytest.skip("Set LOAD_TRACE_ID to an existing trace_id")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    latencies = []
    errors = []

    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=30
    ) as client:

        health = await client.get("/healthz")
        assert health.status_code == 200

        async def search():
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
