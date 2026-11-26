"""Проверки HTTP-заголовков приёма спанов.

Содержит валидацию ``Content-Type`` (допускается ``application/json``
с параметрами и в любом регистре) и ``Content-Length`` (целое
положительное число в пределах лимита).
"""
from typing import Optional, Tuple


SUPPORTED_CONTENT_TYPES: set[str] = {
    "application/json",
    "application/json; charset=utf-8",
    "application/json;charset=utf-8",
}


def check_content_type(header: Optional[str]) -> Tuple[bool, str]:
    """Проверяет допустимость заголовка ``Content-Type``.

    Значение нормализуется: части разбиваются по ``;``, обрезаются и
    приводятся к нижнему регистру. Допускается точное совпадение с
    одним из поддерживаемых типов, а также любой тип, начинающийся с
    ``application/json``.

    Args:
        header: Значение заголовка ``Content-Type`` или None.

    Returns:
        Кортеж ``(ok, message)``: ``(True, "")`` при допустимом типе,
        иначе ``(False, <причина>)``.
    """
    if not header:
        return False, "missing Content-Type header"

    normalized = ";".join(part.strip().lower() for part in header.split(";"))
    if normalized in SUPPORTED_CONTENT_TYPES:
        return True, ""

    if normalized.startswith("application/json"):
        return True, ""

    return False, f"unsupported Content-Type: {header}"


def validate_content_length(
    header: Optional[str],
    max_bytes: int = 1_048_576,
) -> Tuple[bool, str]:
    """Проверяет корректность заголовка ``Content-Length``.

    Значение должно быть целым положительным числом, не превышающим
    ``max_bytes``. Отсутствие заголовка, нечисловое значение, ноль,
    отрицательное число и превышение лимита считаются ошибкой.

    Args:
        header: Значение заголовка ``Content-Length`` или None.
        max_bytes: Максимально допустимый размер тела в байтах.

    Returns:
        Кортеж ``(ok, message)``: ``(True, "")`` при корректном значении,
        иначе ``(False, <причина>)``.
    """
    if header is None:
        return False, "missing Content-Length"
    try:
        n = int(header)
    except ValueError:
        return False, "Content-Length must be integer"
    if n <= 0:
        return False, "Content-Length must be positive"
    if n > max_bytes:
        return False, f"Content-Length exceeds {max_bytes} bytes"
    return True, ""