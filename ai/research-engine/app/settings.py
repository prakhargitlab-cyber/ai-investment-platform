from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIP_", env_file=".env", extra="ignore")

    service_name: str = "research-engine"
    environment: str = "DEV"
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://ollama:11434"
    research_live_enabled: bool = False
    research_demo_enabled: bool = True
    research_user_agent: str = "AIInvestmentResearchBot/0.1 contact=research-compliance@example.invalid"
    research_request_timeout_seconds: float = 10.0
    research_connect_timeout_seconds: float = 3.0
    research_max_content_bytes: int = 1_500_000
    research_max_redirects: int = 5
    research_max_retries: int = 2
    research_playwright_enabled: bool = False
    research_playwright_concurrency: int = 1
