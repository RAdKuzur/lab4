"""Генерация OpenAPI-спецификации Trace Collector в формате YAML.

Скрипт получает схему через ``app.openapi()`` и сохраняет её в
``../docs/trace-collector/api_spec.yaml`` относительно текущего каталога.
"""
from typing import Any

import yaml

from app import app

openapi_schema: dict[str, Any] = app.openapi()

with open("../docs/trace-collector/api_spec.yaml", "w", encoding="utf-8") as file:
    yaml.safe_dump(
        openapi_schema,
        file,
        allow_unicode=True,
        sort_keys=False
    )

print("api_spec.yaml успешно создан")