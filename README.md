# 🛡️ Vibe Ops Center

Terminal-based operations dashboard for managing an AI infrastructure stack (health checks, budgets, Docker, auto-heal).

Repository: https://github.com/iurii-izman/vibe-ops-center

## Features

- Real-time service monitoring (Ollama, LiteLLM, n8n, Langfuse, Qdrant)
- Auto-healing with verification and escalation
- Budget enforcement with automatic shutdown
- Service dependency management (sleep / wake order)
- SQLite-backed history and analytics
- Docker container management (list, logs, stats)
- Structured JSON logs and a cyberpunk Textual UI

## Screenshots

<!-- TODO: add screenshots -->

## Quick Start

Dependencies and tool versions live in **`pyproject.toml`** (runtime + `[project.optional-dependencies] dev`). Use a virtual environment so installs stay isolated from your system Python.

```bash
python -m venv .venv
# Windows (PowerShell): .\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

cp .env.example .env
# Edit .env — required keys match the table below (defaults align with repo config.yaml).

# Adjust config.yaml for your hosts, commands, and paths.

python main.py
```

Production-style install (app dependencies only, no pytest/ruff/pre-commit):

```bash
python -m pip install -e .
```

### Optional: pre-commit

After `pip install -e ".[dev]"`:

```bash
pre-commit install
```

Hooks run **Ruff** check and format on each commit (see `.pre-commit-config.yaml`).

### One-shot stack + `.env` (Windows)

If you use the same layout as `actions.py` (`%USERPROFILE%\ai-stack\docker-compose.yml` with Langfuse and Qdrant), from the repo root run:

```powershell
pwsh -ExecutionPolicy Bypass -File .\scripts\stack_bootstrap.ps1
```

This will:

1. Start **Docker Desktop** if the engine is not reachable, then wait for the daemon.
2. Run **`docker compose up -d`** for that compose file (ports **3000** Langfuse, **6333** Qdrant, etc.).
3. Create **`.env`** from **`.env.example`** only if `.env` does not exist (never overwrites).

You still need real values inside `.env` for n8n / Langfuse API keys (see table below). Windows Credential Manager is not read automatically; copy secrets manually or set env vars in the same shell before `python main.py`.

## Configuration

### `config.yaml`

- **`app`**: `polling_interval`, `health_check_timeout`, `degraded_threshold`, `autoheal_threshold`, `command_timeout`.
- **`circuit_breaker`**: `failure_threshold`, `recovery_timeout` (seconds).
- **`notifications`**: `sound_enabled` (terminal bell on critical transitions / budget).
- **`services`**: `name`, `url`, `type` (`process` | `docker`), `stop_cmd` / `start_cmd` (argv lists), optional `depends_on` for topological sleep/wake.
- **`n8n_api`**: `url`, `header_key`, **`api_key_env`** (name of env var holding the API key).
- **`langfuse_api`**: metrics `url`, **`public_key_env`**, **`secret_key_env`**, `daily_budget_limit`.
- **`qdrant_api`**: collections `url`.
- **`scripts`**: `profile_local_cmd` / `profile_cloud_cmd` (argv lists, often PowerShell on Windows).
- **`tests`**: optional `replyline_webhook` argv for the `t` shortcut.

The bundled `config.yaml` uses Windows-oriented `stop_cmd` / `start_cmd` (e.g. `taskkill`, PowerShell). On Linux or macOS, replace those lists with equivalent commands or use the helpers in `platform_utils.py` as a reference.

### Environment variables (from `.env.example`)

These names are the **defaults** referenced by the repository `config.yaml`. If you change `api_key_env`, `public_key_env`, or `secret_key_env` in YAML, define the matching variables in `.env`.

| Variable | Required | Used for |
| --- | --- | --- |
| `N8N_API_KEY` | Yes (for n8n tab / executions) | n8n API (`n8n_api.api_key_env` → default `N8N_API_KEY`) |
| `LANGFUSE_PUBLIC_KEY` | Yes (for Langfuse metrics) | Basic auth public key (`langfuse_api.public_key_env`) |
| `LANGFUSE_SECRET_KEY` | Yes (for Langfuse metrics) | Basic auth secret key (`langfuse_api.secret_key_env`) |

Secrets are read with `ConfigManager.get_secret()` after `python-dotenv` loads `.env` into the process environment.

**Windows Credential Manager:** the app does **not** read the Windows credential store. Open **Control Panel → Credential Manager → Windows Credentials**, find the entry, copy the password (or use your team’s vault export), and paste into `.env`. Alternatively, set variables in the shell before starting the app (`set` / `$env:NAME = '...'` in PowerShell) and run `python main.py` from that session.

**Why logs still show “connection refused”:** that message means nothing accepted the TCP connection on that host/port (service stopped, wrong port, or Docker not running). It is **not** the same as a missing API key (missing keys usually produce HTTP 401/403 after a connection succeeds).

### Local data files

| Path | Purpose |
| --- | --- |
| `vibe_ops.db` | SQLite (checks, cost history, events) — created automatically |
| `vibe_ops.log` | Rotating JSON-lines log (DEBUG); console stays at WARNING |

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `1` / `2` / `3` | Dashboard / Analytics / Docker |
| `r` | Restart selected service |
| `a` | Toggle auto-heal |
| `t` | Fire test webhook (`tests.replyline_webhook`) |
| `d` | Fix Docker (daemon / Desktop) |
| `i` | Inspect containers (dump to Dashboard log) |
| `c` | Clear Dashboard Rich log |
| `Enter` | Service details when the services table is focused (also double-click row) |
| `?` / `F1` | Help modal |

On **Analytics**, use **“View Log File”** to tail `vibe_ops.log` in a modal.

## Architecture

| Module | Role |
| --- | --- |
| `main.py` | Textual app, tabs, auto-heal, notifications |
| `checker.py` | HTTP health checks, Langfuse / n8n / Qdrant integrations |
| `actions.py` | Subprocess and Docker operations |
| `config_manager.py` | YAML singleton, `.env`, validation, mtime reload |
| `storage.py` | SQLite persistence |
| `resilience.py` | Async retry, circuit breaker |
| `log_config.py` | Console + rotating JSON file logging |
| `platform_utils.py` | OS-specific argv and subprocess flags |
| `screens.py` | Modal screens (help, confirm, service detail, logs) |
| `config.yaml` | Declarative service and app settings |

## Running tests and lint

```bash
pytest tests/ -v
ruff check .
ruff format .
```

## Requirements

- **Python** 3.10+
- **Docker** (optional): for Langfuse container lines, `docker ps` / logs / stats, and compose paths such as `~/ai-stack/docker-compose.yml`
- Processes and URLs defined in **your** `config.yaml` (Ollama, LiteLLM, n8n, Langfuse, Qdrant, …)
