import copy
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from requests import Response


TEST_DIR = Path(__file__).resolve().parent
COLLECTOR_DIR = TEST_DIR.parent
_UI_CANDIDATES = (
    Path(os.getenv("TRACE_UI_DIR", "")) if os.getenv("TRACE_UI_DIR") else None,
    COLLECTOR_DIR / "trace-ui",
    COLLECTOR_DIR.parent / "trace-ui",
    Path("/trace-ui"),
)
UI_DIR = next(
    (path.resolve() for path in _UI_CANDIDATES if path and (path / "app.py").is_file()),
    None,
)


def load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def collector_app(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("collector")
    rules_path = tmp_path / "rules.json"

    rules_path.write_text(
        json.dumps(
            {
                "drop_keys": ["secret"],
                "mask_keys": ["user.email"],
                "mask_value": "***",
            }
        ),
        encoding="utf-8",
    )

    env_values = {
        "SPAN_DB_PATH": str(tmp_path / "spans.db"),
        "AUDIT_LOG_PATH": str(tmp_path / "audit.jsonl"),
        "MASKING_RULES_PATH": str(rules_path),
    }
    old_env = {key: os.environ.get(key) for key in env_values}

    for key, value in env_values.items():
        os.environ[key] = value

    collector_path = str(COLLECTOR_DIR)
    sys.path.insert(0, collector_path)

    module_name = "integration_collector_app"
    module = load_module(module_name, COLLECTOR_DIR / "app.py")

    try:
        yield module
    finally:
        sys.path.remove(collector_path)
        sys.modules.pop(module_name, None)

        for key, old_value in old_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


@pytest.fixture(scope="module")
def collector_client(collector_app):
    with TestClient(collector_app.app) as client:
        yield client


@pytest.fixture(scope="module")
def ui_app(tmp_path_factory):
    if UI_DIR is None:
        pytest.skip(
            "Trace UI не найден. Укажи TRACE_UI_DIR или скопируй trace-ui "
            "в /app/trace-ui внутри тестового контейнера."
        )

    tmp_path = tmp_path_factory.mktemp("ui")
    users_path = tmp_path / "users.json"

    users_path.write_text(
        json.dumps(
            {
                "sre": {"display_name": "SRE User", "role": "SRE"},
                "developer": {"display_name": "Developer User", "role": "DEV"},
                "admin": {"display_name": "Admin User", "role": "ADMIN"},
                "auditor": {"display_name": "Auditor User", "role": "AUDITOR"},
            }
        ),
        encoding="utf-8",
    )

    env_values = {
        "USERS_PATH": str(users_path),
        "SESSION_SECRET": "integration-test-secret",
        "COLLECTOR_URL": "http://collector-test",
    }
    old_env = {key: os.environ.get(key) for key in env_values}

    for key, value in env_values.items():
        os.environ[key] = value

    ui_path = str(UI_DIR)
    sys.path.insert(0, ui_path)
    old_auth = sys.modules.pop("auth", None)
    module_name = "integration_ui_app"

    try:
        module = load_module(module_name, UI_DIR / "app.py")
        yield module
    finally:
        sys.path.remove(ui_path)
        sys.modules.pop(module_name, None)
        sys.modules.pop("auth", None)

        if old_auth is not None:
            sys.modules["auth"] = old_auth

        for key, old_value in old_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


@pytest.fixture
def ui_client(ui_app, collector_client, monkeypatch):
    def fake_collector_get(url, timeout=15, headers=None, **kwargs):
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        upstream = collector_client.get(path, headers=headers or {})
        response = Response()
        response.status_code = upstream.status_code
        response._content = upstream.content
        response.headers.update(dict(upstream.headers))
        response.url = url
        response.encoding = "utf-8"
        return response

    monkeypatch.setattr(ui_app.requests, "get", fake_collector_get)

    with TestClient(ui_app.app) as client:
        yield client


@pytest.fixture
def valid_span():
    return {
        "trace_id": uuid.uuid4().hex,
        "span_id": uuid.uuid4().hex[:16],
        "parent_span_id": None,
        "name": "test-operation",
        "service_name": "test-service",
        "kind": "SERVER",
        "start_time_unix_nano": 1_000_000_000,
        "end_time_unix_nano": 1_050_000_000,
        "status": "OK",
        "attributes": {
            "user.email": "user@example.com",
            "secret": "sensitive-value",
            "http.method": "GET",
        },
        "events": [],
        "links": [],
    }


def login(client, username):
    response = client.post("/api/login", json={"username": username})
    assert response.status_code == 200, response.text
    assert "session" in response.cookies
    return response

def test_collector_healthz(collector_client):
    response = collector_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_collector_readyz(collector_client):
    response = collector_client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ready": True}


def test_collector_ingest_span(collector_client, valid_span):
    response = collector_client.post(
        "/v1/traces",
        json=valid_span,
        headers={
            "traceparent": (
                f"00-{valid_span['trace_id']}-{valid_span['span_id']}-01"
            )
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_collector_masks_sensitive_attributes(collector_client, valid_span):
    response = collector_client.post("/v1/traces", json=valid_span)
    assert response.status_code == 200

    result = collector_client.get(
        "/spans",
        params={"trace_id": valid_span["trace_id"]},
    )
    assert result.status_code == 200

    spans = result.json()["spans"]
    assert len(spans) == 1

    attributes = spans[0]["attributes"]
    assert attributes["user.email"] == "***"
    assert "secret" not in attributes
    assert attributes["http.method"] == "GET"


def test_collector_rejects_duplicate_span(collector_client, valid_span):
    first = collector_client.post("/v1/traces", json=valid_span)
    second = collector_client.post("/v1/traces", json=copy.deepcopy(valid_span))

    assert first.status_code == 200
    assert first.json()["status"] == "ok"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_collector_rejects_invalid_content_type(collector_client):
    response = collector_client.post(
        "/v1/traces",
        content=b'{"trace_id": "invalid"}',
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415


def test_collector_rejects_invalid_trace_id(collector_client, valid_span):
    valid_span["trace_id"] = "g" * 32
    response = collector_client.post("/v1/traces", json=valid_span)
    assert response.status_code == 400


def test_collector_rejects_invalid_traceparent(collector_client, valid_span):
    response = collector_client.post(
        "/v1/traces",
        json=valid_span,
        headers={"traceparent": "invalid-traceparent"},
    )
    assert response.status_code == 400


def test_collector_search_by_trace_id(collector_client, valid_span):
    ingest = collector_client.post("/v1/traces", json=valid_span)
    assert ingest.status_code == 200

    response = collector_client.get(
        "/spans",
        params={"trace_id": valid_span["trace_id"]},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["total"] == 1
    assert data["truncated"] is False
    assert data["spans"][0]["trace_id"] == valid_span["trace_id"]


def test_collector_rejects_invalid_trace_id_in_search(collector_client):
    response = collector_client.get("/spans", params={"trace_id": "g" * 32})
    assert response.status_code == 400


def test_collector_rejects_invalid_search_limit(collector_client):
    response = collector_client.get("/spans", params={"limit": 0})
    assert response.status_code == 400


def test_collector_metrics(collector_client):
    response = collector_client.get("/metrics")
    assert response.status_code == 200
    assert "spans_total" in response.text
    assert "spans_rejected_total" in response.text


@pytest.mark.parametrize(
    "role,expected_status",
    [
        ("ADMIN", 200),
        ("AUDITOR", 200),
        ("SRE", 403),
        ("DEV", 403),
    ],
)
def test_collector_audit_permissions(
    collector_client,
    role,
    expected_status,
):
    response = collector_client.get(
        "/audit/logs",
        headers={"X-Role": role},
    )
    assert response.status_code == expected_status



def test_ui_login_success(ui_client):
    response = login(ui_client, "sre")
    assert response.json()["user"] == "sre"
    assert response.json()["role"] == "SRE"


def test_ui_login_unknown_user(ui_client):
    response = ui_client.post("/api/login", json={"username": "unknown"})
    assert response.status_code == 401


def test_ui_login_invalid_json(ui_client):
    response = ui_client.post(
        "/api/login",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_ui_me_requires_authentication(ui_client):
    response = ui_client.get("/api/me")
    assert response.status_code == 401


def test_ui_me_returns_current_user(ui_client):
    login(ui_client, "sre")
    response = ui_client.get("/api/me")

    assert response.status_code == 200
    assert response.json()["user"] == "sre"
    assert response.json()["role"] == "SRE"


def test_ui_logout(ui_client):
    login(ui_client, "sre")

    response = ui_client.post("/api/logout")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    response = ui_client.get("/api/me")
    assert response.status_code == 401


def test_ui_proxy_healthz(ui_client):
    login(ui_client, "sre")

    response = ui_client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ui_proxy_spans(ui_client, collector_client, valid_span):
    ingest = collector_client.post("/v1/traces", json=valid_span)
    assert ingest.status_code == 200

    login(ui_client, "sre")
    response = ui_client.get(
        "/api/spans",
        params={"trace_id": valid_span["trace_id"]},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.headers["X-Auth-User"] == "sre"
    assert response.headers["X-Auth-Role"] == "SRE"


@pytest.mark.parametrize(
    "username,expected_status",
    [
        ("admin", 200),
        ("auditor", 200),
        ("sre", 403),
        ("developer", 403),
    ],
)
def test_ui_audit_permissions(ui_client, username, expected_status):
    login(ui_client, username)

    response = ui_client.get("/api/audit/logs")
    assert response.status_code == expected_status


def test_ui_proxy_requires_authentication(ui_client):
    response = ui_client.get("/api/spans")
    assert response.status_code == 401


def test_ui_proxy_rejects_unknown_api(ui_client):
    login(ui_client, "admin")

    response = ui_client.get("/api/unknown")
    assert response.status_code == 404


def test_ui_public_login_page(ui_client):
    response = ui_client.get("/login.html")
    assert response.status_code == 200


def test_ui_root_redirects_unauthenticated_user(ui_client):
    response = ui_client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login.html"
