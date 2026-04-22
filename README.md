# SDM — Simple Download Manager

A multithreaded, segmented HTTP download manager with a REST API and browser UI.

---

## Features

- **Parallel segmented downloads** — splits files into N byte-range segments downloaded simultaneously
- **Pause / Resume / Cancel** — per-download control with no data loss on resume
- **Automatic fallback** — single-segment mode when server doesn't support `Accept-Ranges`
- **Exponential back-off retry** — each segment retries up to `MAX_RETRIES` times (1 s, 2 s, 4 s …)
- **Persistent history** — every download recorded in SQLite with status, speed, and timestamps
- **REST API** — full CRUD via FastAPI with auto-generated OpenAPI docs at `/docs`
- **Browser UI** — Vanilla JS SPA with live progress bars, speed, ETA, and inline controls
- **Native folder picker** — `GET /browse-folder` opens a Tkinter dialog on the server

---

## Architecture

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
└────────────────────────┬────────────────────────────────────┘
                         │  Python calls
┌────────────────────────▼────────────────────────────────────┐
│                     CORE LAYER                              │
│  DownloadManager ──► ThreadController ──► SegmentWorker×N  │
│        └──────────────────────────► FileAssembler          │
└──────┬────────────────────────────────┬─────────────────────┘
       │  SQLite                        │  HTTP Range requests
┌──────▼──────────┐          ┌──────────▼──────────┐
│ persistence/    │          │  utils/              │
│ history.py      │          │  http_utils.py       │
└─────────────────┘          └─────────────────────┘
```

---

## Project Structure

```
sdm/
├── main.py                   # FastAPI app factory + uvicorn entry point
├── config.py                 # All tuneable constants
├── api/
│   └── routes.py             # All REST endpoints (FastAPI Router)
├── core/
│   ├── download_manager.py   # Orchestrator — owns all DownloadState objects
│   ├── segment_worker.py     # Downloads one byte-range segment
│   ├── thread_controller.py  # ThreadPoolExecutor wrapper
│   └── file_assembler.py     # Merges .partN files → final file
├── persistence/
│   └── history.py            # SQLite CRUD (sdm_history.db)
├── utils/
│   └── http_utils.py         # HEAD probe, filename extraction, range check
├── ui/
│   └── index.html            # Single-file Vanilla JS SPA
└── requirements.txt
```

---

## Installation

**Prerequisites:** Python 3.10 or later.

```bash
cd sdm
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt`: `fastapi`, `uvicorn[standard]`, `requests`, `aiofiles`

SQLite is part of Python's standard library — `sdm_history.db` is created automatically on first run.

---

## Running the Application

```bash
cd sdm
python main.py
```

Server starts on `http://0.0.0.0:8000`. Open `http://localhost:8000` for the UI or `http://localhost:8000/docs` for the API explorer.

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/downloads` | Start a new download |
| `GET` | `/downloads` | List all downloads (active + history) |
| `GET` | `/downloads/{id}` | Get status of one download |
| `POST` | `/downloads/{id}/pause` | Pause a running download |
| `POST` | `/downloads/{id}/resume` | Resume a paused download |
| `DELETE` | `/downloads/{id}` | Cancel a download |
| `GET` | `/test-urls` | List curated test URLs |
| `GET` | `/browse-folder` | Open native folder picker dialog |

---

## Configuration

All constants live in `config.py`.

| Constant | Default | Description |
|---|---|---|
| `NUM_SEGMENTS` | `4` | Default number of parallel segments |
| `MAX_RETRIES` | `3` | Maximum per-segment retry attempts |
| `CHUNK_SIZE` | `8192` | Read buffer size per chunk (bytes) |
| `DEFAULT_DOWNLOAD_DIR` | `/mnt/c/Users/user/Downloads` | Fallback save directory |
| `REQUEST_TIMEOUT` | `30` | HTTP request timeout in seconds |
