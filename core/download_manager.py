"""Central orchestrator for the Simple Download Manager."""

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from config import DEFAULT_DOWNLOAD_DIR, MAX_RETRIES, NUM_SEGMENTS
from core.file_assembler import FileAssembler
from core.segment_worker import SegmentWorker
from core.thread_controller import ThreadController
from persistence.history import DownloadHistory
from utils.http_utils import get_file_info

logger = logging.getLogger(__name__)

# Valid status literals
STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


@dataclass
class DownloadState:
    """In-memory state for a single active or recently finished download."""

    id: str
    url: str
    filename: str
    save_path: str
    total_size: int
    status: str
    segments_count: int
    retries: int

    # Threading primitives
    pause_event: threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    # Progress tracking (lock-protected)
    _bytes_lock: threading.Lock = field(default_factory=threading.Lock)
    _bytes_downloaded: int = 0

    start_time: float = field(default_factory=time.monotonic)
    wall_start: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Computed fields updated by the background thread
    speed: float = 0.0          # bytes per second
    progress_pct: float = 0.0
    eta_seconds: float = 0.0
    error_message: Optional[str] = None

    def add_bytes(self, n: int) -> None:
        """Thread-safe increment of the downloaded byte counter.

        Args:
            n: Number of bytes just received.
        """
        with self._bytes_lock:
            self._bytes_downloaded += n

    @property
    def bytes_downloaded(self) -> int:
        """Return the current byte count in a thread-safe manner."""
        with self._bytes_lock:
            return self._bytes_downloaded


