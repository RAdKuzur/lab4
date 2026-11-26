"""Модульные тесты подсистемы аутентификации и авторизации ``auth``.

Проверяют полноту матрицы ролей, логику проверки прав, выпуск и проверку
подписанных сессий (включая обнаружение подмены и мусорных токенов),
разбор cookie и наличие человекочитаемых меток для всех ролей.
"""
import pytest

from auth import (
    can,
    make_session,
    verify_session,
    parse_cookie,
    ROLE_MATRIX,
    ROLE_LABELS,
)


def test_role_matrix_complete() -> None:
    """Проверяет, что матрица ролей полна и у каждой роли есть ``spans:read``.

    Ожидается ровно четыре роли: SRE, DEV, ADMIN, AUDITOR, и каждая из
    них обязана иметь право чтения спанов.
    """
    assert set(ROLE_MATRIX.keys()) == {"SRE", "DEV", "ADMIN", "AUDITOR"}

    for role, actions in ROLE_MATRIX.items():
        assert "spans:read" in actions, f"{role} must read spans"


@pytest.mark.parametrize("role,action,expected", [
    ("SRE", "spans:read", True),
    ("SRE", "audit:read", False),
    ("SRE", "pii:read", True),
    ("SRE", "policies:write", False),

    ("DEV", "spans:read", True),
    ("DEV", "audit:read", False),
    ("DEV", "pii:read", False),

    ("ADMIN", "spans:read", True),
    ("ADMIN", "audit:read", True),
    ("ADMIN", "policies:write", True),
    ("ADMIN", "pii:read", True),

    ("AUDITOR", "spans:read", True),
    ("AUDITOR", "audit:read", True),
    ("AUDITOR", "policies:write", False),
    ("AUDITOR", "pii:read", True),
])
def test_can(role: str, action: str, expected: bool) -> None:
    """Проверяет результат ``can`` для 15 пар «роль–действие».

    Args:
        role: Имя роли.
        action: Проверяемое действие.
        expected: Ожидаемый результат проверки прав.
    """
    assert can(role, action) is expected


def test_can_unknown_role() -> None:
    """Проверяет, что неизвестная роль не получает никаких прав."""
    assert can("HACKER", "spans:read") is False


def test_make_and_verify() -> None:
    """Проверяет выпуск сессии и её успешную верификацию.

    Токен должен содержать разделитель, а ``verify_session`` — вернуть
    исходные ``user`` и ``role``.
    """
    token = make_session("ivanov_sre", "SRE")

    assert "." in token

    data = verify_session(token)

    assert data == {
        "user": "ivanov_sre",
        "role": "SRE",
    }


def test_verify_tampered() -> None:
    """Проверяет обнаружение подмены payload по несовпадению подписи."""
    token = make_session("ivanov_sre", "SRE")
    payload, sig = token.split(".")

    bad = payload[::-1] + "." + sig

    assert verify_session(bad) is None


def test_verify_garbage() -> None:
    """Проверяет устойчивость ``verify_session`` к мусорным токенам.

    Ожидается ``None`` для ``None``, пустой строки, строки без точки
    и строки с некорректным base64.
    """
    assert verify_session(None) is None
    assert verify_session("") is None
    assert verify_session("no-dot") is None
    assert verify_session("aa.bb") is None


def test_parse_cookie() -> None:
    """Проверяет разбор заголовка Cookie в словарь.

    Пустой или отсутствующий заголовок даёт пустой словарь; пары
    разделяются точкой с запятой; атрибуты cookie (например, ``Path``)
    попадают в результат как обычные ключи.
    """
    assert parse_cookie(None) == {}
    assert parse_cookie("") == {}

    assert parse_cookie("a=1; b=2") == {
        "a": "1",
        "b": "2",
    }

    assert parse_cookie("session=abc; Path=/") == {
        "session": "abc",
        "Path": "/",
    }


def test_role_labels_cover_all() -> None:
    """Проверяет наличие человекочитаемой метки для каждой роли."""
    for role in ROLE_MATRIX:
        assert role in ROLE_LABELS