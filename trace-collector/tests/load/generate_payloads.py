"""Генерация файла payloads.jsonl с тестовыми спанами для нагрузочных тестов.

Создаёт ``TOTAL`` спанов со случайными идентификаторами и записывает их
построчно в JSONL-файл рядом со скриптом.
"""
import json
import uuid
from pathlib import Path
from typing import Any

OUTPUT: Path = Path(__file__).parent / "payloads.jsonl"

TOTAL: int = 10_000


def generate_span(index: int) -> dict[str, Any]:
    """Создаёт один тестовый спан.

    Args:
        index: Порядковый номер спана, используемый в имени операции.

    Returns:
        Словарь спана сервиса ``service-b`` со случайными ``trace_id``
        и ``span_id`` и фиксированными временными метками.
    """
    return {
        "trace_id": uuid.uuid4().hex,
        "span_id": uuid.uuid4().hex[:16],
        "parent_span_id": None,
        "service_name": "service-b",
        "name": f"service-b-work-{index}",
        "kind": "SERVER",
        "start_time_unix_nano": 1795544129995150600,
        "end_time_unix_nano": 1795544130005482800,
        "status": "OK",
        "attributes": {
            "http.method": "GET",
            "http.path": "/work"
        },
        "events": [],
        "links": []
    }


with OUTPUT.open("w", encoding="utf-8") as file:
    for i in range(TOTAL):
        file.write(
            json.dumps(
                generate_span(i),
                ensure_ascii=False
            ) + "\n"
        )

print(f"Generated {TOTAL} spans: {OUTPUT}")