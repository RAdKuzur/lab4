"""Журнал аудита с защитой от модификации через хеш-цепочку.

Каждая запись содержит хеш SHA-256 от хеша предыдущей записи и
собственного содержимого, что позволяет обнаружить изменение или
удаление записи в середине журнала. Журнал хранится в JSONL-файле,
дописывание защищено блокировкой.
"""
import hashlib
import json
import os
import threading
import time
from typing import Any


class AuditLog:
    """Журнал аудита с хеш-цепочкой и потокобезопасной записью.

    Attributes:
        _path: Путь к JSONL-файлу журнала.
        _lock: Блокировка для сериализации операций записи.
        _last_hash: Хеш последней записи (или 64 нуля для пустого журнала).
    """

    def __init__(self, path: str) -> None:
        """Инициализирует журнал и восстанавливает хеш последней записи.

        Создаёт каталог для файла, если он отсутствует, и считывает
        последнюю запись, чтобы продолжить цепочку.

        Args:
            path: Путь к JSONL-файлу журнала.
        """
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._last_hash = self._load_last_hash()

    def _load_last_hash(self) -> str:
        """Считывает хеш последней записи журнала.

        Returns:
            Хеш последней записи или строка из 64 нулей, если файл
            отсутствует, пуст или не содержит поля ``hash``.
        """
        if not os.path.exists(self._path):
            return "0" * 64
        with open(self._path, encoding="utf-8") as f:
            last = ""
            for line in f:
                last = line.strip()
        if not last:
            return "0" * 64
        return json.loads(last).get("hash", "0" * 64)

    def append(
        self,
        user: str,
        role: str,
        op: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Добавляет запись в журнал и возвращает её с хешем.

        Хеш вычисляется как SHA-256 от конкатенации хеша предыдущей
        записи и JSON-представления текущей записи с сортировкой ключей.
        Запись в файл выполняется под блокировкой.

        Args:
            user: Имя пользователя, выполнившего операцию.
            role: Роль пользователя.
            op: Код операции (например, ``SEARCH``).
            params: Параметры операции.

        Returns:
            Словарь записи с полями ts, user, role, op, params, hash.
        """
        entry = {
            "ts": time.time(),
            "user": user,
            "role": role,
            "op": op,
            "params": params,
        }
        payload = json.dumps(entry, sort_keys=True)
        h = hashlib.sha256((self._last_hash + payload).encode()).hexdigest()
        entry["hash"] = h
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            self._last_hash = h
        return entry

    def read_all(self) -> list[dict[str, Any]]:
        """Читает все записи журнала.

        Returns:
            Список записей в порядке добавления; пустой список, если
            файл отсутствует.
        """
        if not os.path.exists(self._path):
            return []
        with open(self._path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]