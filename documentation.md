# LLM Gateway — Documentation

## 1. What this project does

`llm-gateway` is (planned to be) a single HTTP entry point that internal
"teams" call instead of calling OpenAI/Anthropic/Ollama directly. It exists
so that provider API keys, per-team quotas, spend limits, and failover logic
live in one place instead of being duplicated in every client application.

**What's actually built right now (Phase 0 + Phase 1):**

Phase 0 — a FastAPI app that:

1. Reads a YAML file (`config/config.yaml`) describing which teams exist, what each team is allowed to call, and their rate/budget limits.
2. Validates that file strictly against a Pydantic schema (unknown fields,wrong types, or duplicate team names all fail loudly).

3. Watches the file on disk and hot-swaps the in-memory config whenever it changes — no process restart needed.
4. Exposes `GET /healthz`, a liveness probe that also reports how many
   teams are currently loaded (a cheap way to confirm hot-reload is alive).

Phase 1 — the actual gateway route, on top of Phase 0:

5. Exposes `POST /v1/chat/completions`, a provider-agnostic chat completion
   endpoint, authenticated by a per-team API key.
6. Routes each request to the right upstream provider (OpenAI, Anthropic,
   or Ollama) behind a common `ProviderClient` interface, translating
   to/from each provider's native request/response shape.
7. Enforces per-team authorization: a team can only use models it's
   configured to be allowed to use, via providers it's allowed to use.

Enforcing the rate limit, enforcing the budget, or falling back to a second
provider on failure still don't exist yet — no retries, no fallback if a
provider call fails (it's just a clean `502`). Those are future phases (see
`CONTEXT.md`).

## 2. Architecture at a glance

```
┌───────────────────────────────────────────────────────────────────┐
│                            FastAPI app                             │
│  main.py: create_app() + lifespan                                  │
│                                                                      │
│   lifespan startup:                                                 │
│     ConfigLoader(CONFIG_PATH).load()   ── sync, raises              │
│     loader.start_watching()            ── background task           │
│     app.state.config_loader = loader                                │
│     app.state.provider_registry = build_registry()  ── built once   │
│                                                                      │
│   ┌───────────────┐                                                 │
│   │ api/health.py  │──────► request.app.state.config_loader.current │
│   │ GET /healthz   │                                                │
│   └───────────────┘                                                 │
│                                                                      │
│   ┌────────────────────────────────────────────────────────────┐   │
│   │ POST /v1/chat/completions            (api/chat.py)          │   │
│   │                                                              │   │
│   │  1. Depends(require_team)     (api/auth.py)                 │   │
│   │     Authorization: Bearer <k> / X-API-Key: <k>               │   │
│   │        └─► loader.current.team_by_api_key(key)               │   │
│   │            401 if unknown                                    │   │
│   │                                                              │   │
│   │  2. body.model in team.allowed_models?    else 403           │   │
│   │                                                              │   │
│   │  3. walk team.allowed_providers in order,                    │   │
│   │     first ProviderClient whose supported_models has the      │   │
│   │     model wins                            else 403           │   │
│   │            │                                                 │   │
│   │            ▼                                                 │   │
│   │  4. registry.get(provider).complete(body)                    │   │
│   │        │            (providers/openai_provider.py etc.)      │   │
│   │        │            translates to/from provider-native shape  │   │
│   │        │            LLM_GATEWAY_MOCK_PROVIDERS=true ──►       │   │
│   │        │              build_mock_response() (no network)      │   │
│   │        └─► 502 on ProviderError, else normalized response     │   │
│   └────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
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
`app.state.config_loader`" (Phase 0) or "a normalized response came back
from `provider_client.complete()`" (Phase 1) is where future rate-limit/
budget/fallback logic will hang off.

## 3. Building this from scratch

This walks through recreating the current (Phase 0 + Phase 1) state step by
step. It assumes Python 3.11+ and, optionally, Docker.

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

### Step 9 — provider-agnostic schemas (`src/llm_gateway/api/schemas.py`)

Before writing any provider code, fix the shape every provider translates
to/from. Modeled on the OpenAI Chat Completions API, since it's the closest
thing to a lingua franca here:

```python
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant"]

