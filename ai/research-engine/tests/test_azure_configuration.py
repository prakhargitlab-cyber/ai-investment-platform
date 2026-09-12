from app.settings import Settings


def test_local_profile_loads_without_azure_or_secrets() -> None:
    settings = Settings(_env_file=None, environment="LOCAL")
    assert settings.environment == "LOCAL"
    assert settings.research_database_ssl_mode == "disable"
    assert settings.research_database_password is None
    assert settings.research_distributed_lock_backend == "process"


def test_azure_profile_accepts_secure_external_postgres_without_embedded_secret() -> None:
    settings = Settings(
        _env_file=None,
        environment="AZURE",
        research_database_host="postgres.example.invalid",
        research_database_ssl_mode="verify-full",
        research_database_connect_timeout_seconds=10,
        research_database_statement_timeout_seconds=30,
    )
    assert settings.environment == "AZURE"
    assert settings.research_database_ssl_mode == "verify-full"
    assert settings.research_database_password is None


def test_provider_endpoints_and_lock_backend_are_environment_configurable() -> None:
    settings = Settings(
        _env_file=None,
        yahoo_search_url="https://market.example.invalid/search",
        nse_announcements_url="https://exchange.example.invalid/announcements",
        research_distributed_lock_backend="postgres",
    )
    assert settings.yahoo_search_url == "https://market.example.invalid/search"
    assert settings.nse_announcements_url == "https://exchange.example.invalid/announcements"
    assert settings.research_distributed_lock_backend == "postgres"
