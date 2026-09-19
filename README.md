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