class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role
    content: str

class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)

class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class ChatCompletionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"

class ChatCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    model: str
    provider: str
    choices: list[ChatCompletionChoice] = Field(min_length=1)
    usage: Usage
```

Everything downstream — routes, provider clients, tests — imports these
instead of ever touching a provider-native payload directly.

### Step 10 — the provider abstraction (`src/llm_gateway/providers/`)

An abstract base plus a shared mock-response helper:

```python
# providers/base.py
from __future__ import annotations
import uuid
from abc import ABC, abstractmethod
from llm_gateway.api.schemas import (
    ChatCompletionChoice, ChatCompletionRequest, ChatCompletionResponse, ChatMessage, Usage,
)

class ProviderError(RuntimeError):
    """Network error, non-2xx response, or a response shape the gateway can't normalize."""

class ProviderClient(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def supported_models(self) -> frozenset[str]: ...

    @abstractmethod
    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse: ...

def build_mock_response(provider: str, request: ChatCompletionRequest) -> ChatCompletionResponse:
    """Deterministic canned reply, used when LLM_GATEWAY_MOCK_PROVIDERS is on."""
    last_user_msg = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
    reply_text = f"[mock {provider} reply to: {last_user_msg[:80]!r}]"
    prompt_tokens = sum(len(m.content.split()) for m in request.messages)
    completion_tokens = len(reply_text.split())
    return ChatCompletionResponse(
        id=f"mock-{uuid.uuid4().hex[:12]}",
        model=request.model,
        provider=provider,
        choices=[ChatCompletionChoice(index=0, message=ChatMessage(role="assistant", content=reply_text), finish_reason="stop")],
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=prompt_tokens + completion_tokens),
    )
```

Then one implementation per provider (`openai_provider.py`,
`anthropic_provider.py`, `ollama_provider.py`). Each:

- Reads its own env var for credentials/base URL at construction time
  (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OLLAMA_BASE_URL`).
- Checks a module-level `_MOCK = os.getenv("LLM_GATEWAY_MOCK_PROVIDERS", "true")...`
  flag first in `complete()`, returning `build_mock_response()` if it's on
  — so Phase 1 is fully runnable and testable with zero credentials and zero
  network access, and flipping the flag off later is a one-line change to
  go live.
- Otherwise makes a real `httpx.AsyncClient` call and translates the
  response, raising `ProviderError` on any HTTP failure or unexpected
  response shape.

The two non-trivial translations:

- **Anthropic** has no `"system"` role inside `messages` — it's a separate
  top-level `system` string — and `max_tokens` is required on every
  request (no server default), unlike the gateway's optional field. The
  provider pulls system messages out and applies a default `max_tokens`
  when the caller didn't supply one.
- **Ollama** reports token counts under different field names
  (`prompt_eval_count`/`eval_count` instead of `prompt_tokens`/
  `completion_tokens`) and has no request id in its response, so one is
  synthesized with `uuid`.

Finally, a registry that builds all three exactly once:

```python
# providers/registry.py
from llm_gateway.config.schema import Provider
from llm_gateway.providers.anthropic_provider import AnthropicProvider
from llm_gateway.providers.base import ProviderClient
from llm_gateway.providers.ollama_provider import OllamaProvider
from llm_gateway.providers.openai_provider import OpenAIProvider

class ProviderRegistry:
    def __init__(self, clients: dict[Provider, ProviderClient]):
        self._clients = clients
    def get(self, provider: Provider) -> ProviderClient:
        return self._clients[provider]

def build_registry() -> ProviderRegistry:
    return ProviderRegistry({
        Provider.OPENAI: OpenAIProvider(),
        Provider.ANTHROPIC: AnthropicProvider(),
        Provider.OLLAMA: OllamaProvider(),
    })
```

