"""Политика маскирования чувствительных атрибутов спана.

Загружает правила из JSON-файла (с дефолтами при отсутствии файла) и
применяет их к атрибутам спана: часть ключей удаляется, часть
заменяется фиксированным значением. Исходный спан не мутируется.
"""
import json
import os
from typing import Any, Dict

DEFAULT_RULES: Dict[str, Any] = {
    "drop_keys": [],
    "mask_keys": [],
    "mask_value": "***",
}


def load_rules(path: str | None = None) -> Dict[str, Any]:
    """Загружает правила маскирования из JSON-файла.

    Путь берётся из аргумента, а при его отсутствии — из переменной
    окружения ``MASKING_RULES_PATH`` (по умолчанию
    ``/app/config/masking_rules.json``). Если файл не существует,
    возвращаются ``DEFAULT_RULES``; иначе содержимое файла накладывается
    поверх дефолтов, так что недостающие ключи берутся из ``DEFAULT_RULES``.

    Args:
        path: Путь к файлу правил или None.

    Returns:
        Словарь с ключами ``drop_keys``, ``mask_keys``, ``mask_value``.
    """
    path = path or os.getenv("MASKING_RULES_PATH", "/app/config/masking_rules.json")
    if not os.path.exists(path):
        return DEFAULT_RULES
    with open(path, encoding="utf-8") as f:
        return {**DEFAULT_RULES, **json.load(f)}


def apply(span: dict, rules: Dict[str, Any]) -> dict:
    """Применяет правила маскирования к атрибутам спана.

    Ключи из ``drop_keys`` удаляются, ключи из ``mask_keys`` заменяются
    на ``mask_value`` (по умолчанию ``***``). Остальные атрибуты
    сохраняются. Возвращается новый словарь спана; исходный объект и
    его ``attributes`` не изменяются.

    Args:
        span: Исходный спан.
        rules: Правила маскирования.

    Returns:
        Новый спан с преобразованными атрибутами.
    """
    attrs = dict(span.get("attributes") or {})
    for key in list(attrs.keys()):
        if key in rules.get("drop_keys", []):
            del attrs[key]
        elif key in rules.get("mask_keys", []):
            attrs[key] = rules.get("mask_value", "***")
    out = dict(span)
    out["attributes"] = attrs
    return out