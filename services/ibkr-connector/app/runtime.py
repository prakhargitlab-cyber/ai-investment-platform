import asyncio
import logging
import os
import signal
import secrets
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from uuid import UUID

import httpx

from app.models import ConnectorState, ConnectorStatusResponse
from app.settings import Settings

logger = logging.getLogger(__name__)
GATEWAY_STOP_TIMEOUT_SECONDS = 10
GATEWAY_TERMINATE_SIGNAL = signal.SIGTERM
GATEWAY_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)


@dataclass
class ConnectorSession:
    connector_id: UUID
    user_id: UUID
    login_token: str
    runtime_status: ConnectorState = ConnectorState.STARTING
    auth_status: ConnectorState = ConnectorState.AUTHENTICATION_REQUIRED
    heartbeat_at: datetime | None = None
    authenticated_at: datetime | None = None
    process: subprocess.Popen | None = None
    gateway_cookies: dict[str, str] = None
    # A new Gateway process cannot safely consume a browser session created by
    # an earlier process.  The first login request clears the Gateway-scoped
    # browser state before allowing this process to issue authoritative cookies.
    gateway_cookie_names_to_clear: set[str] = None
    gateway_browser_reset_pending: bool = False
    code: str = "STARTING"
    message: str = "IBKR connector runtime is starting."

    def __post_init__(self) -> None:
        if self.gateway_cookies is None:
            self.gateway_cookies = {}
        if self.gateway_cookie_names_to_clear is None:
            self.gateway_cookie_names_to_clear = set()


class ConnectorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConnectorManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions: dict[UUID, ConnectorSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, connector_id: UUID, user_id: UUID) -> ConnectorSession:
        async with self._lock:
            existing = self.sessions.get(connector_id)
            if existing:
                self.require_owner(existing, user_id)
                if existing.runtime_status == ConnectorState.STOPPED:
                    existing = ConnectorSession(
                        connector_id=connector_id,
                        user_id=user_id,
                        login_token=secrets.token_urlsafe(32),
                        gateway_browser_reset_pending=True,
                    )
                    self.sessions[connector_id] = existing
                    self._start_gateway(existing)
                if existing.runtime_status != ConnectorState.ERROR:
                    await self.refresh_status(existing)
                return existing
            active = [session for session in self.sessions.values() if session.runtime_status != ConnectorState.STOPPED]
            if active:
                raise ConnectorError(
                    "DEV_SINGLE_CONNECTOR_LIMIT",
                    "This runtime can host one IBKR Gateway session. Use one pod per connector for isolation.",
                )
            session = ConnectorSession(
                connector_id=connector_id,
                user_id=user_id,
                login_token=secrets.token_urlsafe(32),
                gateway_browser_reset_pending=True,
            )
            self.sessions[connector_id] = session
            self._start_gateway(session)
            if session.runtime_status != ConnectorState.ERROR:
                await self.refresh_status(session)
            return session

    def get(self, connector_id: UUID, user_id: UUID) -> ConnectorSession:
        session = self.sessions.get(connector_id)
        if not session:
            raise ConnectorError("CONNECTOR_NOT_FOUND", "Connector runtime was not found.")
        self.require_owner(session, user_id)
        return session

    async def status(self, connector_id: UUID, user_id: UUID) -> ConnectorSession:
        session = self.get(connector_id, user_id)
        await self.refresh_status(session)
        return session

    async def stop(self, connector_id: UUID, user_id: UUID) -> ConnectorSession:
        session = self.get(connector_id, user_id)
        session.runtime_status = ConnectorState.STOPPING
        session.code = "STOPPING"
        session.message = "IBKR Gateway process is stopping."
        process = session.process
        if process and process.poll() is None:
            _terminate_gateway_process(process)
        session.runtime_status = ConnectorState.STOPPED
        session.auth_status = ConnectorState.SESSION_EXPIRED
        session.process = None
        session.code = "STOPPED"
        session.message = "IBKR connector runtime is stopped."
        return session

    async def restart(self, connector_id: UUID, user_id: UUID) -> ConnectorSession:
        previous_cookie_names = set(self.get(connector_id, user_id).gateway_cookies)
        await self.stop(connector_id, user_id)

        async with self._lock:
            old_session = self.sessions[connector_id]
            self.require_owner(old_session, user_id)
            session = ConnectorSession(
                connector_id=connector_id,
                user_id=user_id,
                login_token=secrets.token_urlsafe(32),
                gateway_cookie_names_to_clear=previous_cookie_names,
                gateway_browser_reset_pending=True,
            )
            self.sessions[connector_id] = session

            self._start_gateway(session)

        if session.runtime_status != ConnectorState.ERROR:
            await self.refresh_status(session)

        return session

    async def gateway_get(self, connector_id: UUID, user_id: UUID, path: str) -> object:
        session = await self.status(connector_id, user_id)
        if session.auth_status != ConnectorState.CONNECTED:
            raise ConnectorError("SESSION_EXPIRED", "IBKR Gateway session is not authenticated.")
        return await self._gateway_get(path, session)

    async def refresh_status(self, session: ConnectorSession) -> None:
        if session.runtime_status in {ConnectorState.STOPPED, ConnectorState.STOPPING}:
            return
        process = session.process
        if process and process.poll() is not None:
            session.runtime_status = ConnectorState.ERROR
            session.auth_status = ConnectorState.SESSION_EXPIRED
            session.code = "GATEWAY_PROCESS_EXITED"
            session.message = "IBKR Gateway process exited unexpectedly."
            return
        if not self.settings.gateway_api_base_url:
            session.runtime_status = ConnectorState.DEGRADED
            session.auth_status = ConnectorState.AUTHENTICATION_REQUIRED
            session.code = "GATEWAY_BASE_URL_MISSING"
            session.message = "AIP_IBKR_GATEWAY_BASE_URL must be configured."
            return
        try:
            payload = await self._gateway_get("/iserver/auth/status", session)
        except ConnectorError as exc:
            if session.runtime_status == ConnectorState.STARTING or session.authenticated_at is None:
                session.runtime_status = ConnectorState.AUTHENTICATION_REQUIRED
                session.auth_status = ConnectorState.AUTHENTICATION_REQUIRED
            else:
                session.runtime_status = ConnectorState.DEGRADED
                session.auth_status = ConnectorState.SESSION_EXPIRED
            session.code = exc.code
            session.message = exc.message
            return
        now = datetime.now(UTC)
        session.heartbeat_at = now
        # Client Portal can report authenticated=true before the popup leaves its MFA document and
        # before the auxiliary connected/established flags converge. Authentication is the authority;
        # requiring every transport flag caused successful second MFA approvals to remain pending.
        authenticated = bool(payload.get("authenticated"))
        competing = not authenticated and (bool(payload.get("competing")) or payload.get("connected") is False)
        logger.info(
            "ibkr_auth_probe connector=%s runtime_exists=%s authenticated=%s connected=%s established=%s competing=%s authenticated_at_present=%s",
            session.connector_id,
            session.process is not None,
            authenticated,
            bool(payload.get("connected")),
            bool(payload.get("established", True)),
            bool(payload.get("competing")),
            session.authenticated_at is not None,
        )
        if authenticated:
            session.runtime_status = ConnectorState.CONNECTED
            session.auth_status = ConnectorState.CONNECTED
            session.authenticated_at = now
            session.code = "CONNECTED"
            session.message = "IBKR Gateway session is authenticated."
        elif competing:
            session.runtime_status = ConnectorState.DEGRADED
            session.auth_status = ConnectorState.SESSION_EXPIRED
            session.code = "SESSION_EXPIRED"
            session.message = "IBKR Gateway reports the session is not connected."
        else:
            session.runtime_status = ConnectorState.AUTHENTICATION_REQUIRED
            session.auth_status = ConnectorState.AUTHENTICATION_REQUIRED
            session.code = "AUTHENTICATION_REQUIRED"
            session.message = "Interactive IBKR authentication is required."

    def login_url(self, session: ConnectorSession) -> str:
        # The Gateway login bundle only submits its final Dispatcher callback
        # after mobile approval when forwardTo is present.  That callback is
        # what establishes the local Client Portal session.
        path = (
            f"/connector-sessions/{session.connector_id}/login/sso/Login"
            f"?forwardTo=22&RL=1&ip2loc=on&loginToken={session.login_token}"
        )
        return f"{self.settings.login_public_base_url}{path}" if self.settings.login_public_base_url else path

    def response(self, session: ConnectorSession) -> ConnectorStatusResponse:
        return ConnectorStatusResponse(
            connectorId=session.connector_id,
            userId=session.user_id,
            runtimeStatus=session.runtime_status,
            authStatus=session.auth_status,
            heartbeatAt=_iso(session.heartbeat_at),
            authenticatedAt=_iso(session.authenticated_at),
            loginUrl=self.login_url(session),
            code=session.code,
            message=session.message,
        )

    def require_login_token(self, connector_id: UUID, token: str) -> ConnectorSession:
        session = self.sessions.get(connector_id)
        if not session or not secrets.compare_digest(session.login_token, token):
            raise ConnectorError("LOGIN_TOKEN_INVALID", "Connector login route is invalid or expired.")
        return session

    @staticmethod
    def remember_gateway_request_cookies(session: ConnectorSession, cookie_header: str) -> None:
        if not cookie_header:
            return
        parsed = SimpleCookie(cookie_header)
        for name, morsel in parsed.items():
            session.gateway_cookies[name] = morsel.value

    @staticmethod
    def remember_gateway_response_cookies(session: ConnectorSession, set_cookie_headers: list[str]) -> None:
        for header in set_cookie_headers:
            parsed = SimpleCookie(header)
            for name, morsel in parsed.items():
                if morsel.value:
                    session.gateway_cookies[name] = morsel.value
                else:
                    session.gateway_cookies.pop(name, None)


    @staticmethod
    def require_owner(session: ConnectorSession, user_id: UUID) -> None:
        if session.user_id != user_id:
            raise ConnectorError("CONNECTOR_FORBIDDEN", "Connector belongs to a different user.")

    def _start_gateway(self, session: ConnectorSession) -> None:
        package_path = Path(self.settings.aip_ibkr_gateway_package_path)
        run_script = package_path / "bin" / "run.sh"
        config_path = package_path / "root" / "conf.yaml"
        if not self.settings.aip_ibkr_gateway_package_path:
            session.runtime_status = ConnectorState.ERROR
            session.auth_status = ConnectorState.SESSION_EXPIRED
            session.code = "GATEWAY_PACKAGE_MISSING"
            session.message = "AIP_IBKR_GATEWAY_PACKAGE_PATH must point to the official extracted IBKR Gateway package."
            return
        if not run_script.is_file() or not config_path.is_file():
            session.runtime_status = ConnectorState.ERROR
            session.auth_status = ConnectorState.SESSION_EXPIRED
            session.code = "GATEWAY_PACKAGE_INVALID"
            session.message = "IBKR Gateway package must contain bin/run.sh and root/conf.yaml."
            return
        session.process = subprocess.Popen(
            [str(run_script), "root/conf.yaml"],
            cwd=str(package_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_gateway_process_options(),
        )
        session.runtime_status = ConnectorState.STARTING
        session.auth_status = ConnectorState.AUTHENTICATION_REQUIRED
        session.code = "STARTING"
        session.message = "IBKR Gateway process was started."

    async def _gateway_get(self, path: str, session: ConnectorSession | None = None) -> object:
        if not self.settings.gateway_api_base_url:
            raise ConnectorError("GATEWAY_BASE_URL_MISSING", "AIP_IBKR_GATEWAY_BASE_URL must be configured.")
        if _is_trading_path(path):
            raise ConnectorError("TRADING_FORBIDDEN", "Trading operations are not exposed by this connector.")
        url = f"{self.settings.gateway_api_base_url}/{path.lstrip('/')}"
        headers = {}
        if session and session.gateway_cookies:
            headers["cookie"] = "; ".join(f"{name}={value}" for name, value in session.gateway_cookies.items())
        try:
            async with httpx.AsyncClient(
                verify=self.settings.aip_ibkr_gateway_tls_verify,
                timeout=self.settings.aip_ibkr_heartbeat_timeout_seconds,
            ) as client:
                response = await client.get(url, headers=headers)
            if session:
                _trace_gateway_get(path, session, response)
            if session:
                self.remember_gateway_response_cookies(session, response.headers.get_list("set-cookie"))
            if response.status_code in {401, 403}:
                raise ConnectorError("SESSION_EXPIRED", "IBKR Gateway session is expired or unauthenticated.")
            if response.status_code == 404:
                raise ConnectorError("GATEWAY_NOT_FOUND", "IBKR Gateway endpoint was not found.")
            response.raise_for_status()
            return response.json()
        except ConnectorError:
            raise
        except httpx.HTTPError as exc:
            raise ConnectorError("GATEWAY_UNAVAILABLE", "IBKR Gateway is unavailable.") from exc


def _gateway_process_options() -> dict[str, object]:
    if os.name == "posix":
        return {"start_new_session": True}
    return {}


def _terminate_gateway_process(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, GATEWAY_TERMINATE_SIGNAL)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=GATEWAY_STOP_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, GATEWAY_KILL_SIGNAL)
            except ProcessLookupError:
                return
            process.wait(timeout=GATEWAY_STOP_TIMEOUT_SECONDS)
            return

    process.terminate()
    try:
        process.wait(timeout=GATEWAY_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=GATEWAY_STOP_TIMEOUT_SECONDS)


def _is_trading_path(path: str) -> bool:
    normalized = path.lower()
    blocked_terms = ("/orders", "/iserver/account/orders", "/iserver/reply", "/iserver/order")
    return any(term in normalized for term in blocked_terms)


def _trace_gateway_get(path: str, session: ConnectorSession, response: httpx.Response) -> None:
    logger.info(
        "ibkr_login_trace timestamp=%s connectorId=%s method=GET proxiedPath=%s upstreamStatus=%s "
        "contentType=%s locationPresent=%s setCookieNames=%s cookieRequestNames=%s",
        datetime.now(UTC).isoformat(),
        session.connector_id,
        "/" + path.lstrip("/"),
        response.status_code,
        response.headers.get("content-type", ""),
        bool(response.headers.get("location")),
        ",".join(_set_cookie_names(response.headers)),
        ",".join(sorted((session.gateway_cookies or {}).keys())),
    )


def _set_cookie_names(headers: httpx.Headers) -> list[str]:
    names = set()
    for header in headers.get_list("set-cookie"):
        parsed = SimpleCookie(header)
        names.update(parsed.keys())
    return sorted(names)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
