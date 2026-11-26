import re
from src.storage.span_store import SpanStore


TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
def lookup(store: SpanStore, trace_id: str) -> list[dict]:
    if not isinstance(trace_id, str) or not TRACE_ID_RE.fullmatch(trace_id):
        raise ValueError("trace_id must be 32 hexadecimal characters")

    spans, _, _ = store.search(
        trace_id=trace_id.lower(),
        limit=100000,
    )

    return spans