### Step 11 — auth dependency (`src/llm_gateway/api/auth.py`)

A FastAPI dependency, not ASGI middleware — it needs
`request.app.state.config_loader`, which is only reliably attached once
routing starts, and "this route needs a resolved team" is exactly what
`Depends()` + `HTTPException` expresses:

```python
from __future__ import annotations
from fastapi import Header, HTTPException, Request, status
from llm_gateway.config.schema import TeamConfig

def _extract_api_key(authorization: str | None, x_api_key: str | None) -> str | None:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    if x_api_key:
        return x_api_key
    return None

async def require_team(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> TeamConfig:
    api_key = _extract_api_key(authorization, x_api_key)
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing API key")

    loader = request.app.state.config_loader
    team = loader.current.team_by_api_key(api_key)
    if team is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")

    request.state.team = team
    return team
```

Looking the key up against `loader.current` on every request (not a cached
copy) means revoking or rotating a team's key in `config.yaml` takes effect
immediately via the Phase 0 hot-reload, with no extra plumbing.

### Step 12 — the chat route (`src/llm_gateway/api/chat.py`)

```python
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Request, status
from llm_gateway.api.auth import require_team
from llm_gateway.api.schemas import ChatCompletionRequest, ChatCompletionResponse
from llm_gateway.config.schema import TeamConfig
from llm_gateway.providers.base import ProviderClient, ProviderError

router = APIRouter(tags=["chat"])

def _select_provider(request: Request, team: TeamConfig, model: str) -> ProviderClient:
    registry = request.app.state.provider_registry
    for provider in team.allowed_providers:
        try:
            candidate = registry.get(provider)
        except KeyError:
            continue
        if model in candidate.supported_models:
            return candidate
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"model '{model}' not served by any allowed provider")

@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    body: ChatCompletionRequest,
    request: Request,
    team: TeamConfig = Depends(require_team),
) -> ChatCompletionResponse:
    if body.model not in team.allowed_models:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"team '{team.name}' is not allowed to use model '{body.model}'")

    provider_client = _select_provider(request, team, body.model)
    try:
        return await provider_client.complete(body)
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
```

Two authorization checks, deliberately layered: `allowed_models` is what
the team is contractually permitted to use; the provider-support check
that follows is what's actually *implemented*. Keeping them separate means
a config listing a model no provider client supports yet fails clearly at
request time (403) rather than crashing.

Provider selection walks `team.allowed_providers` **in the order
configured** and stops at the first match — that ordering is deliberately
reused as the fallback order once Phase 4 adds retry-on-failure.

### Step 13 — wire it into `main.py`

```python
from llm_gateway.api.chat import router as chat_router
from llm_gateway.providers.registry import build_registry
# ...inside lifespan(), after loader.start_watching():
app.state.provider_registry = build_registry()
# ...inside create_app():
app.include_router(chat_router)
```

The registry is built once per process (in `lifespan`, alongside the
config loader) and reused across every request — not rebuilt per call,
same principle as the config loader itself.

### Step 14 — tests (`tests/test_chat.py`)

Three things worth testing deliberately, plus a routing sanity check:

- **Auth rejection** — missing key and an unrecognized key both 401.
- **Authorization rejection** — a model outside `team.allowed_models` 403s;
  separately, a model no *registered* provider client claims to support
  403s too (tests this by swapping in a fake client with an empty
  `supported_models`).
- **Happy path** — a hand-written `FakeProviderClient` (implementing the
  `ProviderClient` interface, no real network) is swapped into
  `app.state.provider_registry` after the `TestClient` context has already
  triggered `lifespan` startup. This is enough to assert both the HTTP
  response shape *and* that the fake actually received the call — a real
  routing test, not just a schema check.

```python
def test_happy_path_completion_routes_to_correct_provider(client):
    fake = FakeOpenAIProvider()
    client.app.state.provider_registry = ProviderRegistry({Provider.OPENAI: fake})

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"},
        json=CHAT_BODY,
    )
    assert response.status_code == 200
    assert len(fake.received) == 1
```

