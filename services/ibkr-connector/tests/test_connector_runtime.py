from pathlib import Path
from http.cookies import SimpleCookie
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import app.runtime as runtime
from app.main import (
    _completion_monitor_javascript,
    _inject_auth_completion_monitor,
    _rewrite_content,
    _trace_authenticator_state,
    app,
    manager,
    settings,
)
from app.models import ConnectorState
from app.settings import Settings


TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
def reset_runtime(monkeypatch, tmp_path):
    manager.sessions.clear()
    settings.aip_internal_token = TOKEN
    settings.aip_connector_require_internal_token = True
    settings.aip_ibkr_gateway_package_path = str(tmp_path / "missing")
    settings.aip_ibkr_gateway_base_url = "https://gateway.internal/v1/api"
    settings.aip_ibkr_gateway_tls_verify = True
    settings.aip_ibkr_login_public_base_url = "https://broker.example.test"
    yield
    for session in manager.sessions.values():
        if session.process and session.process.poll() is None:
            if hasattr(session.process, "kill"):
                session.process.kill()
    manager.sessions.clear()


def headers(user_id):
    return {"X-Internal-Token": TOKEN, "X-AIP-User-Id": str(user_id)}


def create(client, connector_id, user_id):
    return client.post(
        "/internal/connectors",
        headers={"X-Internal-Token": TOKEN},
        json={"connectorId": str(connector_id), "userId": str(user_id)},
    )


def test_missing_gateway_artifact_sets_error() -> None:
    client = TestClient(app)
    connector_id = uuid4()
    user_id = uuid4()

    response = create(client, connector_id, user_id)

    assert response.status_code == 200
    body = response.json()
    assert body["runtimeStatus"] == ConnectorState.ERROR
    assert body["code"] == "GATEWAY_PACKAGE_INVALID"


def test_existing_connected_runtime_is_live_reconciled_on_start(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    session = runtime.ConnectorSession(connector_id=connector_id, user_id=user_id, login_token="opaque")
    session.runtime_status = ConnectorState.CONNECTED
    session.auth_status = ConnectorState.CONNECTED
    manager.sessions[connector_id] = session

    async def unauthenticated_gateway(path, current_session=None):
        assert path == "/iserver/auth/status"
        assert current_session is session
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_gateway_get", unauthenticated_gateway)
    response = create(TestClient(app), connector_id, user_id)

    assert response.status_code == 200
    assert response.json()["authStatus"] == ConnectorState.AUTHENTICATION_REQUIRED
    assert response.json()["loginUrl"]


def test_login_proxy_waits_for_healthy_gateway_during_runtime_startup(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    calls = 0

    async def unauthenticated_gateway(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b'<html><script src="/sso/js/login.js"></script></html>'
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "text/html;charset=utf-8"})

        @property
        def text(self):
            return self.content.decode()

    class StartupClient:
        def __init__(self, verify, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("gateway is starting")
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", unauthenticated_gateway)
    monkeypatch.setattr(httpx, "AsyncClient", StartupClient)
    monkeypatch.setattr("app.main.asyncio.sleep", lambda delay: _completed())
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]

    response = client.get(login.replace("https://broker.example.test", ""))

    assert response.status_code == 200
    assert calls == 2
    assert f'/connector-sessions/{connector_id}/login/sso/js/login.js' in response.text


async def _completed():
    return None


def test_startup_uses_verified_unix_launcher(monkeypatch, tmp_path: Path) -> None:
    package = tmp_path / "clientportal.gw"
    (package / "bin").mkdir(parents=True)
    (package / "root").mkdir()
    (package / "bin" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (package / "root" / "conf.yaml").write_text("listen: true\n", encoding="utf-8")
    settings.aip_ibkr_gateway_package_path = str(package)
    launched = {}

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            launched["terminated"] = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(args, cwd, stdout, stderr, **kwargs):
        launched["args"] = args
        launched["cwd"] = cwd
        launched["kwargs"] = kwargs
        return FakeProcess()

    async def fake_gateway_get(path, session=None):
        assert path == "/iserver/auth/status"
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr("app.runtime.subprocess.Popen", fake_popen)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)

    response = create(client, uuid4(), uuid4())

    assert response.status_code == 200
    assert launched["args"] == [str(package / "bin" / "run.sh"), "root/conf.yaml"]
    assert launched["cwd"] == str(package)
    if runtime.os.name == "posix":
        assert launched["kwargs"] == {"start_new_session": True}
    else:
        assert launched["kwargs"] == {}
    assert response.json()["runtimeStatus"] == ConnectorState.AUTHENTICATION_REQUIRED


def test_stop_terminates_gateway_process_group_on_posix(monkeypatch) -> None:
    events = []

    class LiveProcess:
        pid = 1234

        def poll(self):
            return None

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            return 0

    monkeypatch.setattr(runtime.os, "name", "posix")
    monkeypatch.setattr(runtime.os, "getpgid", lambda pid: events.append(("getpgid", pid)) or 4321, raising=False)
    monkeypatch.setattr(runtime.os, "killpg", lambda pgid, sig: events.append(("killpg", pgid, sig)), raising=False)

    runtime._terminate_gateway_process(LiveProcess())

    assert events == [
        ("getpgid", 1234),
        ("killpg", 4321, runtime.GATEWAY_TERMINATE_SIGNAL),
        ("wait", runtime.GATEWAY_STOP_TIMEOUT_SECONDS),
    ]


def test_stop_force_kills_gateway_process_group_after_timeout(monkeypatch) -> None:
    events = []

    class StubbornProcess:
        pid = 1234

        def poll(self):
            return None

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if len([event for event in events if event[0] == "wait"]) == 1:
                raise runtime.subprocess.TimeoutExpired(cmd="gateway", timeout=timeout)
            return 0

    monkeypatch.setattr(runtime.os, "name", "posix")
    monkeypatch.setattr(runtime.os, "getpgid", lambda pid: events.append(("getpgid", pid)) or 4321, raising=False)
    monkeypatch.setattr(runtime.os, "killpg", lambda pgid, sig: events.append(("killpg", pgid, sig)), raising=False)

    runtime._terminate_gateway_process(StubbornProcess())

    assert events == [
        ("getpgid", 1234),
        ("killpg", 4321, runtime.GATEWAY_TERMINATE_SIGNAL),
        ("wait", runtime.GATEWAY_STOP_TIMEOUT_SECONDS),
        ("killpg", 4321, runtime.GATEWAY_KILL_SIGNAL),
        ("wait", runtime.GATEWAY_STOP_TIMEOUT_SECONDS),
    ]


def test_auth_success_session_expiry_heartbeat_and_read_only_proxy(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    calls = []

    async def fake_gateway_get(path, session=None):
        calls.append(path)
        if path == "/iserver/auth/status":
            return {"authenticated": len(calls) > 1, "connected": True}
        if path == "/portfolio/accounts":
            return [{"id": "U1234567", "currency": "EUR"}]
        if path == "/portfolio/U1234567/positions/0":
            return [{"conid": "1", "ticker": "AIXA", "position": "1"}]
        if path == "/portfolio/U1234567/ledger":
            return {"EUR": {"cashbalance": "10", "currency": "EUR"}}
        raise AssertionError(path)

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)

    create_response = create(client, connector_id, user_id)
    assert create_response.json()["authStatus"] == ConnectorState.AUTHENTICATION_REQUIRED

    status = client.get(f"/internal/connectors/{connector_id}/status", headers=headers(user_id)).json()
    assert status["authStatus"] == ConnectorState.CONNECTED
    assert status["heartbeatAt"] is not None

    accounts = client.get(f"/internal/connectors/{connector_id}/accounts", headers=headers(user_id))
    positions = client.get(
        f"/internal/connectors/{connector_id}/positions?account_id=U1234567&page=0",
        headers=headers(user_id),
    )
    ledger = client.get(
        f"/internal/connectors/{connector_id}/ledger?account_id=U1234567",
        headers=headers(user_id),
    )

    assert accounts.json()["data"][0]["id"] == "U1234567"
    assert positions.json()["data"][0]["ticker"] == "AIXA"
    assert ledger.json()["data"]["EUR"]["cashbalance"] == "10"


def test_process_failure_restart_and_cleanup(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    events = []

    class FailedProcess:
        def poll(self):
            return 1

    class LiveProcess:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append("wait")

    def first_start(session):
        session.process = FailedProcess()

    async def fake_gateway_get(_, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", first_start)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(runtime.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(runtime.os, "killpg", lambda pgid, sig: events.append("killpg"), raising=False)
    client = TestClient(app)
    create(client, connector_id, user_id)

    status = client.get(f"/internal/connectors/{connector_id}/status", headers=headers(user_id))
    assert status.json()["code"] == "GATEWAY_PROCESS_EXITED"

    monkeypatch.setattr(manager, "_start_gateway", lambda session: setattr(session, "process", LiveProcess()))
    restart = client.post(f"/internal/connectors/{connector_id}/restart", headers=headers(user_id))
    assert restart.status_code == 200

    stopped = client.post(f"/internal/connectors/{connector_id}/stop", headers=headers(user_id))
    assert stopped.json()["runtimeStatus"] == ConnectorState.STOPPED
    assert "terminate" in events or "killpg" in events


def test_restart_replaces_connector_session_and_cookie_jar(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)

    create(client, connector_id, user_id)
    old_session = manager.sessions[connector_id]
    old_cookie_jar = old_session.gateway_cookies
    old_cookie_jar["STALE_GATEWAY_COOKIE"] = "old"

    restart = client.post(f"/internal/connectors/{connector_id}/restart", headers=headers(user_id))

    assert restart.status_code == 200
    new_session = manager.sessions[connector_id]
    assert new_session is not old_session
    assert new_session.gateway_cookies is not old_cookie_jar
    assert new_session.gateway_cookies == {}


def test_explicit_second_login_restarts_previously_authenticated_stale_session(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def unauthenticated(path, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", unauthenticated)
    client = TestClient(app)
    create(client, connector_id, user_id)
    old_session = manager.sessions[connector_id]
    old_session.authenticated_at = datetime.now(UTC)
    old_session.gateway_cookies["JSESSIONID"] = "first-gateway-session"
    old_session.gateway_cookies["URL_PARAM"] = "first-login-state"
    old_token = old_session.login_token

    response = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id))

    assert response.status_code == 200
    assert manager.sessions[connector_id] is not old_session
    assert manager.sessions[connector_id].login_token != old_token
    assert manager.sessions[connector_id].gateway_cookie_names_to_clear == {"JSESSIONID", "URL_PARAM"}
    assert str(connector_id) in response.json()["loginUrl"]


def test_second_login_discards_stale_browser_gateway_session_before_fresh_login(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def unauthenticated(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"<html></html>"
        encoding = "utf-8"
        headers = httpx.Headers([
            ("content-type", "text/html"),
            ("set-cookie", "JSESSIONID=fresh-gateway-session; Path=/"),
        ])

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["cookie"] = headers.get("cookie", "")
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", unauthenticated)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    old_session = manager.sessions[connector_id]
    old_session.authenticated_at = datetime.now(UTC)
    old_session.gateway_cookies.update({"JSESSIONID": "first", "URL_PARAM": "first-state"})

    login_url = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    assert manager.sessions[connector_id].gateway_cookie_names_to_clear == {"JSESSIONID", "URL_PARAM"}
    response = client.get(
        login_url,
        headers={"cookie": "JSESSIONID=first; URL_PARAM=first-state; browser-only=stale"},
    )

    assert response.status_code == 200
    assert observed["cookie"] == ""
    set_cookies = response.headers.get_list("set-cookie")
    assert any("JSESSIONID=\"\"" in header and "Max-Age=0" in header for header in set_cookies)
    assert any("URL_PARAM=\"\"" in header and "Max-Age=0" in header for header in set_cookies)
    assert any("browser-only=\"\"" in header and "Max-Age=0" in header for header in set_cookies)
    assert any("JSESSIONID=fresh-gateway-session" in header for header in set_cookies)
    assert manager.sessions[connector_id].gateway_cookie_names_to_clear == set()


def test_fresh_runtime_discards_stale_browser_gateway_cookies_only_on_first_login(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed_cookies = []

    async def unauthenticated(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"<html></html>"
        encoding = "utf-8"
        headers = httpx.Headers([
            ("content-type", "text/html"),
            ("set-cookie", "JSESSIONID=fresh-gateway-session; Path=/"),
        ])

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed_cookies.append(headers.get("cookie", ""))
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", unauthenticated)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    session = manager.sessions[connector_id]
    assert session.gateway_browser_reset_pending is True

    login_url = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    first = client.get(
        login_url,
        headers={"cookie": "JSESSIONID=stale; URL_PARAM=stale-state; USERID=stable-identity; browser-only=stale"},
    )

    assert first.status_code == 200
    assert observed_cookies == ["USERID=stable-identity"]
    assert session.gateway_cookies == {"JSESSIONID": "fresh-gateway-session", "USERID": "stable-identity"}
    assert session.gateway_browser_reset_pending is False
    expired = first.headers.get_list("set-cookie")
    assert any("JSESSIONID=\"\"" in header and "Max-Age=0" in header for header in expired)
    assert any("URL_PARAM=\"\"" in header and "Max-Age=0" in header for header in expired)
    assert any("browser-only=\"\"" in header and "Max-Age=0" in header for header in expired)
    assert not any("USERID=\"\"" in header and "Max-Age=0" in header for header in expired)

    second = client.get(login_url, headers={"cookie": "JSESSIONID=fresh-gateway-session; USERID=stable-identity"})

    assert second.status_code == 200
    assert observed_cookies[0] == "USERID=stable-identity"
    assert SimpleCookie(observed_cookies[1]).keys() == {"JSESSIONID", "USERID"}
    assert not any("Max-Age=0" in header for header in second.headers.get_list("set-cookie"))


def test_repeated_login_during_current_mfa_does_not_restart_runtime(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def unauthenticated(path, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", unauthenticated)
    client = TestClient(app)
    create(client, connector_id, user_id)
    current_session = manager.sessions[connector_id]
    current_token = current_session.login_token

    first = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id))
    second = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id))

    assert first.status_code == 200
    assert second.status_code == 200
    assert manager.sessions[connector_id] is current_session
    assert manager.sessions[connector_id].login_token == current_token


def test_user_ownership_and_cross_user_access_are_enforced(monkeypatch) -> None:
    connector_id = uuid4()
    user_a = uuid4()
    user_b = uuid4()
    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)

    create(client, connector_id, user_a)
    response = client.get(f"/internal/connectors/{connector_id}/status", headers=headers(user_b))

    assert response.status_code == 403
    assert response.json()["code"] == "CONNECTOR_FORBIDDEN"


def test_no_trading_route_and_internal_mutations_are_absent(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    async def fake_gateway_get(path, session=None):
        return {"authenticated": True, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)
    create(client, connector_id, user_id)

    response = client.post(
        f"/internal/connectors/{connector_id}/orders",
        headers=headers(user_id),
        json={"side": "BUY"},
    )

    assert response.status_code == 404


def test_login_route_uses_token_and_does_not_expose_gateway_url(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)

    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()

    assert login["loginUrl"].startswith("https://broker.example.test/connector-sessions/")
    assert (
        f"/connector-sessions/{connector_id}/login/sso/Login?forwardTo=22&RL=1&ip2loc=on&loginToken="
        in login["loginUrl"]
    )
    assert "gateway.internal" not in login["loginUrl"]
    assert client.get(f"/connector-sessions/{connector_id}/login").status_code == 409


def test_valid_login_query_sets_connector_login_cookie(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"<html></html>"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "text/html"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(f"/connector-sessions/{connector_id}/login?loginToken={token}")
    cookies = response.headers.get_list("set-cookie")
    parsed = SimpleCookie()
    for header in cookies:
        parsed.load(header)

    cookie_name = f"aip_ibkr_login_{connector_id.hex}"
    assert response.status_code == 200
    assert cookie_name in parsed
    assert parsed[cookie_name]["path"] == "/connector-sessions"
    assert parsed[cookie_name]["httponly"] is True
    assert parsed[cookie_name]["samesite"] == "lax"
    assert parsed[cookie_name]["secure"] == ""


def test_upstream_route_accepts_connector_login_cookie_from_prior_login(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["url"] = url
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]
    assert client.get(f"/connector-sessions/{connector_id}/login?loginToken={token}").status_code == 200

    response = client.get(f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator")

    assert response.status_code == 200
    assert observed["url"] == "https://api.ibkr.com/Authenticator"


def test_upstream_route_without_cookie_or_query_token_fails(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)
    create(client, connector_id, user_id)

    response = client.get(f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator")

    assert response.status_code == 409
    assert response.json()["code"] == "LOGIN_TOKEN_INVALID"


def test_upstream_route_with_wrong_token_fails(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)
    create(client, connector_id, user_id)

    response = client.get(
        f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}=stale-token"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "LOGIN_TOKEN_INVALID"


def test_valid_connector_cookie_takes_precedence_over_stale_query_token(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["url"] = url
            observed["params"] = params
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(
        f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator?loginToken=stale-token&a=1",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}={token}"},
    )

    assert response.status_code == 200
    assert observed["url"] == "https://api.ibkr.com/Authenticator"
    assert observed["params"] == {"a": "1"}


def test_runtime_recreated_with_new_token_rejects_old_cookie(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    old_token = login.split("loginToken=", 1)[1]
    assert client.get(f"/connector-sessions/{connector_id}/login?loginToken={old_token}").status_code == 200
    manager.sessions[connector_id].login_token = "new-server-token"

    response = client.get(f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator")

    assert response.status_code == 409
    assert response.json()["code"] == "LOGIN_TOKEN_INVALID"


def test_successful_login_query_refreshes_connector_login_cookie(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(
        f"/connector-sessions/{connector_id}/login?loginToken={token}",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}=stale-token"},
    )
    parsed = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        parsed.load(header)

    assert response.status_code == 200
    assert parsed[f"aip_ibkr_login_{connector_id.hex}"].value == token


def test_login_context_diagnostics_do_not_log_secret_values(monkeypatch, caplog) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    with caplog.at_level("INFO"):
        client.get(f"/connector-sessions/{connector_id}/login?loginToken={token}")
        client.get(
            f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator",
            headers={"cookie": f"aip_ibkr_login_{connector_id.hex}=wrong-secret"},
        )

    assert "ibkr_login_context" in caplog.text
    assert "loginTokenQueryPresent=True" in caplog.text
    assert "loginTokenCookiePresent=True" in caplog.text
    assert "loginTokenMatches=True" in caplog.text
    assert "loginTokenMatches=False" in caplog.text
    assert token not in caplog.text
    assert "wrong-secret" not in caplog.text


def test_login_proxy_preserves_redirect_cookie_status_and_content_type(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 302
        content = b""
        encoding = "utf-8"
        headers = httpx.Headers(
            [
                ("location", "https://gateway.internal/sso/Login?forward=one"),
                ("set-cookie", "IBKR_SESSION=abc; Path=/; HttpOnly"),
                ("content-type", "text/html;charset=utf-8"),
            ]
        )

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            observed["verify"] = verify

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed.update(
                {
                    "method": method,
                    "url": url,
                    "params": params,
                    "cookie": headers.get("cookie"),
                    "follow_redirects": follow_redirects,
                }
            )
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(
        f"/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}&x=1",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}={token}; IBKR_EXISTING=old"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert observed["url"] == "https://gateway.internal/sso/Login"
    assert observed["params"] == {"x": "1"}
    # A brand-new Gateway must not inherit a browser session belonging to the
    # deleted runtime; its response supplies the authoritative replacement.
    assert observed["cookie"] is None
    assert observed["follow_redirects"] is False
    assert response.headers["location"] == f"/connector-sessions/{connector_id}/sso/Login?forward=one"
    assert "gateway.internal" not in response.headers["location"]
    gateway_cookie = next(header for header in response.headers.get_list("set-cookie") if "IBKR_SESSION=abc" in header)
    assert f"Path=/connector-sessions/{connector_id}/login" in gateway_cookie
    assert response.headers["content-type"] == "text/html;charset=utf-8"


def test_javascript_form_dispatcher_action_is_not_rewritten_to_script_directory() -> None:
    class FakeResponse:
        content = (
            b'<form class="xyzform-submit" method="post" action="Dispatcher">'
            b'<script src="relative-helper.js"></script>'
        )
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/javascript"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    rewritten = _rewrite_content(
        FakeResponse(),
        "/connector-sessions/connector-id/login",
        "login-token",
        "sso/lib/xyz.bundle.min.js",
    ).decode("utf-8")

    assert 'action="Dispatcher"' in rewritten
    assert "sso/lib/Dispatcher" not in rewritten
    assert (
        'src="/connector-sessions/connector-id/login/sso/lib/relative-helper.js?loginToken=login-token"'
        in rewritten
    )


def test_html_relative_resource_rewriting_still_uses_current_path() -> None:
    class FakeResponse:
        content = b'<link href="theme.css"><form action="Dispatcher"></form>'
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "text/html;charset=UTF-8"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    rewritten = _rewrite_content(
        FakeResponse(),
        "/connector-sessions/connector-id/login",
        "login-token",
        "sso/Login",
    ).decode("utf-8")

    assert 'href="/connector-sessions/connector-id/login/sso/theme.css?loginToken=login-token"' in rewritten
    assert 'action="/connector-sessions/connector-id/login/sso/Dispatcher?loginToken=login-token"' in rewritten


@pytest.mark.parametrize(
    ("upstream_location", "expected_path"),
    [
        ("https://api.ibkr.com/Authenticator", "/upstream/api.ibkr.com/Authenticator"),
        ("https://api.ibkr.com/report", "/upstream/api.ibkr.com/report"),
        ("/Authenticator", "/Authenticator"),
        ("/Authenticator?foo=bar", "/Authenticator?foo=bar"),
        ("http://localhost:5000/Authenticator", "/Authenticator"),
        ("https://api.ibkr.com/Authenticator?foo=bar#step", "/upstream/api.ibkr.com/Authenticator?foo=bar#step"),
    ],
)
def test_gateway_redirect_locations_stay_connector_scoped(monkeypatch, upstream_location, expected_path) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 302
        content = b""
        encoding = "utf-8"
        headers = httpx.Headers({"location": upstream_location})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["params"] = params
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(
        f"/connector-sessions/{connector_id}/Authenticator?loginToken={token}&requestId=abc",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"/connector-sessions/{connector_id}{expected_path}"
    assert "loginToken" not in response.headers["location"]
    assert observed["params"] == {"requestId": "abc"}


def test_distinct_upstream_authorities_do_not_collapse_into_redirect_loop(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = []

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 302
        content = b""
        encoding = "utf-8"

        def __init__(self, location):
            self.headers = httpx.Headers({"location": location})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed.append((method, url))
            if url == "https://gateway.internal/Authenticator":
                return FakeResponse("https://api.ibkr.com/Authenticator")
            if url == "https://api.ibkr.com/Authenticator":
                return FakeResponse("https://gateway.internal/Authenticator")
            raise AssertionError(url)

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    first = client.post(
        f"/connector-sessions/{connector_id}/login/Authenticator?loginToken={token}",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}={token}"},
        follow_redirects=False,
    )

    assert first.status_code == 302
    assert first.headers["location"] == f"/connector-sessions/{connector_id}/Authenticator"
    assert observed == [
        ("POST", "https://gateway.internal/Authenticator"),
        ("POST", "https://api.ibkr.com/Authenticator"),
    ]


def test_gateway_post_authenticator_redirect_preserves_method_body_and_content_type(monkeypatch, caplog) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = []

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        content = b"ok"
        encoding = "utf-8"

        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = httpx.Headers(headers or {"content-type": "text/plain"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed.append(
                {
                    "method": method,
                    "url": url,
                    "params": params,
                    "content": content,
                    "content_type": headers.get("content-type"),
                    "cookie": headers.get("cookie", ""),
                }
            )
            if url == "https://gateway.internal/Authenticator":
                return FakeResponse(302, {"location": "https://api.ibkr.com/Authenticator"})
            if url == "https://api.ibkr.com/Authenticator":
                return FakeResponse(200)
            raise AssertionError(url)

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    with caplog.at_level("INFO"):
        response = client.post(
            f"/connector-sessions/{connector_id}/login/Authenticator?loginToken={token}&keep=1",
            content=b"username=user&password=secret",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "cookie": f"aip_ibkr_login_{connector_id.hex}={token}; aip_ibkr_login_context={connector_id}; SBID=allowed",
            },
        )

    assert response.status_code == 200
    assert observed[0]["method"] == "POST"
    assert observed[1]["method"] == "POST"
    assert observed[1]["url"] == "https://api.ibkr.com/Authenticator"
    assert observed[1]["content"] == b"username=user&password=secret"
    assert observed[1]["content_type"] == "application/x-www-form-urlencoded"
    assert observed[1]["params"] == {}
    assert observed[1]["cookie"] == "SBID=allowed"
    assert token not in observed[1]["cookie"]
    assert "password=secret" not in caplog.text
    assert token not in caplog.text
    assert "redirectHandling=SERVER_SIDE_POST_PRESERVED" in caplog.text
    assert "originalMethod=POST" in caplog.text
    assert "targetHost=api.ibkr.com" in caplog.text
    assert "targetPath=/Authenticator" in caplog.text


def test_gateway_post_report_redirect_preserves_post_when_required(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = []

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        content = b"ok"
        encoding = "utf-8"

        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = httpx.Headers(headers or {"content-type": "text/plain"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed.append((method, url, content, headers.get("content-type")))
            if url == "https://gateway.internal/report":
                return FakeResponse(302, {"location": "https://api.ibkr.com/report"})
            if url == "https://api.ibkr.com/report":
                return FakeResponse(204)
            raise AssertionError(url)

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.post(
        f"/connector-sessions/{connector_id}/report?loginToken={token}",
        content=b'{"event":"submit"}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 204
    assert observed == [
        ("POST", "https://gateway.internal/report", b'{"event":"submit"}', "application/json"),
        ("POST", "https://api.ibkr.com/report", b'{"event":"submit"}', "application/json"),
    ]


def test_upstream_authenticator_targets_api_ibkr_not_gateway(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["method"] = method
            observed["url"] = url
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(
        f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator?loginToken={token}"
    )

    assert response.status_code == 200
    assert observed["method"] == "GET"
    assert observed["url"] == "https://api.ibkr.com/Authenticator"
    assert observed["url"] != "https://gateway.internal/Authenticator"


def test_escaped_gateway_authenticator_maps_to_sso_authenticator(monkeypatch, caplog) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["method"] = method
            observed["url"] = url
            observed["content"] = content
            observed["content_type"] = headers.get("content-type")
            observed["cookie"] = headers.get("cookie", "")
            observed["params"] = params
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]
    client.get(f"/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}")

    with caplog.at_level("INFO"):
        response = client.post(
            "/connector-sessions/Authenticator?loginToken=stale-token&step=1",
            content=b"username=user&password=secret",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "cookie": (
                    f"aip_ibkr_login_{connector_id.hex}={token}; "
                    f"aip_ibkr_login_context={connector_id}; SBID=allowed; IBKR_SESSION=gateway-session"
                ),
            },
        )

    assert response.status_code == 200
    assert observed["method"] == "POST"
    assert observed["url"] == "https://gateway.internal/sso/Authenticator"
    assert observed["content"] == b"username=user&password=secret"
    assert observed["content_type"] == "application/x-www-form-urlencoded"
    assert observed["cookie"] == "SBID=allowed; IBKR_SESSION=gateway-session"
    assert observed["params"] == {"step": "1"}
    assert "recoveryMapping=SSO" in caplog.text
    assert "browserPath=/Authenticator" in caplog.text
    assert "gatewayPath=/sso/Authenticator" in caplog.text
    assert "method=POST" in caplog.text
    assert "password=secret" not in caplog.text
    assert token not in caplog.text


def test_escaped_gateway_dispatcher_maps_to_sso_dispatcher(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["method"] = method
            observed["url"] = url
            observed["content"] = content
            observed["content_type"] = headers.get("content-type")
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.post(
        "/connector-sessions/Dispatcher",
        content=b"ACTION=NEXT",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "cookie": f"aip_ibkr_login_{connector_id.hex}={token}; aip_ibkr_login_context={connector_id}",
        },
    )

    assert response.status_code == 200
    assert observed["method"] == "POST"
    assert observed["url"] == "https://gateway.internal/sso/Dispatcher"
    assert observed["content"] == b"ACTION=NEXT"
    assert observed["content_type"] == "application/x-www-form-urlencoded"


def test_connector_scoped_login_dispatcher_targets_sso_dispatcher_and_preserves_response(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"<html>dispatcher ok</html>"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "text/html;charset=UTF-8"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["method"] = method
            observed["url"] = url
            observed["content"] = content
            observed["content_type"] = headers.get("content-type")
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.post(
        f"/connector-sessions/{connector_id}/login/sso/Dispatcher?loginToken={token}",
        content=b"ACTION=NEXT",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "cookie": f"aip_ibkr_login_{connector_id.hex}={token}; aip_ibkr_login_context={connector_id}",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html;charset=UTF-8"
    assert b"<html>dispatcher ok</html>" in response.content
    assert b"/completion-monitor.js" in response.content
    assert observed["method"] == "POST"
    assert observed["url"] == "https://gateway.internal/sso/Dispatcher"
    assert observed["content"] == b"ACTION=NEXT"
    assert observed["content_type"] == "application/x-www-form-urlencoded"


def test_unknown_escaped_recovery_path_is_not_sso_prefixed(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["url"] = url
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.post(
        "/connector-sessions/UnknownEndpoint",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}={token}; aip_ibkr_login_context={connector_id}"},
    )

    assert response.status_code == 200
    assert observed["url"] == "https://gateway.internal/UnknownEndpoint"


def test_upstream_report_targets_api_ibkr(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 204
        content = b""
        encoding = "utf-8"
        headers = httpx.Headers()

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["url"] = url
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/report?loginToken={token}")

    assert response.status_code == 204
    assert observed["url"] == "https://api.ibkr.com/report"


def test_upstream_query_is_preserved_without_login_token(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["url"] = url
            observed["params"] = params
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(
        f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator?loginToken={token}&a=1"
    )

    assert response.status_code == 200
    assert observed["url"] == "https://api.ibkr.com/Authenticator"
    assert observed["params"] == {"a": "1"}


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "localhost",
        "127.0.0.1",
        "169.254.169.254",
        "api.ibkr.com.",
        "api.ibkr.com:443",
        "user@api.ibkr.com",
        "https:%2f%2fapi.ibkr.com",
    ],
)
def test_disallowed_upstream_hosts_are_rejected(monkeypatch, host) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {"proxied": False}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["proxied"] = True
            raise AssertionError("Disallowed upstream host should not be proxied.")

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(f"/connector-sessions/{connector_id}/upstream/{host}/test?loginToken={token}")

    assert response.status_code == 404
    assert observed["proxied"] is False


def test_upstream_route_preserves_connector_user_isolation(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    other_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)
    create(client, connector_id, user_id)

    response = client.get(
        f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}=invalid"},
    )
    other = client.get(f"/internal/connectors/{connector_id}/status", headers=headers(other_id))

    assert response.status_code == 409
    assert other.status_code == 403


def test_upstream_does_not_send_login_token_or_gateway_cookies(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["params"] = params
            observed["cookie"] = headers.get("cookie", "")
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]
    session = manager.sessions[connector_id]
    session.gateway_cookies["IBKR_GATEWAY"] = "gateway-secret"

    response = client.get(
        f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator?loginToken={token}&a=1",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}={token}; IBKR_GATEWAY=gateway-secret; API_COOKIE=api"},
    )

    assert response.status_code == 200
    assert observed["params"] == {"a": "1"}
    assert observed["cookie"] == "API_COOKIE=api"
    assert token not in observed["cookie"]
    assert "gateway-secret" not in observed["cookie"]


def test_upstream_keeps_same_named_sso_cookie_separate_from_gateway_cookie(monkeypatch) -> None:
    """An SSO JSESSIONID must not be discarded because Gateway has one too."""
    connector_id = uuid4()
    user_id = uuid4()
    observed = []

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"<html></html>"
        encoding = "utf-8"

        def __init__(self, headers):
            self.headers = httpx.Headers(headers)

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed.append((url, headers.get("cookie", "")))
            if "api.ibkr.com" in url:
                return FakeResponse(
                    [("set-cookie", "JSESSIONID=sso-new; Path=/; HttpOnly"), ("content-type", "text/html")]
                )
            return FakeResponse(
                [("set-cookie", "JSESSIONID=gateway-old; Path=/; HttpOnly"), ("content-type", "text/html")]
            )

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    gateway = client.get(f"/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}")
    sso = client.get(f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/Authenticator?loginToken={token}")
    followup = client.get(f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/report?loginToken={token}")

    assert gateway.status_code == sso.status_code == followup.status_code == 200
    assert f"Path=/connector-sessions/{connector_id}/login" in gateway.headers.get_list("set-cookie")[0]
    assert f"Path=/connector-sessions/{connector_id}/upstream/api.ibkr.com" in sso.headers.get_list("set-cookie")[0]
    assert observed[-1][1] == "JSESSIONID=sso-new"


def test_upstream_trading_paths_are_blocked(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {"proxied": False}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["proxied"] = True
            raise AssertionError("Trading path should not be proxied.")

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.post(
        f"/connector-sessions/{connector_id}/upstream/api.ibkr.com/iserver/account/orders?loginToken={token}"
    )

    assert response.status_code == 404
    assert observed["proxied"] is False


def test_gateway_redirect_to_upstream_then_targets_api_ibkr(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = []

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 302
        content = b""
        encoding = "utf-8"

        def __init__(self, location):
            self.headers = httpx.Headers({"location": location})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed.append((method, url))
            if url == "https://gateway.internal/Authenticator":
                return FakeResponse("https://api.ibkr.com/Authenticator")
            if url == "https://api.ibkr.com/Authenticator":
                return FakeResponse("/next")
            raise AssertionError(url)

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    first = client.post(
        f"/connector-sessions/{connector_id}/login/Authenticator?loginToken={token}",
        headers={"cookie": f"aip_ibkr_login_context={connector_id}; aip_ibkr_login_{connector_id.hex}={token}"},
        follow_redirects=False,
    )

    assert first.status_code == 302
    assert first.headers["location"] == f"/connector-sessions/{connector_id}/next"
    assert observed == [
        ("POST", "https://gateway.internal/Authenticator"),
        ("POST", "https://api.ibkr.com/Authenticator"),
    ]


def test_unrelated_external_redirect_location_is_not_rewritten(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 302
        content = b""
        encoding = "utf-8"
        headers = httpx.Headers({"location": "https://example.com/help"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(f"/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/help"


def test_login_proxy_captures_gateway_cookies_for_internal_api_calls(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        observed["status_cookie_names"] = sorted((session.gateway_cookies or {}).keys()) if session else []
        return {"authenticated": True, "connected": True, "established": True}

    class FakeResponse:
        status_code = 200
        content = b"<html></html>"
        encoding = "utf-8"
        headers = httpx.Headers(
            [
                ("set-cookie", "JSESSIONID=gateway-session; Path=/; HttpOnly; Secure; SameSite=None"),
                ("set-cookie", "x-sess-uuid=gateway-uuid; Path=/; HttpOnly; Secure"),
                ("content-type", "text/html;charset=utf-8"),
            ]
        )

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["proxied_cookie_names"] = sorted(
                name.strip().split("=", 1)[0] for name in headers.get("cookie", "").split(";") if name
            )
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    client.get(
        f"/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}={token}; IBKR_EXISTING=old"},
    )
    status = client.get(f"/internal/connectors/{connector_id}/status", headers=headers(user_id))

    assert status.json()["authStatus"] == ConnectorState.CONNECTED
    assert observed["proxied_cookie_names"] == []
    assert observed["status_cookie_names"] == ["JSESSIONID", "x-sess-uuid"]


def test_escaped_root_relative_login_request_uses_connector_context_and_gateway_cookie_jar(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}
    calls = []

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        encoding = "utf-8"

        def __init__(self, content: bytes, headers):
            self.content = content
            self.headers = httpx.Headers(headers)

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            calls.append(url)
            if url.endswith("/sso/Login"):
                return FakeResponse(
                    b"<html></html>",
                    [
                        ("set-cookie", "IBKR_SESSION=gateway-session; Path=/; HttpOnly"),
                        ("content-type", "text/html"),
                    ],
                )
            observed.update(
                {
                    "method": method,
                    "url": url,
                    "params": params,
                    "content": content,
                    "cookie": headers.get("cookie"),
                    "origin": headers.get("origin"),
                    "referer": headers.get("referer"),
                }
            )
            return FakeResponse(b"{}", {"content-type": "application/json"})

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    client.get(f"/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}")
    response = client.post(
        "/connector-sessions/Authenticator?step=1",
        content=b"username=user&password=secret",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "origin": "http://testserver",
            "referer": f"http://testserver/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}",
        },
    )

    assert response.status_code == 200
    assert observed["method"] == "POST"
    assert observed["url"] == "https://gateway.internal/sso/Authenticator"
    assert observed["params"] == {"step": "1"}
    assert observed["content"] == b"username=user&password=secret"
    assert observed["cookie"] == "IBKR_SESSION=gateway-session"
    assert observed["origin"] == "https://gateway.internal"
    assert observed["referer"] == "https://gateway.internal/sso/Login"


def test_escaped_connector_relative_login_request_preserves_connector_isolation(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["url"] = url
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    invalid = client.post(f"/connector-sessions/{uuid4()}/Authenticator")
    valid = client.post(
        f"/connector-sessions/{connector_id}/Authenticator",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}={token}"},
    )

    assert invalid.status_code == 409
    assert valid.status_code == 200
    assert observed["url"] == "https://gateway.internal/sso/Authenticator"


def test_escaped_login_request_with_invalid_context_cookie_is_denied(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)
    create(client, connector_id, user_id)

    response = client.post(
        "/connector-sessions/Authenticator",
        headers={"cookie": f"aip_ibkr_login_context={connector_id}; aip_ibkr_login_{connector_id.hex}=expired-token"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "LOGIN_TOKEN_INVALID"


def test_connector_scoped_trading_paths_are_blocked_before_proxy(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {"proxied": False}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["proxied"] = True
            raise AssertionError("Trading path should not be proxied.")

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.post(
        f"/connector-sessions/{connector_id}/iserver/account/orders",
        headers={"cookie": f"aip_ibkr_login_{connector_id.hex}={token}"},
    )

    assert response.status_code == 404
    assert observed["proxied"] is False


@pytest.mark.parametrize(
    ("method", "content_type", "body"),
    [
        ("POST", "application/x-www-form-urlencoded", b"username=user&password=secret"),
        ("POST", "application/json", b'{"username":"user","password":"secret"}'),
        (
            "POST",
            "multipart/form-data; boundary=AIP",
            b"--AIP\r\nContent-Disposition: form-data; name=\"password\"\r\n\r\nsecret\r\n--AIP--\r\n",
        ),
        ("PUT", "application/json", b'{"mfa":"123456"}'),
    ],
)
def test_login_proxy_forwards_supported_request_bodies_without_inspection(
    monkeypatch, caplog, method, content_type, body
) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "application/json"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            observed["method"] = method
            observed["content"] = content
            observed["content_type"] = headers.get("content-type")
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    with caplog.at_level("INFO"):
        response = client.request(
            method,
            f"/connector-sessions/{connector_id}/login/sso/Validate?loginToken={token}",
            content=body,
            headers={"content-type": content_type},
        )

    assert response.status_code == 200
    assert observed["method"] == method
    assert observed["content"] == body
    assert observed["content_type"] == content_type
    assert "ibkr_login_trace" in caplog.text
    assert "secret" not in caplog.text
    assert "123456" not in caplog.text
    assert token not in caplog.text


def test_login_trace_logs_cookie_names_only(monkeypatch, caplog) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 302
        content = b""
        encoding = "utf-8"
        headers = httpx.Headers(
            [
                ("location", "/sso/Next"),
                ("set-cookie", "JSESSIONID=response-secret; Path=/; HttpOnly"),
                ("content-type", "text/html"),
            ]
        )

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    with caplog.at_level("INFO"):
        client.get(
            f"/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}",
            headers={"cookie": f"aip_ibkr_login_{connector_id.hex}={token}; IBKR_EXISTING=request-secret"},
            follow_redirects=False,
        )

    assert "setCookieNames=JSESSIONID" in caplog.text
    assert "cookieRequestNames=IBKR_EXISTING" in caplog.text
    assert "response-secret" not in caplog.text
    assert "request-secret" not in caplog.text
    assert token not in caplog.text


def test_login_proxy_rewrites_login_resources_and_relative_forms(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        encoding = "utf-8"
        headers = httpx.Headers({"content-type": "text/html"})
        content = (
            b'<form action="../submit"><input name="username"><input name="password"></form>'
            b'<script src="/sso/js/login.js"></script>'
            b"<script>var config = { PASSWORD_RESET_URL: '/credential.recovery/login-help' };</script>"
            b'<link href="https://gateway.internal/sso/css/login.css">'
        )

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    response = client.get(f"/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}")
    body = response.text

    assert f'action="/connector-sessions/{connector_id}/login/submit?loginToken=' in body
    assert f'src="/connector-sessions/{connector_id}/login/sso/js/login.js?loginToken=' in body
    assert f"PASSWORD_RESET_URL: '/connector-sessions/{connector_id}/login/credential.recovery/login-help?loginToken=" in body
    assert "https://gateway.internal" not in body
    assert f'href="/connector-sessions/{connector_id}/login/sso/css/login.css' in body


def test_login_bundle_scopes_its_dynamic_authenticator_post_to_the_connector(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    observed = {}
    bundle_prefix = (
        'document.location.protocol+"//"+document.location.host+"/"+'
        'document.location.pathname.split("/")[1]+"/"'
    )

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True}

    class FakeResponse:
        status_code = 200
        encoding = "utf-8"

        def __init__(self, content, content_type):
            self.content = content
            self.headers = httpx.Headers({"content-type": content_type})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            if url == "https://gateway.internal/sso/Login":
                return FakeResponse(b'<script src="lib/xyz.bundle.min.js"></script>', "text/html")
            if url == "https://gateway.internal/sso/lib/xyz.bundle.min.js":
                return FakeResponse(
                    (
                        "let o=" + bundle_prefix + ",n=o+\"Authenticator\";"
                        "return ajaxQuery(n,{ACTION:\"INIT\",USER:user,A:A},\"POST\")"
                    ).encode(),
                    "application/javascript",
                )
            observed.update(
                {"method": method, "url": url, "params": params, "body": content, "content_type": headers.get("content-type")}
            )
            return FakeResponse(b"{}", "application/json")

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    page = client.get(f"/connector-sessions/{connector_id}/login/sso/Login?loginToken={token}")
    bundle = client.get(f"/connector-sessions/{connector_id}/login/sso/lib/xyz.bundle.min.js")
    body = b"ACTION=INIT&USER=alice&A=client-proof"
    response = client.post(
        f"/connector-sessions/{connector_id}/login/sso/Authenticator?locale=en_US",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert f'/connector-sessions/{connector_id}/login/sso/lib/xyz.bundle.min.js?loginToken=' in page.text
    assert f'let o="/connector-sessions/{connector_id}/login/sso/",n=o+"Authenticator"' in bundle.text
    assert response.status_code == 200
    assert observed == {
        "method": "POST",
        "url": "https://gateway.internal/sso/Authenticator",
        "params": {"locale": "en_US"},
        "body": body,
        "content_type": "application/x-www-form-urlencoded",
    }


def test_gateway_tls_verification_is_connector_scoped(monkeypatch) -> None:
    settings.aip_ibkr_gateway_tls_verify = False
    created = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"authenticated": True, "connected": True}

    class FakeClient:
        def __init__(self, verify, timeout):
            created["verify"] = verify
            created["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            created["url"] = url
            created["headers"] = headers or {}
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    import anyio

    result = anyio.run(manager._gateway_get, "/iserver/auth/status")

    assert result["authenticated"] is True
    assert created["verify"] is False
    assert created["url"] == "https://gateway.internal/v1/api/iserver/auth/status"


def test_gateway_tls_verify_environment_false_disables_httpx_verification(monkeypatch) -> None:
    monkeypatch.setenv("AIP_IBKR_GATEWAY_TLS_VERIFY", "false")
    configured = Settings()
    created = {}

    class FakeClient:
        def __init__(self, verify, timeout):
            created["verify"] = verify

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    httpx.AsyncClient(verify=configured.aip_ibkr_gateway_tls_verify, timeout=30)

    assert configured.aip_ibkr_gateway_tls_verify is False
    assert created["verify"] is False


def test_credentials_are_not_logged(monkeypatch, caplog) -> None:
    connector_id = uuid4()
    user_id = uuid4()
    secret = "super-secret-password"

    async def fake_gateway_get(path, session=None):
        return {"authenticated": False, "connected": True, "password": secret}

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", fake_gateway_get)
    client = TestClient(app)

    with caplog.at_level("INFO"):
        create(client, connector_id, user_id)

    assert secret not in caplog.text


def test_auth_res_classifier_uses_only_allowlisted_value_free_categories() -> None:
    from app.main import _classify_auth_res

    assert _classify_auth_res("true") == "TRUE_STRING"
    assert _classify_auth_res("false") == "FALSE_STRING"
    assert _classify_auth_res("Approved") == "SUCCESS_WORD"
    assert _classify_auth_res("waiting") == "PENDING_WORD"
    assert _classify_auth_res("denied") == "FAILURE_WORD"
    assert _classify_auth_res("") == "EMPTY_STRING"
    assert _classify_auth_res("AUTH_COMPLETE_V2") == "OTHER_PROTOCOL"
    assert _classify_auth_res("opaque-token-value-that-must-not-be-retained-" * 3) == "OPAQUE"
    assert _classify_auth_res(True) == "OPAQUE"


def test_authenticator_state_trace_logs_only_safe_structural_flags(caplog) -> None:
    connector_id = uuid4()
    response = httpx.Response(
        200,
        json={
            "challenge": True,
            "completed": False,
            "dispatcher": False,
            "auth_res": True,
            "isIBKey": True,
            "pushSent": False,
            "status": "sensitive-state-value",
            "token": "secret-token",
        },
    )

    with caplog.at_level("INFO"):
        _trace_authenticator_state(connector_id, response)

    assert "ibkr_authenticator_state" in caplog.text
    assert "challenge=True" in caplog.text
    assert "completed=False" in caplog.text
    assert "dispatcher=False" in caplog.text
    assert "authres=OPAQUE" in caplog.text
    assert "isibkey=True" in caplog.text
    assert "pushsent=False" in caplog.text
    assert "status" in caplog.text
    assert "sensitive-state-value" not in caplog.text
    assert "secret-token" not in caplog.text


def test_no_proprietary_gateway_package_is_committed() -> None:
    forbidden_suffixes = {".zip", ".war", ".jar"}
    test_file = Path(__file__).resolve()
    service_tree = next(
        (
            candidate / "services" / "ibkr-connector"
            for candidate in test_file.parents
            if (candidate / "services" / "ibkr-connector" / "pyproject.toml").is_file()
        ),
        test_file.parents[1],
    )
    assert (service_tree / "pyproject.toml").is_file(), "Unable to locate the ibkr-connector source tree"
    committed_like_files = [
        path for path in service_tree.rglob("*")
        if path.is_file()
        and path.suffix.lower() in forbidden_suffixes
        and not any(part in {".venv", "build", "target"} for part in path.parts)
    ]

    assert committed_like_files == []


def test_authentication_completion_monitor_notifies_opener_only_after_live_status() -> None:
    connector_id = uuid4()
    page = _inject_auth_completion_monitor(
        b"<html><body>IBKR MFA remains visible</body></html>",
        "text/html",
        connector_id,
        top_level_document=True,
    ).decode("utf-8")

    assert f"/connector-sessions/{connector_id}/completion-monitor.js" in page
    assert "opaque-test-token" not in page
    assert "IBKR MFA remains visible" in page
    monitor = _completion_monitor_javascript()
    assert "aip:ibkr-authenticated" in monitor
    assert "if(s.authenticated)" in monitor
    assert "window.opener.postMessage" in monitor
    assert "window.location.origin" in monitor
    assert "window.close()" in monitor
    assert "Authentication completed. You may close this window." in monitor


def test_top_level_dispatcher_without_content_type_loads_completion_monitor(monkeypatch) -> None:
    connector_id = uuid4()
    user_id = uuid4()

    async def authenticated_gateway(path, current_session=None):
        assert path == "/iserver/auth/status"
        return {"authenticated": True, "connected": True, "established": True}

    class FakeResponse:
        status_code = 200
        content = b"Client login succeeds"
        encoding = "utf-8"
        headers = httpx.Headers({"location": "https://www.interactivebrokers.com/portal/"})

        @property
        def text(self):
            return self.content.decode("utf-8")

    class FakeClient:
        def __init__(self, verify, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, params, content, headers, follow_redirects):
            assert method == "POST"
            assert url == "https://gateway.internal/sso/Dispatcher"
            return FakeResponse()

    monkeypatch.setattr(manager, "_start_gateway", lambda session: None)
    monkeypatch.setattr(manager, "_gateway_get", authenticated_gateway)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    client = TestClient(app)
    create(client, connector_id, user_id)
    login = client.get(f"/internal/connectors/{connector_id}/login", headers=headers(user_id)).json()["loginUrl"]
    token = login.split("loginToken=", 1)[1]

    dispatcher = client.post(
        f"/connector-sessions/{connector_id}/login/sso/Dispatcher?loginToken={token}",
        headers={"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"},
        follow_redirects=False,
    )

    assert dispatcher.status_code == 200
    assert dispatcher.headers["content-type"] == "text/html; charset=utf-8"
    assert "Client login succeeds" in dispatcher.text
    assert f'/connector-sessions/{connector_id}/completion-monitor.js' in dispatcher.text
    monitor = client.get(f"/connector-sessions/{connector_id}/completion-monitor.js")
    assert monitor.status_code == 200
    assert "aip:ibkr-authenticated" in monitor.text


def test_html_xhr_response_is_not_replaced_with_completion_document() -> None:
    connector_id = uuid4()
    payload = b"<html><body>MFA polling response</body></html>"

    assert _inject_auth_completion_monitor(
        payload, "text/html", connector_id, top_level_document=False
    ) == payload


def test_authentication_completion_monitor_does_not_modify_mfa_xhr_json() -> None:
    connector_id = uuid4()
    payload = b'{"status":"MFA_PENDING"}'

    assert _inject_auth_completion_monitor(
        payload, "application/json", connector_id, top_level_document=False
    ) == payload


def test_browser_auth_status_keeps_popup_pending_before_mfa_approval(monkeypatch) -> None:
    connector_id = uuid4()
    session = runtime.ConnectorSession(connector_id=connector_id, user_id=uuid4(), login_token="opaque")
    manager.sessions[connector_id] = session

    async def pending(path, current_session=None):
        assert path == "/iserver/auth/status"
        return {"authenticated": False, "connected": True}

    monkeypatch.setattr(manager, "_gateway_get", pending)
    response = TestClient(app).get(
        f"/connector-sessions/{connector_id}/auth-status?loginToken={session.login_token}"
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
    assert session.auth_status == ConnectorState.AUTHENTICATION_REQUIRED


def test_browser_auth_status_reports_connected_only_after_mfa_approval(monkeypatch) -> None:
    connector_id = uuid4()
    session = runtime.ConnectorSession(connector_id=connector_id, user_id=uuid4(), login_token="opaque")
    manager.sessions[connector_id] = session

    async def approved(path, current_session=None):
        assert path == "/iserver/auth/status"
        return {"authenticated": True, "connected": True, "established": True}

    monkeypatch.setattr(manager, "_gateway_get", approved)
    response = TestClient(app).get(
        f"/connector-sessions/{connector_id}/auth-status?loginToken={session.login_token}"
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": True}
    assert session.auth_status == ConnectorState.CONNECTED


def test_live_authenticated_flag_completes_second_mfa_before_popup_transport_flags_converge(monkeypatch) -> None:
    connector_id = uuid4()
    previous_auth = datetime(2026, 8, 29, tzinfo=UTC)
    session = runtime.ConnectorSession(
        connector_id=connector_id,
        user_id=uuid4(),
        login_token="opaque",
        authenticated_at=previous_auth,
    )
    manager.sessions[connector_id] = session

    async def second_mfa_approved(path, current_session=None):
        assert path == "/iserver/auth/status"
        return {"authenticated": True, "connected": False, "established": False, "competing": False}

    monkeypatch.setattr(manager, "_gateway_get", second_mfa_approved)
    response = TestClient(app).get(
        f"/connector-sessions/{connector_id}/auth-status?loginToken={session.login_token}"
    )

    assert response.json() == {"authenticated": True}
    assert session.auth_status == ConnectorState.CONNECTED
    assert session.authenticated_at > previous_auth