class DownloadManager:
    """Orchestrates the full lifecycle of parallel, segmented downloads.

    A single ``DownloadManager`` instance should be created at application
    startup and shared across all API requests (dependency injection).

    Downloads are stored in :attr:`_downloads` while active and persisted to
    :class:`~persistence.history.DownloadHistory` for durability.
    """

    def __init__(self, history: DownloadHistory) -> None:
        """Initialise the manager.

        Args:
            history: An initialised :class:`~persistence.history.DownloadHistory`
                instance used for persistent storage.
        """
        self._history = history
        self._downloads: dict[str, DownloadState] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API — download control
    # ------------------------------------------------------------------

    def start_download(
        self,
        url: str,
        save_path: str = DEFAULT_DOWNLOAD_DIR,
        segments: int = NUM_SEGMENTS,
        retries: int = MAX_RETRIES,
    ) -> str:
        """Initiate a new segmented download.

        Steps performed:
        1. Issue a HEAD request to determine file size, range support, filename.
        2. Degrade to a single segment when the server does not support ranges
           or the file size is unknown.
        3. Build :class:`~core.segment_worker.SegmentWorker` instances for
           each byte-range partition.
        4. Persist an initial history record.
        5. Launch a background thread that runs the workers, assembles the
           file, and updates the history record on completion.

        Args:
            url: Remote URL to download.
            save_path: Local directory where the file should be saved.
            segments: Desired number of parallel download segments.
            retries: Maximum per-segment retry attempts.

        Returns:
            The unique download identifier (UUID4 string).

        Raises:
            RuntimeError: If file metadata cannot be retrieved.
        """
        logger.info("Starting download: %s", url)

        try:
            file_info = get_file_info(url)
        except Exception as exc:
            raise RuntimeError(f"Failed to retrieve file info for {url}: {exc}") from exc

        filename: str = file_info["filename"]
        total_size: int = file_info["total_size"]
        accepts_ranges: bool = file_info["accepts_ranges"]

        # Degrade to single-segment when range requests are unsupported.
        effective_segments = segments
        if not accepts_ranges or total_size == 0:
            logger.info(
                "Range not supported or size unknown — using 1 segment for %s", url
            )
            effective_segments = 1

        download_id = str(uuid.uuid4())
        os.makedirs(save_path, exist_ok=True)
        output_path = os.path.join(save_path, filename)

        state = DownloadState(
            id=download_id,
            url=url,
            filename=filename,
            save_path=output_path,
            total_size=total_size,
            status=STATUS_DOWNLOADING,
            segments_count=effective_segments,
            retries=retries,
        )
        # pause_event starts *set* so workers run immediately.
        state.pause_event.set()

        with self._lock:
            self._downloads[download_id] = state

        # Compute byte ranges.
        byte_ranges = self._compute_ranges(total_size, effective_segments)

        # Build temporary part file paths.
        part_paths = [
            f"{output_path}.part{i}" for i in range(effective_segments)
        ]

        # Build workers.
        workers = [
            SegmentWorker(
                segment_id=i,
                url=url,
                start=byte_ranges[i][0],
                end=byte_ranges[i][1],
                temp_path=part_paths[i],
                max_retries=retries,
                pause_event=state.pause_event,
                cancel_event=state.cancel_event,
                progress_callback=state.add_bytes,
            )
            for i in range(effective_segments)
        ]

        # Persist initial record.
        self._history.create({
            "id": download_id,
            "url": url,
            "filename": filename,
            "save_path": output_path,
            "total_size": total_size,
            "status": STATUS_DOWNLOADING,
            "segments": effective_segments,
            "retries": retries,
            "start_time": state.wall_start,
            "end_time": None,
            "avg_speed": None,
            "error_message": None,
        })

        # Launch background orchestration thread.
        bg_thread = threading.Thread(
            target=self._run_download,
            args=(state, workers, part_paths, output_path),
            daemon=True,
            name=f"sdm-{download_id[:8]}",
        )
        bg_thread.start()

        logger.info("Download %s started in background thread.", download_id)
        return download_id

    def pause_download(self, download_id: str) -> bool:
        """Pause an active download by clearing its pause event.

        Args:
            download_id: The unique download identifier.

        Returns:
            ``True`` if the download was found and paused, ``False`` otherwise.
        """
        state = self._get_state(download_id)
        if state is None or state.status != STATUS_DOWNLOADING:
            logger.warning("pause_download: id=%s not found or not downloading.", download_id)
            return False
        state.pause_event.clear()
        state.status = STATUS_PAUSED
        self._history.update(download_id, status=STATUS_PAUSED)
        logger.info("Download %s paused.", download_id)
        return True

    def resume_download(self, download_id: str) -> bool:
        """Resume a paused download by setting its pause event.

        Args:
            download_id: The unique download identifier.

        Returns:
            ``True`` if the download was found and resumed, ``False`` otherwise.
        """
        state = self._get_state(download_id)
        if state is None or state.status != STATUS_PAUSED:
            logger.warning("resume_download: id=%s not found or not paused.", download_id)
            return False
        state.status = STATUS_DOWNLOADING
        state.pause_event.set()
        self._history.update(download_id, status=STATUS_DOWNLOADING)
        logger.info("Download %s resumed.", download_id)
        return True

    def cancel_download(self, download_id: str) -> bool:
        """Cancel an active or paused download.

        Sets the cancel event and, if paused, also sets the pause event so
        workers are unblocked and can observe the cancellation flag.

        Args:
            download_id: The unique download identifier.

        Returns:
            ``True`` if the download was found and cancelled, ``False`` otherwise.
        """
        state = self._get_state(download_id)
        if state is None:
            logger.warning("cancel_download: id=%s not found.", download_id)
            return False
        state.cancel_event.set()
        # Unblock paused workers so they can exit.
        state.pause_event.set()
        state.status = STATUS_CANCELLED
        self._history.update(download_id, status=STATUS_CANCELLED)
        logger.info("Download %s cancelled.", download_id)
        return True

    def get_status(self, download_id: str) -> Optional[dict[str, Any]]:
        """Return the real-time status of a download.

        Speed and ETA are recomputed on each call from current byte counters.

        Args:
            download_id: The unique download identifier.

        Returns:
            A dict with current state fields, or ``None`` if the id is unknown.
        """
        state = self._get_state(download_id)
        if state is None:
            return None
        return self._build_status_dict(state)

    def get_all_status(self) -> list[dict[str, Any]]:
        """Return real-time status dicts for every tracked download.

        Returns:
            A list of status dicts (one per download, newest first).
        """
        with self._lock:
            states = list(self._downloads.values())
        return [self._build_status_dict(s) for s in states]

    def get_history(self) -> list[dict[str, Any]]:
        """Return all records from persistent storage.

        Returns:
            List of history record dicts from the database.
        """
        return self._history.get_all()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_state(self, download_id: str) -> Optional[DownloadState]:
        with self._lock:
            return self._downloads.get(download_id)

    def _compute_ranges(self, total_size: int, segments: int) -> list[tuple[int, int]]:
        """Divide *total_size* bytes into *segments* non-overlapping ranges.

        Args:
            total_size: Total file size in bytes.
            segments: Number of parts to split into.

        Returns:
            List of ``(start, end)`` tuples (both inclusive).
        """
        if total_size == 0 or segments == 1:
            return [(0, max(total_size - 1, 0))]

        part_size = total_size // segments
        ranges: list[tuple[int, int]] = []
        for i in range(segments):
            start = i * part_size
            end = (start + part_size - 1) if i < segments - 1 else total_size - 1
            ranges.append((start, end))
        return ranges

    def _build_status_dict(self, state: DownloadState) -> dict[str, Any]:
        """Compute a status snapshot from *state*.

        Args:
            state: The :class:`DownloadState` to summarise.

        Returns:
            A JSON-serialisable dict.
        """
        elapsed = time.monotonic() - state.start_time
        downloaded = state.bytes_downloaded

        speed = downloaded / elapsed if elapsed > 0 else 0.0
        remaining = state.total_size - downloaded if state.total_size > 0 else 0
        eta = remaining / speed if speed > 0 else 0.0
        progress = (
            (downloaded / state.total_size * 100) if state.total_size > 0 else 0.0
        )

        return {
            "id": state.id,
            "url": state.url,
            "filename": state.filename,
            "save_path": state.save_path,
            "total_size": state.total_size,
            "bytes_downloaded": downloaded,
            "status": state.status,
            "segments_count": state.segments_count,
            "retries": state.retries,
            "progress_pct": round(progress, 2),
            "speed_bps": round(speed, 2),
            "eta_seconds": round(eta, 1),
            "start_time": state.wall_start,
            "error_message": state.error_message,
        }

    def _run_download(
        self,
        state: DownloadState,
        workers: list[SegmentWorker],
        part_paths: list[str],
        output_path: str,
    ) -> None:
        """Background thread target: run workers, assemble, update history.

        Args:
            state: Shared mutable state for this download.
            workers: List of segment workers to execute.
            part_paths: Ordered list of temporary part file paths.
            output_path: Final assembled file path.
        """
        controller = ThreadController(max_workers=len(workers))
        try:
            results = controller.run_segments(workers)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ThreadController raised for download %s: %s", state.id, exc)
            results = [False] * len(workers)

        # If already cancelled, stop here.
        if state.cancel_event.is_set():
            logger.info("Download %s was cancelled — skipping assembly.", state.id)
            self._cleanup_parts(part_paths)
            return

        all_ok = all(results)
        if all_ok:
            assembler = FileAssembler()
            assembled = assembler.assemble(part_paths, output_path)
        else:
            assembled = False
            self._cleanup_parts(part_paths)

        elapsed = time.monotonic() - state.start_time
        avg_speed = state.bytes_downloaded / elapsed if elapsed > 0 else 0.0
        end_time = datetime.now(timezone.utc).isoformat()

        if assembled:
            state.status = STATUS_COMPLETED
            logger.info("Download %s completed → %s", state.id, output_path)
        else:
            state.status = STATUS_FAILED
            state.error_message = "One or more segments failed or assembly error."
            logger.error("Download %s FAILED.", state.id)

        self._history.update(
            state.id,
            status=state.status,
            end_time=end_time,
            avg_speed=round(avg_speed, 2),
            error_message=state.error_message,
        )

    def _cleanup_parts(self, part_paths: list[str]) -> None:
        """Remove leftover part files silently.

        Args:
            part_paths: Paths to attempt deletion on.
        """
        for path in part_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as exc:
                logger.warning("Could not remove part file %s: %s", path, exc)
