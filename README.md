# SDM — Simple Download Manager

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-latest-green)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Status](https://img.shields.io/badge/Status-Complete-brightgreen)

> *A multithreaded, segmented HTTP download manager — built as an IDM/XDM equivalent for the Distributed Systems course at MedTech.*

---

## ✨ Features

- ⚡ Downloads files up to 4× faster using parallel segmented connections
- 🔀 Splits every file into N byte-range segments (HTTP Range — RFC 7233)
- 🧵 Each segment runs in its own thread via `ThreadPoolExecutor`
- 🔁 Auto-retry with exponential backoff (1s → 2s → 4s) on failure
- ⏸ Pause, Resume, Cancel any download at any time
- 💾 Persistent download history stored in SQLite
- 🖥 Clean browser UI — no frontend framework, pure Vanilla JS
- 📁 Native OS folder picker — no typing paths manually

---

## ⚙️ How It Works

- SDM sends a `HEAD` request to detect file size and Range support
- Splits the file into N equal byte ranges: `bytes=0–24MB`, `bytes=25–49MB` …
- Opens N parallel TCP connections, one per segment
- Each thread downloads its range independently → written to `.partN` temp file
- Bypasses per-connection throttling → effective speed multiplied by N
- `FileAssembler` concatenates all parts in order → final file
- If server doesn't support `Range`: automatic fallback to single-thread

---

## 🏗 Architecture

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

## 🚀 Installation

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

## ▶️ Running the Application

```bash
cd sdm
python main.py
```

Server starts on `http://0.0.0.0:8000`. Open `http://localhost:8000` for the UI or `http://localhost:8000/docs` for the API explorer.

---

## 📡 API Reference

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

## ⚙️ Configuration

All constants live in `config.py`.

| Constant | Default | Description |
|---|---|---|
| `NUM_SEGMENTS` | `4` | Default number of parallel segments |
| `MAX_RETRIES` | `3` | Maximum per-segment retry attempts |
| `CHUNK_SIZE` | `8192` | Read buffer size per chunk (bytes) |
| `DEFAULT_DOWNLOAD_DIR` | `/mnt/c/Users/user/Downloads` | Fallback save directory |
| `REQUEST_TIMEOUT` | `30` | HTTP request timeout in seconds |
