import asyncio
import json
import logging
import re
import secrets
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.models import ConnectorState, CreateConnectorRequest, GatewayPayload
from app.observability import configure_logging, reset_request_id, set_request_id
from app.runtime import ConnectorError, ConnectorManager
from app.settings import Settings

settings = Settings()
configure_logging(settings.service_name, settings.aip_environment)
logging.getLogger("httpx").setLevel(logging.WARNING)
manager = ConnectorManager(settings)
app = FastAPI(title="IBKR Connector", version="0.1.0")
logger = logging.getLogger(__name__)
HOP_BY_HOP_HEADERS = {
    "connection",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "http2-settings",
}
EXCLUDED_RESPONSE_HEADERS = {"content-encoding", "transfer-encoding", "connection", "content-length"}
GATEWAY_HOSTS = {"127.0.0.1:5000", "localhost:5000"}
IBKR_BROWSER_REDIRECT_HOSTS = {"api.ibkr.com", "www.interactivebrokers.com", "interactivebrokers.com"}
SERVER_SIDE_POST_REDIRECT_PATHS = {"/authenticator", "/report"}
SSO_RECOVERY_PATHS = {
    "/Authenticator": "/sso/Authenticator",
    "/Dispatcher": "/sso/Dispatcher",
}
LOGIN_CONTEXT_COOKIE = "aip_ibkr_login_context"
GATEWAY_BROWSER_IDENTITY_COOKIES = {"USERID"}
PROXY_TARGET_GATEWAY = "GATEWAY"
PROXY_TARGET_IBKR_UPSTREAM = "IBKR_UPSTREAM"
ROUTE_TYPE_LOGIN = "LOGIN"
ROUTE_TYPE_RECOVERY = "RECOVERY"
GATEWAY_LOGIN_STARTUP_ATTEMPTS = 8


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    candidate = request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-Id")
    request_id = candidate.strip() if candidate else ""
    if not request_id or len(request_id) > 120 or not all(
        character.isalnum() or character in "-_.:/" for character in request_id
    ):
        request_id = str(uuid4())
    token = set_request_id(request_id)
    try:
        response = await call_next(request)
    finally:
        reset_request_id(token)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Correlation-Id"] = request_id
    return response


def require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    if not settings.aip_connector_require_internal_token:
        return
    if not settings.aip_internal_token or x_internal_token != settings.aip_internal_token:
        raise HTTPException(status_code=403, detail="Internal connector token is required.")


def user_header(x_aip_user_id: str = Header(alias="X-AIP-User-Id")) -> UUID:
    try:
        return UUID(x_aip_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="X-AIP-User-Id must be a UUID.") from exc


@app.exception_handler(ConnectorError)
def connector_error_handler(_: Request, exc: ConnectorError) -> JSONResponse:
    status = 403 if exc.code == "CONNECTOR_FORBIDDEN" else 404 if exc.code == "CONNECTOR_NOT_FOUND" else 409
    return JSONResponse(status_code=status, content={"code": exc.code, "message": exc.message})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/actuator/health/readiness")
def readiness() -> dict[str, str]:
    return {"status": "UP"}


@app.get("/actuator/health/liveness")
def liveness() -> dict[str, str]:
    return {"status": "UP"}


@app.post("/internal/connectors", dependencies=[Depends(require_internal_token)])
async def create_connector(request: CreateConnectorRequest):
    session = await manager.create(request.connector_id, request.user_id)
    return manager.response(session)


@app.get("/internal/connectors/{connector_id}/status", dependencies=[Depends(require_internal_token)])
async def connector_status(connector_id: UUID, user_id: UUID = Depends(user_header)):
    session = await manager.status(connector_id, user_id)
    return manager.response(session)


@app.get("/internal/connectors/{connector_id}/login", dependencies=[Depends(require_internal_token)])
async def connector_login(connector_id: UUID, user_id: UUID = Depends(user_header)):
    session = await manager.status(connector_id, user_id)

    if session.auth_status == ConnectorState.SESSION_EXPIRED or (
        session.auth_status == ConnectorState.AUTHENTICATION_REQUIRED
        and session.authenticated_at is not None
    ):
        session = await manager.restart(connector_id, user_id)

    return {
        "connectorId": connector_id,
        "loginUrl": manager.login_url(session),
        "authStatus": session.auth_status,
    }


@app.post("/internal/connectors/{connector_id}/stop", dependencies=[Depends(require_internal_token)])
async def stop_connector(connector_id: UUID, user_id: UUID = Depends(user_header)):
    session = await manager.stop(connector_id, user_id)
    return manager.response(session)


@app.post("/internal/connectors/{connector_id}/restart", dependencies=[Depends(require_internal_token)])
async def restart_connector(connector_id: UUID, user_id: UUID = Depends(user_header)):
    session = await manager.restart(connector_id, user_id)
    return manager.response(session)


@app.get("/internal/connectors/{connector_id}/accounts", dependencies=[Depends(require_internal_token)])
async def accounts(connector_id: UUID, user_id: UUID = Depends(user_header)) -> GatewayPayload:
    return GatewayPayload(data=await manager.gateway_get(connector_id, user_id, "/portfolio/accounts"))


