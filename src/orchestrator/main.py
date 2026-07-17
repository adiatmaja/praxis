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
from orchestrator.core.build_info import build_stamp
from orchestrator.core.effective_settings import EffectiveSettings
from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import (
    PatCredentialProvider,
    build_credential_provider,
)
from orchestrator.core.llm_router import LLMRouter
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

    settings = Settings()
    # Build ONE credential provider and thread it through every component.
    # When no credentials are configured at all (local dev / tests), fall back
    # to an empty-token PAT provider so startup does not raise CredentialError.
    _has_git_creds = bool(
        settings.github_token
        or (settings.github_app_id and settings.github_app_private_key)
    )
    credential_provider = (
        build_credential_provider(settings)
        if _has_git_creds
        else PatCredentialProvider("")
    )
    database = Database(settings.database_url)

    db_path = Path(database.db_path)
    if database.db_path != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    # Construct BrainstormManager before db.initialize() so the before_drop
    # backfill closure can reference it.  BrainstormManager has no dependency
    # on the database, so this reorder is safe.
    from orchestrator.core.backfill import backfill_legacy_specs
    from orchestrator.core.brainstorm import BrainstormManager

    _brainstorm_pre = BrainstormManager(
        workspace_base=settings.brainstorm_workspace,
        event_bus=None,  # EventBus not yet created; backfill doesn't need events
        credentials=credential_provider,
    )

    async def _before_drop() -> None:
        await backfill_legacy_specs(database, _brainstorm_pre)

    # Only backfill when any GitHub credential is configured (PAT or App).
    _before_drop_cb = _before_drop if _has_git_creds else None

    await database.initialize(before_drop=_before_drop_cb)

    # Seed default user for single-token auth (v1)
    existing = await database.fetch_one("SELECT id FROM users LIMIT 1")
    if existing is None:
        import hashlib

        token_to_store = settings.auth_token.get_secret_value()
        if settings.auth_token_hashed:
            token_to_store = hashlib.sha256(token_to_store.encode()).hexdigest()
        await database.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("default", "admin", token_to_store),
        )
        logger.info("Seeded default admin user")

    app.state.db = database
    app.state.settings = settings
    # Resolve the internal callback secret once at startup.
    # Precedence: explicit INTERNAL_CALLBACK_SECRET env var > derived from AUTH_TOKEN.
    app.state.internal_callback_secret = (
        settings.internal_callback_secret or settings.auth_token.get_secret_value()
    )
    effective_settings = EffectiveSettings(settings, database)
    app.state.effective_settings = effective_settings
    app.state.task_queue = TaskQueue(database)
    router = LLMRouter(
        resolve=effective_settings.call_site_config,
        lm_studio_url=settings.lm_studio_url,
    )
    app.state.llm_router = router
    app.state.opus_bridge = OpusBridge(
        database,
        default_model=settings.agent_model,
        default_effort=settings.agent_model_effort,
        effective_settings=effective_settings,
        router=router,
    )
    git_ops = GitOps(credential_provider)
    app.state.git_ops = git_ops
    app.state.event_bus = EventBus()
    try:
        app.state.agent_manager = AgentManager(
            lm_studio_url=settings.lm_studio_url,
            credentials=credential_provider,
            effective_settings=effective_settings,
            git_author_name=settings.git_author_name,
            git_author_email=settings.git_author_email,
            gemini_creds_volume=settings.gemini_creds_volume,
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
        callback_url=settings.callback_url(),
        callback_token=app.state.internal_callback_secret,
        effective_settings=effective_settings,
        llm_router=app.state.llm_router,
    )
    app.state.brainstorm = BrainstormManager(
        workspace_base=settings.brainstorm_workspace,
        event_bus=app.state.event_bus,
        credentials=credential_provider,
    )

    from orchestrator.core.context_sync import ContextSync

    app.state.context_sync = ContextSync(
        workspace_base=settings.brainstorm_workspace,
        credentials=credential_provider,
        memory_md_path=settings.memory_md_path,
    )

    from orchestrator.core.doc_indexer import DocIndexer

    app.state.doc_indexer = DocIndexer(
        db=database,
        docs_root=settings.docs_root,
        classify=app.state.opus_bridge.classify_doc,
    )
    app.state.orchestrator._doc_indexer = app.state.doc_indexer
    app.state.orchestrator._context_sync = app.state.context_sync
    try:
        await app.state.doc_indexer.scan()
    except Exception as exc:  # noqa: BLE001 - non-fatal at startup
        logger.warning("Initial doc scan failed: %s", exc)

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
        await app.state.orchestrator.shutdown()
        await database.close()
        logger.info("Application shutdown complete")


app = FastAPI(title="AI Agent Orchestrator", version="0.1.0", lifespan=lifespan)

from orchestrator.api.context import router as context_router  # noqa: E402
from orchestrator.api.dispatch import router as dispatch_router  # noqa: E402
from orchestrator.api.docs import router as docs_router  # noqa: E402
from orchestrator.api.events import router as events_router  # noqa: E402
from orchestrator.api.execute_plan import router as execute_plan_router  # noqa: E402
from orchestrator.api.git_state import router as git_state_router  # noqa: E402
from orchestrator.api.harnesses import router as harnesses_router  # noqa: E402
from orchestrator.api.internal import router as internal_router  # noqa: E402
from orchestrator.api.lifecycle import router as lifecycle_router  # noqa: E402
from orchestrator.api.plans import router as plans_router  # noqa: E402
from orchestrator.api.projects import router as projects_router  # noqa: E402
from orchestrator.api.settings import router as settings_router  # noqa: E402
from orchestrator.api.specs import router as specs_router  # noqa: E402
from orchestrator.api.system import router as system_router  # noqa: E402
from orchestrator.api.tasks import router as tasks_router  # noqa: E402


app.include_router(context_router)
app.include_router(docs_router)
app.include_router(specs_router)
app.include_router(projects_router, prefix="/api")
app.include_router(plans_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(system_router, prefix="/api")
app.include_router(internal_router, prefix="/api/internal")
app.include_router(events_router, prefix="/api")
app.include_router(harnesses_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(lifecycle_router, prefix="/api")
app.include_router(dispatch_router, prefix="/api")
app.include_router(execute_plan_router, prefix="/api")
app.include_router(git_state_router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, object]:
    """Healthcheck endpoint (includes the running build stamp)."""

    return {"status": "ok", "build": build_stamp()}


web_dir = Path(__file__).resolve().parents[2] / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
