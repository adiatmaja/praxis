"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from orchestrator.config import Settings
from orchestrator.database import Database


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize and teardown shared application resources."""

    settings = Settings()  # type: ignore[call-arg]
    database = Database(settings.database_url)

    db_path = Path(database.db_path)
    if database.db_path != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    await database.initialize()
    app.state.db = database
    app.state.settings = settings
    logger.info("Application startup complete")

    try:
        yield
    finally:
        await database.close()
        logger.info("Application shutdown complete")


app = FastAPI(title="AI Agent Orchestrator", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Healthcheck endpoint."""

    return {"status": "ok"}
