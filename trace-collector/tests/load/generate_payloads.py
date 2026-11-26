import json
import uuid
from pathlib import Path

OUTPUT = Path(__file__).parent / "payloads.jsonl"

TOTAL = 10_000


def generate_span(index: int) -> dict:
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
