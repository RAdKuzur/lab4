import os
from pathlib import Path
from typing import Any

import yaml

BASE_DIR: Path = Path(__file__).resolve().parent

os.environ["USERS_PATH"] = str(
    BASE_DIR / "config" / "users.json"
)

from app import app

openapi_schema: dict[str, Any] = app.openapi()

output_path: Path = BASE_DIR / "api_spec.yaml"

with open(output_path, "w", encoding="utf-8") as file:
    yaml.safe_dump(
        openapi_schema,
        file,
        allow_unicode=True,
        sort_keys=False,
    )

print(f"OpenAPI успешно сохранён: {output_path}")