`conftest.py` also grew one line: `LLM_GATEWAY_MOCK_PROVIDERS=true` is set
(via `setdefault`, so a developer's real `.env` can still override it
locally) before `llm_gateway.main` is imported, for the same reason
`CONFIG_PATH` already was — providers read it at *module* import time, so
it has to exist before that import happens, not before the test body runs.

### Step 15 — containerize it

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

### Step 16 — `.env.example`

Document every env var the app *and* the eventual full stack will read,
even the ones unused today (OTel endpoint, Redis URL) — one file to copy to
`.env`, once, instead of accreting vars phase by phase. `LLM_GATEWAY_MOCK_PROVIDERS`
isn't in there by default (it defaults to `true` in code), but add it
explicitly, set to `false`, once you're ready to hit real providers.

### Step 17 — verify

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
pytest                                    # should pass 8 tests (see section 4)
uvicorn llm_gateway.main:app --reload     # http://localhost:8000/healthz
```

Or via Docker: `docker compose up --build`, then check
`http://localhost:8000/healthz`, `http://localhost:9090` (Prometheus),
`http://localhost:3000` (Grafana, admin/admin).

## 4. Testing this, and what we actually got

This section is real output captured from this exact codebase — not a
hypothetical.

### Running the test suite

```bash
$ pytest -v
```

```
collected 8 items

tests/test_chat.py::test_missing_api_key_rejected PASSED                 [ 12%]
tests/test_chat.py::test_invalid_api_key_rejected PASSED                 [ 25%]
tests/test_chat.py::test_x_api_key_header_accepted PASSED                [ 37%]
tests/test_chat.py::test_model_not_allowed_for_team_rejected PASSED      [ 50%]
tests/test_chat.py::test_model_not_served_by_any_allowed_provider_rejected PASSED [ 62%]
tests/test_chat.py::test_happy_path_completion_routes_to_correct_provider PASSED [ 75%]
tests/test_smoke.py::test_app_boots_and_healthz_ok PASSED                [ 87%]
tests/test_smoke.py::test_openapi_schema_available PASSED                [100%]

======================== 8 passed, 1 warning in 0.37s =========================
```

(The one warning is `StarletteDeprecationWarning: Using httpx with
starlette.testclient is deprecated` — pre-existing, unrelated to this
code, harmless for now.)

What each test actually proves:

| Test | Proves |
|---|---|
| `test_missing_api_key_rejected` | No `Authorization`/`X-API-Key` header → `401`, not a 500 or silent pass-through |
| `test_invalid_api_key_rejected` | A well-formed but unrecognized key → `401` |
| `test_x_api_key_header_accepted` | The `X-API-Key` header path works, not just `Authorization: Bearer` |
| `test_model_not_allowed_for_team_rejected` | A team's `allowed_models` list is actually enforced (`403`) |
| `test_model_not_served_by_any_allowed_provider_rejected` | A model no *registered client* supports 403s cleanly instead of raising an unhandled `KeyError` |
| `test_happy_path_completion_routes_to_correct_provider` | End-to-end: auth passes, authorization passes, the right `ProviderClient` is invoked with the right request, and its response is returned as-is |

### Running the server and hitting it manually

With `LLM_GATEWAY_MOCK_PROVIDERS` left at its default (`true`), no provider
credentials are needed:

```bash
$ uvicorn llm_gateway.main:app --host 127.0.0.1 --port 8123
```

**Liveness check:**

```bash
$ curl -s http://127.0.0.1:8123/healthz
{"status":"ok","config_teams":3}
```

**Happy path — `platform-eng` team asking for `gpt-4o` (routed to the mocked OpenAI client):**

```bash
$ curl -s http://127.0.0.1:8123/v1/chat/completions \
    -H "Authorization: Bearer team-platform-eng-devkey-001" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-4o","messages":[{"role":"user","content":"What is the capital of France?"}]}'
```
```json
{"id":"mock-03177acac010","model":"gpt-4o","provider":"openai","choices":[{"index":0,"message":{"role":"assistant","content":"[mock openai reply to: 'What is the capital of France?']"},"finish_reason":"stop"}],"usage":{"prompt_tokens":6,"completion_tokens":10,"total_tokens":16}}
```

