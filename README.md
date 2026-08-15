# LLM Gateway

A production-style API gateway that sits in front of multiple LLM providers
(OpenAI, Anthropic, Ollama) and gives every internal "team" (tenant) a
single, provider-agnostic HTTP endpoint to call instead of talking to each
provider directly. It centralizes:

- **Authentication** — per-team API keys, resolved against a hot-reloadable config file
- **Authorization** — each team is scoped to specific models and providers
- **Rate limiting** — Redis-backed, per-team requests/minute *and* tokens/minute
- **Budget enforcement** — Redis-backed, per-team daily/monthly USD spend caps
- **Provider abstraction** — one request/response shape regardless of which upstream provider actually serves it
- **Observability** *(planned)* — OpenTelemetry traces → Prometheus metrics → Grafana dashboards

> **Status:** Phase 3 complete — request routing, provider abstraction,
> auth, per-team Redis-backed rate limiting, and per-team budget enforcement
> are all implemented and tested. Provider fallback (Phase 4) and the
> OTel/Prometheus/Grafana instrumentation (Phase 5) are not implemented yet
> — see [Roadmap](#roadmap).

## Table of contents

- [Why this exists](#why-this-exists)
- [Project layout](#project-layout)
- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [Local development (without Docker)](#local-development-without-docker)
- [Running the full stack (Docker Compose)](#running-the-full-stack-docker-compose)
- [Configuration](#configuration)
- [Authentication](#authentication)
- [API reference](#api-reference)
- [Rate limiting](#rate-limiting)
- [Budget enforcement](#budget-enforcement)
- [Providers](#providers)
- [Tests](#tests)
- [Environment variables](#environment-variables)
- [Roadmap](#roadmap)
- [Non-obvious design notes](#non-obvious-design-notes)

## Why this exists

Without a gateway, every internal application that wants to call an LLM
ends up duplicating the same concerns: where to get a provider API key,
how to keep one team's usage from starving another's, how to stop a bug (or
an abusive client) from blowing through a monthly budget, and what to do
when a provider is down. `llm-gateway` centralizes all of that behind one
HTTP endpoint per concern (`POST /v1/chat/completions`, `GET
/v1/teams/{team}/budget`), so application code only needs a gateway API key
and the provider-agnostic request/response shape.

## Project layout

```
llm-gateway/
├── src/llm_gateway/            # application package (src layout)
│   ├── main.py                  # FastAPI app factory + lifespan (config, providers, redis, limiter, budget tracker)
│   ├── redis_client.py          # shared redis.asyncio connection, built once at startup
│   ├── redis_lua.py             # shared atomic increment-and-expire Lua script (rate_limit + budget)
│   ├── config/
│   │   ├── schema.py             # Pydantic models: GatewayConfig, TeamConfig, RateLimitConfig, BudgetConfig, Provider
│   │   └── loader.py             # ConfigLoader: sync load() + async file-watch hot-reload
│   ├── api/
│   │   ├── health.py             # GET /healthz — liveness + loaded team count
│   │   ├── auth.py               # require_team dependency: Bearer/X-API-Key -> TeamConfig, 401 if unknown
│   │   ├── rate_limit.py         # enforce_rate_limit dependency: requests/min + tokens/min, 429 on violation
│   │   ├── budget.py              # enforce_budget dependency + GET /v1/teams/{team}/budget
│   │   ├── chat.py                # POST /v1/chat/completions — the core route
│   │   └── schemas.py             # ChatCompletionRequest/Response, ChatMessage, Usage (provider-agnostic)
│   ├── providers/                 # ProviderClient abstraction
│   │   ├── base.py                 # ProviderClient ABC, ProviderError, build_mock_response()
│   │   ├── openai_provider.py      # OpenAIProvider
│   │   ├── anthropic_provider.py   # AnthropicProvider
│   │   ├── ollama_provider.py      # OllamaProvider
│   │   └── registry.py             # ProviderRegistry + build_registry() factory (built once at startup)
│   ├── rate_limit/
│   │   └── limiter.py             # Redis-backed per-team requests/min + tokens/min limiter
│   └── budget/
│       ├── pricing.py             # hand-maintained per-model USD pricing table
│       └── tracker.py             # Redis-backed daily/monthly spend tracker
├── config/
│   └── config.yaml               # runtime config: teams, keys, allowed models/providers, limits, budgets
├── tests/                        # pytest suite (smoke, chat, rate limit, budget)
├── monitoring/
│   ├── prometheus.yml             # Prometheus scrape config
│   └── grafana/provisioning/      # Grafana datasource + dashboard provisioning stubs
├── docker-compose.yml             # gateway + redis + prometheus + grafana
├── Dockerfile
├── pyproject.toml                 # deps, ruff/mypy/pytest config
├── documentation.md                # deep-dive build log / architecture notes
├── CONTEXT.md                      # agent/contributor onboarding snapshot
└── .env.example
```

## Requirements

- Python 3.11+
- Docker + Docker Compose (only needed for the full stack — Redis, Prometheus, Grafana)
- Redis (required even for local dev without Docker — see below; the app fails to start without it)

## Quickstart

Fastest way to see the gateway respond, using the full Docker Compose stack:

```bash
git clone <this-repo>
cd llm-gateway
cp .env.example .env
docker compose up --build
```

Then, in another terminal, call the chat completions endpoint. Provider
calls are **mocked by default** (`LLM_GATEWAY_MOCK_PROVIDERS=true`), so this
works immediately with no provider API keys:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer team-platform-eng-devkey-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}'
```

Set `LLM_GATEWAY_MOCK_PROVIDERS=false` plus the relevant provider credential
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OLLAMA_BASE_URL`) to hit a real
provider instead of the canned mock response.

## Local development (without Docker)

The app needs Redis reachable at `REDIS_URL` — it fails to boot without it
(see [Non-obvious design notes](#non-obvious-design-notes)). Easiest is to
run just Redis via Compose and the app itself locally:

```bash
docker compose up redis -d

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# .env's REDIS_URL defaults to redis://redis:6379/0 (the in-Compose hostname) --
# point it at redis://localhost:6379/0 when running the app outside Docker.

uvicorn llm_gateway.main:app --reload
```

The app boots on `http://localhost:8000`.

- Health check: `GET /healthz`
- Interactive API docs (Swagger UI): `http://localhost:8000/docs`
- OpenAPI schema: `http://localhost:8000/openapi.json`

## Running the full stack (Docker Compose)

```bash
docker compose up --build
```

| Service     | URL                                   | Notes                                          |
|-------------|-----------------------------------------|-------------------------------------------------|
| Gateway     | http://localhost:8000                   | `/healthz`, `/docs`, `/v1/...`                   |
| Prometheus  | http://localhost:9090                   | scrape config in `monitoring/prometheus.yml`     |
| Grafana     | http://localhost:3000 (`admin`/`admin`) | provisioning stubs only — no dashboards populated yet (Phase 5) |
| Redis       | localhost:6379                          | backs rate limiting + budget counters            |

`docker-compose.yml` mounts `./config` into the gateway container
read-write specifically so you can exercise hot-reload by editing
`config/config.yaml` on the host while the container keeps running — no
rebuild or restart needed.

## Configuration

Runtime behavior — which teams exist, their gateway API keys, which
providers/models each is allowed to use, and their rate limits and budgets
— lives entirely in `config/config.yaml`, validated on load (and on every
change) against the Pydantic schema in `src/llm_gateway/config/schema.py`.
The schema is `extra="forbid"` everywhere, so a YAML typo or unknown field
fails loudly instead of being silently ignored.

`ConfigLoader` (`src/llm_gateway/config/loader.py`) watches the file's
parent directory and hot-reloads it into memory on change — **no process
restart required**. A bad edit is logged and the previous good config stays
active; the process never crashes from a config typo. `CONFIG_PATH` (env
var, default `config/config.yaml`) selects which file to load.

Example team entry:

```yaml
teams:
  - name: platform-eng
    api_key: "team-platform-eng-devkey-001"   # gateway-facing key, distinct from provider keys
    allowed_providers: [openai, anthropic]     # tried in this order
    allowed_models:
      - gpt-4o
      - claude-sonnet-4-6
    rate_limit:
      requests_per_minute: 300
      tokens_per_minute: 200000
      burst: 50
    budget:
      daily_usd: 100
      monthly_usd: 2000
```

`config/config.yaml` ships with three sample teams (`platform-eng`,
`data-science`, `sandbox`) with different provider access, limits, and
budgets — useful for exercising 401/403/429/402 responses locally.

## Authentication

Every route except `/healthz` requires a team API key, via either header:

```
Authorization: Bearer <team_api_key>
X-API-Key: <team_api_key>
```

The key is looked up against the **current** hot-reloaded config on every
request, so rotating or revoking a team's key in `config.yaml` takes effect
immediately, without a restart. An unrecognized or missing key returns
`401 Unauthorized`.

## API reference

### `GET /healthz`

Liveness probe. No auth required.

```json
{"status": "ok", "config_teams": 3}
```

`config_teams` also doubles as a cheap check that hot-reload is alive —
it reflects the count from whatever config is currently loaded.

### `POST /v1/chat/completions`

The core, provider-agnostic chat completion route. Request/response are
shaped like the OpenAI Chat Completions API (the closest thing to a lingua
franca for this kind of endpoint) — callers don't need to know or care
which upstream provider actually served a given model.

**Request:**

```json
{
  "model": "gpt-4o",
  "messages": [
    {"role": "system", "content": "You are concise."},
    {"role": "user", "content": "hello"}
  ],
  "temperature": 1.0,
  "max_tokens": 256
}
```

`temperature` (0.0–2.0, default 1.0) and `max_tokens` (optional) are
passed through; `messages` requires at least one entry with `role` one of
`system` / `user` / `assistant`.

**Response (`200`):**

```json
{
  "id": "mock-...",
  "model": "gpt-4o",
  "provider": "openai",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}
  ],
  "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
}
```

**Request flow, in order** — each step can short-circuit the rest:

1. `require_team` — resolve the API key to a `TeamConfig`, else `401`.
2. `enforce_rate_limit` — check requests/min and tokens/min for the team,
   else `429` with a `Retry-After` header. A request-count credit is
   consumed here even if a later step rejects the request (so probing
   disallowed models isn't a way to dodge the limit).
3. `enforce_budget` — check daily/monthly spend against the team's budget,
   else `402 Payment Required`.
4. Validate `model` is in the team's `allowed_models`, else `403`.
5. Pick the first of the team's `allowed_providers` (in configured order)
   whose `ProviderClient` supports the model, else `403`.
6. Call that provider once. No retry, no fallback to a second provider yet
   — a failed call returns `502 Bad Gateway`.
7. On success, record token usage (rate limiter) and USD cost (budget
   tracker) against the team.

| Status | Meaning |
|--------|---------|
| `200` | Success |
| `401` | Missing or invalid API key |
| `403` | Model/provider not allowed for this team |
| `402` | Daily or monthly budget exceeded |
| `429` | Requests/min or tokens/min rate limit exceeded (`Retry-After` header set) |
| `502` | Upstream provider call failed |

### `GET /v1/teams/{team_name}/budget`

Self-service budget status — a team can only look up **its own** budget;
`{team_name}` must match the authenticated team or the request `403`s.
There is no cross-team or admin lookup. Deliberately does **not** consume a
rate-limit credit or get blocked by being over budget, so you can always
see *why* you're being rejected once you actually are.

```json
{
  "team": "platform-eng",
  "daily": {"spend_usd": 12.4, "limit_usd": 100.0, "remaining_usd": 87.6, "reset_at": "2026-08-16T00:00:00+00:00"},
  "monthly": {"spend_usd": 340.9, "limit_usd": 2000.0, "remaining_usd": 1659.1, "reset_at": "2026-09-01T00:00:00+00:00"}
}
```

```bash
curl -s http://localhost:8000/v1/teams/platform-eng/budget \
  -H "Authorization: Bearer team-platform-eng-devkey-001"
```

## Rate limiting

Each team's `rate_limit` (`requests_per_minute`, `tokens_per_minute`,
`burst`) in `config/config.yaml` is enforced via Redis-backed counters —
see `src/llm_gateway/rate_limit/limiter.py`'s module docstring for the full
algorithm and its trade-offs. Key points:

- Requests/minute is checked and incremented **before** the provider is
  called; tokens/minute is checked before the call but only **recorded**
  after a successful provider response (token counts aren't known until
  then).
- Exceeding either dimension returns `429 Too Many Requests` with a
  `Retry-After` header telling the client how long to back off.
- `REDIS_URL` points the gateway at Redis; **startup fails loudly** if
  Redis is unreachable, rather than booting into a state where limits
  silently don't work.
- Limit changes in `config.yaml` take effect on the next request via the
  existing hot-reload — no restart needed.

## Budget enforcement

Each team's `budget` (`daily_usd`, `monthly_usd`) is enforced against
Redis-backed spend counters tracked over UTC calendar-day and
calendar-month windows — see `src/llm_gateway/budget/tracker.py`'s module
docstring for the windowing rationale. Key points:

- Same check-before/record-after pattern as tokens/minute rate limiting,
  for the same reason: USD cost isn't known until the provider responds.
- Cost per request is computed from `src/llm_gateway/budget/pricing.py`'s
  per-model USD table — **hand-maintained**, documented there as a
  deliberate manual-upkeep liability (no automated price feed). If a model
  has no pricing entry, cost enforcement fails open (logs an error, records
  $0) rather than turning an otherwise-successful completion into a `500`.
- Exceeding either window returns `402 Payment Required` with a body
  naming which window (`daily`/`monthly`), current spend, the limit, and
  the reset time.
- `GET /v1/teams/{team_name}/budget` (see [API reference](#api-reference))
  is the self-service way to check current spend before hitting the cap.
- Budget changes in `config.yaml` take effect on the next request via
  hot-reload; already-accumulated spend for a window is kept as-is when a
  limit changes mid-window.

## Providers

All providers implement the same `ProviderClient` interface
(`src/llm_gateway/providers/base.py`) and are built once at startup
(`providers/registry.py::build_registry()`), not re-created per request.

| Provider | Module | Env var(s) |
|----------|--------|------------|
| OpenAI | `providers/openai_provider.py` | `OPENAI_API_KEY` |
| Anthropic | `providers/anthropic_provider.py` | `ANTHROPIC_API_KEY` |
| Ollama | `providers/ollama_provider.py` | `OLLAMA_BASE_URL` |

Each `ProviderClient` translates the gateway's own provider-agnostic
`ChatCompletionRequest`/`ChatCompletionResponse` to/from that provider's
native wire format — nothing outside `src/llm_gateway/providers/` ever
sees a provider-native shape. A team's `allowed_providers` are tried in the
configured order; the first provider whose client reports the requested
model in its `supported_models` wins (this ordering is also what will
become fallback order once Phase 4 lands).

**Mock mode:** set with `LLM_GATEWAY_MOCK_PROVIDERS` (env var, default
`true`). When on, every provider returns a deterministic canned response
(`providers/base.py::build_mock_response`) instead of making a real network
call — useful for local dev and is forced on explicitly in the test suite.
Each provider's mock flag is read from the environment once at **module
import time**, not per-request, so it can't be changed at runtime via the
hot-reloadable config — it's a process-level setting by design. Set it to
`false` plus the relevant provider credential to hit a real provider.

## Tests

```bash
pytest
```

The default run excludes real-Redis integration tests
(`@pytest.mark.redis`) — everything else runs against `fakeredis`, no
Redis instance required. To also run the integration tests:

```bash
docker compose up redis -d
pytest -m redis
```

Test files:

| File | Covers |
|------|--------|
| `tests/test_smoke.py` | App boots, `/healthz` reports the right team count, `openapi.json` is served |
| `tests/test_chat.py` | Auth rejection, model/provider authorization, happy-path chat completion |
| `tests/test_rate_limit.py` | Requests/min + tokens/min enforcement, `Retry-After` header |
| `tests/test_budget.py` | Daily/monthly budget enforcement, self-service budget status endpoint |

## Environment variables

See `.env.example` for the full list with defaults. The most relevant for local dev:

| Variable | Purpose | Default |
|----------|---------|---------|
| `CONFIG_PATH` | Path to the runtime config YAML | `config/config.yaml` |
| `REDIS_URL` | Redis connection string for rate limiting / budget counters | `redis://redis:6379/0` |
| `LLM_GATEWAY_MOCK_PROVIDERS` | Mock provider responses instead of real network calls | `true` |
| `OPENAI_API_KEY` | Real OpenAI credential (only used when mocking is off) | `sk-changeme` |
| `ANTHROPIC_API_KEY` | Real Anthropic credential (only used when mocking is off) | `sk-ant-changeme` |
| `OLLAMA_BASE_URL` | Ollama server URL (only used when mocking is off) | `http://ollama:11434` |
| `LOG_LEVEL` | Application log level | `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_SERVICE_NAME` / `PROMETHEUS_METRICS_PORT` | Reserved for Phase 5 observability wiring | — |

## Roadmap

- [x] **Phase 0** — Project scaffolding, config loader + hot reload, Docker Compose stub, smoke test
- [x] **Phase 1** — Request routing, provider abstraction (OpenAI/Anthropic/Ollama), auth, `POST /v1/chat/completions`
- [x] **Phase 2** — Rate limiting (Redis-backed sliding-window counters, per-team requests/min + tokens/min)
- [x] **Phase 3** — Budget enforcement (per-model USD pricing, Redis-backed daily/monthly spend tracking, `GET /v1/teams/{team}/budget`)
- [ ] **Phase 4** — Provider fallback logic (walk `allowed_providers` past the first failure instead of stopping at it)
- [ ] **Phase 5** — OpenTelemetry instrumentation + Prometheus metrics + Grafana dashboards

## Non-obvious design notes

- **Redis is a hard startup dependency**, not an optional one — the app
  calls `verify_connection()` during the FastAPI `lifespan` startup and
  raises (refusing to boot) if Redis is unreachable. This is intentional:
  there's no correct fallback behavior for rate limiting/budget
  enforcement if the backing store isn't there, so failing loudly at
  startup beats booting into a state where limits silently don't work.
- **Config is the source of truth for tenancy.** Each team's gateway API
  key is distinct from any provider API key. Nothing in the schema maps a
  model to a provider — that mapping lives in code, in each
  `ProviderClient.supported_models`. A model listed in a team's
  `allowed_models` that no allowed provider actually supports will `403`
  at request time, not at config-load time.
- **Rate limiting and budget enforcement share one Redis connection**
  (`app.state.redis_client`, built once at startup) and one atomic
  increment-and-expire Lua script (`redis_lua.py`) — both are just
  Redis-backed counters under the hood, so there's no reason to duplicate
  the client or the script.
- **`create_app()`** in `main.py` is the factory to use in tests/ASGI
  servers; the module-level `app` also exists for
  `uvicorn llm_gateway.main:app`.

For a narrative, step-by-step walkthrough of how this was built (including
a from-scratch rebuild guide), see [`documentation.md`](documentation.md).
For a machine/human-readable snapshot meant for a new contributor or agent
picking up work, see [`CONTEXT.md`](CONTEXT.md).
