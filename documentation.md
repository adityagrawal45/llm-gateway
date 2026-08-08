# LLM Gateway — Documentation

## 1. What this project does

`llm-gateway` is (planned to be) a single HTTP entry point that internal
"teams" call instead of calling OpenAI/Anthropic/Ollama directly. It exists
so that provider API keys, per-team quotas, spend limits, and failover logic
live in one place instead of being duplicated in every client application.

**What's actually built right now (Phase 0):** the scaffolding for that
system — a FastAPI app that:

1. Reads a YAML file (`config/config.yaml`) describing which teams exist, what each team is allowed to call, and their rate/budget limits.
2. Validates that file strictly against a Pydantic schema (unknown fields,wrong types, or duplicate team names all fail loudly).

3. Watches the file on disk and hot-swaps the in-memory config whenever it changes — no process restart needed.
4. Exposes `GET /healthz`, a liveness probe that also reports how many
   teams are currently loaded (a cheap way to confirm hot-reload is alive).

None of the actual gateway behavior — proxying a chat completion request to
a provider, enforcing the rate limit, enforcing the budget, or falling back
to a second provider on failure — exists yet. Those are future phases (see
`CONTEXT.md`).

## 2. Architecture at a glance

```
┌─────────────────────────────────────────────────────────┐
│                        FastAPI app                       │
│  main.py: create_app() + lifespan                        │
│                                                            │
│   lifespan startup:                                       │
│     ConfigLoader(CONFIG_PATH).load()   ── sync, raises    │
│     loader.start_watching()            ── background task │
│     app.state.config_loader = loader                      │
│                                                            │
│   ┌───────────────┐        ┌─────────────────────────┐   │
│   │  api/health.py │◄──────│ request.app.state         │  │
│   │  GET /healthz  │        │   .config_loader.current  │  │
│   └───────────────┘        └─────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
              ▲
              │ watches for file changes
              │
   config/config.yaml  ──validated by──►  config/schema.py
   (teams, keys, limits,                   (GatewayConfig,
    budgets, allowed                        TeamConfig,
    providers/models)                       RateLimitConfig,
                                             BudgetConfig)
```

Everything downstream of "config is loaded and reachable via
`app.state.config_loader`" is where future routing/rate-limit/budget/
fallback logic will hang off.

## 3. Building this from scratch

This walks through recreating the current (Phase 0) state step by step. It
assumes Python 3.11+ and, optionally, Docker.

### Step 1 — project skeleton

```bash
mkdir llm-gateway && cd llm-gateway
mkdir -p src/llm_gateway/config src/llm_gateway/api tests config monitoring
```

Use a **src layout** (`src/llm_gateway/...`) rather than a flat package —
it prevents accidentally importing the package from the repo root instead
of the installed one, which matters once you `pip install -e .`.

### Step 2 — `pyproject.toml`

Declare the package, runtime deps, and dev tooling in one file (no
`setup.py`/`requirements.txt` split):

```toml
[project]
name = "llm-gateway"
version = "0.1.0"
description = "Production-style API gateway in front of multiple LLM providers with rate limiting, budget enforcement, fallback, and observability."
requires-python = ">=3.11"
readme = "README.md"

dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "pydantic>=2.8.0",
    "pydantic-settings>=2.4.0",
    "pyyaml>=6.0.2",
    "redis>=5.0.8",
    "watchfiles>=0.23.0",
    "opentelemetry-api>=1.27.0",
    "opentelemetry-sdk>=1.27.0",
    "opentelemetry-exporter-otlp>=1.27.0",
    "opentelemetry-instrumentation-fastapi>=0.48b0",
    "prometheus-client>=0.20.0",
    "python-dotenv>=1.0.1",
    "httpx>=0.27.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.3.0", "pytest-asyncio>=0.24.0", "pytest-cov>=5.0.0", "ruff>=0.6.0", "mypy>=1.11.0"]

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true
```

Only `redis`, `opentelemetry-*`, and `prometheus-client` are unused right
now — they're declared up front because the roadmap needs them, so the
Docker image and lockstep don't need touching again per phase.

### Step 3 — the config schema (`src/llm_gateway/config/schema.py`)

Model the runtime config as strict Pydantic models. The key design choices:

- `model_config = ConfigDict(extra="forbid")` on every model, so a typo'd
  YAML key fails validation instead of being silently ignored.
- A `Provider` string enum so `allowed_providers` can only ever be one of
  the providers you actually support.
- Cross-field validation via `@model_validator(mode="after")` for rules a
  single field can't express (e.g. `monthly_usd >= daily_usd`, no duplicate
  team names across the whole file).
- A convenience lookup method (`team_by_api_key`) on the top-level model,
  anticipating that request handlers will need "which team owns this
  incoming API key" — even before anything calls it.

