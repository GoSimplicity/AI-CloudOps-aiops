# AI-CloudOps AIOps Service

**English** | [简体中文](README.md)

The AIOps service of **AI-CloudOps**, an open-source, AI-assisted cloud-native operations platform. It is an internal FastAPI service that provides root cause analysis, load prediction, Kubernetes inspection, an operations assistant (RAG + MCP tool calling) and human-approved Kubernetes auto-remediation.

The service depends on Prometheus, Kubernetes, Redis and an LLM provider in your environment. Prediction and diagnosis results should always be validated against your actual environment.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.159+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

## Project Components

AI-CloudOps is made up of three repositories that are deployed together:

| Repository | Role | Stack |
| --- | --- | --- |
| **[AI-CloudOps](https://github.com/GoSimplicity/AI-CloudOps)** | Backend API, authentication/RBAC, and gateway to this service | Go, Gin, GORM, MySQL, Redis |
| **[AI-CloudOps-web](https://github.com/GoSimplicity/AI-CloudOps-web)** | Web console | Vue 3, TypeScript, Vben Admin, Ant Design Vue |
| **[AI-CloudOps-aiops](https://github.com/GoSimplicity/AI-CloudOps-aiops)** (this repo) | AIOps service: RCA, prediction, assistant (RAG + MCP), auto-remediation | Python, FastAPI, LangGraph, scikit-learn |

```text
Browser ──> Go backend (JWT + RBAC, /api/v1/*) ──X-API-Key──> AIOps API (:8080) ──> Prometheus / Kubernetes / Redis / LLM
                                                                   │
                                                                   └──X-API-Key──> MCP server (:9000) ──> Kubernetes tools
```

This service is **not meant to be exposed to browsers or the public internet**. End users reach it only through the Go backend, which checks the user's JWT and role permissions and then injects the shared `AIOPS_API_KEY`.

## Features

### Operations assistant (RAG + MCP)
- **RAG mode** (`mode: 1`): answers questions from a knowledge base you provide (Markdown documents under `data/knowledge_base`, or uploaded through the API). Documents and metadata are stored in Redis; vector search uses an in-process FAISS index, so Redis Stack is not required.
- **MCP mode** (`mode: 2`): calls tools on the bundled MCP server: Kubernetes pods, deployments, services, ingresses, namespaces, logs, scaling, cluster checks and configuration, plus time, calculator, system info and sandboxed file tools.
- Multi-turn sessions, HTTP and WebSocket streaming (`/api/v1/assistant/stream`).
- OpenAI-compatible LLM providers or Ollama.

### Root cause analysis (RCA)
- Collects Prometheus metrics, Kubernetes events and Pod logs for a namespace and time window, detects anomalies and correlations, and produces a root cause report.
- Quick diagnosis, event patterns, error summaries and raw data endpoints.

### Load prediction
- QPS, CPU, memory and disk prediction for 1 to 168 hours, with instance and resource recommendations.
- Uses real historical metrics from Prometheus. If Prometheus fails or returns no data, the API returns an explicit error instead of generating synthetic history. When no trained model is available, a rule-based extrapolation is used. Its confidence values are heuristic, not calibrated guarantees, so do not use them to drive autoscaling directly.
- Training scripts for the four models are in [`data/`](data/README.md).

### Kubernetes inspection
- Rule-based inspection of Pods, Services, PVCs and performance signals (for example Pod phase, restart counts, Services without endpoints, unbound PVCs, CPU throttling, high error rates), with profiles, history, Markdown reports and an optional scheduler.

### Auto-remediation
- Diagnose Kubernetes workloads and generate a remediation plan through a multi-agent workflow (supervisor, K8s fixer, notifier agents built with LangGraph).
- Plans are executed only for the action IDs a human explicitly approves (`POST /api/v1/autofix/workflow/confirm`).
- Grant the service a least-privilege Kubernetes identity. Never use a cluster-admin identity.

## API Overview

All business endpoints are under `/api/v1` and require the service key, sent as `X-API-Key: <key>` or `Authorization: Bearer <key>`.

| Module | Prefix | Examples |
| --- | --- | --- |
| Assistant | `/api/v1/assistant` | `POST /query`, `POST /session`, `GET /ready`, `POST /upload-knowledge-file`, `POST /add-document`, `WS /stream` |
| RCA | `/api/v1/rca` | `POST /analyze`, `POST /quick-diagnosis`, `GET /metrics`, `POST /incident` |
| Prediction | `/api/v1/predict` | `POST /qps`, `POST /cpu`, `POST /memory`, `POST /disk`, `GET /ready`, `GET /models` |
| Inspection | `/api/v1/inspection` | `POST /run`, `GET /history`, `GET /report/{report_id}/markdown` |
| Auto-remediation | `/api/v1/autofix` | `POST /diagnose`, `POST /workflow`, `POST /workflow/confirm`, `GET /ready` |
| Cache | `/api/v1/cache` | `GET /stats`, `POST /clear` |

- `GET /health` is the only unauthenticated endpoint. It is a liveness check and does not mean Redis, Kubernetes or the LLM are available. Use the authenticated per-module `/ready` endpoints for readiness.
- Interactive OpenAPI docs are served at `/docs` (and `/openapi.json`), behind the same service-key check.
- MCP server documentation (Chinese): [app/mcp/README.md](app/mcp/README.md).

## Quick Start

### Requirements

- Python 3.11
- Docker with Docker Compose v2
- Redis, Prometheus and an LLM API (OpenAI-compatible or Ollama)
- Optional: a Kubernetes cluster and a least-privilege kubeconfig

### Run with Docker Compose (this repo only)

```bash
git clone https://github.com/GoSimplicity/AI-CloudOps-aiops.git
cd AI-CloudOps-aiops
cp env.example .env
# Edit .env: set LLM_API_KEY / LLM_BASE_URL / LLM_MODEL, a random REDIS_PASSWORD,
# and AIOPS_API_KEY (generate with: openssl rand -hex 32)
# Optional: put a least-privilege kubeconfig at deploy/kubernetes/config (mounted read-only)

docker compose config --quiet      # validate without starting anything
docker compose up -d --build
```

This starts the API (`cloudops-platform`, port 8080), the MCP server (`cloudops-mcp`, port 9000), Prometheus (9090) and Redis (6379). All ports are bound to `127.0.0.1`.

```bash
curl --fail http://127.0.0.1:8080/health
curl --fail -H "X-API-Key: $AIOPS_API_KEY" http://127.0.0.1:8080/api/v1/predict/ready
```

### Run the full platform

To run the backend, web console, this service and the MCP server together behind one gateway, use the release Compose bundle in the backend repository: [AI-CloudOps `deploy/release`](https://github.com/GoSimplicity/AI-CloudOps#deployment-all-three-services). It expects this repository to be checked out next to the backend as `Ai-CloudOps-aiops`.

### Run locally without Docker

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env               # point REDIS_*, PROMETHEUS_HOST, LLM_* at your services

python -m app.mcp.main --host 127.0.0.1 --port 9000   # MCP server (separate terminal)
python -m app.main                                     # API on HOST:PORT (default 0.0.0.0:8080)
```

## Configuration

Settings are read from environment variables / `.env` (see [`env.example`](env.example)) and `config/*.yaml`. Key variables:

| Variable | Description |
| --- | --- |
| `ENV` | `development` or `production`. |
| `AIOPS_API_KEY` | Shared service key for the API and MCP server. **Required in production, minimum 32 characters**. Startup fails if it is missing or too short. Must match the Go backend's `AIOPS_API_KEY`. |
| `HOST`, `PORT` | Bind address, default `0.0.0.0:8080`. |
| `LLM_PROVIDER` | `openai` (any OpenAI-compatible API) or `ollama`. |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_TASK_MODEL`, `LLM_EMBEDDING_MODEL` | LLM and embedding settings. |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | Local Ollama settings. |
| `MCP_SERVER_URL` | URL of the MCP server, e.g. `http://localhost:9000` (in Compose: `http://cloudops-mcp:9000` or `http://mcp:9000`). |
| `MCP_FILE_ROOT` | Root directory for the MCP file tool (default `data/knowledge_base`). Paths outside it and escaping symlinks are rejected, and reads are capped at 1 MiB. |
| `PROMETHEUS_HOST` | Prometheus `host:port` **without** scheme, e.g. `prometheus:9090`. |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB` | Redis connection. |
| `K8S_IN_CLUSTER`, `K8S_CONFIG_PATH`, `K8S_NAMESPACE` | Kubernetes access. In containers use `/app/deploy/kubernetes/config`. |
| `CORS_ORIGINS` | Explicit comma-separated origins. Wildcards are not allowed. |
| `AIOPS_PREWARM` | Set `true` to initialise dependencies at startup. By default they are initialised on demand, and startup and `/health` never call the model. |
| `RAG_*`, `RCA_*`, `PREDICTION_*`, `INSPECTION_*` | Feature tuning. |
| `FEISHU_WEBHOOK`, `TAVILY_API_KEY` | Optional notifications and web search. |

### Production notes

- Set `ENV=production` and inject `AIOPS_API_KEY` from a secret manager. Use the same value in the Go backend, the API and the MCP server.
- Keep the API and MCP server on a private network. Remote access should go through the Go gateway with TLS.
- Kubeconfigs are mounted read-only from `deploy/kubernetes/`. The image contains no cluster credentials. Never commit kubeconfigs, `.env` or provider keys.
- Never put the service key or provider keys into frontend `VITE_*` configuration.
- Earlier versions of this repository tracked a Redis password and a kubeconfig containing a client private key. Those credentials must be revoked/rotated before any production use, and the historical values must not be reused.

Full details (Chinese): [docs/production.md](docs/production.md) and [docs/security-review.md](docs/security-review.md).

## Development and Tests

```bash
pip install -r requirements.txt
python -m pip check
python -m pytest tests/unit tests/test_predict_service_unit.py -q
```

`requirements.in` lists direct dependencies. `requirements.txt` is the fully pinned lock for Python 3.11 (`uv pip compile requirements.in --python-version 3.11 --universal -o requirements.txt`). The top-level scripts in `tests/` call real services and are not part of the offline gate. CI (`.github/workflows/verify.yml`) runs `pip check`, `pip-audit`, the unit tests, `docker compose config` and a Docker build.

## Repository Layout

```text
AI-CloudOps-aiops/
├── app/
│   ├── main.py             # FastAPI application entry point
│   ├── api/                # Routes (assistant, rca, predict, inspection, autofix, cache) and middleware (auth, CORS)
│   ├── core/               # Agents, RCA engine, prediction, inspection rules, retrieval, vector store
│   ├── services/           # Service layer (LLM, Kubernetes, Prometheus, MCP, notification, ...)
│   ├── mcp/                # MCP server and tools
│   ├── models/             # Pydantic request/response models
│   └── config/             # Settings and logging
├── config/                 # config.yaml / config.production.yaml / config.test.yaml
├── data/                   # Knowledge base, sample data, model training scripts
├── deploy/                 # Prometheus/Grafana config, kubeconfig mount point, predict operator, test manifests
├── docs/                   # Guides and security/production notes
└── tests/                  # Unit tests (tests/unit) and integration scripts
```

## Online Demo

The public online demo is **temporarily unavailable**. Please run the service locally with Docker Compose as described above.

## Contributing

1. Fork the repository and create a feature branch.
2. Run `python -m pytest tests/unit -q` and `ruff` (configured in `pyproject.toml`) before submitting.
3. Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages.
4. Open a pull request describing the change and how it was tested.

## License

This repository is licensed under the [Apache License 2.0](LICENSE).

## Contact

- **Email:** <bamboocloudops@gmail.com>
- **Issues:** <https://github.com/GoSimplicity/AI-CloudOps-aiops/issues>
