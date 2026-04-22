"""FastAPI router exposing all SDM REST endpoints."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from core.download_manager import DownloadManager

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class StartDownloadRequest(BaseModel):
    """Payload for POST /downloads."""

    url: str = Field(..., description="Remote URL to download.")
    save_path: str = Field(
        default="/mnt/c/Users/user/Downloads",
        description="Local directory to save the file into.",
    )
    segments: int = Field(default=4, ge=1, le=32, description="Number of parallel segments.")
    retries: int = Field(default=3, ge=0, le=10, description="Per-segment retry count.")


class StartDownloadResponse(BaseModel):
    """Response for POST /downloads."""

    id: str
    filename: str
    status: str


# ---------------------------------------------------------------------------
# Dependency — injected DownloadManager
# ---------------------------------------------------------------------------

# The manager is set by main.py at startup and accessed via this closure.
_manager: Optional[DownloadManager] = None


def set_manager(manager: DownloadManager) -> None:
    """Register the application-wide DownloadManager instance.

    Called once from ``main.py`` during the lifespan startup event.

    Args:
        manager: The initialised :class:`~core.download_manager.DownloadManager`.
    """
    global _manager  # noqa: PLW0603
    _manager = manager


def get_manager() -> DownloadManager:
    """FastAPI dependency that returns the shared DownloadManager.

    Returns:
        The application-wide :class:`~core.download_manager.DownloadManager`.

    Raises:
        RuntimeError: If the manager has not been initialised.
    """
    if _manager is None:
        raise RuntimeError("DownloadManager has not been initialised.")
    return _manager


# ---------------------------------------------------------------------------
# Hardcoded test URLs
# ---------------------------------------------------------------------------

_TEST_URLS: list[dict] = [
    {
        "label": "Tiny TGZ — CPython 3.9.0 source (~25 MB, Range: yes)",
        "url": "https://www.python.org/ftp/python/3.9.0/Python-3.9.0.tgz",
    },
    {
        "label": "Medium TGZ — CPython 3.12.0 source (~26 MB, Range: yes)",
        "url": "https://www.python.org/ftp/python/3.12.0/Python-3.12.0.tgz",
    },
    {
        "label": "Medium ZIP — CPython 3.12.0 Windows installer (~27 MB, Range: yes)",
        "url": "https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe",
    },
    {
        "label": "PDF — RFC 7233 Range Requests specification (~50 KB)",
        "url": "https://www.rfc-editor.org/rfc/pdfrfc/rfc7233.txt.pdf",
    },
    {
        "label": "Large ISO — Ubuntu 22.04.4 server (~1.6 GB, Range: yes)",
        "url": "https://releases.ubuntu.com/22.04/ubuntu-22.04.4-live-server-amd64.iso",
    },
]

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/downloads",
    response_model=StartDownloadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new download",
)
def start_download(
    body: StartDownloadRequest,
    manager: DownloadManager = Depends(get_manager),
) -> StartDownloadResponse:
    """Enqueue and begin downloading a remote file.

    Args:
        body: URL, destination folder, segment count, and retry count.
        manager: Injected download manager.

    Returns:
        The new download id, derived filename, and initial status.

    Raises:
        HTTPException 400: If the URL is unreachable or metadata cannot be
            fetched.
    """
    logger.info("POST /downloads url=%s segments=%d", body.url, body.segments)
    try:
        download_id = manager.start_download(
            url=body.url,
            save_path=body.save_path,
            segments=body.segments,
            retries=body.retries,
        )
    except RuntimeError as exc:
        logger.error("start_download failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    state = manager.get_status(download_id)
    return StartDownloadResponse(
        id=download_id,
        filename=state["filename"],  # type: ignore[index]
        status=state["status"],      # type: ignore[index]
    )


@router.get(
    "/downloads",
    summary="List all downloads",
)
def list_downloads(
    manager: DownloadManager = Depends(get_manager),
) -> list[dict]:
    """Return real-time status for all active downloads plus full history.

    Args:
        manager: Injected download manager.

    Returns:
        List of status dicts merged from active state and persistent history.
    """
    active = {s["id"]: s for s in manager.get_all_status()}
    historical = {r["id"]: r for r in manager.get_history()}
    # Merge: active state takes priority over historical record.
    merged = {**historical, **active}
    return list(merged.values())


@router.get(
    "/downloads/{download_id}",
    summary="Get status of a single download",
)
def get_download(
    download_id: str,
    manager: DownloadManager = Depends(get_manager),
) -> dict:
    """Return full status of one download (active or historical).

    Args:
        download_id: UUID of the target download.
        manager: Injected download manager.

    Returns:
        Status dict for the download.

    Raises:
        HTTPException 404: If the download id is not found.
    """
    state = manager.get_status(download_id)
    if state is not None:
        return state
    record = manager.get_history()
    for r in record:
        if r["id"] == download_id:
            return r
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Download {download_id} not found.",
    )


@router.post(
    "/downloads/{download_id}/pause",
    summary="Pause an active download",
)
def pause_download(
    download_id: str,
    manager: DownloadManager = Depends(get_manager),
) -> dict:
    """Pause a running download.

    Args:
        download_id: UUID of the download to pause.
        manager: Injected download manager.

    Returns:
        Confirmation dict.

    Raises:
        HTTPException 404: If the download is not found or not pauseable.
    """
    ok = manager.pause_download(download_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Download {download_id} not found or not in a pauseable state.",
        )
    return {"id": download_id, "status": "paused"}


@router.post(
    "/downloads/{download_id}/resume",
    summary="Resume a paused download",
)
def resume_download(
    download_id: str,
    manager: DownloadManager = Depends(get_manager),
) -> dict:
    """Resume a paused download.

    Args:
        download_id: UUID of the download to resume.
        manager: Injected download manager.

    Returns:
        Confirmation dict.

    Raises:
        HTTPException 404: If the download is not found or not paused.
    """
    ok = manager.resume_download(download_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Download {download_id} not found or not paused.",
        )
    return {"id": download_id, "status": "downloading"}


@router.delete(
    "/downloads/{download_id}",
    summary="Cancel a download",
)
def cancel_download(
    download_id: str,
    manager: DownloadManager = Depends(get_manager),
) -> dict:
    """Cancel and clean up an active or paused download.

    Args:
        download_id: UUID of the download to cancel.
        manager: Injected download manager.

    Returns:
        Confirmation dict.

    Raises:
        HTTPException 404: If the download is not found.
    """
    ok = manager.cancel_download(download_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Download {download_id} not found.",
        )
    return {"id": download_id, "status": "cancelled"}


@router.get(
    "/test-urls",
    summary="List hardcoded reliable test download URLs",
)
def get_test_urls() -> list[dict]:
    """Return a curated list of publicly available files for download testing.

    Each entry contains a human-readable ``label`` and a ``url`` string.
    The frontend displays these as one-click quick-fill buttons in the modal.

    Returns:
        List of ``{"label": str, "url": str}`` dicts.
    """
    return _TEST_URLS


@router.get(
    "/browse-folder",
    summary="Open a native folder picker dialog",
)
def browse_folder() -> dict:
    """Launch a Tkinter folder-picker dialog on the server and return the path.

    Returns:
        ``{"path": "/chosen/directory"}`` or ``{"path": null}`` if cancelled.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        folder = filedialog.askdirectory(title="Select download folder")
        root.destroy()
        return {"path": folder or None}
    except Exception as exc:  # noqa: BLE001
        logger.error("browse-folder dialog failed: %s", exc)
        return {"path": None}