**Same team, asking for an Anthropic model instead (routed to the mocked Anthropic client):**

```bash
$ curl -s http://127.0.0.1:8123/v1/chat/completions \
    -H "Authorization: Bearer team-platform-eng-devkey-001" \
    -H "Content-Type: application/json" \
    -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"Say hi in one word"}]}'
```
```json
{"id":"mock-d22932776ab0","model":"claude-sonnet-4-6","provider":"anthropic","choices":[{"index":0,"message":{"role":"assistant","content":"[mock anthropic reply to: 'Say hi in one word']"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":9,"total_tokens":14}}
```

**`sandbox` team, asking for `llama3` (routed to the mocked Ollama client):**

```bash
$ curl -s http://127.0.0.1:8123/v1/chat/completions \
    -H "Authorization: Bearer team-sandbox-devkey-003" \
    -H "Content-Type: application/json" \
    -d '{"model":"llama3","messages":[{"role":"user","content":"hi"}]}'
```
```json
{"id":"mock-bab66389e013","model":"llama3","provider":"ollama","choices":[{"index":0,"message":{"role":"assistant","content":"[mock ollama reply to: 'hi']"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":5,"total_tokens":6}}
```

**No API key at all:**

```bash
$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8123/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
401
```

**`sandbox` team (only allowed `llama3`/`ollama`) asking for `gpt-4o`:**

```bash
$ curl -s http://127.0.0.1:8123/v1/chat/completions \
    -H "Authorization: Bearer team-sandbox-devkey-003" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
```
```json
{"detail":"team 'sandbox' is not allowed to use model 'gpt-4o'"}
```

The mocked reply text and the `id` are deterministic in shape
(`mock-<random hex>`) but random per call — the request/response wiring,
routing decision, and per-team authorization are what's actually being
exercised here, not the reply content itself. Swap `LLM_GATEWAY_MOCK_PROVIDERS=false`
plus real credentials and the same curl commands hit the real providers,
unchanged.

### Available team API keys (from the sample `config/config.yaml`)

| Team | API key | Allowed providers | Allowed models |
|---|---|---|---|
| `platform-eng` | `team-platform-eng-devkey-001` | openai, anthropic | gpt-4o, claude-sonnet-4-6 |
| `data-science` | `team-data-science-devkey-002` | anthropic, ollama | claude-sonnet-4-6, llama3 |
| `sandbox` | `team-sandbox-devkey-003` | ollama | llama3 |

## 5. Extending this (next phases)

Phase 0 (config) and Phase 1 (routing) are both done; the code already
anticipates where the rest attaches:

- **Rate limiting (Phase 2):** `TeamConfig.rate_limit` already has the
  numbers; implement the Redis token-bucket/sliding-window logic, likely as
  another dependency chained after `require_team` in `chat.py`, and hang it
  off `loader.on_reload()` so limits update live when config changes.
- **Budgets (Phase 3):** same shape, using `TeamConfig.budget` and a
  running spend counter (likely also Redis-backed), computed from each
  `ChatCompletionResponse.usage`.
- **Fallback (Phase 4):** `chat.py::_select_provider` already walks
  `team.allowed_providers` in order and stops at the first match — Phase 4
  is "on `ProviderError`, keep walking and try the next one" instead of
  stopping, reusing the same ordering as the routing decision.
- **Observability (Phase 5):** the OTel/Prometheus deps are already in
  `pyproject.toml` and Prometheus/Grafana are already in
  `docker-compose.yml`; wire `opentelemetry-instrumentation-fastapi` into
  `create_app()` and add a `/metrics` endpoint. `ChatCompletionResponse`
  already carries `usage` and `provider`, which is most of what a
  per-provider request/token/cost metric needs.
