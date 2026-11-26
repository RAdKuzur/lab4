
import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager

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
    def format(self, record):
        return json.dumps({
            "ts": self.formatTime(record),
            "level": record.levelname,
            "msg": record.getMessage(),
        })


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler],
)

log = logging.getLogger("collector")


DB_PATH = os.getenv("SPAN_DB_PATH", "/data/spans.db")
AUDIT_PATH = os.getenv("AUDIT_LOG_PATH", "/data/audit.jsonl")

STORE = SpanStore(DB_PATH)
AUDIT = AuditLog(AUDIT_PATH)
MASK_RULES = masking.load_rules()

REJECTED = {
    "total": 0,
    "by_reason": {},
}

REJECTED_LOCK = threading.Lock()


def reject(reason: str) -> None:
    with REJECTED_LOCK:
        REJECTED["total"] += 1

        REJECTED["by_reason"][reason] = (
            REJECTED["by_reason"].get(reason, 0) + 1
        )

async def retention_worker():
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
async def lifespan(app: FastAPI):
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

def error_response(code: int, message: str):
    return JSONResponse(
        status_code=code,
        content={"error": message},
    )


def get_role(request: Request) -> str:
    return request.headers.get("X-Role", "viewer")

@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/readyz")
def readiness():
    return {"ready": True}

@app.post("/v1/traces")
async def ingest_trace(request: Request):

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
def metrics():

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
    trace_id: str | None = None,
    service: str | None = None,
    status: str | None = None,
    limit: int = 1000,
    min_duration_ms: float | None = None,
):

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
async def audit_logs(request: Request):

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
