"""HTTP utility helpers for the Simple Download Manager."""

import logging
import os
import time
from urllib.parse import urlparse, unquote

import requests

from config import REQUEST_HEADERS, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# Maps Content-Type values to file extensions when none can be inferred from the URL.
_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "text/plain": ".txt",
    "application/octet-stream": ".bin",
}


def _extract_filename(headers: dict, url: str) -> str:
    """Derive a safe filename from response headers or the URL.

    Priority:
    1. ``Content-Disposition`` header (``filename*=`` then ``filename=``).
    2. Last path segment of the URL (URL-decoded).
    3. Extension appended from ``Content-Type`` when the candidate has none.
    4. Timestamp-based fallback.

    Args:
        headers: HTTP response headers as a case-insensitive dict.
        url: The original request URL.

    Returns:
        A non-empty filename string.
    """
    filename: str = ""

    # --- 1. Content-Disposition ---
    cd: str = headers.get("Content-Disposition", "")
    if cd:
        # RFC 5987 encoded: filename*=UTF-8''encoded%20name
        for part in cd.split(";"):
            part = part.strip()
            if part.lower().startswith("filename*="):
                value = part[len("filename*="):]
                if value.upper().startswith("UTF-8''"):
                    value = value[7:]
                filename = unquote(value).strip().strip('"')
                break
            if part.lower().startswith("filename="):
                filename = part[len("filename="):].strip().strip('"')
                # Don't break — a later filename*= should win.

    # --- 2. URL path ---
    if not filename:
        path_part = urlparse(url).path
        basename = os.path.basename(unquote(path_part))
        if basename:
            filename = basename

    # --- 3. Extension from Content-Type ---
    if filename and not os.path.splitext(filename)[1]:
        raw_ct: str = headers.get("Content-Type", "")
        mime = raw_ct.split(";")[0].strip().lower()
        ext = _CONTENT_TYPE_EXT.get(mime, "")
        if ext:
            filename = filename + ext

    # --- 4. Timestamp fallback ---
    if not filename:
        filename = f"download_{int(time.time())}"

    return filename


def get_file_info(url: str) -> dict:
    """Retrieve metadata about a remote file via an HTTP HEAD request.

    Args:
        url: The URL of the resource to inspect.

    Returns:
        A dict with keys:
            - ``total_size`` (int): Content-Length in bytes; 0 if unknown.
            - ``accepts_ranges`` (bool): Whether the server supports Range requests.
            - ``filename`` (str): Best-guess filename for the download.
            - ``content_type`` (str): Value of the Content-Type header.

    Raises:
        requests.RequestException: If both HEAD and GET fallback fail.
    """
    _SEP = "-" * 64
    logger.info(_SEP)
    logger.info("get_file_info() → %s", url)

    response: requests.Response | None = None

    # ── Attempt 1: HEAD ──────────────────────────────────────────────
    logger.info("[HEAD] Sending request (timeout=%ds, allow_redirects=True)", REQUEST_TIMEOUT)
    try:
        response = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                                  headers=REQUEST_HEADERS)

        logger.info("[HEAD] Status : HTTP %d %s", response.status_code, response.reason)
        logger.info("[HEAD] Final URL (after redirects): %s", response.url)
        logger.info("[HEAD] Response headers (%d):", len(response.headers))
        for k, v in response.headers.items():
            logger.info("[HEAD]   %-30s %s", k + ":", v)

        response.raise_for_status()
        logger.info("[HEAD] Success — using HEAD headers.")

    except requests.HTTPError as exc:
        logger.warning("[HEAD] HTTP error %s — will fall back to GET.", exc)
        response = None
    except requests.ConnectionError as exc:
        logger.warning("[HEAD] Connection error: %s — will fall back to GET.", exc)
        response = None
    except requests.Timeout:
        logger.warning("[HEAD] Timed out after %ds — will fall back to GET.", REQUEST_TIMEOUT)
        response = None
    except requests.RequestException as exc:
        logger.warning("[HEAD] Failed (%s: %s) — will fall back to GET.", type(exc).__name__, exc)
        response = None

    # ── Attempt 2: GET fallback (headers only, body closed immediately) ──
    if response is None:
        logger.info("[GET ] Falling back to streaming GET (stream=True, allow_redirects=True)")
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True,
                                    headers=REQUEST_HEADERS)

            logger.info("[GET ] Status : HTTP %d %s", response.status_code, response.reason)
            logger.info("[GET ] Final URL (after redirects): %s", response.url)
            logger.info("[GET ] Response headers (%d):", len(response.headers))
            for k, v in response.headers.items():
                logger.info("[GET ]   %-30s %s", k + ":", v)

            # Close before raise_for_status — status_code is already cached.
            response.close()
            response.raise_for_status()
            logger.info("[GET ] Success — using GET headers.")

        except requests.RequestException as exc:
            logger.error("[GET ] Also failed: %s: %s", type(exc).__name__, exc)
            raise

    # ── Parse relevant headers ───────────────────────────────────────
    headers = response.headers

    total_size_raw = headers.get("Content-Length", "")
    logger.info("[PARSE] Content-Length raw   : %r", total_size_raw)
    try:
        total_size = int(total_size_raw) if total_size_raw.strip() else 0
    except ValueError:
        logger.warning("[PARSE] Cannot parse Content-Length %r as int — defaulting to 0.", total_size_raw)
        total_size = 0

    ar_raw = headers.get("Accept-Ranges", "")
    accepts_ranges: bool = ar_raw.strip().lower() == "bytes"
    logger.info("[PARSE] Accept-Ranges raw    : %r → accepts_ranges=%s", ar_raw, accepts_ranges)

    content_type: str = headers.get("Content-Type", "")
    logger.info("[PARSE] Content-Type         : %r", content_type)

    filename: str = _extract_filename(headers, url)
    logger.info("[PARSE] Extracted filename   : %r", filename)

    info = {
        "total_size": total_size,
        "accepts_ranges": accepts_ranges,
        "filename": filename,
        "content_type": content_type,
    }
    logger.info("[RESULT] %s", info)
    logger.info(_SEP)
    return info


def check_range_support(url: str) -> bool:
    """Check whether the server at *url* honours HTTP Range requests.

    Args:
        url: The URL to probe.

    Returns:
        ``True`` if ``Accept-Ranges: bytes`` is present in the response
        headers, ``False`` otherwise.
    """
    try:
        info = get_file_info(url)
        return info["accepts_ranges"]
    except requests.RequestException as exc:
        logger.error("Range support check failed for %s: %s", url, exc)
        return False
