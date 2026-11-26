import json
import os
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse, Response
from starlette.concurrency import run_in_threadpool

from auth import (
    can,
    make_session,
    verify_session,
    ROLE_LABELS,
)

COLLECTOR: str = os.getenv(
    "COLLECTOR_URL",
    "http://trace-collector:4318",
)

STATIC_DIR: Path = Path(__file__).parent / "static"

USERS_PATH: str = os.getenv(
    "USERS_PATH",
    "/app/config/users.json",
)

PUBLIC_PATHS: set[str] = {
    "/login.html",
    "/login.css",
    "/login.js",
}


def load_users() -> dict:
    """Загружает справочник пользователей из JSON-файла.

    Returns:
        Словарь вида ``{username: {"display_name": ..., "role": ...}}``.

    Raises:
        OSError: Если файл недоступен для чтения.
        json.JSONDecodeError: Если содержимое файла не является валидным JSON.
    """
    with open(USERS_PATH, encoding="utf-8") as f:
        return json.load(f)


USERS: dict = load_users()


app = FastAPI(
    title="Trace UI",
    description="Web interface for Trace Collector",
    version="1.0.0",
)


def get_session(request: Request) -> Optional[dict]:
    """Извлекает и проверяет сессию из cookie запроса.

    Args:
        request: Входящий HTTP-запрос.

    Returns:
        Словарь ``{"user": ..., "role": ...}`` при валидной сессии, иначе None.
    """
    token = request.cookies.get("session")
    return verify_session(token)


def json_error(code: int, message: str) -> JSONResponse:
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


def map_api(path: str) -> tuple[Optional[str], str]:
    """Сопоставляет путь UI-прокси с действием RBAC и путём коллектора.

    Args:
        path: Путь вида ``/api/spans``.

    Returns:
        Кортеж ``(action, target)``; для неизвестного пути — ``(None, "")``.
    """
    routes = {
        "/api/spans": ("spans:read", "/spans"),
        "/api/audit/logs": ("audit:read", "/audit/logs"),
        "/api/healthz": ("spans:read", "/healthz"),
        "/api/readyz": ("spans:read", "/readyz"),
    }

    return routes.get(path, (None, ""))


@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    """Аутентифицирует пользователя по имени и выдаёт cookie сессии.

    Args:
        request: Запрос с JSON-телом ``{"username": ...}``.

    Returns:
        JSONResponse с данными пользователя и установленной cookie
        ``session`` либо ошибка 400 (невалидный JSON) / 401 (неизвестный
        пользователь).
    """

    try:
        data = json.loads(await request.body() or b"{}")

        if not isinstance(data, dict):
            return json_error(400, "invalid json")

    except (json.JSONDecodeError, UnicodeDecodeError):
        return json_error(400, "invalid json")

    username = data.get("username", "")
    user = USERS.get(username)

    if not user:
        return json_error(401, "unknown user")

    token = make_session(username, user["role"])

    response = JSONResponse({
        "user": username,
        "display_name": user["display_name"],
        "role": user["role"],
        "role_label": ROLE_LABELS.get(
            user["role"],
            user["role"],
        ),
    })

    response.set_cookie(
        key="session",
        value=token,
        path="/",
        httponly=True,
        samesite="lax",
        max_age=28800,
    )

    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    """Завершает сессию, удаляя cookie ``session``.

    Returns:
        JSONResponse ``{"ok": true}`` с удалённой cookie.
    """

    response = JSONResponse({"ok": True})

    response.delete_cookie(
        key="session",
        path="/",
    )

    return response


@app.get("/api/users")
def get_users() -> dict:
    """Возвращает список пользователей с ролями и метками.

    Returns:
        Словарь ``{"users": [...]}`` с полями username, display_name,
        role и role_label.
    """

    return {
        "users": [
            {
                "username": username,
                "display_name": user["display_name"],
                "role": user["role"],
                "role_label": ROLE_LABELS.get(
                    user["role"],
                    user["role"],
                ),
            }
            for username, user in USERS.items()
        ]
    }


@app.get("/api/me")
def get_me(request: Request):
    """Возвращает данные текущего аутентифицированного пользователя.

    Args:
        request: Входящий HTTP-запрос с cookie сессии.

    Returns:
        Словарь с полями user, role, role_label либо ошибка 401.
    """

    session = get_session(request)

    if not session:
        return json_error(401, "unauthorized")

    return {
        "user": session["user"],
        "role": session["role"],
        "role_label": ROLE_LABELS.get(
            session["role"],
            session["role"],
        ),
    }


@app.get("/api/{path:path}")
async def collector_proxy(path: str, request: Request) -> Response:
    """Проксирует запрос к коллектору с проверкой прав по RBAC.

    Args:
        path: Путь после ``/api/``.
        request: Входящий HTTP-запрос.

    Returns:
        Ответ коллектора с добавленными заголовками ``X-Auth-User`` и
        ``X-Auth-Role``; ошибка 401 (нет сессии), 404 (неизвестный API),
        403 (недостаточно прав) или 502 (ошибка обращения к коллектору).
    """

    session = get_session(request)

    if not session:
        return json_error(401, "unauthorized")

    action, target = map_api("/api/" + path)

    if action is None:
        return json_error(404, "unknown api")

    if not can(session["role"], action):
        return json_error(
            403,
            f"role {session['role']} cannot {action}",
        )

    url = COLLECTOR + target

    if request.url.query:
        url += "?" + request.url.query

    headers = {
        "X-User": session["user"],
        "X-Role": session["role"],
    }

    try:
        response = await run_in_threadpool(
            requests.get,
            url,
            timeout=15,
            headers=headers,
        )

        return Response(
            content=response.content,
            status_code=response.status_code,
            headers={
                "Content-Type": response.headers.get(
                    "Content-Type",
                    "application/json",
                ),
                "X-Auth-User": session["user"],
                "X-Auth-Role": session["role"],
            },
        )

    except requests.RequestException as e:
        return json_error(502, str(e))


@app.get("/")
@app.get("/{path:path}")
def static_files(path: str, request: Request) -> Response:
    """Отдаёт статические файлы UI с проверкой аутентификации.

    Публичные пути из ``PUBLIC_PATHS`` доступны без сессии; для остальных
    при отсутствии сессии выполняется редирект на ``/login.html``.
    Пустой путь трактуется как ``index.html``. Выход за пределы
    ``STATIC_DIR`` запрещён.

    Args:
        path: Запрошенный путь относительно каталога статики.
        request: Входящий HTTP-запрос.

    Returns:
        FileResponse с файлом, RedirectResponse на страницу входа или
        ошибка 404.
    """

    requested_path = "/" + path

    if requested_path not in PUBLIC_PATHS:

        session = get_session(request)

        if not session:
            return RedirectResponse(
                url="/login.html",
                status_code=302,
            )

    if not path:
        path = "index.html"

    file_path = (STATIC_DIR / path).resolve()

    try:
        file_path.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return json_error(404, "not found")

    if not file_path.is_file():
        return json_error(404, "not found")

    return FileResponse(file_path)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("UI_PORT", "8080"))

    print(f"UI listening on :{port}", flush=True)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )