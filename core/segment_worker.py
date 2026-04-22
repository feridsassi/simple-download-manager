"""Worker that downloads a single byte-range segment of a file."""

import logging
import time
import threading
from typing import Callable, Optional

import requests

from config import CHUNK_SIZE, REQUEST_HEADERS, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


class SegmentWorker:
    """Downloads one contiguous byte-range segment using HTTP Range requests.

    Each instance is designed to run inside its own thread (submitted via
    :class:`~core.thread_controller.ThreadController`).  Progress is reported
    back to the orchestrator through a callback so the central download state
    can be updated without locking across threads.
    """

    def __init__(
        self,
        segment_id: int,
        url: str,
        start: int,
        end: int,
        temp_path: str,
        max_retries: int,
        pause_event: threading.Event,
        cancel_event: threading.Event,
        progress_callback: Callable[[int], None],
    ) -> None:
        """Initialise a segment worker.

        Args:
            segment_id: Zero-based index identifying this segment.
            url: Remote URL to download from.
            start: First byte offset (inclusive) for the Range request.
            end: Last byte offset (inclusive) for the Range request.
            temp_path: Local file path where this segment will be written
                (e.g. ``/tmp/file.mp4.part0``).
            max_retries: Maximum number of retry attempts on transient errors.
            pause_event: When this event is *cleared*, the worker blocks until
                it is set again (pause/resume mechanism).
            cancel_event: When this event is set the worker stops immediately.
            progress_callback: Callable invoked with the number of bytes just
                read so the caller can accumulate total progress.
        """
        self.segment_id = segment_id
        self.url = url
        self.start = start
        self.end = end
        self.temp_path = temp_path
        self.max_retries = max_retries
        self.pause_event = pause_event
        self.cancel_event = cancel_event
        self.progress_callback = progress_callback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def download(self) -> bool:
        """Execute the segment download with retry logic.

        The method streams bytes from *start* to *end* using an HTTP
        ``Range`` header, writes them to :attr:`temp_path`, and honours
        :attr:`pause_event` and :attr:`cancel_event` between each chunk.

        Retry behaviour uses exponential back-off: wait ``2 ** attempt``
        seconds before each retry (i.e. 1 s, 2 s, 4 s for ``max_retries=3``).

        Returns:
            ``True`` if the segment was downloaded successfully, ``False`` if
            all retries were exhausted or the download was cancelled.
        """
        for attempt in range(self.max_retries + 1):
            if self.cancel_event.is_set():
                logger.info("Segment %d cancelled before attempt %d.", self.segment_id, attempt)
                return False

            if attempt > 0:
                backoff = 2 ** (attempt - 1)   # 1, 2, 4 …
                logger.warning(
                    "Segment %d retry %d/%d — waiting %ds.",
                    self.segment_id, attempt, self.max_retries, backoff,
                )
                time.sleep(backoff)

            try:
                success = self._attempt_download()
                if success:
                    return True
            except requests.RequestException as exc:
                logger.error(
                    "Segment %d network error on attempt %d: %s",
                    self.segment_id, attempt, exc,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Segment %d unexpected error on attempt %d: %s",
                    self.segment_id, attempt, exc,
                )

        logger.error(
            "Segment %d failed after %d attempts.", self.segment_id, self.max_retries + 1
        )
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _attempt_download(self) -> bool:
        """Perform a single download attempt for this segment.

        Returns:
            ``True`` on success; raises on network errors so the caller can
            implement retry logic.
        """
        headers = {**REQUEST_HEADERS, "Range": f"bytes={self.start}-{self.end}"}
        logger.debug(
            "Segment %d: GET %s Range: bytes=%d-%d",
            self.segment_id, self.url, self.start, self.end,
        )

        with requests.get(
            self.url,
            headers=headers,
            stream=True,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            response.raise_for_status()

            with open(self.temp_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    # Honour pause — blocks until resumed.
                    self.pause_event.wait()

                    # Honour cancel.
                    if self.cancel_event.is_set():
                        logger.info(
                            "Segment %d: cancel detected mid-download.", self.segment_id
                        )
                        return False

                    if chunk:
                        fh.write(chunk)
                        self.progress_callback(len(chunk))

        logger.info("Segment %d: download complete → %s", self.segment_id, self.temp_path)
        return True
