import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional


SECRET: bytes = os.getenv("SESSION_SECRET", "dev-secret-change-me").encode()
SESSION_TTL: int = 8 * 3600


ROLE_MATRIX: dict[str, set[str]] = {
    "SRE": {
        "spans:read",
        "pii:read",
    },
    "DEV": {
        "spans:read",
    },
    "ADMIN": {
        "spans:read",
        "audit:read",
        "policies:write",
        "pii:read",
    },
    "AUDITOR": {
        "spans:read",
        "audit:read",
        "pii:read",
    },
}
ROLE_LABELS: dict[str, str] = {
    "SRE": "SRE",
    "DEV": "Разработчик",
    "ADMIN": "Администратор",
    "AUDITOR": "Аудитор безопасности",
}


def can(role: str, action: str) -> bool:
    """Проверяет, разрешено ли роли выполнение действия.

    Args:
        role: Имя роли (SRE, DEV, ADMIN, AUDITOR).
        action: Действие в формате ``домен:операция``.

    Returns:
        True, если действие входит в набор прав роли, иначе False.
        Для неизвестной роли возвращается False.
    """
    return action in ROLE_MATRIX.get(role, set())


def _b64e(data: bytes) -> str:
    """Кодирует байты в base64url без выравнивающих символов.

    Args:
        data: Исходные байты.

    Returns:
        Строка в base64url без символов ``=``.
    """
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(data: str) -> bytes:
    """Декодирует строку base64url с восстановлением выравнивания.

    Args:
        data: Строка в base64url без символов ``=``.

    Returns:
        Декодированные байты.
    """
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def make_session(username: str, role: str) -> str:
    """Создаёт подписанный токен сессии.

    Args:
        username: Имя пользователя.
        role: Роль пользователя.

    Returns:
        Токен вида ``<payload>.<signature>``, где обе части в base64url.
    """
    payload = json.dumps({
        "user": username, "role": role, "exp": int(time.time()) + SESSION_TTL,
    }).encode()
    sig = hmac.new(SECRET, payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify_session(token: Optional[str]) -> Optional[dict]:
    """Проверяет подпись, формат и срок действия токена сессии.

    Args:
        token: Токен, полученный от клиента, или None.

    Returns:
        Словарь ``{"user": ..., "role": ...}`` при успешной проверке,
        иначе None (пустой токен, неверный формат, несоответствие подписи,
        некорректный JSON или истёкший срок действия).
    """
    if not token or "." not in token:
        return None
    try:
        p_b64, s_b64 = token.split(".", 1)
        payload = _b64d(p_b64)
        sig = _b64d(s_b64)
    except Exception:
        return None
    expected = hmac.new(SECRET, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return {"user": data["user"], "role": data["role"]}


def parse_cookie(header: Optional[str]) -> dict[str, str]:
    """Разбирает строку заголовка Cookie в словарь.

    Args:
        header: Строка заголовка Cookie или None.

    Returns:
        Словарь пар имя→значение; пустой словарь, если заголовок пуст.
        Пары без символа ``=`` игнорируются.
    """
    if not header:
        return {}
    out = {}
    for part in header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out