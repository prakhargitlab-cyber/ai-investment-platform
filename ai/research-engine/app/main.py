from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query

from app.models import ReliabilityLevel, ResearchEventType
from app.repository import ResearchRepository
from app.scheduler import default_schedule_rules
from app.settings import Settings
from app.sources import default_source_providers

settings = Settings()
repository = ResearchRepository()
app = FastAPI(title="Research Engine", version="0.3.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/providers/llm")
def llm_provider() -> dict[str, str]:
    return {"provider": settings.llm_provider, "mode": "optional-not-called"}


@app.get("/api/v1/research/sources")
def sources():
    return default_source_providers()


@app.get("/api/v1/research/schedule")
def schedule():
    return default_schedule_rules()


@app.get("/api/v1/research/companies")
def companies():
    return repository.list_profiles()


@app.get("/api/v1/research/companies/{instrument_id}")
def company(instrument_id: UUID):
    try:
        return repository.profile(instrument_id)
    except StopIteration as exc:
        raise HTTPException(status_code=404, detail="Research profile not found") from exc


@app.get("/api/v1/research/companies/{instrument_id}/events")
def events(
    instrument_id: UUID,
    event_type: ResearchEventType | None = Query(default=None, alias="eventType"),
    impact: str | None = None,
    reliability: ReliabilityLevel | None = None,
):
    _require_profile(instrument_id)
    return repository.events_for(instrument_id, event_type=event_type, impact=impact, reliability=reliability)


@app.get("/api/v1/research/companies/{instrument_id}/documents")
def documents(instrument_id: UUID):
    _require_profile(instrument_id)
    return repository.documents_for(instrument_id)


@app.get("/api/v1/research/companies/{instrument_id}/summary")
def summary(instrument_id: UUID):
    _require_profile(instrument_id)
    return repository.summary(instrument_id)


@app.post("/api/v1/research/companies/{instrument_id}/refresh")
def refresh(instrument_id: UUID, x_correlation_id: str | None = Header(default=None)):
    _require_profile(instrument_id)
    # Phase 3 refresh is fixture-backed unless AIP_RESEARCH_LIVE_ENABLED is explicitly enabled later.
    result = repository.refresh(instrument_id)
    return result.model_copy(update={"data_freshness": "DEMO" if settings.research_demo_enabled else "UNAVAILABLE"})


def _require_profile(instrument_id: UUID) -> None:
    try:
        repository.profile(instrument_id)
    except StopIteration as exc:
        raise HTTPException(status_code=404, detail="Research profile not found") from exc
