"""Хранилище спанов на SQLite.

Схема содержит таблицу ``span`` с составным первичным ключом
``(trace_id, span_id)`` и индексами по трассе, сервису и статусу.
Атрибуты, события и ссылки хранятся как JSON-строки. Все операции
доступа к базе сериализуются блокировкой, поскольку соединение
открыто с ``check_same_thread=False``.
"""
import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS span (
    trace_id       TEXT NOT NULL,
    span_id        TEXT NOT NULL,
    parent_span_id TEXT,
    service_name   TEXT NOT NULL,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    start_time_ns  INTEGER NOT NULL,
    end_time_ns    INTEGER NOT NULL,
    status         TEXT NOT NULL,
    attributes     TEXT,
    events         TEXT,
    links          TEXT,
    received_at    INTEGER NOT NULL,
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_span_trace   ON span(trace_id);
CREATE INDEX IF NOT EXISTS idx_span_service ON span(service_name, start_time_ns);
CREATE INDEX IF NOT EXISTS idx_span_status  ON span(status, start_time_ns);
"""


class SpanStore:
    """Хранилище спанов на SQLite с потокобезопасным доступом.

    Attributes:
        _lock: Блокировка для сериализации операций с базой.
        _db: Соединение SQLite с фабрикой строк ``sqlite3.Row``.
    """

    def __init__(self, path: str) -> None:
        """Открывает соединение с базой и создаёт схему.

        Создаёт каталог для файла базы, если он отсутствует, открывает
        соединение с ``check_same_thread=False`` и применяет ``SCHEMA``.

        Args:
            path: Путь к файлу базы данных SQLite.
        """
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def add(self, span: dict[str, Any]) -> bool:
        """Добавляет спан в хранилище.

        Args:
            span: Словарь спана с обязательными полями trace_id, span_id,
                service_name, name, kind, start_time_unix_nano,
                end_time_unix_nano, status; необязательные — parent_span_id,
                attributes, events, links.

        Returns:
            True при успешной вставке, False при нарушении первичного
            ключа (дубликат ``trace_id`` + ``span_id``).
        """
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO span VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        span["trace_id"], span["span_id"], span.get("parent_span_id"),
                        span["service_name"], span["name"], span["kind"],
                        span["start_time_unix_nano"], span["end_time_unix_nano"],
                        span["status"],
                        json.dumps(span.get("attributes", {})),
                        json.dumps(span.get("events", [])),
                        json.dumps(span.get("links", [])),
                        int(time.time()),
                    ),
                )
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def search(
        self,
        trace_id: Optional[str] = None,
        service: Optional[str] = None,
        status: Optional[str] = None,
        min_duration_ns: Optional[int] = None,
        limit: int = 1000,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Ищет спаны по фильтрам с ограничением числа результатов.

        Args:
            trace_id: Фильтр по идентификатору трассы.
            service: Фильтр по имени сервиса.
            status: Фильтр по статусу.
            min_duration_ns: Минимальная длительность в наносекундах.
            limit: Максимальное число возвращаемых спанов.

        Returns:
            Кортеж ``(spans, total, truncated)``: найденные спаны,
            общее число подходящих записей и признак усечения
            (``total > limit``).
        """
        where, args = ["1=1"], []
        if trace_id:
            where.append("trace_id = ?"); args.append(trace_id)
        if service:
            where.append("service_name = ?"); args.append(service)
        if status:
            where.append("status = ?"); args.append(status)
        if min_duration_ns is not None:
            where.append("(end_time_ns - start_time_ns) >= ?"); args.append(min_duration_ns)

        w = " AND ".join(where)
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) FROM span WHERE {w}", args).fetchone()[0]
            rows = self._db.execute(
                f"SELECT * FROM span WHERE {w} ORDER BY start_time_ns ASC LIMIT ?",
                args + [limit],
            ).fetchall()
        return [self._row(r) for r in rows], total, total > limit

    def all(self, limit: int = 1000) -> tuple[list[dict[str, Any]], int, bool]:
        """Возвращает спаны без фильтров.

        Args:
            limit: Максимальное число возвращаемых спанов.

        Returns:
            Кортеж ``(spans, total, truncated)``, как в ``search``.
        """
        return self.search(limit=limit)

    def count(self) -> int:
        """Возвращает общее число спанов в хранилище.

        Returns:
            Количество записей в таблице ``span``.
        """
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM span").fetchone()[0]

    def apply_retention(self, hot_days: int = 7) -> int:
        """Удаляет спаны старше заданного числа дней.

        Args:
            hot_days: Глубина хранения в днях.

        Returns:
            Число удалённых записей.
        """
        cutoff = int((time.time() - hot_days * 86400) * 1e9)
        with self._lock:
            cur = self._db.execute("DELETE FROM span WHERE start_time_ns < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        """Преобразует строку SQLite в словарь спана.

        Args:
            r: Строка результата запроса.

        Returns:
            Словарь с полями спана; JSON-поля ``attributes``, ``events``
            и ``links`` десериализуются, пустые значения заменяются на
            ``{}`` и ``[]`` соответственно.
        """
        return {
            "trace_id": r["trace_id"],
            "span_id": r["span_id"],
            "parent_span_id": r["parent_span_id"],
            "service_name": r["service_name"],
            "name": r["name"],
            "kind": r["kind"],
            "start_time_unix_nano": r["start_time_ns"],
            "end_time_unix_nano": r["end_time_ns"],
            "status": r["status"],
            "attributes": json.loads(r["attributes"] or "{}"),
            "events": json.loads(r["events"] or "[]"),
            "links": json.loads(r["links"] or "[]"),
        }