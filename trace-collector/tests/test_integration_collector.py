"""Интеграционные и сквозные тесты коллектора и UI.

Поднимает приложения коллектора и UI через ``TestClient`` с изоляцией
окружения через ``tmp_path_factory`` и переменные окружения, проверяет
приём, маскирование, поиск, метрики, RBAC и проксирование запросов
между UI и коллектором.
"""
import copy
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from requests import Response


TEST_DIR: Path = Path(__file__).resolve().parent
COLLECTOR_DIR: Path = TEST_DIR.parent
_UI_CANDIDATES: tuple[Optional[Path], ...] = (
    Path(os.getenv("TRACE_UI_DIR", "")) if os.getenv("TRACE_UI_DIR") else None,
    COLLECTOR_DIR / "trace-ui",
    COLLECTOR_DIR.parent / "trace-ui",
    Path("/trace-ui"),
)
UI_DIR: Optional[Path] = next(
    (path.resolve() for path in _UI_CANDIDATES if path and (path / "app.py").is_file()),
    None,
)


def load_module(module_name: str, file_path: Path) -> Any:
    """Загружает модуль из файла по указанному пути.

    Args:
        module_name: Имя, под которым модуль регистрируется в ``sys.modules``.
        file_path: Путь к файлу модуля.

    Returns:
        Загруженный модуль.

    Raises:
        ImportError: Если для пути не удалось создать спецификацию
            или загрузчик.
    """
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def collector_app(tmp_path_factory) -> Any:
    """Поднимает приложение коллектора с изолированным окружением.

    Создаёт временные пути для базы, журнала аудита и правил маскирования,
    выставляет переменные окружения, импортирует ``app.py`` и по
    завершении восстанавливает окружение и ``sys.path``.

    Args:
        tmp_path_factory: Фабрика временных каталогов pytest.

    Yields:
        Модуль приложения коллектора.
    """
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
def collector_client(collector_app) -> Any:
    """Создаёт ``TestClient`` для приложения коллектора.

    Args:
        collector_app: Модуль приложения коллектора.

    Yields:
        Клиент ``TestClient`` с запущенным приложением.
    """
    with TestClient(collector_app.app) as client:
        yield client


@pytest.fixture(scope="module")
def ui_app(tmp_path_factory) -> Any:
    """Поднимает приложение UI с изолированным окружением.

    Пропускает тест, если каталог ``trace-ui`` не найден. Создаёт
    временный ``users.json``, выставляет переменные окружения, подменяет
    модуль ``auth`` и импортирует ``app.py`` UI.

    Args:
        tmp_path_factory: Фабрика временных каталогов pytest.

    Yields:
        Модуль приложения UI.

    Raises:
        pytest.skip.Exception: Если ``UI_DIR`` не найден.
    """
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
def ui_client(ui_app, collector_client, monkeypatch) -> Any:
    """Создаёт ``TestClient`` UI с подменённым HTTP-клиентом коллектора.

    Функция ``requests.get`` в модуле UI заменяется на прокси, который
    перенаправляет запросы в ``collector_client``, чтобы не обращаться
    к реальному сетевому адресу.

    Args:
        ui_app: Модуль приложения UI.
        collector_client: Клиент коллектора.
        monkeypatch: Фикстура pytest для подмены атрибутов.

    Yields:
        Клиент ``TestClient`` UI.
    """
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
def valid_span() -> dict[str, Any]:
    """Возвращает валидный спан со случайными идентификаторами.

    Returns:
        Словарь спана с PII-атрибутами ``user.email`` и ``secret``,
        подлежащими маскированию и удалению.
    """
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


def login(client, username: str) -> Any:
    """Выполняет вход в UI под указанным пользователем.

    Args:
        client: Клиент ``TestClient`` UI.
        username: Имя пользователя.

    Returns:
        Ответ на запрос входа с установленной cookie ``session``.
    """
    response = client.post("/api/login", json={"username": username})
    assert response.status_code == 200, response.text
    assert "session" in response.cookies
    return response


