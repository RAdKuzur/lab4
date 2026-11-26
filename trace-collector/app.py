"""HTTP-сервис сбора и запроса трассировок (Trace Collector).

Принимает спаны по OTLP-совместимому JSON-эндпоинту ``POST /v1/traces``,
маскирует PII, сохраняет в SQLite, отдаёт поиск по ``/spans``, метрики
Prometheus и журнал аудита с разграничением доступа по ролям.
"""
import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from src.ingest.json_decoder import decode_span
from src.ingest.header_parser import parse_traceparent
from src.ingest.http_middleware import (
    check_content_type,
    validate_content_length,
)
from src.storage.span_store import SpanStore
from src.query.trace_lookup import lookup
from src.policy import masking
from src.audit.audit_log import AuditLog


class JsonFormatter(logging.Formatter):
    """Форматтер логов, сериализующий запись в JSON.

    Поля: ``ts`` (время), ``level`` (уровень), ``msg`` (сообщение).
    """

    def format(self, record: logging.LogRecord) -> str:
        """Форматирует запись лога в JSON-строку.

        Args:
            record: Запись лога.

        Returns:
            JSON-строка с полями ts, level, msg.
        """
        return json.dumps({
            "ts": self.formatTime(record),
            "level": record.levelname,
            "msg": record.getMessage(),
        })


handler: logging.Handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler],
)

log: logging.Logger = logging.getLogger("collector")


DB_PATH: str = os.getenv("SPAN_DB_PATH", "/data/spans.db")
AUDIT_PATH: str = os.getenv("AUDIT_LOG_PATH", "/data/audit.jsonl")

STORE: SpanStore = SpanStore(DB_PATH)
AUDIT: AuditLog = AuditLog(AUDIT_PATH)
MASK_RULES: dict[str, Any] = masking.load_rules()

REJECTED: dict[str, Any] = {
    "total": 0,
    "by_reason": {},
}

REJECTED_LOCK: threading.Lock = threading.Lock()


def reject(reason: str) -> None:
    """Учитывает отклонённый запрос в счётчике отказов.

    Args:
        reason: Причина отказа (например, ``bad_content_type``).
    """
    with REJECTED_LOCK:
        REJECTED["total"] += 1

        REJECTED["by_reason"][reason] = (
            REJECTED["by_reason"].get(reason, 0) + 1
        )


async def retention_worker() -> None:
    """Фоновая задача периодического удаления старых спанов.

    Раз в час вызывает ``STORE.apply_retention(hot_days=7)`` в threadpool.
    Ошибки логируются и не прерывают цикл.
    """
    while True:
        await asyncio.sleep(3600)

        try:
            removed = await run_in_threadpool(
                STORE.apply_retention,
                hot_days=7,
            )

            log.info(
                f"retention: removed {removed} old spans"
            )

        except Exception as e:
            log.error(f"retention failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Управляет жизненным циклом приложения.

    При старте запускает ``retention_worker``, при остановке отменяет
    задачу и дожидается её завершения.

    Args:
        app: Экземпляр FastAPI.

    Yields:
        None на время работы приложения.
    """
    task = asyncio.create_task(retention_worker())

    log.info("Trace Collector started")

    try:
        yield

    finally:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        log.info("Trace Collector stopped")


app = FastAPI(
    title="Trace Collector",
    description="Distributed tracing collector and query API",
    version="1.0.0",
    lifespan=lifespan,
)


def error_response(code: int, message: str) -> JSONResponse:
    """Формирует JSON-ответ с ошибкой.

    Args:
        code: HTTP-код ответа.
        message: Текст ошибки.

    Returns:
        JSONResponse с телом ``{"error": message}``.
    """
    return JSONResponse(
        status_code=code,
        content={"error": message},
    )


def get_role(request: Request) -> str:
    """Извлекает роль из заголовка ``X-Role``.

    Args:
        request: Входящий HTTP-запрос.

    Returns:
        Значение заголовка ``X-Role`` или ``viewer``, если он не задан.
    """
    return request.headers.get("X-Role", "viewer")


@app.get("/healthz")
def health() -> dict[str, str]:
    """Проверка живости сервиса.

    Returns:
        ``{"status": "ok"}``.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readiness() -> dict[str, bool]:
    """Проверка готовности сервиса.

    Returns:
        ``{"ready": True}``.
    """
    return {"ready": True}


@app.post("/v1/traces")
async def ingest_trace(request: Request) -> Any:
    """Принимает и сохраняет один спан.

    Последовательно проверяет ``Content-Type``, ``Content-Length``,
    длину тела, декодирует спан, валидирует ``traceparent``, применяет
    маскирование и сохраняет в хранилище.

    Args:
        request: HTTP-запрос с JSON-телом спана.

    Returns:
        ``{"status": "ok"}`` при успешной записи,
        ``{"status": "duplicate"}`` для дубликата,
        либо JSON-ответ с ошибкой (400/415).
    """

    # Content-Type validation
    ok, msg = check_content_type(
        request.headers.get("Content-Type")
    )

    if not ok:
        reject("bad_content_type")
        return error_response(415, msg)

    ok, msg = validate_content_length(
        request.headers.get("Content-Length")
    )

    if not ok:
        reject("bad_content_length")
        return error_response(400, msg)

    try:
        length = int(
            request.headers.get("Content-Length", 0)
        )

        raw = await request.body()

        if len(raw) != length:
            reject("invalid_body_length")
            return error_response(
                400,
                "invalid body length",
            )

        span = await run_in_threadpool(
            decode_span,
            raw,
        )

    except ValueError as e:
        reject(str(e))
        return error_response(400, str(e))

    traceparent = request.headers.get("traceparent")

    if traceparent and parse_traceparent(traceparent) is None:
        reject("invalid_traceparent")
        return error_response(
            400,
            "invalid traceparent",
        )

    span = masking.apply(span, MASK_RULES)

    added = await run_in_threadpool(
        STORE.add,
        span,
    )

    if not added:
        return {"status": "duplicate"}

    return {"status": "ok"}


@app.get(
    "/metrics",
    response_class=PlainTextResponse,
)
def metrics() -> PlainTextResponse:
    """Отдаёт метрики в формате Prometheus.

    Returns:
        Текстовый ответ с ``spans_total``, ``spans_rejected_total`` и
        разбивкой отказов по причинам.
    """

    with REJECTED_LOCK:
        rejected_total = REJECTED["total"]
        rejected_by_reason = dict(
            REJECTED["by_reason"]
        )

    total_spans = STORE.count()

    lines = [
        "# TYPE spans_total counter",
        f"spans_total {total_spans}",
        "# TYPE spans_rejected_total counter",
        f"spans_rejected_total {rejected_total}",
    ]

    for reason, count in rejected_by_reason.items():
        lines.append(
            f'spans_rejected_total{{reason="{reason}"}} {count}'
        )

    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4",
    )


