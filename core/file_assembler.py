"""Assembles ordered segment files into a single output file."""

import logging
import os

logger = logging.getLogger(__name__)


class FileAssembler:
    """Concatenates ordered ``.partN`` temporary files into the final download.

    After a successful assembly all temporary part files are deleted.  If any
    part file is missing the assembly is aborted and ``False`` is returned so
    the caller can mark the download as failed.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(self, part_paths: list[str], output_path: str) -> bool:
        """Concatenate *part_paths* in order and write to *output_path*.

        Args:
            part_paths: Ordered list of absolute paths to ``.partN`` files.
                The files will be concatenated in the list order.
            output_path: Absolute path for the final assembled file.

        Returns:
            ``True`` if all parts were found and successfully merged;
            ``False`` if any part file is missing or an I/O error occurs.
        """
        # Validate that every part file exists before touching the output.
        for path in part_paths:
            if not os.path.exists(path):
                logger.error("Assembly failed: missing part file %s", path)
                return False

        logger.info(
            "Assembling %d parts → %s", len(part_paths), output_path
        )
        try:
            with open(output_path, "wb") as out_fh:
                for path in part_paths:
                    logger.debug("Appending %s", path)
                    with open(path, "rb") as part_fh:
                        while True:
                            chunk = part_fh.read(1024 * 64)  # 64 KB read buffer
                            if not chunk:
                                break
                            out_fh.write(chunk)
        except OSError as exc:
            logger.error("I/O error during assembly: %s", exc)
            return False

        # Clean up part files after a successful merge.
        self._delete_parts(part_paths)
        logger.info("Assembly complete: %s", output_path)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _delete_parts(self, part_paths: list[str]) -> None:
        """Delete temporary part files, logging but not raising on failure.

        Args:
            part_paths: List of file paths to remove.
        """
        for path in part_paths:
            try:
                os.remove(path)
                logger.debug("Deleted part file %s", path)
            except OSError as exc:
                logger.warning("Could not delete part file %s: %s", path, exc)
