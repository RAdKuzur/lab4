import json
import os
from pathlib import Path

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

COLLECTOR = os.getenv(
    "COLLECTOR_URL",
    "http://trace-collector:4318",
)

STATIC_DIR = Path(__file__).parent / "static"

USERS_PATH = os.getenv(
    "USERS_PATH",
    "/app/config/users.json",
)

PUBLIC_PATHS = {
    "/login.html",
    "/login.css",
    "/login.js",
}


def load_users() -> dict:
    with open(USERS_PATH, encoding="utf-8") as f:
        return json.load(f)


USERS = load_users()


app = FastAPI(
    title="Trace UI",
    description="Web interface for Trace Collector",
    version="1.0.0",
)

def get_session(request: Request):
    token = request.cookies.get("session")
    return verify_session(token)


def json_error(code: int, message: str):
    return JSONResponse(
        status_code=code,
        content={"error": message},
    )


def map_api(path: str) -> tuple:
    routes = {
        "/api/spans": ("spans:read", "/spans"),
        "/api/audit/logs": ("audit:read", "/audit/logs"),
        "/api/healthz": ("spans:read", "/healthz"),
        "/api/readyz": ("spans:read", "/readyz"),
    }

    return routes.get(path, (None, ""))


@app.post("/api/login")
async def login(request: Request):

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
def logout():

    response = JSONResponse({"ok": True})

    response.delete_cookie(
        key="session",
        path="/",
    )

    return response

@app.get("/api/users")
def get_users():

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
async def collector_proxy(path: str, request: Request):

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
def static_files(path: str, request: Request):

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
