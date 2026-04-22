# SDM — Simple Download Manager

> **Distributed Systems Project — MedTech University, 3rd Year**  
> A multithreaded, segmented HTTP download manager with a REST API and browser UI.

---

## Table of Contents

1. [Overview](#overview)  
2. [Features](#features)  
3. [Architecture](#architecture)  
4. [Project Structure](#project-structure)  
5. [Installation](#installation)  
6. [Running the Application](#running-the-application)  
7. [API Reference](#api-reference)  
8. [Configuration](#configuration)  
9. [How Segmented Downloading Works](#how-segmented-downloading-works)  
10. [Multithreading Design](#multithreading-design)  

---

## Overview

SDM is a backend service that accelerates file downloads by splitting each download into multiple concurrent byte-range segments, downloading them in parallel, and reassembling them into the final file. It exposes a REST API consumed by a lightweight browser UI served from the same process.

The project demonstrates core Distributed Systems concepts:

| Concept | Implementation |
|---|---|
| Concurrency | `ThreadPoolExecutor` with one thread per segment |
| Coordination | `threading.Event` for pause / resume / cancel |
| Fault tolerance | Per-segment exponential back-off retry |
| State management | SQLite persistence + in-memory real-time state |
| Distributed protocol | HTTP/1.1 Range requests (RFC 7233) |

---

## Features

- **Parallel segmented downloads** — splits any file into N byte-range segments downloaded simultaneously
- **Pause / Resume / Cancel** — per-download control with zero data loss on resume (segments resume from their last written byte)
- **Automatic fallback** — degrades gracefully to single-segment mode when the server does not advertise `Accept-Ranges: bytes`
- **Exponential back-off retry** — each segment retries up to `MAX_RETRIES` times with delays of 1 s, 2 s, 4 s …
- **Persistent history** — every download is recorded in SQLite with status, speed, timestamps, and error messages
- **REST API** — full CRUD over downloads with a FastAPI backend and auto-generated OpenAPI docs at `/docs`
- **Browser UI** — single-file Vanilla JS SPA with live progress bars, speed, ETA, and action buttons, polling every second
- **Native folder picker** — `GET /browse-folder` opens a Tkinter dialog on the server side so the user never has to type a path

---

## Architecture

SDM follows a **Layered Architecture** with strict downward dependencies — each layer only calls the layer directly beneath it.

```
┌─────────────────────────────────────────────────────────────┐
│                    PRESENTATION LAYER                       │
│              ui/index.html  (Vanilla JS SPA)                │
│   polls GET /downloads every 1 s · POST /downloads to start │
└────────────────────────┬────────────────────────────────────┘
                         │  HTTP / JSON
┌────────────────────────▼────────────────────────────────────┐
│                      API LAYER                              │
│              api/routes.py  (FastAPI Router)                │
│  POST /downloads · GET /downloads · GET /downloads/{id}     │
│  POST /downloads/{id}/pause  · POST /downloads/{id}/resume  │
│  DELETE /downloads/{id}      · GET /browse-folder           │
└────────────────────────┬────────────────────────────────────┘
                         │  Python calls
┌────────────────────────▼────────────────────────────────────┐
│                     CORE LAYER                              │
│                                                             │
│  DownloadManager ──► ThreadController ──► SegmentWorker×N  │
│        │                                        │           │
│        └──────────────────────► FileAssembler ◄─┘           │
└──────┬────────────────────────────────┬─────────────────────┘
       │                                │
       │  SQLite                        │  HTTP Range requests
┌──────▼──────────┐          ┌──────────▼──────────┐
│ PERSISTENCE     │          │  NETWORK LAYER       │
│ history.py      │          │  http_utils.py       │
│ sdm_history.db  │          │  requests library    │
└─────────────────┘          └─────────────────────┘
```

### Data flow for a new download

```
User clicks Download
      │
      ▼
POST /downloads  ──►  DownloadManager.start_download()
                            │
                            ├─ get_file_info()   ← HEAD request to server
                            ├─ compute byte ranges  [0–24MB] [25–49MB] …
                            ├─ create SegmentWorker×N
                            ├─ persist record (status=downloading)
                            └─ spawn background thread
                                    │
                                    ├─ ThreadController.run_segments()
                                    │       └─ ThreadPoolExecutor
                                    │           ├─ SegmentWorker[0].download()  ─► file.part0
                                    │           ├─ SegmentWorker[1].download()  ─► file.part1
                                    │           ├─ SegmentWorker[2].download()  ─► file.part2
                                    │           └─ SegmentWorker[3].download()  ─► file.part3
                                    │
                                    ├─ FileAssembler.assemble()
                                    │       └─ part0 + part1 + part2 + part3 ─► file.zip
                                    │
                                    └─ update history (status=completed, avg_speed)
```

---

## Project Structure

```
sdm/
├── main.py                   # FastAPI app factory + uvicorn entry point
├── config.py                 # All tuneable constants
│
├── api/
│   ├── __init__.py
│   └── routes.py             # All REST endpoints (FastAPI Router)
│
├── core/
│   ├── __init__.py
│   ├── download_manager.py   # Orchestrator — owns all DownloadState objects
│   ├── segment_worker.py     # Downloads one byte-range segment
│   ├── thread_controller.py  # ThreadPoolExecutor wrapper
│   └── file_assembler.py     # Merges .partN files → final file
│
├── persistence/
│   ├── __init__.py
│   └── history.py            # SQLite CRUD (sdm_history.db)
│
├── utils/
│   ├── __init__.py
│   └── http_utils.py         # HEAD probe, filename extraction, range check
│
├── ui/
│   └── index.html            # Single-file Vanilla JS SPA
│
├── requirements.txt
├── README.md
└── REPORT.md
```

---

## Installation

**Prerequisites:** Python 3.10 or later.

```bash
# 1. Enter the project directory
cd sdm

# 2. (Optional) Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

`requirements.txt` contents:

```
fastapi
uvicorn[standard]
requests
aiofiles
```

No external database setup is required — SQLite is part of Python's standard library and the database file (`sdm_history.db`) is created automatically on first run.

---

## Running the Application

```bash
cd sdm
python main.py
```

The server starts on `http://0.0.0.0:8000`.

| URL | Purpose |
|---|---|
| `http://localhost:8000` | Browser UI |
| `http://localhost:8000/docs` | Interactive OpenAPI documentation |
| `http://localhost:8000/redoc` | Alternative API docs (ReDoc) |

---

## API Reference

### `POST /downloads` — Start a download

**Request body:**
```json
{
  "url":       "https://example.com/file.zip",
  "save_path": "/home/user/Downloads",
  "segments":  4,
  "retries":   3
}
```

**Response `202 Accepted`:**
```json
{
  "id":       "d3f1a2b4-...",
  "filename": "file.zip",
  "status":   "downloading"
}
```

---

### `GET /downloads` — List all downloads

Returns a merged list of active real-time states and historical DB records.

**Response `200 OK`:** array of download status objects (see below).

---

### `GET /downloads/{id}` — Get one download

**Response `200 OK`:**
```json
{
  "id":               "d3f1a2b4-...",
  "url":              "https://example.com/file.zip",
  "filename":         "file.zip",
  "save_path":        "/home/user/Downloads/file.zip",
  "total_size":       104857600,
  "bytes_downloaded": 52428800,
  "status":           "downloading",
  "segments_count":   4,
  "progress_pct":     50.0,
  "speed_bps":        2097152,
  "eta_seconds":      25.0,
  "start_time":       "2026-04-21T10:00:00+00:00",
  "error_message":    null
}
```

---

### `POST /downloads/{id}/pause` — Pause

Clears the download's `pause_event`; all segment workers block on `Event.wait()`.

**Response `200 OK`:** `{ "id": "...", "status": "paused" }`

---

### `POST /downloads/{id}/resume` — Resume

Sets the `pause_event`; blocked workers unblock immediately.

**Response `200 OK`:** `{ "id": "...", "status": "downloading" }`

---

### `DELETE /downloads/{id}` — Cancel

Sets the `cancel_event`; workers detect it between chunks and exit cleanly.

**Response `200 OK`:** `{ "id": "...", "status": "cancelled" }`

---

### `GET /browse-folder` — Native folder picker

Opens a Tkinter `askdirectory` dialog on the server. Returns the selected path or `null` if the user cancels.

**Response `200 OK`:** `{ "path": "/home/user/Downloads" }` or `{ "path": null }`

---

## Configuration

All constants live in `config.py` — edit once and all modules pick up the change.

| Constant | Default | Description |
|---|---|---|
| `NUM_SEGMENTS` | `4` | Default number of parallel segments |
| `MAX_RETRIES` | `3` | Maximum per-segment retry attempts |
| `CHUNK_SIZE` | `8192` (8 KB) | Read buffer size inside each segment |
| `DEFAULT_DOWNLOAD_DIR` | `/mnt/c/Users/user/Downloads` | Fallback save directory |
| `REQUEST_TIMEOUT` | `30` | HTTP request timeout in seconds |

---

## How Segmented Downloading Works

HTTP/1.1 defines the `Range` request header (RFC 7233) which lets a client request an arbitrary byte range of a resource:

```
GET /file.zip HTTP/1.1
Host: example.com
Range: bytes=0-26214399
```

SDM's process for a 100 MB file with 4 segments:

```
Segment 0:  bytes=0–26214399        → file.zip.part0
Segment 1:  bytes=26214400–52428799 → file.zip.part1
Segment 2:  bytes=52428800–78643199 → file.zip.part2
Segment 3:  bytes=78643200–99999999 → file.zip.part3

Assembly:   part0 + part1 + part2 + part3 → file.zip
```

If the server does not respond with `Accept-Ranges: bytes`, SDM automatically falls back to a single segment (a plain `GET` without a `Range` header).

---

## Multithreading Design

```
Main thread (FastAPI / uvicorn)
│
└─ Per-download background thread  [threading.Thread]
       │
       └─ ThreadPoolExecutor
               ├─ Worker thread 0  [SegmentWorker.download()]
               ├─ Worker thread 1  [SegmentWorker.download()]
               ├─ Worker thread 2  [SegmentWorker.download()]
               └─ Worker thread 3  [SegmentWorker.download()]
```

**Shared state** between the main thread and worker threads is coordinated via:

| Primitive | Purpose |
|---|---|
| `threading.Event` (pause) | Workers block on `event.wait()` between chunks |
| `threading.Event` (cancel) | Workers check `event.is_set()` between chunks |
| `threading.Lock` + `int` | Byte counter incremented by each worker; read by API for progress |

No segment shares mutable data with any other segment — each writes to its own `.partN` file — eliminating inter-worker race conditions entirely.
