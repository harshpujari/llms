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
