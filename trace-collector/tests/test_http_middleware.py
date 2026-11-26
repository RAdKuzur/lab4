"""Тесты проверок HTTP-заголовков приёма спанов.

Проверяют допустимые и недопустимые значения ``Content-Type``, а также
валидацию ``Content-Length`` с граничными значениями вокруг лимита
1 МиБ.
"""
from typing import Optional

import pytest

from src.ingest.http_middleware import check_content_type, validate_content_length


@pytest.mark.parametrize("header", [
    "application/json",
    "application/json; charset=utf-8",
    "application/json;charset=utf-8",
    "APPLICATION/JSON",
    "application/json; boundary=xxx",
])
def test_content_type_ok(header: str) -> None:
    """Проверяет допустимые значения ``Content-Type``.

    Все варианты ``application/json`` (с параметрами, без пробела после
    ``;``, в верхнем регистре, с ``boundary``) должны приниматься без
    сообщения об ошибке.

    Args:
        header: Проверяемое значение заголовка.
    """
    ok, msg = check_content_type(header)
    assert ok and msg == ""


@pytest.mark.parametrize("header", [
    None, "", "text/plain", "application/x-protobuf", "application/xml",
])
def test_content_type_rejected(header: Optional[str]) -> None:
    """Проверяет отклонение недопустимых ``Content-Type``.

    Args:
        header: Проверяемое значение заголовка (строка или None).
    """
    ok, msg = check_content_type(header)
    assert not ok and msg


@pytest.mark.parametrize("header,expected", [
    ("100", True),
    ("1048576", True),
    ("1048577", False),
    ("0", False),
    ("-1", False),
    ("abc", False),
    (None, False),
])
def test_content_length(header: Optional[str], expected: bool) -> None:
    """Проверяет валидацию ``Content-Length`` с граничными значениями.

    Граница лимита — 1 МиБ: ``1048576`` допустимо, ``1048577`` — нет.
    Нечисловые, нулевые, отрицательные значения и отсутствие заголовка
    отклоняются.

    Args:
        header: Проверяемое значение заголовка (строка или None).
        expected: Ожидаемый результат проверки.
    """
    ok, _ = validate_content_length(header)
    assert ok == expected