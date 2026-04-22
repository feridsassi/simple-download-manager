"""FastAPI application entry point for the Simple Download Manager."""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.routes import router, set_manager
from core.download_manager import DownloadManager
from persistence.history import DownloadHistory

# ---------------------------------------------------------------------------
# Logging — configure once at the top level
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — startup & shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown resources.

    On startup:
    - Initialises the SQLite history database.
    - Creates and registers the shared :class:`~core.download_manager.DownloadManager`.

    Args:
        app: The FastAPI application instance.
    """
    logger.info("SDM starting up …")
    history = DownloadHistory()
    history.init_db()

    manager = DownloadManager(history=history)
    set_manager(manager)

    logger.info("DownloadManager ready.")
    yield
    logger.info("SDM shutting down.")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Construct and configure the FastAPI application.

    Returns:
        A fully wired :class:`fastapi.FastAPI` instance.
    """
    app = FastAPI(
        title="Simple Download Manager",
        description="Multithreaded segmented download manager — MedTech Distributed Systems.",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.include_router(router, tags=["downloads"])

    # Serve the frontend if it exists; skip gracefully during backend-only runs.
    ui_dir = os.path.join(os.path.dirname(__file__), "ui")
    if os.path.isdir(ui_dir):
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
        logger.info("Serving static UI from %s", ui_dir)

    return app


app = create_app()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