@app.get("/internal/connectors/{connector_id}/positions", dependencies=[Depends(require_internal_token)])
async def positions(
    connector_id: UUID, account_id: str, page: int = 0, user_id: UUID = Depends(user_header)
) -> GatewayPayload:
    return GatewayPayload(
        data=await manager.gateway_get(connector_id, user_id, f"/portfolio/{account_id}/positions/{page}")
    )


@app.get("/internal/connectors/{connector_id}/instruments/{conid}", dependencies=[Depends(require_internal_token)])
async def instrument(connector_id: UUID, conid: str, user_id: UUID = Depends(user_header)) -> GatewayPayload:
    return GatewayPayload(data=await manager.gateway_get(connector_id, user_id, f"/iserver/contract/{conid}/info"))


@app.get("/internal/connectors/{connector_id}/ledger", dependencies=[Depends(require_internal_token)])
async def ledger(connector_id: UUID, account_id: str, user_id: UUID = Depends(user_header)) -> GatewayPayload:
    return GatewayPayload(data=await manager.gateway_get(connector_id, user_id, f"/portfolio/{account_id}/ledger"))


@app.api_route("/internal/connectors/{connector_id}/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
async def deny_internal_mutations() -> None:
    raise HTTPException(status_code=404, detail="Trading and mutation routes are not exposed.")


@app.api_route(
    "/connector-sessions/{connector_id}/login/{path:path}",
    methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"],
)
@app.api_route("/connector-sessions/{connector_id}/login", methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"])
async def login_proxy(connector_id: UUID, request: Request, path: str = "") -> Response:
    session = _require_login_session(connector_id, request, ROUTE_TYPE_LOGIN)
    target_path = path or "sso/Login"
    return await _proxy_login_request(connector_id, session, request, target_path)


@app.get("/connector-sessions/{connector_id}/auth-status")
async def browser_auth_status(connector_id: UUID, request: Request) -> JSONResponse:
    session = _require_login_session(connector_id, request, ROUTE_TYPE_LOGIN)

    cookie_header = _gateway_cookie_header(request, connector_id, session)
    if cookie_header:
        manager.remember_gateway_request_cookies(session, cookie_header)

    await manager.refresh_status(session)

    return JSONResponse({
        "authenticated": session.auth_status == ConnectorState.CONNECTED
    })


@app.get("/connector-sessions/{connector_id}/completion-monitor.js")
async def browser_completion_monitor(connector_id: UUID, request: Request) -> Response:
    _require_login_session(connector_id, request, ROUTE_TYPE_LOGIN)
    return Response(content=_completion_monitor_javascript(), media_type="application/javascript")


@app.api_route(
    "/connector-sessions/{connector_id}/upstream/{upstream_host}/{path:path}",
    methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"],
)
@app.api_route(
    "/connector-sessions/{connector_id}/upstream/{upstream_host}",
    methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"],
)
async def upstream_login_proxy(
    connector_id: UUID, upstream_host: str, request: Request, path: str = ""
) -> Response:
    session = _require_login_session(connector_id, request, PROXY_TARGET_IBKR_UPSTREAM)
    upstream_origin = _upstream_origin(upstream_host)
    if not upstream_origin:
        raise HTTPException(status_code=404, detail="Connector upstream route was not found.")
    return await _proxy_login_request(connector_id, session, request, path, upstream_origin=upstream_origin)


@app.api_route("/connector-sessions/{path:path}", methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"])
async def escaped_login_proxy(request: Request, path: str = "") -> Response:
    connector_id, browser_path, target_path = _resolve_escaped_login_path(request, path)
    session = _require_login_session(connector_id, request, ROUTE_TYPE_RECOVERY)
    if browser_path != target_path:
        _trace_recovery_mapping(connector_id, request.method, browser_path, target_path)
    return await _proxy_login_request(connector_id, session, request, target_path)


async def _proxy_login_request(
    connector_id: UUID, session, request: Request, target_path: str, upstream_origin: str | None = None
) -> Response:
    if not settings.gateway_api_base_url:
        raise HTTPException(status_code=503, detail="AIP_IBKR_GATEWAY_BASE_URL must be configured.")
    if _is_trading_path(target_path):
        raise HTTPException(status_code=404, detail="Trading routes are not exposed.")
    proxy_target_type = PROXY_TARGET_IBKR_UPSTREAM if upstream_origin else PROXY_TARGET_GATEWAY
    target_origin = upstream_origin or settings.gateway_api_base_url.rsplit("/v1/api", 1)[0].rstrip("/")
    target = f"{target_origin}/{target_path.lstrip('/')}"
    outbound_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "content-length"
    }
    # Cookies are rebuilt below for the destination's scoped session.  Leaving
    # the browser header here would leak stale cookies whenever the rebuilt
    # header is intentionally empty (notably the first request after reset).
    outbound_headers.pop("cookie", None)
    _rewrite_origin_headers(request, connector_id, target_path, outbound_headers, target_origin)
    reset_gateway_browser_cookies = (
        proxy_target_type == PROXY_TARGET_GATEWAY
        and target_path.lstrip("/") == "sso/Login"
        and session.gateway_browser_reset_pending
    )
    if reset_gateway_browser_cookies:
        # Include names which may only exist in the browser.  They are all
        # scoped to this connector's Gateway login route, never an external
        # SSO upstream route.
        session.gateway_cookie_names_to_clear.update(
            name for name in _gateway_request_cookie_names(request, connector_id)
            if name not in GATEWAY_BROWSER_IDENTITY_COOKIES
        )
    cookie_header = (
        _upstream_cookie_header(request, connector_id, session)
        if proxy_target_type == PROXY_TARGET_IBKR_UPSTREAM
        else _gateway_cookie_header(
            request,
            connector_id,
            session,
            exclude_browser_cookies=reset_gateway_browser_cookies,
        )
    )
    if cookie_header:
        outbound_headers["cookie"] = cookie_header
        if proxy_target_type == PROXY_TARGET_GATEWAY:
            manager.remember_gateway_request_cookies(session, cookie_header)
    request_cookie_names = (
        _cookie_header_names(cookie_header)
        if proxy_target_type == PROXY_TARGET_IBKR_UPSTREAM
        else _gateway_request_cookie_names(request, connector_id)
    )
    request_body = await request.body()
    try:
        async with httpx.AsyncClient(verify=settings.aip_ibkr_gateway_tls_verify, timeout=30) as client:
            attempts = (
                GATEWAY_LOGIN_STARTUP_ATTEMPTS
                if proxy_target_type == PROXY_TARGET_GATEWAY and target_path.lstrip("/") == "sso/Login"
                else 1
            )
            for attempt in range(attempts):
                try:
                    proxied = await client.request(
                        request.method,
                        target,
                        params={k: v for k, v in request.query_params.items() if k != "loginToken"},
                        content=request_body,
                        headers=outbound_headers,
                        follow_redirects=False,
                    )
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    logger.warning(
                        "ibkr_login_proxy_http_error connectorId=%s exceptionClass=%s exception=%r "
                        "target=%s targetPath=%s proxyTargetType=%s attempt=%s method=%s",
                        connector_id,
                        type(exc).__name__,
                        exc,
                        target,
                        target_path,
                        proxy_target_type,
                        attempt + 1,
                        request.method,
                    )
                    if attempt + 1 == attempts:
                        raise
                    logger.info(
                        "ibkr_login_gateway_startup_wait connectorId=%s attempt=%s",
                        connector_id,
                        attempt + 1,
                    )
                    await asyncio.sleep(min(0.25 * (attempt + 1), 1.0))
            server_side_redirect = _server_side_post_redirect_target(request.method, target_path, proxied)
            if proxy_target_type == PROXY_TARGET_GATEWAY and server_side_redirect:
                manager.remember_gateway_response_cookies(session, proxied.headers.get_list("set-cookie"))
                redirect_origin, redirect_path, redirect_query = server_side_redirect
                redirected_headers = {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "content-length"
                }
                _rewrite_origin_headers(request, connector_id, redirect_path, redirected_headers, redirect_origin)
                if redirected_cookie_header := _gateway_cookie_header(request, connector_id, session):
                    redirected_headers["cookie"] = redirected_cookie_header
                proxied = await client.request(
                    request.method,
                    f"{redirect_origin}/{redirect_path.lstrip('/')}",
                    params=_params_without_login_token(redirect_query),
                    content=request_body,
                    headers=redirected_headers,
                    follow_redirects=False,
                )
                _trace_server_side_post_redirect(
                    connector_id,
                    request.method,
                    redirect_origin,
                    redirect_path,
                    proxied.status_code,
                )
                proxy_target_type = PROXY_TARGET_IBKR_UPSTREAM
                target_origin = redirect_origin
                target_path = redirect_path
                request_cookie_names = _cookie_header_names(redirected_headers.get("cookie", ""))
    except httpx.HTTPError as exc:
        logger.warning(
            "ibkr_login_proxy_http_error connectorId=%s exceptionClass=%s exception=%r "
            "target=%s targetPath=%s proxyTargetType=%s attempt=%s method=%s",
            connector_id,
            type(exc).__name__,
            exc,
            target,
            target_path,
            proxy_target_type,
            attempt + 1,
            request.method,
        )
        raise HTTPException(status_code=503, detail="IBKR Gateway login UI is unavailable.") from exc
    prefix = f"/connector-sessions/{connector_id}/login"
    cookie_path = _proxy_cookie_path(connector_id, proxy_target_type, target_origin)
    headers = []
    if reset_gateway_browser_cookies:
        # These headers deliberately precede the fresh Gateway Set-Cookie
        # headers below, so a newly-issued cookie with the same name wins.
        cookie_path = _proxy_cookie_path(connector_id, PROXY_TARGET_GATEWAY, target_origin)
        headers.extend(
            ("set-cookie", _expired_cookie_header(name, cookie_path))
            for name in sorted(session.gateway_cookie_names_to_clear)
        )
    rewritten_location = None
    for name, value in proxied.headers.multi_items():
        lower = name.lower()
        if lower in EXCLUDED_RESPONSE_HEADERS:
            continue
        if lower == "location":
            value = _rewrite_location(value, prefix, session.login_token)
            rewritten_location = value
        elif lower == "set-cookie":
            value = _rewrite_set_cookie(value, cookie_path)
        headers.append((name, value))
    _trace_login_proxy(
        connector_id,
        request.method,
        target_path,
        request_cookie_names,
        proxied,
        rewritten_location,
        proxy_target_type,
        target_origin,
    )
    if _is_repeated_redirect(request, proxied, rewritten_location):
        _trace_redirect_loop(connector_id, request.method, target_path, proxied, rewritten_location)
        raise HTTPException(status_code=502, detail="IBKR Gateway login redirect loop detected.")
    if proxy_target_type == PROXY_TARGET_GATEWAY:
        manager.remember_gateway_response_cookies(session, proxied.headers.get_list("set-cookie"))
    if reset_gateway_browser_cookies:
        session.gateway_cookie_names_to_clear.clear()
        session.gateway_browser_reset_pending = False
    content = _rewrite_content(proxied, prefix, session.login_token, target_path)
    top_level_document = _is_top_level_document_request(request)
    upstream_content_type = proxied.headers.get("content-type", "")
    content = _inject_auth_completion_monitor(
        content, upstream_content_type, connector_id, top_level_document=top_level_document
    )
    response = Response(content=content, status_code=proxied.status_code)
    for name, value in headers:
        response.headers.append(name, value)
    if top_level_document and not upstream_content_type:
        response.headers["content-type"] = "text/html; charset=utf-8"
    response.set_cookie(
        key=_login_cookie_name(connector_id),
        value=session.login_token,
        httponly=True,
        samesite="lax",
        path="/connector-sessions",
    )
    response.set_cookie(
        key=LOGIN_CONTEXT_COOKIE,
        value=str(connector_id),
        httponly=True,
        samesite="lax",
        path="/connector-sessions",
    )
    return response


def _is_top_level_document_request(request: Request) -> bool:
    return request.headers.get("sec-fetch-dest", "").lower() == "document" or request.headers.get(
        "sec-fetch-mode", ""
    ).lower() == "navigate"


def _inject_auth_completion_monitor(
    content: bytes, content_type: str, connector_id: UUID, *, top_level_document: bool
) -> bytes:
    if not top_level_document or (content_type and "text/html" not in content_type.lower()):
        return content
    text = content.decode("utf-8", errors="replace")
    script_url = f"/connector-sessions/{connector_id}/completion-monitor.js"
    script = f'<script src="{script_url}"></script>'
    return (text.replace("</body>", script + "</body>") if "</body>" in text else text + script).encode("utf-8")


def _completion_monitor_javascript() -> str:
    return """(function(){
const status=document.currentScript.src.replace('/completion-monitor.js','/auth-status');
const check=async()=>{try{const r=await fetch(status,{credentials:'same-origin',cache:'no-store'});const s=await r.json();
if(s.authenticated){if(window.opener&&!window.opener.closed){window.opener.postMessage({type:'aip:ibkr-authenticated'},window.location.origin);window.close();}else{document.body.textContent='Authentication completed. You may close this window.';}return;}}catch(_){}setTimeout(check,5000);};setTimeout(check,5000);
})();"""


def _login_cookie(request: Request, connector_id: UUID) -> str:
    parsed = SimpleCookie(request.headers.get("cookie", ""))
    morsel = parsed.get(_login_cookie_name(connector_id))
    return morsel.value if morsel else ""


def _require_login_session(connector_id: UUID, request: Request, route_type: str):
    metadata = _login_context_metadata(connector_id, request, route_type)
    _trace_login_context(metadata)
    token = _selected_login_token(connector_id, request)
    try:
        return manager.require_login_token(connector_id, token)
    except ConnectorError as exc:
        if exc.code == "LOGIN_TOKEN_INVALID":
            _trace_login_context_failure(metadata)
        raise


def _login_context_metadata(connector_id: UUID, request: Request, route_type: str) -> dict[str, object]:
    parsed = SimpleCookie(request.headers.get("cookie", ""))
    query_token = request.query_params.get("loginToken", "")
    cookie_name = _login_cookie_name(connector_id)
    cookie_token = parsed.get(cookie_name).value if parsed.get(cookie_name) else ""
    selected_token = _selected_login_token(connector_id, request)
    session = manager.sessions.get(connector_id)
    server_token = session.login_token if session else ""
    login_token_matches = bool(selected_token and server_token and secrets.compare_digest(server_token, selected_token))
    return {
        "connectorId": connector_id,
        "routeType": route_type,
        "method": request.method,
        "loginTokenQueryPresent": bool(query_token),
        "loginTokenCookiePresent": bool(cookie_token),
        "loginContextCookiePresent": LOGIN_CONTEXT_COOKIE in parsed,
        "serverSessionPresent": session is not None,
        "serverLoginTokenPresent": bool(server_token),
        "loginTokenMatches": login_token_matches,
    }


def _selected_login_token(connector_id: UUID, request: Request) -> str:
    query_token = request.query_params.get("loginToken", "")
    cookie_token = _login_cookie(request, connector_id)
    session = manager.sessions.get(connector_id)
    server_token = session.login_token if session else ""
    if cookie_token and server_token and secrets.compare_digest(cookie_token, server_token):
        return cookie_token
    return query_token or cookie_token


def _trace_login_context(metadata: dict[str, object]) -> None:
    logger.info(
        "ibkr_login_context connectorId=%s routeType=%s method=%s loginTokenQueryPresent=%s "
        "loginTokenCookiePresent=%s loginContextCookiePresent=%s serverSessionPresent=%s "
        "serverLoginTokenPresent=%s loginTokenMatches=%s",
        metadata["connectorId"],
        metadata["routeType"],
        metadata["method"],
        metadata["loginTokenQueryPresent"],
        metadata["loginTokenCookiePresent"],
        metadata["loginContextCookiePresent"],
        metadata["serverSessionPresent"],
        metadata["serverLoginTokenPresent"],
        metadata["loginTokenMatches"],
    )


def _trace_login_context_failure(metadata: dict[str, object]) -> None:
    logger.warning(
        "ibkr_login_context_invalid connectorId=%s routeType=%s method=%s loginTokenQueryPresent=%s "
        "loginTokenCookiePresent=%s loginContextCookiePresent=%s serverSessionPresent=%s "
        "serverLoginTokenPresent=%s loginTokenMatches=%s",
        metadata["connectorId"],
        metadata["routeType"],
        metadata["method"],
        metadata["loginTokenQueryPresent"],
        metadata["loginTokenCookiePresent"],
        metadata["loginContextCookiePresent"],
        metadata["serverSessionPresent"],
        metadata["serverLoginTokenPresent"],
        metadata["loginTokenMatches"],
    )


def _login_cookie_name(connector_id: UUID) -> str:
    return f"aip_ibkr_login_{connector_id.hex}"


def _gateway_cookie_header(request: Request, connector_id: UUID, session, *, exclude_browser_cookies: bool = False) -> str:
    parsed = SimpleCookie(request.headers.get("cookie", ""))
    parsed.pop(_login_cookie_name(connector_id), None)
    parsed.pop(LOGIN_CONTEXT_COOKIE, None)
    # A browser value can have been replaced by an SSO redirect, so prefer it
    # over the server-side fallback jar.
    values = dict(session.gateway_cookies or {})
    browser_values = {name: morsel.value for name, morsel in parsed.items()}
    if exclude_browser_cookies:
        # USERID is an IBKR browser identity association required by the
        # mobile-approval handoff, not a destroyed local Gateway session.
        browser_values = {
            name: value for name, value in browser_values.items()
            if name in GATEWAY_BROWSER_IDENTITY_COOKIES
        }
    values.update(browser_values)
    return "; ".join(f"{name}={value}" for name, value in values.items())


def _expired_cookie_header(name: str, path: str) -> str:
    response = Response()
    response.delete_cookie(key=name, path=path, samesite="lax")
    return response.headers["set-cookie"]


def _upstream_cookie_header(request: Request, connector_id: UUID, session) -> str:
    parsed = SimpleCookie(request.headers.get("cookie", ""))
    parsed.pop(_login_cookie_name(connector_id), None)
    parsed.pop(LOGIN_CONTEXT_COOKIE, None)
    for name, value in (session.gateway_cookies or {}).items():
        # Gateway and external SSO both issue same-named cookies (notably
        # JSESSIONID).  Remove only an actual Gateway value, not a newer SSO
        # value that shares its name.
        morsel = parsed.get(name)
        if morsel and morsel.value == value:
            parsed.pop(name, None)
    return "; ".join(f"{name}={morsel.value}" for name, morsel in parsed.items())


def _cookie_header_names(cookie_header: str) -> list[str]:
    parsed = SimpleCookie(cookie_header)
    return sorted(parsed.keys())


def _gateway_request_cookie_names(request: Request, connector_id: UUID) -> list[str]:
    parsed = SimpleCookie(request.headers.get("cookie", ""))
    parsed.pop(_login_cookie_name(connector_id), None)
    parsed.pop(LOGIN_CONTEXT_COOKIE, None)
    return sorted(parsed.keys())


def _resolve_escaped_login_path(request: Request, path: str) -> tuple[UUID, str, str]:
    segments = [segment for segment in path.split("/") if segment]
    if segments:
        try:
            connector_id = UUID(segments[0])
            target_path = "/".join(segments[1:])
            if target_path.startswith("login/"):
                target_path = target_path.removeprefix("login/")
            if target_path:
                browser_path = "/" + target_path.lstrip("/")
                return connector_id, browser_path, _sso_recovery_path(browser_path) or target_path
        except ValueError:
            pass
    connector_id = _login_context_connector_id(request)
    if not connector_id or not path:
        raise HTTPException(status_code=404, detail="Connector login route was not found.")
    browser_path = "/" + path.lstrip("/")
    return connector_id, browser_path, _sso_recovery_path(browser_path) or path


def _sso_recovery_path(browser_path: str) -> str | None:
    return SSO_RECOVERY_PATHS.get("/" + browser_path.lstrip("/"))


def _trace_recovery_mapping(connector_id: UUID, method: str, browser_path: str, gateway_path: str) -> None:
    logger.info(
        "ibkr_login_recovery_mapping connectorId=%s routeType=%s recoveryMapping=SSO "
        "browserPath=%s gatewayPath=%s method=%s",
        connector_id,
        ROUTE_TYPE_RECOVERY,
        "/" + browser_path.lstrip("/"),
        "/" + gateway_path.lstrip("/"),
        method,
    )


def _login_context_connector_id(request: Request) -> UUID | None:
    parsed = SimpleCookie(request.headers.get("cookie", ""))
    morsel = parsed.get(LOGIN_CONTEXT_COOKIE)
    if not morsel:
        return None
    try:
        return UUID(morsel.value)
    except ValueError:
        return None


def _rewrite_origin_headers(
    request: Request,
    connector_id: UUID,
    target_path: str,
    outbound_headers: dict[str, str],
    target_origin: str,
) -> None:
    public_origin = f"{request.url.scheme}://{request.url.netloc}"
    for name in list(outbound_headers.keys()):
        if name.lower() == "origin" and outbound_headers[name] == public_origin:
            outbound_headers[name] = target_origin
        if name.lower() == "referer":
            outbound_headers[name] = _rewrite_referer(
                outbound_headers[name], public_origin, connector_id, target_path, target_origin
            )


def _rewrite_referer(
    value: str, public_origin: str, connector_id: UUID, fallback_path: str, target_origin: str
) -> str:
    split = urlsplit(value)
    referer_origin = urlunsplit((split.scheme, split.netloc, "", "", ""))
    if referer_origin != public_origin:
        return value
    query = _strip_login_token(split.query)
    gateway_path = _gateway_path_from_public_path(split.path, connector_id) or fallback_path
    return f"{target_origin}/{gateway_path.lstrip('/')}" + (f"?{query}" if query else "")


def _gateway_path_from_public_path(path: str, connector_id: UUID) -> str | None:
    login_prefix = f"/connector-sessions/{connector_id}/login"
    upstream_prefix = f"/connector-sessions/{connector_id}/upstream/"
    connector_prefix = f"/connector-sessions/{connector_id}"
    if path == login_prefix:
        return "sso/Login"
    if path.startswith(login_prefix + "/"):
        return path.removeprefix(login_prefix + "/")
    if path.startswith(upstream_prefix):
        parts = path.removeprefix(upstream_prefix).split("/", 1)
        return parts[1] if len(parts) > 1 else ""
    if path.startswith(connector_prefix + "/"):
        return path.removeprefix(connector_prefix + "/")
    if path.startswith("/connector-sessions/"):
        return path.removeprefix("/connector-sessions/")
    return None


def _strip_login_token(query: str) -> str:
    return urlencode([(name, value) for name, value in parse_qsl(query, keep_blank_values=True) if name != "loginToken"])


def _params_without_login_token(query: str) -> dict[str, str]:
    return {name: value for name, value in parse_qsl(query, keep_blank_values=True) if name != "loginToken"}


def _gateway_origin() -> str:
    split = urlsplit(settings.gateway_api_base_url)
    return urlunsplit((split.scheme, split.netloc, "", "", ""))


def _upstream_origin(upstream_host: str) -> str | None:
    normalized = upstream_host.strip().lower()
    if (
        not normalized
        or normalized.endswith(".")
        or any(marker in normalized for marker in (":", "/", "\\", "@", "%"))
    ):
        return None
    if normalized in IBKR_BROWSER_REDIRECT_HOSTS:
        return f"https://{normalized}"
    if normalized in _gateway_hosts():
        configured = urlsplit(settings.gateway_api_base_url)
        scheme = configured.scheme or "https"
        return f"{scheme}://{normalized}"
    return None


def _is_trading_path(path: str) -> bool:
    normalized = "/" + path.lower().lstrip("/")
    blocked_terms = ("/orders", "/iserver/account/orders", "/iserver/reply", "/iserver/order", "/trades")
    return any(term in normalized for term in blocked_terms)


def _server_side_post_redirect_target(
    method: str, current_path: str, response: httpx.Response
) -> tuple[str, str, str] | None:
    if method.upper() != "POST" or response.status_code not in {301, 302, 303}:
        return None
    if "/" + current_path.lower().lstrip("/") not in SERVER_SIDE_POST_REDIRECT_PATHS:
        return None
    location = response.headers.get("location", "")
    split = urlsplit(location)
    if not split.scheme or not split.netloc:
        return None
    upstream_origin = _upstream_origin(split.netloc)
    if not upstream_origin:
        return None
    if urlsplit(upstream_origin).netloc not in IBKR_BROWSER_REDIRECT_HOSTS:
        return None
    return upstream_origin, split.path or "/", split.query


def _set_cookie_names(headers: httpx.Headers) -> list[str]:
    names = set()
    for header in headers.get_list("set-cookie"):
        parsed = SimpleCookie(header)
        names.update(parsed.keys())
    return sorted(names)


def _trace_login_proxy(
    connector_id: UUID,
    method: str,
    proxied_path: str,
    cookie_request_names: list[str],
    response: httpx.Response,
    rewritten_location: str | None = None,
    proxy_target_type: str = PROXY_TARGET_GATEWAY,
    target_origin: str = "",
) -> None:
    location = response.headers.get("location", "")
    location_split = urlsplit(location)
    rewritten_split = urlsplit(rewritten_location or "")
    target_split = urlsplit(target_origin)
    logger.info(
        "ibkr_login_trace timestamp=%s connectorId=%s method=%s proxyTargetType=%s upstreamHost=%s "
        "upstreamPath=%s proxiedPath=%s upstreamStatus=%s "
        "contentType=%s locationPresent=%s upstreamLocationHost=%s upstreamLocationPath=%s "
        "rewrittenLocationPath=%s setCookieNames=%s cookieRequestNames=%s",
        datetime.now(UTC).isoformat(),
        connector_id,
        method,
        proxy_target_type,
        target_split.netloc,
        "/" + proxied_path.lstrip("/"),
        "/" + proxied_path.lstrip("/"),
        response.status_code,
        response.headers.get("content-type", ""),
        bool(response.headers.get("location")),
        location_split.netloc,
        location_split.path,
        rewritten_split.path,
        ",".join(_set_cookie_names(response.headers)),
        ",".join(cookie_request_names),
    )
    if proxied_path.lstrip("/") == "sso/Authenticator":
        _trace_authenticator_state(connector_id, response)


def _trace_authenticator_state(connector_id: UUID, response: httpx.Response) -> None:
    """Log only structural MFA state, never response values or credentials."""
    content_type = response.headers.get("content-type", "").lower()
    is_json = "json" in content_type
    payload = None
    if is_json:
        try:
            payload = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    state_fields = {
        "challenge",
        "challengecreated",
        "challengecomplete",
        "challengecompleted",
        "push",
        "pushchallenge",
        "pushcomplete",
        "pushcompleted",
        "approved",
        "complete",
        "completed",
        "success",
        "authenticated",
        "authres",
        "isibkey",
        "pushsent",
        "dispatcher",
        "dispatch",
        "forwardto",
        "status",
    }
    present_fields: list[str] = []
    boolean_states: list[str] = []
    value_categories: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = key.replace("_", "").replace("-", "").lower()
            if normalized not in state_fields:
                continue
            present_fields.append(normalized)
            if normalized == "authres":
                value_categories.append("authres=" + _classify_auth_res(value))
            elif isinstance(value, bool):
                boolean_states.append(f"{normalized}={value}")
    logger.info(
        "ibkr_authenticator_state connectorId=%s upstreamStatus=%s jsonResponse=%s jsonObject=%s "
        "stateFields=%s booleanStates=%s valueCategories=%s",
        connector_id,
        response.status_code,
        is_json,
        isinstance(payload, dict),
        ",".join(sorted(present_fields)),
        ",".join(sorted(boolean_states)),
        ",".join(sorted(value_categories)),
    )


def _classify_auth_res(value: object) -> str:
    """Classify the Gateway completion signal without retaining its value."""
    if not isinstance(value, str):
        return "OPAQUE"
    if value == "true":
        return "TRUE_STRING"
    if value == "false":
        return "FALSE_STRING"
    normalized = value.lower()
    if normalized in {"success", "successful", "approved", "complete", "completed", "ok"}:
        return "SUCCESS_WORD"
    if normalized in {"pending", "waiting", "wait", "challenge", "pushsent"}:
        return "PENDING_WORD"
    if normalized in {"failed", "failure", "error", "rejected", "denied", "cancelled", "canceled"}:
        return "FAILURE_WORD"
    if value == "":
        return "EMPTY_STRING"
    if len(value) <= 64 and value.isprintable() and re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        return "OTHER_PROTOCOL"
    return "OPAQUE"


def _trace_redirect_loop(
    connector_id: UUID,
    method: str,
    proxied_path: str,
    response: httpx.Response,
    rewritten_location: str | None,
) -> None:
    location_split = urlsplit(response.headers.get("location", ""))
    rewritten_split = urlsplit(rewritten_location or "")
    logger.warning(
        "ibkr_login_redirect_loop timestamp=%s connectorId=%s method=%s proxiedPath=%s upstreamStatus=%s "
        "upstreamLocationHost=%s upstreamLocationPath=%s rewrittenLocationPath=%s",
        datetime.now(UTC).isoformat(),
        connector_id,
        method,
        "/" + proxied_path.lstrip("/"),
        response.status_code,
        location_split.netloc,
        location_split.path,
        rewritten_split.path,
    )


def _trace_server_side_post_redirect(
    connector_id: UUID,
    original_method: str,
    target_origin: str,
    target_path: str,
    upstream_status: int,
) -> None:
    target_split = urlsplit(target_origin)
    logger.info(
        "ibkr_login_redirect_bridge connectorId=%s redirectHandling=SERVER_SIDE_POST_PRESERVED "
        "originalMethod=%s targetHost=%s targetPath=%s upstreamStatus=%s",
        connector_id,
        original_method,
        target_split.netloc,
        "/" + target_path.lstrip("/"),
        upstream_status,
    )


def _is_repeated_redirect(request: Request, response: httpx.Response, rewritten_location: str | None) -> bool:
    if request.method.upper() != "GET" or response.status_code not in {301, 302, 303, 307, 308}:
        return False
    if not rewritten_location:
        return False
    rewritten = urlsplit(rewritten_location)
    request_url = request.url
    current_path = request_url.path
    current_query = _strip_login_token(request_url.query)
    rewritten_query = _strip_login_token(rewritten.query)
    return rewritten.path == current_path and rewritten_query == current_query


def _rewrite_location(value: str, prefix: str, login_token: str) -> str:
    split = urlsplit(value)
    connector_prefix = prefix.removesuffix("/login")
    if split.scheme and not _is_gateway_redirect_host(split):
        return value
    if split.scheme:
        path = split.path
        query = split.query
        if split.netloc.lower() in IBKR_BROWSER_REDIRECT_HOSTS:
            return urlunsplit(
                ("", "", f"{connector_prefix}/upstream/{split.netloc.lower()}{path}", query, split.fragment)
            )
    elif value.startswith("/"):
        path = split.path
        query = split.query
    else:
        return _with_login_token(f"{prefix}/{value.lstrip('/')}", login_token)
    return urlunsplit(("", "", f"{connector_prefix}{path}", query, split.fragment))


def _rewrite_content(response: httpx.Response, prefix: str, login_token: str, current_path: str) -> bytes:
    content_type = response.headers.get("content-type", "")
    if not any(kind in content_type.lower() for kind in ("text/html", "text/css", "javascript")):
        return response.content
    is_javascript = "javascript" in content_type.lower()
    text = response.text
    text = _rewrite_absolute_gateway_urls(text, prefix, login_token)
    if is_javascript:
        text = _rewrite_login_bundle_origin_prefix(text, prefix)
    text = re.sub(
        r"""(?P<before>[:=,\[\(\s]\s*["'])(?P<url>/(?!/|connector-sessions/)[^"'\\\s<>)]+)""",
        lambda match: match.group("before") + _with_login_token(prefix + match.group("url"), login_token),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"""(?P<attr>\b(?:href|src|action)=["'])(?P<url>/(?!/|connector-sessions/)[^"']*)""",
        lambda match: match.group("attr") + _with_login_token(prefix + match.group("url"), login_token),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"""(?P<attr>\b(?:href|src|action)=["'])(?P<url>(?![a-z][a-z0-9+.-]*:|/|#|javascript:)[^"']+)""",
        lambda match: match.group("attr")
        + (
            match.group("url")
            if is_javascript and match.group("attr").lower().startswith("action=")
            else _with_login_token(_relative_proxy_url(prefix, current_path, match.group("url")), login_token)
        ),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"""url\((?P<quote>["']?)(?P<url>/(?!/|connector-sessions/)[^"')]+)(?P=quote)\)""",
        lambda match: f"url({match.group('quote')}{_with_login_token(prefix + match.group('url'), login_token)}{match.group('quote')})",
        text,
        flags=re.IGNORECASE,
    )
    return text.encode(response.encoding or "utf-8")


def _relative_proxy_url(prefix: str, current_path: str, value: str) -> str:
    base = f"/{current_path}" if current_path else "/sso/Login"
    normalized = urljoin(base, value)
    return f"{prefix}{normalized}"


def _rewrite_login_bundle_origin_prefix(text: str, prefix: str) -> str:
    """Keep the Gateway login bundle's Authenticator POST connector-scoped.

    The observed xyz bundle derives its request prefix from only the first path
    segment of ``document.location.pathname``.  Under the connector route that
    becomes ``/connector-sessions/``, dropping the connector id and login
    prefix.  Replace that one known prefix expression in the login bundle with
    the already-scoped Gateway SSO route.
    """
    origin_prefix_expression = (
        'document.location.protocol+"//"+document.location.host+"/"+'
        'document.location.pathname.split("/")[1]+"/"'
    )
    return text.replace(origin_prefix_expression, f'"{prefix}/sso/"')


def _rewrite_absolute_gateway_urls(text: str, prefix: str, login_token: str) -> str:
    for host in _gateway_hosts():
        for scheme in ("https", "http"):
            base = f"{scheme}://{host}"
            text = text.replace(base, _with_login_token(prefix, login_token).split("?")[0])
    return text


def _gateway_hosts() -> set[str]:
    hosts = set(GATEWAY_HOSTS)
    configured = urlsplit(settings.gateway_api_base_url)
    if configured.netloc:
        hosts.add(configured.netloc)
    return hosts


def _is_gateway_redirect_host(split) -> bool:
    host = split.netloc.lower()
    return host in _gateway_hosts() or host in IBKR_BROWSER_REDIRECT_HOSTS


def _rewrite_set_cookie(value: str, prefix: str) -> str:
    parts = [part.strip() for part in value.split(";")]
    if not parts:
        return value
    rewritten = [parts[0]]
    has_path = False
    for part in parts[1:]:
        lower = part.lower()
        if lower.startswith("domain="):
            continue
        if lower.startswith("path="):
            rewritten.append(f"Path={prefix}")
            has_path = True
        else:
            rewritten.append(part)
    if not has_path:
        rewritten.append(f"Path={prefix}")
    return "; ".join(rewritten)


def _proxy_cookie_path(connector_id: UUID, proxy_target_type: str, target_origin: str) -> str:
    """Keep Gateway and external SSO cookies in separate browser namespaces."""
    base = f"/connector-sessions/{connector_id}"
    if proxy_target_type == PROXY_TARGET_IBKR_UPSTREAM:
        return f"{base}/upstream/{urlsplit(target_origin).netloc.lower()}"
    return f"{base}/login"


def _with_login_token(value: str, login_token: str) -> str:
    split = urlsplit(value)
    params = parse_qsl(split.query, keep_blank_values=True)
    if not any(name == "loginToken" for name, _ in params):
        params.append(("loginToken", login_token))
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(params), split.fragment))