@app.get("/spans")
async def get_spans(
    request: Request,
    trace_id: Optional[str] = None,
    service: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 1000,
    min_duration_ms: Optional[float] = None,
) -> Any:
    """Ищет спаны по ``trace_id`` или по комбинации фильтров.

    Каждый поиск фиксируется в журнале аудита. Числовые параметры
    валидируются: ``limit < 1`` и отрицательный ``min_duration_ms``
    дают 400.

    Args:
        request: Входящий HTTP-запрос.
        trace_id: Идентификатор трассы (32 hex-символа).
        service: Имя сервиса.
        status: Статус спана.
        limit: Максимальное число возвращаемых спанов.
        min_duration_ms: Минимальная длительность в миллисекундах.

    Returns:
        Словарь ``{"spans", "total", "truncated"}`` либо ошибка 400.
    """

    if limit < 1:
        return error_response(
            400,
            "invalid numeric parameter",
        )

    if min_duration_ms is not None and min_duration_ms < 0:
        return error_response(
            400,
            "invalid numeric parameter",
        )

    min_dur_ns = (
        int(min_duration_ms * 1e6)
        if min_duration_ms is not None
        else None
    )

    role = get_role(request)

    await run_in_threadpool(
        AUDIT.append,
        user=request.headers.get("X-User", "anonymous"),
        role=role,
        op="SEARCH",
        params={
            "trace_id": trace_id,
            "service": service,
            "status": status,
            "limit": limit,
            "min_duration_ms": min_duration_ms,
        },
    )

    if trace_id:
        try:
            spans = await run_in_threadpool(
                lookup,
                STORE,
                trace_id,
            )

        except ValueError as e:
            return error_response(400, str(e))

        return {
            "spans": spans,
            "total": len(spans),
            "truncated": False,
        }
    spans, total, truncated = await run_in_threadpool(
        STORE.search,
        service=service,
        status=status,
        min_duration_ns=min_dur_ns,
        limit=limit,
    )

    return {
        "spans": spans,
        "total": total,
        "truncated": truncated,
    }


@app.get("/audit/logs")
async def audit_logs(request: Request) -> Any:
    """Возвращает журнал аудита для ролей ADMIN и AUDITOR.

    Args:
        request: Входящий HTTP-запрос.

    Returns:
        ``{"logs": [...]}`` при наличии прав, иначе ошибка 403.
    """

    role = get_role(request)

    if role not in ("ADMIN", "AUDITOR"):
        return error_response(403, "forbidden")

    logs = await run_in_threadpool(
        AUDIT.read_all
    )

    return {
        "logs": logs,
    }


if __name__ == "__main__":

    port = int(
        os.getenv("COLLECTOR_PORT", "4318")
    )

    log.info(
        f"Trace Collector listening on :{port}"
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )