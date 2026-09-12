from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ConnectorState(StrEnum):
    STARTING = "STARTING"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    AUTHENTICATING = "AUTHENTICATING"
    CONNECTED = "CONNECTED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class CreateConnectorRequest(BaseModel):
    connector_id: UUID = Field(alias="connectorId")
    user_id: UUID = Field(alias="userId")


class ConnectorStatusResponse(BaseModel):
    connector_id: UUID = Field(alias="connectorId")
    user_id: UUID = Field(alias="userId")
    runtime_status: ConnectorState = Field(alias="runtimeStatus")
    auth_status: ConnectorState = Field(alias="authStatus")
    heartbeat_at: str | None = Field(default=None, alias="heartbeatAt")
    authenticated_at: str | None = Field(default=None, alias="authenticatedAt")
    login_url: str | None = Field(default=None, alias="loginUrl")
    code: str
    message: str


class GatewayPayload(BaseModel):
    data: Any
