"""Thread-pool wrapper that drives concurrent segment downloads."""

import logging
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Optional

from core.segment_worker import SegmentWorker

logger = logging.getLogger(__name__)


class ThreadController:
    """Manages a :class:`~concurrent.futures.ThreadPoolExecutor` that runs
    :class:`~core.segment_worker.SegmentWorker` instances concurrently.

    One ``ThreadController`` is created per download so each download gets
    its own isolated pool.  The pool is shut down automatically after
    :py:meth:`run_segments` returns.
    """

    def __init__(self, max_workers: Optional[int] = None) -> None:
        """Initialise the controller.

        Args:
            max_workers: Maximum threads in the pool.  Defaults to the number
                of segments submitted (one thread per segment).
        """
        self._max_workers = max_workers
        self._executor: Optional[ThreadPoolExecutor] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_segments(self, workers: list[SegmentWorker]) -> list[bool]:
        """Submit all segment workers to the thread pool and collect results.

        Each worker's :py:meth:`~core.segment_worker.SegmentWorker.download`
        method is submitted as a separate future.  The method blocks until
        every segment has either succeeded or exhausted its retries.

        Args:
            workers: Ordered list of :class:`~core.segment_worker.SegmentWorker`
                instances (one per byte-range segment).

        Returns:
            A list of booleans, one per worker in the same order, where
            ``True`` indicates that the segment downloaded successfully.
        """
        n = len(workers)
        effective_workers = self._max_workers if self._max_workers is not None else n
        logger.info(
            "Starting ThreadPoolExecutor with %d threads for %d segments.",
            effective_workers, n,
        )

        results: list[bool] = [False] * n
        future_to_index: dict[Future, int] = {}

        self._executor = ThreadPoolExecutor(max_workers=effective_workers)
        try:
            for idx, worker in enumerate(workers):
                future = self._executor.submit(worker.download)
                future_to_index[future] = idx

            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Unhandled exception in segment %d future: %s", idx, exc
                    )
                    results[idx] = False
        finally:
            self.shutdown()

        logger.info("All segments finished. Results: %s", results)
        return results

    def shutdown(self) -> None:
        """Shut down the underlying thread pool, waiting for running tasks.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._executor is not None:
            logger.debug("Shutting down ThreadPoolExecutor.")
            self._executor.shutdown(wait=True)
            self._executor = None
