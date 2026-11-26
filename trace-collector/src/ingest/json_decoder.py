"""Декодирование и валидация спанов, пришедших в JSON.

Проверяет обязательные поля и формат идентификаторов, подставляет
значения по умолчанию для необязательных полей и нормализует
недопустимый статус в ``UNSET``.
"""
import json
from typing import Any

REQUIRED: tuple[str, ...] = ("trace_id", "span_id", "name", "service_name")

DEFAULTS: dict[str, Any] = {
    "parent_span_id": None,
    "kind": "INTERNAL",
    "status": "UNSET",
    "attributes": {},
    "events": [],
    "links": [],
    "start_time_unix_nano": 0,
    "end_time_unix_nano": 0,
}


def decode_span(raw: bytes) -> dict[str, Any]:
    """Декодирует и валидирует спан из JSON-байтов.

    Проверяет, что вход является JSON-объектом, содержит все поля из
    ``REQUIRED`` и что ``trace_id``/``span_id`` состоят из нужного числа
    hex-символов. Недостающие необязательные поля заполняются из
    ``DEFAULTS``; недопустимый ``status`` приводится к ``UNSET``.

    Args:
        raw: Тело запроса в виде байтов.

    Returns:
        Словарь спана с заполненными полями и нормализованным статусом.

    Raises:
        ValueError: Если JSON невалиден, корень не является объектом,
            отсутствует обязательное поле или идентификатор имеет
            неверную длину либо не-hex символы.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid json: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("span must be an object")

    for key in REQUIRED:
        if key not in data:
            raise ValueError(f"missing field: {key}")

    tid, sid = data["trace_id"], data["span_id"]
    if len(tid) != 32 or not all(c in "0123456789abcdef" for c in tid):
        raise ValueError("trace_id must be 32 hex chars")
    if len(sid) != 16 or not all(c in "0123456789abcdef" for c in sid):
        raise ValueError("span_id must be 16 hex chars")

    for key, default in DEFAULTS.items():
        data.setdefault(key, default)

    if data["status"] not in ("OK", "ERROR", "UNSET"):
        data["status"] = "UNSET"

    return data