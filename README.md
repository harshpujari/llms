# Local Llama

A self-contained, fully local chat stack: **Ollama** for inference, a **FastAPI** backend, and a single-page web UI, plus a document library that turns your files into markdown for retrieval.

Everything runs in Docker. Nothing is installed on the host, and no data leaves your machine.

> The app lives in [`ollama/`](ollama/). Unless noted otherwise, run the commands below from that directory. This repo also holds a few other LLM experiments; see [Other projects in this repo](#other-projects-in-this-repo).

![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.121-009688)
![Ollama](https://img.shields.io/badge/Ollama-llama3.2%3A1b-black)
![Docker](https://img.shields.io/badge/docker-compose-2496ED)

---

## Table of contents

- [Features](#features)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [API](#api)
- [Project structure](#project-structure)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [Security](#security)
- [Contributing](#contributing)
- [Other projects in this repo](#other-projects-in-this-repo)
- [License](#license)

## Features

- **Streaming chat** with `llama3.2:1b` by default, or any model Ollama can pull.
- **Two modes**: `chat` uses the instruction template and keeps history. `generate` is a raw text continuation, like a base model.
- **Document library**: create folders and upload several files at once. Each file is converted to markdown with [MarkItDown](https://github.com/microsoft/markitdown).
- **Background extraction**: uploads return immediately. A single queue extracts one file at a time, and a restart picks up any file that isn't extracted yet.
- **File viewer**: preview the original file, read the extracted text, or download it.
- **One-command lifecycle**: `./scripts/start.sh` and `./scripts/stop.sh`.
- **Live reload**: backend and frontend are bind-mounted, so code edits need no rebuild.

## Architecture

```
 browser ──▶  web  (nginx, :3000)           serves frontend/index.html
    │
    └────▶  api  (FastAPI, :8000)  ──▶  ollama  (:11434)
                 │                        model in the ollama-data volume
                 ├── ollama/storage       uploaded files (bind mount)
                 └── library-data         SQLite metadata + extracted text
```

| Service  | Image                  | Port    | Purpose                                   |
| -------- | ---------------------- | ------- | ----------------------------------------- |
| `ollama` | `ollama/ollama:latest` | `11434` | Model runtime (CPU inside the Docker VM)  |
| `api`    | built from `ollama/DockerFile` | `8000`  | Chat proxy, library, extraction worker    |
| `web`    | `nginx:alpine`         | `3000`  | Static UI                                 |

The backend is layered top to bottom: `routes` (HTTP only) → `services` (business logic) → `repository` / `models` (SQL) → `schemas` (request/response shapes).

## Quick start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine with Compose v2)
- ~2 GB free disk for the default model and images

### Run

```bash
git clone git@github.com:harshpujari/llms.git
cd llms/ollama
./scripts/start.sh
```

On the first run the script builds the API image and pulls the model (about 1.3 GB). Later runs reuse both.

When it's ready:

| What   | URL                          |
| ------ | ---------------------------- |
| UI     | http://localhost:3000        |
| API    | http://localhost:8000/docs   |
| Ollama | http://localhost:11434       |

## Usage

```bash
./scripts/start.sh            # start everything (ollama + api + ui)
./scripts/start.sh --build    # force a rebuild (after requirements.txt changes)
./scripts/start.sh --logs     # follow logs

./scripts/stop.sh             # stop and remove containers; the model is kept
./scripts/stop.sh --purge     # also delete the model volume (it's pulled again on next start)
```

### Supported upload formats

| Group                | Extensions                                         |
| -------------------- | -------------------------------------------------- |
| Documents            | `.pdf` `.docx` `.pptx` `.epub` `.msg`              |
| Spreadsheets & data  | `.xlsx` `.xls` `.csv` `.json` `.xml`               |
| Text & web           | `.txt` `.md` `.markdown` `.html` `.htm` `.rst`     |

Max 25 MB per file. PDFs need a text layer, because OCR isn't enabled, so scanned pages won't extract.

### Uninstall

```bash
./scripts/stop.sh --purge                         # containers + model volume
docker volume rm local-llama_library-data         # library metadata and extracted text
docker image rm local-llama-api                   # the API image
```

Uploaded files stay in `ollama/storage/` until you delete them yourself.

## Configuration

Set these in your shell before `./scripts/start.sh`, or in a `.env` file next to `ollama/DockerCompse.yml`.

| Variable       | Default                  | Description                                    |
| -------------- | ------------------------ | ---------------------------------------------- |
| `MODEL`        | `llama3.2:1b`            | Ollama model to pull and chat with             |
| `OLLAMA_HOST`  | `http://ollama:11434`    | Where the API reaches Ollama                   |
| `LOG_LEVEL`    | `INFO`                   | Backend log level                              |
| `STORAGE_ROOT` | `/app/storage`           | Upload directory inside the container          |
| `LIBRARY_DB`   | `/app/data/library.db`   | SQLite database path inside the container      |

For example, to use a larger model:

```bash
MODEL=llama3.2:3b ./scripts/start.sh
```

## API

Interactive docs are at http://localhost:8000/docs. Main endpoints:

| Method   | Path                            | Description                                          |
| -------- | ------------------------------- | ---------------------------------------------------- |
| `GET`    | `/health`                       | API status and whether Ollama is reachable           |
| `POST`   | `/chat`                         | Streamed completion (NDJSON)                         |
| `GET`    | `/folders`                      | List folders                                         |
| `POST`   | `/folders`                      | Create a folder                                      |
| `DELETE` | `/folders/{id}`                 | Delete a folder                                      |
| `GET`    | `/folders/{id}/files`           | List files in a folder                               |
| `POST`   | `/folders/{id}/files`           | Upload one or more files (multipart)                 |
| `GET`    | `/files/{id}/raw`               | Original file (`?download=true` to force a save)     |
| `GET`    | `/files/{id}/text`              | Extracted markdown                                   |
| `DELETE` | `/files/{id}`                   | Delete a file                                        |
| `POST`   | `/extract`                      | Re-queue extraction (`?force=true` for all files)    |
| `GET`    | `/formats`                      | Upload allowlist and size limit                      |

Chat example:

```bash
curl -N http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello!"}],"mode":"chat"}'
```

The response streams one JSON object per line: `{"token": "..."}` for each chunk, then `{"done": true}`, or `{"error": "..."}` if something fails mid-stream.

## Project structure

```
ollama/
├── DockerCompse.yml        # ollama + api + web
├── DockerFile              # API image
├── backend/
│   ├── main.py             # app entry: lifespan, CORS, routers
│   ├── routes/             # HTTP layer (chat, folders, files)
│   ├── services/           # ollama, library, storage, extraction worker
│   ├── repository/         # SQL for folders and files
│   ├── models/             # table definitions
│   ├── schemas/            # Pydantic request/response models
│   └── requirements.txt    # fully pinned dependencies
├── frontend/
│   └── index.html          # single-page UI
├── scripts/
│   ├── start.sh
│   └── stop.sh
├── storage/                # uploaded files (bind-mounted into the API)
└── roadmap.md              # RAG plan
```

## Development

- **Backend**: edit anything in `ollama/backend/`. Uvicorn runs with `--reload`, so the API restarts on its own.
- **Frontend**: edit `ollama/frontend/index.html` and refresh the browser.
- **Dependencies**: add the package to the `direct` block of `ollama/backend/requirements.txt` and run `./scripts/start.sh --build`. Then refresh the pinned `transitive` block:

  ```bash
  docker compose -f DockerCompse.yml exec -T api pip freeze
  ```

- **Logs**: `./scripts/start.sh --logs`, or for one service: `docker compose -f DockerCompse.yml logs -f api`.

## Troubleshooting

| Symptom                                           | Fix                                                                                      |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `Docker isn't running`                            | Start Docker Desktop and re-run the script.                                              |
| `WARNING: the API is up but can't reach Ollama`   | Check `docker compose -f DockerCompse.yml logs ollama`. The model may still be loading. |
| Chat is slow                                      | Ollama runs on CPU inside the Docker VM. Try a smaller model or give Docker more CPUs.  |
| Chat slows down during large uploads              | Expected. Extraction is capped at 1 CPU so it doesn't starve Ollama.                    |
| A PDF shows an extraction error                   | It's probably scanned. OCR isn't supported yet.                                          |
| Port 3000 / 8000 / 11434 already in use           | Stop the other process, or change the port mapping in `ollama/DockerCompse.yml`.               |

## FAQ

**Can it use my Mac's GPU?**
Not inside Docker: the Docker VM has no Metal access, so Ollama runs on CPU. The trade is a stack with nothing installed on the host.

**Can I swap the model?**
Yes. Set `MODEL` to any tag from the [Ollama library](https://ollama.com/library) and re-run `./scripts/start.sh`. It's pulled on first use.

**Where is my data stored?**
Uploaded files are in `ollama/storage/`. Metadata and extracted text are in the `library-data` volume, and the model is in `ollama-data`.

## Roadmap

Retrieval-augmented generation is next: SQLite + `sqlite-vec` for vectors, FTS5 for hybrid search, and `nomic-embed-text` embeddings from the same Ollama container. See [ollama/roadmap.md](ollama/roadmap.md) for the phased plan.

## Security

This is built for local, single-user use. The API has **no authentication**, and CORS only allows `localhost:3000`. Don't expose ports `8000` or `11434` to a network you don't trust.

## Contributing