def test_collector_healthz(collector_client) -> None:
    """Проверяет ``/healthz`` коллектора.

    Args:
        collector_client: Клиент коллектора.
    """
    response = collector_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_collector_readyz(collector_client) -> None:
    """Проверяет ``/readyz`` коллектора.

    Args:
        collector_client: Клиент коллектора.
    """
    response = collector_client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ready": True}


def test_collector_ingest_span(collector_client, valid_span) -> None:
    """Проверяет приём валидного спана с корректным ``traceparent``.

    Args:
        collector_client: Клиент коллектора.
        valid_span: Валидный спан.
    """
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


def test_collector_masks_sensitive_attributes(collector_client, valid_span) -> None:
    """Проверяет маскирование и удаление PII при приёме и чтении.

    Args:
        collector_client: Клиент коллектора.
        valid_span: Валидный спан с PII-атрибутами.
    """
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


def test_collector_rejects_duplicate_span(collector_client, valid_span) -> None:
    """Проверяет идемпотентность приёма: повторный спан даёт ``duplicate``.

    Args:
        collector_client: Клиент коллектора.
        valid_span: Валидный спан.
    """
    first = collector_client.post("/v1/traces", json=valid_span)
    second = collector_client.post("/v1/traces", json=copy.deepcopy(valid_span))

    assert first.status_code == 200
    assert first.json()["status"] == "ok"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_collector_rejects_invalid_content_type(collector_client) -> None:
    """Проверяет отклонение запроса с недопустимым ``Content-Type``.

    Args:
        collector_client: Клиент коллектора.
    """
    response = collector_client.post(
        "/v1/traces",
        content=b'{"trace_id": "invalid"}',
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415


def test_collector_rejects_invalid_trace_id(collector_client, valid_span) -> None:
    """Проверяет отклонение спана с невалидным ``trace_id`` при приёме.

    Args:
        collector_client: Клиент коллектора.
        valid_span: Валидный спан (идентификатор подменяется на невалидный).
    """
    valid_span["trace_id"] = "g" * 32
    response = collector_client.post("/v1/traces", json=valid_span)
    assert response.status_code == 400


def test_collector_rejects_invalid_traceparent(collector_client, valid_span) -> None:
    """Проверяет отклонение запроса с невалидным ``traceparent``.

    Args:
        collector_client: Клиент коллектора.
        valid_span: Валидный спан.
    """
    response = collector_client.post(
        "/v1/traces",
        json=valid_span,
        headers={"traceparent": "invalid-traceparent"},
    )
    assert response.status_code == 400


def test_collector_search_by_trace_id(collector_client, valid_span) -> None:
    """Проверяет поиск по ``trace_id`` после приёма спана.

    Args:
        collector_client: Клиент коллектора.
        valid_span: Валидный спан.
    """
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


def test_collector_rejects_invalid_trace_id_in_search(collector_client) -> None:
    """Проверяет отклонение поиска с невалидным ``trace_id``.

    Args:
        collector_client: Клиент коллектора.
    """
    response = collector_client.get("/spans", params={"trace_id": "g" * 32})
    assert response.status_code == 400


def test_collector_rejects_invalid_search_limit(collector_client) -> None:
    """Проверяет отклонение поиска с ``limit < 1``.

    Args:
        collector_client: Клиент коллектора.
    """
    response = collector_client.get("/spans", params={"limit": 0})
    assert response.status_code == 400


def test_collector_metrics(collector_client) -> None:
    """Проверяет наличие ключевых метрик в ``/metrics``.

    Args:
        collector_client: Клиент коллектора.
    """
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
    role: str,
    expected_status: int,
) -> None:
    """Проверяет RBAC на ``/audit/logs`` коллектора по ролям.

    Args:
        collector_client: Клиент коллектора.
        role: Роль из заголовка ``X-Role``.
        expected_status: Ожидаемый HTTP-код.
    """
    response = collector_client.get(
        "/audit/logs",
        headers={"X-Role": role},
    )
    assert response.status_code == expected_status


