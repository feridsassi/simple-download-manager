# Technical Report — Simple Download Manager (SDM)

**Course:** Distributed Systems  
**Institution:** MedTech University  
**Academic Year:** 2025 – 2026  
**Project:** Simple Download Manager (SDM)  

---

## Table of Contents

1. [Introduction](#1-introduction)  
2. [Architecture Design](#2-architecture-design)  
3. [Component Implementation](#3-component-implementation)  
4. [Multithreading Design](#4-multithreading-design)  
5. [REST API Design](#5-rest-api-design)  
6. [Persistence Layer](#6-persistence-layer)  
7. [Frontend Design](#7-frontend-design)  
8. [Error Handling & Fault Tolerance](#8-error-handling--fault-tolerance)  
9. [Design Decisions & Trade-offs](#9-design-decisions--trade-offs)  
10. [Conclusion](#10-conclusion)  

---

## 1. Introduction

### 1.1 Problem Statement

A standard HTTP download is a single sequential stream: the client opens one TCP connection, the server streams bytes, and the client writes them to disk. This approach leaves significant bandwidth unused on high-latency or multi-path connections because a single TCP stream cannot saturate the available throughput.

### 1.2 Solution Approach

SDM exploits HTTP/1.1 Range requests (RFC 7233) to split a remote file into N independent byte ranges and download them in parallel on separate threads. Because each segment is an independent HTTP transaction, they can proceed simultaneously, collectively saturating the available bandwidth much more effectively than a single stream.

### 1.3 Objectives

| Objective | How SDM meets it |
|---|---|
| Parallel downloading | ThreadPoolExecutor with one thread per segment |
| User control | Pause / Resume / Cancel via `threading.Event` |
| Fault tolerance | Per-segment exponential back-off retry |
| Persistence | SQLite history of all downloads |
| Usability | Browser UI with live progress, served from the same process |

### 1.4 Technology Stack

| Layer | Technology | Justification |
|---|---|---|
| HTTP server | FastAPI + uvicorn | Async-capable, auto-generates OpenAPI docs, minimal boilerplate |
| HTTP client | `requests` library | Mature, supports streaming and Range headers natively |
| Concurrency | `concurrent.futures.ThreadPoolExecutor` | Stdlib, clean future-based API, integrates well with I/O-bound work |
| Thread coordination | `threading.Event`, `threading.Lock` | Lightweight primitives; no external dependencies |
| Persistence | SQLite via `sqlite3` | Zero-config, embedded, sufficient for local download history |
| Frontend | Vanilla JS (no framework) | Zero dependency, served as a static file, full control |

---

## 2. Architecture Design

### 2.1 Architectural Style: Layered Architecture

SDM uses a **Layered (N-Tier) Architecture** where each layer has a single, well-defined responsibility and may only depend on the layer directly beneath it. This enforces separation of concerns, simplifies testing, and makes each component independently replaceable.

```
╔══════════════════════════════════════════════════════════════╗
║  LAYER 1 — PRESENTATION                                      ║
║  ui/index.html  ·  Vanilla JS SPA                            ║
║  Polls REST API · Renders progress · Dispatches user actions ║
╠══════════════════════════════════════════════════════════════╣
║  LAYER 2 — API                                               ║
║  api/routes.py  ·  FastAPI Router                            ║
║  Validates HTTP requests · Delegates to core · Returns JSON  ║
╠══════════════════════════════════════════════════════════════╣
║  LAYER 3 — CORE (BUSINESS LOGIC)                             ║
║  download_manager.py  ·  thread_controller.py                ║
║  segment_worker.py    ·  file_assembler.py                   ║
╠══════════════════════════════════════════════════════════════╣
║  LAYER 4 — INFRASTRUCTURE                                    ║
║  persistence/history.py  (SQLite)                            ║
║  utils/http_utils.py     (requests)                          ║
╚══════════════════════════════════════════════════════════════╝
```

### 2.2 Component Diagram

```
                         ┌──────────────┐
                         │  index.html  │  (Browser)
                         └──────┬───────┘
                                │ HTTP/JSON polling (1 s interval)
                         ┌──────▼───────┐
                         │  routes.py   │  FastAPI Router
                         └──────┬───────┘
                                │ Python method calls
                    ┌───────────▼────────────┐
                    │   DownloadManager      │  one instance (app lifetime)
                    │   _downloads: dict     │  in-memory state per download
                    └──┬────────────┬────────┘
                       │            │
          ┌────────────▼──┐   ┌─────▼──────────┐
          │ ThreadController│  │  DownloadHistory│
          │ (per download) │  │  SQLite CRUD    │
          └────────┬───────┘  └────────────────┘
                   │ submits
        ┌──────────┼──────────┬──────────┐
        │          │          │          │
   ┌────▼───┐ ┌────▼───┐ ┌────▼───┐ ┌────▼───┐
   │Segment │ │Segment │ │Segment │ │Segment │
   │Worker 0│ │Worker 1│ │Worker 2│ │Worker 3│
   └────┬───┘ └────┬───┘ └────┬───┘ └────┬───┘
        │          │          │          │
   .part0      .part1      .part2      .part3
        │          │          │          │
        └──────────┴──────────┴──────────┘
                         │
                  ┌──────▼──────┐
                  │FileAssembler│
                  └──────┬──────┘
                         │
                    final file ✓
```

### 2.3 Key Architectural Decisions

**Single application process, multiple threads**  
Rather than spawning sub-processes (which would complicate IPC and state sharing), SDM runs all download workers as threads inside the FastAPI process. The GIL is not a concern here because segment workers spend almost all their time in network I/O — a blocking operation that releases the GIL — not in CPU-bound computation.

**Shared mutable state via explicit primitives, not message passing**  
Worker threads share `pause_event`, `cancel_event`, and a `bytes_downloaded` counter with the owning `DownloadState`. This is intentional: these are the minimum necessary shared state, each protected by the appropriate primitive (`threading.Event` is thread-safe by design; the counter is protected by a `threading.Lock`). There are no other shared data structures between segments.

**Real-time state in memory, durability in SQLite**  
Active download state (speed, progress, ETA) lives in memory for low-latency reads by the polling UI. When a download finishes, the aggregated result (avg_speed, end_time, status) is flushed to SQLite. This separates hot mutable state from cold historical records.

---

## 3. Component Implementation

### 3.1 `utils/http_utils.py` — File Metadata

Before starting a download, SDM must determine three things: the file's total size, whether the server supports Range requests, and the best filename to use. All three are obtained in a single `HEAD` request.

**Filename resolution priority:**

```
1. Content-Disposition: filename*=UTF-8''encoded%20name.zip  (RFC 5987)
2. Content-Disposition: filename="name.zip"
3. os.path.basename(urlparse(url).path)  URL-decoded
4. Append extension from Content-Type mapping (if no extension found)
5. Fallback: "download_{unix_timestamp}"
```

**Range support detection:**
```python
accepts_ranges = headers.get("Accept-Ranges", "").lower() == "bytes"
```

If the server returns `HEAD` with a 4xx or 5xx error, the utility falls back to a streaming `GET` with `stream=True` and closes the connection immediately after reading the headers — a common workaround for servers that block `HEAD`.

### 3.2 `core/segment_worker.py` — Segment Worker

Each `SegmentWorker` is responsible for exactly one byte range. Its `download()` method:

1. Builds a `Range: bytes={start}-{end}` header
2. Opens a streaming `requests.get()` (does not buffer the entire response in memory)
3. Reads in `CHUNK_SIZE` (8 KB) increments
4. After each chunk:
   - calls `pause_event.wait()` — blocks if paused, continues if set
   - checks `cancel_event.is_set()` — returns `False` immediately if set
   - writes the chunk to its `.partN` file
   - calls `progress_callback(len(chunk))` — notifies the manager

**Retry logic with exponential back-off:**

```python
for attempt in range(max_retries + 1):
    backoff = 2 ** (attempt - 1)   # 1s, 2s, 4s …
    time.sleep(backoff)            # skipped on attempt 0
    try:
        success = self._attempt_download()
        if success: return True
    except requests.RequestException:
        continue   # retry

return False  # all attempts exhausted
```

The back-off ensures a failing segment does not hammer a struggling server.

### 3.3 `core/thread_controller.py` — Thread Pool

`ThreadController` wraps a `ThreadPoolExecutor` with a clean interface:

```python
def run_segments(self, workers: list[SegmentWorker]) -> list[bool]:
    future_to_index = {}
    executor = ThreadPoolExecutor(max_workers=len(workers))
    for i, worker in enumerate(workers):
        future = executor.submit(worker.download)
        future_to_index[future] = i
    results = [False] * len(workers)
    for future in as_completed(future_to_index):
        results[future_to_index[future]] = future.result()
    return results
```

Using `as_completed()` rather than `executor.map()` means the controller processes results as they arrive rather than waiting for all workers to finish in submission order — a more responsive design.

### 3.4 `core/file_assembler.py` — File Assembly

Assembly is a sequential, deterministic operation:

```
part0 → part1 → part2 → part3 → output file
```

The assembler:
1. Checks every `.partN` file exists before opening the output (atomic-like validation)
2. Opens the output in `"wb"` mode (truncates any partial file from a previous failed attempt)
3. Reads each part in 64 KB chunks (larger than the download chunk — minimises syscall overhead during local disk-to-disk copy)
4. Deletes all part files after a successful merge

If any part is missing, the assembler returns `False` and leaves no partial output file.

### 3.5 `core/download_manager.py` — Orchestrator

`DownloadManager` is the central coordinator. Its `start_download()` method:

```
1.  get_file_info(url)         → filename, total_size, accepts_ranges
2.  compute byte ranges        → [(0, 25M), (25M, 50M), (50M, 75M), (75M, 99M)]
3.  create DownloadState       → holds pause/cancel events, byte counter, status
4.  persist initial DB record  → status = "downloading"
5.  launch background thread   → runs the full download pipeline
6.  return download_id         → returned to API caller immediately (non-blocking)
```

The background thread runs `ThreadController.run_segments()`, then `FileAssembler.assemble()`, then updates the DB record. The API caller returns immediately with the `id`; the UI polls for progress.

**Progress computation** (computed on every `GET /downloads/{id}` call):

```python
elapsed    = time.monotonic() - state.start_time
downloaded = state.bytes_downloaded          # lock-protected read
speed      = downloaded / elapsed
remaining  = total_size - downloaded
eta        = remaining / speed
progress   = downloaded / total_size * 100
```

---

## 4. Multithreading Design

### 4.1 Thread Inventory

| Thread | Lifecycle | Role |
|---|---|---|
| uvicorn workers | Application lifetime | Serve HTTP requests (FastAPI) |
| Background download thread | Per download | Runs ThreadController + assembler |
| Segment worker threads | Per segment, per download | Download one byte range |

For a server handling 3 simultaneous downloads of 4 segments each, the peak thread count is: `uvicorn threads + 3 background threads + 12 segment threads`.

### 4.2 Synchronisation Primitives

#### `threading.Event` — Pause & Cancel

`threading.Event` is a boolean flag with built-in blocking:

```python
# Segment worker inner loop
self.pause_event.wait()           # blocks when cleared, returns immediately when set
if self.cancel_event.is_set():
    return False
```

`pause_event` is initialised **set** (workers run immediately). Calling `pause_event.clear()` from the API thread causes all workers to block at their next `wait()` call — which happens between chunks, so at most `CHUNK_SIZE` (8 KB) of additional data is written before they block. Calling `pause_event.set()` unblocks all workers simultaneously.

`cancel_event` is checked after each `pause_event.wait()` call, ensuring a paused download can still be cancelled without needing to resume first (the cancel sets both events).

#### `threading.Lock` — Byte Counter

Multiple segment workers call `progress_callback(n)` concurrently. The callback increments a shared counter:

```python
def add_bytes(self, n: int) -> None:
    with self._bytes_lock:          # acquire → increment → release
        self._bytes_downloaded += n
```

The lock is held only for a single integer increment — negligible contention even at high throughput.

### 4.3 Absence of Inter-segment Races

Each `SegmentWorker` writes to its own `.partN` file path. No two workers ever write to the same file descriptor. There is no shared buffer, no shared queue, and no producer-consumer pattern between segments. This design completely eliminates segment-level race conditions.

### 4.4 Thread Safety of DownloadManager

`DownloadManager._downloads` (a `dict`) is protected by a module-level `threading.Lock` (`self._lock`). All reads and writes to the dict use `with self._lock`. The `DownloadState` object retrieved from the dict is then used without the lock because its mutable fields are either:
- `threading.Event` (thread-safe by design), or
- an `int` protected by its own `_bytes_lock`, or
- a `str` field written only by the owning background thread and read by the API thread as an atomic Python object reference.

### 4.5 Concurrency Model Diagram

```
FastAPI thread (API request)
│
│  manager.pause_download(id)
│      state.pause_event.clear()    ←── thread-safe (Event is internally locked)
│      state.status = "paused"      ←── atomic str assignment in CPython
│
▼  returns immediately

Background thread (per download)
│
▼
ThreadPoolExecutor
    ├─ Segment 0 thread
    │      loop:
    │          pause_event.wait()   ←── BLOCKS HERE when paused
    │          cancel_event.is_set()
    │          write chunk
    │          lock.acquire()
    │          bytes_downloaded += n
    │          lock.release()
    │
    ├─ Segment 1 thread  (same pattern)
    ├─ Segment 2 thread
    └─ Segment 3 thread
```

---

## 5. REST API Design

### 5.1 Design Principles

- **Stateless requests** — each request carries all necessary information (the download `id` in the URL)
- **Meaningful HTTP verbs** — `POST` to create, `GET` to read, `DELETE` to cancel
- **`202 Accepted`** on start — signals that the work has been enqueued, not completed
- **JSON throughout** — consistent content type for all requests and responses
- **FastAPI dependency injection** — the `DownloadManager` singleton is injected via `Depends(get_manager)` rather than accessed as a global variable, which improves testability

### 5.2 Endpoint Summary

| Method | Path | Action | Response |
|---|---|---|---|
| `POST` | `/downloads` | Start download | `202` with `{id, filename, status}` |
| `GET` | `/downloads` | List all | `200` array |
| `GET` | `/downloads/{id}` | Get one | `200` status dict |
| `POST` | `/downloads/{id}/pause` | Pause | `200` confirmation |
| `POST` | `/downloads/{id}/resume` | Resume | `200` confirmation |
| `DELETE` | `/downloads/{id}` | Cancel | `200` confirmation |
| `GET` | `/browse-folder` | Folder picker | `200` with `{path}` |

### 5.3 Status Lifecycle

```
              ┌─────────┐
    POST ────►│ pending │
              └────┬────┘
                   │ background thread starts
                   ▼
           ┌──────────────┐
      ┌────│ downloading  │────┐
      │    └──────┬───────┘    │
      │           │ pause      │ cancel
      │           ▼            │
      │      ┌────────┐        │
      │      │ paused │────────┤
      │      └────┬───┘ cancel │
      │           │ resume     │
      │           └────────────┘
      │                        │
      │ all segments succeed   │ any segment fails (all retries)
      ▼                        ▼
┌───────────┐           ┌──────────┐         ┌───────────┐
│ completed │           │  failed  │         │ cancelled │
└───────────┘           └──────────┘         └───────────┘
```

### 5.4 `/browse-folder` — Design Note

This endpoint opens a blocking Tkinter dialog on the server. Since FastAPI runs on uvicorn using an async event loop, the route function is deliberately synchronous (not `async def`) so FastAPI dispatches it to a thread pool, preventing the dialog from blocking the event loop while it waits for user input.

---

## 6. Persistence Layer

### 6.1 Schema

```sql
CREATE TABLE IF NOT EXISTS downloads (
    id            TEXT PRIMARY KEY,   -- UUID4 string
    url           TEXT NOT NULL,
    filename      TEXT,
    save_path     TEXT,
    total_size    INTEGER,            -- bytes; 0 if unknown
    status        TEXT,              -- pending|downloading|paused|completed|failed|cancelled
    segments      INTEGER,
    retries       INTEGER,
    start_time    TEXT,              -- ISO-8601 UTC
    end_time      TEXT,              -- ISO-8601 UTC; NULL until finished
    avg_speed     REAL,             -- bytes/sec; NULL until finished
    error_message TEXT              -- NULL on success
)
```

### 6.2 Write Pattern

| Event | DB operation |
|---|---|
| Download starts | `INSERT OR REPLACE` with status=downloading |
| Download paused | `UPDATE status=paused` |
| Download resumed | `UPDATE status=downloading` |
| Download cancelled | `UPDATE status=cancelled` |
| Download completes | `UPDATE status=completed, end_time=…, avg_speed=…` |
| Download fails | `UPDATE status=failed, error_message=…` |

### 6.3 Connection Strategy

Each public method opens a new connection (`sqlite3.connect`), executes the query, commits, and closes. This avoids thread-safety issues with SQLite connections (which are not thread-safe by default) without requiring a connection pool for the expected concurrency level of a local download manager.

---

## 7. Frontend Design

### 7.1 Architecture

The UI is a **Single-Page Application** delivered as one self-contained HTML file (all CSS and JavaScript inline). It requires no build step, no bundler, and no CDN. FastAPI's `StaticFiles` middleware serves it at `/`.

### 7.2 Polling Architecture

The UI polls `GET /downloads` every 1000 ms using `setInterval`. On each tick:

```
fetch('/downloads')
    └─ split by status:
        ├─ ACTIVE_STATS  = {pending, downloading, paused}  → active table
        └─ HISTORY_STATS = {completed, failed, cancelled}  → history list
```

### 7.3 DOM Update Strategy

To avoid flickering and unnecessary reflows, the UI **never re-renders the entire table**. Instead it maintains two `Map` objects keyed by download `id`:

```javascript
const activeRows   = new Map();  // id → <tr> element
const historyItems = new Map();  // id → <div> element
```

On each poll tick:
1. For each active download: if its `<tr>` exists, update only the changed cells; if not, create and append the row.
2. For rows whose ids are no longer in the active set: remove the `<tr>` and delete from the map.
3. Same logic for history items.

This means a download that has been in progress for 60 seconds will have had its row **created once** and **updated 60 times** with minimal DOM operations — never destroyed and recreated.

### 7.4 Download Flow in the UI

```
User clicks "⬇ Download"
    │
    ├─ Validate URL is not empty
    ├─ setBtn('selecting')       → button disabled, shows "📁 Selecting folder…"
    │
    ├─ fetch GET /browse-folder  → OS dialog opens (may take several seconds)
    │       │
    │       ├─ path == null      → showInfo("Download cancelled"), setBtn('default')
    │       │
    │       └─ path != null
    │               │
    │               ├─ setBtn('starting')    → "⏳ Starting…"
    │               ├─ fetch POST /downloads
    │               │       │
    │               │       ├─ 4xx / network error → showError(…), setBtn('default')
    │               │       └─ 202 Accepted  → clear URL input, setBtn('default')
    │               │
    │               └─ new row appears in active table on next poll tick (≤ 1 s)
```

### 7.5 Progress Display

| Field | Calculation |
|---|---|
| `progress_pct` | Computed by `DownloadManager`: `bytes_downloaded / total_size * 100` |
| `speed_bps` | `bytes_downloaded / elapsed_seconds` |
| `eta_seconds` | `(total_size - bytes_downloaded) / speed_bps` |

These are computed on the server on every `GET /downloads` call and returned as JSON fields. The UI formats them using client-side formatter functions:

```javascript
formatBytes(bytes)    // 1048576     → "1.0 MB"
formatSpeed(bps)      // 2097152     → "2.0 MB/s"
formatETA(seconds)    // 145         → "2m 25s"
formatTime(isoStr)    // "2026-…T…Z" → "14:32:05"
```

---

## 8. Error Handling & Fault Tolerance

### 8.1 Network Errors — Per-segment Retry

A `requests.RequestException` inside a segment worker triggers the retry loop. The worker does not propagate the exception to the thread pool — it catches it internally, waits, and retries. Only after all retries are exhausted does it return `False`.

This means a transient network glitch (dropped connection, server-side timeout) is transparently recovered from without the user seeing any error.

### 8.2 Server-side `HEAD` Fallback

Some servers reject `HEAD` requests with `405 Method Not Allowed`. `get_file_info()` catches this and falls back to a `GET` with `stream=True`, reading only the headers before closing the connection:

```python
except requests.RequestException:
    response = requests.get(url, stream=True, ...)
    response.close()   # do not read the body
```

### 8.3 Missing Part File on Assembly

If a segment worker returns `False` (all retries failed), its `.partN` file may be absent or incomplete. The assembler checks for the existence of every part file before opening the output:

```python
for path in part_paths:
    if not os.path.exists(path):
        logger.error("Missing part: %s", path)
        return False
```

This prevents producing a silently corrupted output file.

### 8.4 Cancellation During Assembly Wait

If the user cancels while all segments are still running, `cancel_event.is_set()` becomes `True`. The background thread checks this flag after `run_segments()` returns:

```python
if state.cancel_event.is_set():
    self._cleanup_parts(part_paths)
    return
```

Part files are deleted without attempting assembly — no partial output is left on disk.

### 8.5 Exception Isolation

Every public method in `DownloadManager`, `ThreadController`, and `SegmentWorker` wraps its body in `try/except`. Exceptions are logged with `logger.exception()` and converted to `False` return values or HTTP `400` responses — never allowed to propagate up and crash the server process.

---

## 9. Design Decisions & Trade-offs

### 9.1 Threads vs. Async I/O

Python's `asyncio` with `aiohttp` could also download segments concurrently, but threads were chosen deliberately:

| Concern | Threads (`ThreadPoolExecutor`) | Async (`asyncio` + `aiohttp`) |
|---|---|---|
| Simplicity | Straightforward — each worker is a normal function | Requires `async/await` throughout |
| Pause / Cancel | `threading.Event` — instant, no event loop needed | Needs `asyncio.Event` + careful task cancellation |
| Integration with FastAPI | Background threads detached from the event loop | Must use `asyncio.create_task` or `run_in_executor` |
| GIL impact | Negligible — workers block in network I/O (GIL released) | N/A |

For I/O-bound workloads at the concurrency levels of a personal download manager (< 20 simultaneous segments), threads are simpler and equally performant.

### 9.2 Segment Count vs. Server Load

More segments does not always mean faster downloads. Diminishing returns set in because:
- Each TCP connection has its own slow-start phase
- Some servers rate-limit per-IP connection count
- The bottleneck may be the client's own disk write speed

The default of 4 segments is a balanced choice. The user can tune it per-download via the UI (1–16 range).

### 9.3 Polling vs. WebSockets for Progress

The UI polls every 1 second rather than using a WebSocket or Server-Sent Events for push-based updates. Polling was chosen for simplicity: no additional server-side infrastructure, no connection state to manage, and 1 s latency is imperceptible to human users watching a download progress bar.

### 9.4 In-memory State vs. Full DB Persistence

Active download state (progress, speed, ETA) is kept in memory rather than written to the DB on every progress update. Writing to SQLite on every 8 KB chunk (at potentially 50 MB/s) would produce ~6000 writes/second — an unnecessary I/O burden. Instead, progress is computed from in-memory counters and only the final summary is persisted.

The trade-off is that if the server process crashes during a download, the in-progress state is lost and the download must be restarted. For a local download manager, this is an acceptable limitation.

---

## 10. Conclusion

SDM demonstrates how standard Python threading primitives — `ThreadPoolExecutor`, `threading.Event`, and `threading.Lock` — combined with HTTP Range requests enable a clean, efficient, and controllable parallel download system.

**Key achievements:**

1. **Architecture**: A clean 4-layer architecture with strict dependency direction, making each component independently testable and replaceable.

2. **Multithreading efficiency**: Segment workers share no mutable state with each other; coordination happens only through two `Event` objects and one `Lock`, minimising contention. Workers spend >99% of their time in network I/O, so the GIL is not a bottleneck.

3. **Fault tolerance**: Per-segment exponential back-off retry, assembly pre-validation, and clean cancellation ensure the system handles real-world network conditions gracefully.

4. **Usability**: The polling UI provides sub-second progress feedback with surgical DOM updates, and the native folder picker eliminates the need for users to type file paths.

5. **Code quality**: Every public method carries type hints and a docstring; all configuration is centralised in `config.py`; no global mutable state; the Python `logging` module is used throughout.

The system is deliberately scoped to a single-machine, single-user context — a natural next step would be to add authentication and multi-user support, replace the SQLite store with PostgreSQL, and move the background threads to a task queue like Celery for resilience across process restarts.
