"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from orchestrator.config import Settings
from orchestrator.core.agent_manager import AgentManager
from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_ops import GitOps
from orchestrator.core.opus_bridge import OpusBridge
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
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
    app.state.task_queue = TaskQueue(database)
    app.state.opus_bridge = OpusBridge(database)
    git_ops = GitOps(settings.github_token)
    app.state.git_ops = git_ops
    app.state.event_bus = EventBus()
    try:
        app.state.agent_manager = AgentManager(
            lm_studio_url=settings.lm_studio_url,
            github_token=settings.github_token,
        )
    except Exception as exc:
        logger.warning("Agent manager unavailable during startup: %s", exc)
        app.state.agent_manager = None
    app.state.orchestrator = Orchestrator(
        task_queue=app.state.task_queue,
        agent_manager=app.state.agent_manager,
        opus_bridge=app.state.opus_bridge,
        git_ops=git_ops,
        event_bus=app.state.event_bus,
    )
    app.state.orchestration_stop_event = asyncio.Event()
    app.state.orchestration_task = asyncio.create_task(
        app.state.orchestrator.run_loop(app.state.orchestration_stop_event)
    )
    logger.info("Application startup complete")

    try:
        yield
    finally:
        app.state.orchestration_stop_event.set()
        await app.state.orchestration_task
        await database.close()
        logger.info("Application shutdown complete")


app = FastAPI(title="AI Agent Orchestrator", version="0.1.0", lifespan=lifespan)

from orchestrator.api.events import router as events_router  # noqa: E402
from orchestrator.api.internal import router as internal_router  # noqa: E402
from orchestrator.api.plans import router as plans_router  # noqa: E402
from orchestrator.api.projects import router as projects_router  # noqa: E402
from orchestrator.api.system import router as system_router  # noqa: E402
from orchestrator.api.tasks import router as tasks_router  # noqa: E402


app.include_router(projects_router, prefix="/api")
app.include_router(plans_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(system_router, prefix="/api")
app.include_router(internal_router, prefix="/api/internal")
app.include_router(events_router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    """Healthcheck endpoint."""

    return {"status": "ok"}


web_dir = Path(__file__).resolve().parents[2] / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