def test_ui_login_success(ui_client) -> None:
    """Проверяет успешный вход в UI.

    Args:
        ui_client: Клиент UI.
    """
    response = login(ui_client, "sre")
    assert response.json()["user"] == "sre"
    assert response.json()["role"] == "SRE"


def test_ui_login_unknown_user(ui_client) -> None:
    """Проверяет отказ при входе неизвестного пользователя.

    Args:
        ui_client: Клиент UI.
    """
    response = ui_client.post("/api/login", json={"username": "unknown"})
    assert response.status_code == 401


def test_ui_login_invalid_json(ui_client) -> None:
    """Проверяет отклонение входа с невалидным JSON.

    Args:
        ui_client: Клиент UI.
    """
    response = ui_client.post(
        "/api/login",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_ui_me_requires_authentication(ui_client) -> None:
    """Проверяет, что ``/api/me`` требует аутентификации.

    Args:
        ui_client: Клиент UI.
    """
    response = ui_client.get("/api/me")
    assert response.status_code == 401


def test_ui_me_returns_current_user(ui_client) -> None:
    """Проверяет, что ``/api/me`` возвращает текущего пользователя.

    Args:
        ui_client: Клиент UI.
    """
    login(ui_client, "sre")
    response = ui_client.get("/api/me")

    assert response.status_code == 200
    assert response.json()["user"] == "sre"
    assert response.json()["role"] == "SRE"


def test_ui_logout(ui_client) -> None:
    """Проверяет выход из сессии и последующий отказ ``/api/me``.

    Args:
        ui_client: Клиент UI.
    """
    login(ui_client, "sre")

    response = ui_client.post("/api/logout")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    response = ui_client.get("/api/me")
    assert response.status_code == 401


def test_ui_proxy_healthz(ui_client) -> None:
    """Проверяет проксирование ``/api/healthz`` в коллектор.

    Args:
        ui_client: Клиент UI.
    """
    login(ui_client, "sre")

    response = ui_client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ui_proxy_spans(ui_client, collector_client, valid_span) -> None:
    """Проверяет проксирование поиска спанов с пробросом заголовков роли.

    Args:
        ui_client: Клиент UI.
        collector_client: Клиент коллектора.
        valid_span: Валидный спан.
    """
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
def test_ui_audit_permissions(
    ui_client,
    username: str,
    expected_status: int,
) -> None:
    """Проверяет RBAC на ``/api/audit/logs`` UI по пользователям.

    Args:
        ui_client: Клиент UI.
        username: Имя пользователя для входа.
        expected_status: Ожидаемый HTTP-код.
    """
    login(ui_client, username)

    response = ui_client.get("/api/audit/logs")
    assert response.status_code == expected_status


def test_ui_proxy_requires_authentication(ui_client) -> None:
    """Проверяет, что прокси требует аутентификации.

    Args:
        ui_client: Клиент UI.
    """
    response = ui_client.get("/api/spans")
    assert response.status_code == 401


def test_ui_proxy_rejects_unknown_api(ui_client) -> None:
    """Проверяет отказ прокси для неизвестного API-пути.

    Args:
        ui_client: Клиент UI.
    """
    login(ui_client, "admin")

    response = ui_client.get("/api/unknown")
    assert response.status_code == 404


def test_ui_public_login_page(ui_client) -> None:
    """Проверяет доступность страницы входа без сессии.

    Args:
        ui_client: Клиент UI.
    """
    response = ui_client.get("/login.html")
    assert response.status_code == 200


def test_ui_root_redirects_unauthenticated_user(ui_client) -> None:
    """Проверяет редирект неаутентифицированного пользователя с ``/``.

    Args:
        ui_client: Клиент UI.
    """
    response = ui_client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login.html"