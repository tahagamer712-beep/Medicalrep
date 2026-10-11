"""FastAPI entry point. Phase 2: health check only (bot + webhooks later)."""
from fastapi import FastAPI

from app.config import settings

app = FastAPI(title="Medical Rep Management System")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "env": settings.app_env,
        "data_dir": str(settings.resolved_data_dir),
    }
