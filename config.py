import os

NUM_SEGMENTS: int = 4
MAX_RETRIES: int = 3
CHUNK_SIZE: int = 1024 * 8          # 8 KB per read
DEFAULT_DOWNLOAD_DIR: str = "/mnt/c/Users/user/Downloads"
REQUEST_TIMEOUT: int = 30

# Sent with every outbound HTTP request.
# Many CDNs (Wikimedia, Cloudflare, etc.) block the default
# "python-requests/x.y.z" agent with HTTP 403.
REQUEST_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}