```python
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Provider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"

class RateLimitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests_per_minute: int = Field(gt=0)
    tokens_per_minute: int = Field(gt=0)
    burst: int = Field(default=0, ge=0)

class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_usd: float = Field(gt=0)
    monthly_usd: float = Field(gt=0)

    @model_validator(mode="after")
    def _monthly_at_least_daily(self) -> "BudgetConfig":
        if self.monthly_usd < self.daily_usd:
            raise ValueError("monthly_usd must be >= daily_usd")
        return self

class TeamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=8)
    allowed_providers: list[Provider] = Field(min_length=1)
    allowed_models: list[str] = Field(min_length=1)
    rate_limit: RateLimitConfig
    budget: BudgetConfig

class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1)
    teams: list[TeamConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_team_names(self) -> "GatewayConfig":
        names = [t.name for t in self.teams]
        if len(names) != len(set(names)):
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(f"duplicate team name(s) in config: {sorted(dupes)}")
        return self

    def team_by_api_key(self, api_key: str) -> TeamConfig | None:
        return next((t for t in self.teams if t.api_key == api_key), None)
```

### Step 4 — the hot-reloading loader (`src/llm_gateway/config/loader.py`)

Requirements this needs to satisfy:

- A **synchronous** `load()` for startup, so a bad config fails the process
  immediately (fail fast, don't serve traffic with no config).
- An **async background watcher** for runtime changes, so ops can edit the
  YAML and have it take effect without a restart/deploy.
- A bad reload must **never** crash the running process or wipe the good
  config — log the error and keep serving with the last-known-good config.
- Thread-safe access to "the current config", since the watch task and
  request handlers run concurrently.

Design:

```python
from __future__ import annotations
import asyncio, logging, threading
from collections.abc import Awaitable, Callable
from pathlib import Path
import yaml
from pydantic import ValidationError
from watchfiles import Change, awatch
from llm_gateway.config.schema import GatewayConfig

logger = logging.getLogger("llm_gateway.config")
ReloadCallback = Callable[[GatewayConfig], Awaitable[None] | None]

class ConfigError(RuntimeError):
    """Raised when the config file is missing, malformed, or fails schema validation."""

class ConfigLoader:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._config: GatewayConfig | None = None
        self._lock = threading.RLock()
        self._watch_task: asyncio.Task[None] | None = None
        self._on_reload: list[ReloadCallback] = []

    @property
    def current(self) -> GatewayConfig:
        with self._lock:
            if self._config is None:
                raise ConfigError(f"config has not been loaded yet; call load() first ({self._path})")
            return self._config

    def on_reload(self, callback: ReloadCallback) -> None:
        self._on_reload.append(callback)

    def load(self) -> GatewayConfig:
        if not self._path.exists():
            raise ConfigError(f"config file not found: {self._path}")
        try:
            raw = yaml.safe_load(self._path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {self._path}: {exc}") from exc
        try:
            config = GatewayConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"config validation failed for {self._path}:\n{exc}") from exc
        with self._lock:
            self._config = config
        logger.info("config loaded: %s (%d team(s))", self._path, len(config.teams))
        return config

    async def start_watching(self) -> None:
        if self._watch_task is not None:
            return
        self._watch_task = asyncio.create_task(self._watch_loop(), name="config-watch")

    async def stop_watching(self) -> None:
        if self._watch_task is None:
            return
        self._watch_task.cancel()
        try:
            await self._watch_task
        except asyncio.CancelledError:
            pass
        self._watch_task = None

    async def _watch_loop(self) -> None:
        logger.info("watching %s for changes", self._path)
        async for changes in awatch(self._path.parent):
            relevant = any(
                Path(changed_path) == self._path
                for _change_type, changed_path in changes
                if _change_type in (Change.added, Change.modified)
            )
            if not relevant:
                continue
            try:
                config = self.load()
            except ConfigError as exc:
                logger.error("config reload failed, keeping previous config: %s", exc)
                continue
            for callback in self._on_reload:
                result = callback(config)
                if asyncio.iscoroutine(result):
                    await result
```

Notes on the tricky parts:

- `awatch()` is pointed at the **parent directory**, not the file itself,
  because some editors/OSes replace the file (rename over it) on save
  rather than writing in place, which some watchers miss if watching the
  file path directly. The loop then filters events down to the one path we
  care about.
- `on_reload` callbacks let future code (e.g. a rate limiter that caches
  per-team buckets) react to config changes without the loader needing to
  know about them.

### Step 5 — the health endpoint (`src/llm_gateway/api/health.py`)

A minimal FastAPI router, kept separate from `main.py` so routes can grow
by module as phases are added:

```python
from __future__ import annotations
from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["health"])

class HealthResponse(BaseModel):
    status: str
    config_teams: int

@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    loader = request.app.state.config_loader
    team_count = len(loader.current.teams) if loader is not None else 0
    return HealthResponse(status="ok", config_teams=team_count)
```

Reporting `config_teams` isn't just a nicety — it's a cheap end-to-end
signal that config loading *and* hot-reload are actually working, without
needing a separate diagnostic endpoint.

### Step 6 — the app entrypoint (`src/llm_gateway/main.py`)

Wire config loading into FastAPI's `lifespan` context manager so it runs
once at startup (and cleans up at shutdown), rather than lazily on first
request:

```python
from __future__ import annotations
import logging, os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from fastapi import FastAPI
from llm_gateway.api.health import router as health_router
from llm_gateway.config.loader import ConfigError, ConfigLoader

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("llm_gateway")
CONFIG_PATH = os.getenv("CONFIG_PATH", "config/config.yaml")

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    loader = ConfigLoader(CONFIG_PATH)
    try:
        loader.load()
    except ConfigError as exc:
        logger.error("startup config load failed: %s", exc)
        raise
    await loader.start_watching()
    app.state.config_loader = loader
    logger.info("llm-gateway started")
    yield
    await loader.stop_watching()
    logger.info("llm-gateway stopped")

def create_app() -> FastAPI:
    app = FastAPI(title="LLM Gateway", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    return app

app = create_app()
```

Key decision: `CONFIG_PATH` is read from the environment at **module import
time**, which is why tests must set that env var before importing this
module (see Step 8).

### Step 7 — sample runtime config (`config/config.yaml`)

Write a config file that exercises every schema field, with a comment
explaining that it's live-editable:

```yaml
version: 1

teams:
  - name: platform-eng
    api_key: "team-platform-eng-devkey-001"
    allowed_providers: [openai, anthropic]
    allowed_models: [gpt-4o, claude-sonnet-4-6]
    rate_limit: {requests_per_minute: 300, tokens_per_minute: 200000, burst: 50}
    budget: {daily_usd: 100, monthly_usd: 2000}
  # ...more teams
```

### Step 8 — tests (`tests/conftest.py`, `tests/test_smoke.py`)

The fixture must set `CONFIG_PATH` **before** `llm_gateway.main` is
imported anywhere (including via pytest collection), so it's set at module
scope in `conftest.py`, not inside the fixture body:

```python
import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("CONFIG_PATH", str(REPO_ROOT / "config" / "config.yaml"))

@pytest.fixture()
def client() -> TestClient:
    from llm_gateway.main import create_app
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
```

`TestClient` used as a context manager triggers the `lifespan` startup/
shutdown, so the config actually loads during the test. Then assert on the
known shape of the sample config:

```python
def test_app_boots_and_healthz_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["config_teams"] == 3  # sample config.yaml ships with 3 teams

def test_openapi_schema_available(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "LLM Gateway"
```

### Step 9 — containerize it

`Dockerfile`: minimal `python:3.11-slim`, install the package, copy config
separately (so config-only changes don't bust the dependency-install
layer), and add a container-native healthcheck hitting `/healthz`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .
COPY config ./config
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
CMD ["uvicorn", "llm_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`docker-compose.yml`: gateway + the infra later phases will need (Redis for
rate-limit/budget counters, Prometheus + Grafana for observability), wired
up now even though the app doesn't use them yet — so the topology only
needs to be built once. Mount `./config` read-write into the container so
hot-reload can be demoed by editing the host file while the container
runs. Leave provider mocks commented out as a pattern to activate later.

### Step 10 — `.env.example`

Document every env var the app *and* the eventual full stack will read,
even the ones unused today (provider keys, OTel endpoint, Redis URL) — one
file to copy to `.env`, once, instead of accreting vars phase by phase.

### Step 11 — verify

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
pytest                                    # should pass 2 tests
uvicorn llm_gateway.main:app --reload     # http://localhost:8000/healthz
```

Or via Docker: `docker compose up --build`, then check
`http://localhost:8000/healthz`, `http://localhost:9090` (Prometheus),
`http://localhost:3000` (Grafana, admin/admin).

## 4. Extending this (next phases)

The scaffolding already anticipates where future logic attaches:

- **Routing (Phase 1):** add request/response Pydantic models and a router
  under `api/`, mirroring `health.py`'s pattern of pulling
  `request.app.state.config_loader.current` to resolve the calling team
  (via `GatewayConfig.team_by_api_key`).
- **Rate limiting (Phase 2):** `TeamConfig.rate_limit` already has the
  numbers; implement the Redis token-bucket/sliding-window logic and hang
  it off `loader.on_reload()` so limits update live when config changes.
- **Budgets (Phase 3):** same shape, using `TeamConfig.budget` and a
  running spend counter (likely also Redis-backed).
- **Fallback (Phase 4):** `TeamConfig.allowed_providers` is already an
  ordered list — a natural fallback order.
- **Observability (Phase 5):** the OTel/Prometheus deps are already in
  `pyproject.toml` and Prometheus/Grafana are already in
  `docker-compose.yml`; wire `opentelemetry-instrumentation-fastapi` into
  `create_app()` and add a `/metrics` endpoint.
