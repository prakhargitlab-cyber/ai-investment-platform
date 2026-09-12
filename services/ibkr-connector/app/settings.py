from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    service_name: str = "ibkr-connector"
    aip_environment: str = Field(default="LOCAL", alias="AIP_ENVIRONMENT")
    aip_ibkr_gateway_package_path: str = Field(default="", alias="AIP_IBKR_GATEWAY_PACKAGE_PATH")
    aip_ibkr_gateway_base_url: str = Field(default="", alias="AIP_IBKR_GATEWAY_BASE_URL")
    aip_ibkr_gateway_tls_verify: bool = Field(
        default=True, validation_alias="AIP_IBKR_GATEWAY_TLS_VERIFY"
    )
    aip_ibkr_login_public_base_url: str = Field(default="", alias="AIP_IBKR_LOGIN_PUBLIC_BASE_URL")
    aip_internal_token: str = Field(default="", alias="AIP_INTERNAL_TOKEN")
    aip_connector_require_internal_token: bool = Field(
        default=True, alias="AIP_CONNECTOR_REQUIRE_INTERNAL_TOKEN"
    )
    aip_ibkr_production_approved: bool = Field(default=False, alias="IBKR_PRODUCTION_APPROVED")
    aip_ibkr_heartbeat_timeout_seconds: int = Field(
        default=20, alias="AIP_IBKR_HEARTBEAT_TIMEOUT_SECONDS"
    )

    @property
    def gateway_api_base_url(self) -> str:
        return self.aip_ibkr_gateway_base_url.rstrip("/")

    @property
    def login_public_base_url(self) -> str:
        return self.aip_ibkr_login_public_base_url.rstrip("/")
