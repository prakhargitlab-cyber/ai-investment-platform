from fastapi import FastAPI

from app.settings import Settings

settings = Settings()
app = FastAPI(title="Valuation Engine", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}
