"""FastAPI entry point. Phase 2: health check only (bot + webhooks later)."""
from fastapi import FastAPI

from app.config import settings
from app.whatsapp.webhook import router as whatsapp_router

app = FastAPI(title="Medical Rep Management System")
app.include_router(whatsapp_router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "env": settings.app_env,
        "data_dir": str(settings.resolved_data_dir),
    }
