# CONTEXT.md

Machine/human-readable snapshot of what this repo is, right now, so an agent
(or a new contributor) can pick up work without re-deriving it from scratch.

## What this is

`llm-gateway` — a production-style HTTP API gateway meant to sit in front of
multiple LLM providers (OpenAI, Anthropic, Ollama) for multiple internal
"teams" (tenants), adding rate limiting, budget caps, provider fallback, and
observability on top of raw provider APIs.

## Current status: Phase 4 — provider fallback

Phase 0 (config loading/hot-reload, `/healthz`) is done and unchanged. Phase
1 adds the actual gateway route on top of it; Phase 2/3 add rate limiting
and budget enforcement (see below); Phase 4 adds provider fallback:

- `POST /v1/chat/completions` — a provider-agnostic chat completion
  endpoint. Auth via `Authorization: Bearer <team_api_key>` or
  `X-API-Key: <team_api_key>`, resolved against
  `GatewayConfig.team_by_api_key()`. Validates the requested model is in
  the calling team's `allowed_models`, then walks **every** one of the
  team's `allowed_providers` (in configured order) whose `ProviderClient`
  claims to support that model and tries them in turn — first success
  wins. A provider that raises `ProviderError` is skipped in favor of the
  next candidate instead of failing the request immediately; only if every
  candidate fails does the route return `502`, with each provider's
  failure reason included (`api/chat.py::_select_providers` +
  `create_chat_completion`).
- A `ProviderClient` abstraction (`src/llm_gateway/providers/`) with three
  implementations (`OpenAIProvider`, `AnthropicProvider`, `OllamaProvider`),
  each translating the gateway's own `ChatCompletionRequest`/
  `ChatCompletionResponse` (`src/llm_gateway/api/schemas.py`) to/from that
  provider's native wire format.
- Real HTTP calls are gated behind `LLM_GATEWAY_MOCK_PROVIDERS`
  (env var, **default `true`**) — when on, providers return a deterministic
  canned response (`providers/base.py::build_mock_response`) instead of
  calling out. Tests force this on explicitly in `conftest.py`. Set it to
  `false` plus the relevant provider credentials
  (`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`OLLAMA_BASE_URL`) to hit a real
  provider.
- A `ProviderRegistry` (`providers/registry.py`) builds all three clients
  once at startup (in `main.py`'s `lifespan`) and stores them on
  `app.state.provider_registry` — never rebuilt per-request.

Rate limiting (Phase 2), budget enforcement (Phase 3), and provider fallback
(Phase 4) are all implemented — see `src/llm_gateway/rate_limit/`,
`src/llm_gateway/budget/`, and `api/chat.py`. Still **not implemented**:
OTel/Prometheus/Grafana instrumentation. Prometheus/Grafana in
`docker-compose.yml` remain unused by app code (Redis is now used, by rate
limiting and budget tracking). Don't assume the observability stack exists
when reading code.

## Repo layout

```
llm-gateway/
├── src/llm_gateway/
│   ├── main.py                    # FastAPI app factory, lifespan (config load+watch, provider registry)
│   ├── config/
│   │   ├── schema.py               # Pydantic models: GatewayConfig, TeamConfig, RateLimitConfig, BudgetConfig, Provider
│   │   └── loader.py               # ConfigLoader: sync load() + async file-watch hot-reload
│   ├── api/
│   │   ├── health.py                # GET /healthz — liveness + team count
│   │   ├── schemas.py               # ChatCompletionRequest/Response, ChatMessage, Usage (provider-agnostic)
│   │   ├── auth.py                  # require_team dependency: Bearer/X-API-Key -> TeamConfig, 401 if unknown
│   │   └── chat.py                  # POST /v1/chat/completions — the core route
│   └── providers/
│       ├── base.py                  # ProviderClient ABC, ProviderError, build_mock_response()
│       ├── openai_provider.py       # OpenAIProvider
│       ├── anthropic_provider.py    # AnthropicProvider
│       ├── ollama_provider.py       # OllamaProvider
│       └── registry.py              # ProviderRegistry + build_registry() factory
├── config/config.yaml        # sample runtime config: 3 teams, keys, limits, budgets
├── tests/
│   ├── conftest.py           # TestClient fixture; sets CONFIG_PATH + LLM_GATEWAY_MOCK_PROVIDERS before import
│   ├── test_smoke.py         # app boots, /healthz returns 3 teams, openapi.json served
│   └── test_chat.py          # auth rejection, model/provider authorization, happy-path via fake ProviderClient
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/provisioning/ # datasource + dashboard provisioning stubs
├── docker-compose.yml        # gateway + redis + prometheus + grafana services
├── Dockerfile
├── pyproject.toml            # deps, ruff/mypy/pytest config
├── .env.example
└── README.md
```

## Key mechanisms worth knowing before touching code

- **Config is the source of truth for tenancy.** Each "team" in
  `config/config.yaml` has its own gateway-facing API key
  (`TeamConfig.api_key` — distinct from provider keys), allowed
  providers/models, a rate limit, and a budget. Schema is `extra="forbid"`
  everywhere, so a YAML typo fails validation loudly instead of silently
  no-opping.
- **Hot reload, not restart.** `ConfigLoader` (`src/llm_gateway/config/loader.py`)
  watches the config file's parent directory via `watchfiles.awatch` and
  re-validates on change. A bad edit is logged and the *previous* good
  config stays active — the process never crashes from a config typo.
  `app.state.config_loader` is how request handlers reach the current
  config (see `health.py` for the pattern).
- **`CONFIG_PATH` env var** controls which YAML file loads; read once at
  `main.py` module import time, so tests must set it *before* importing
  `llm_gateway.main` (see `tests/conftest.py`).
- **`create_app()` factory** in `main.py` is the thing to call in tests /
  ASGI servers, not a bare module-level `app` alone (though `app` exists too
  for `uvicorn llm_gateway.main:app`).

## Roadmap (from README)

1. ~~Phase 1 — routing / request-response models (the actual proxy logic)~~ done
2. ~~Phase 2 — Redis-backed rate limiting~~ done
3. ~~Phase 3 — budget enforcement~~ done
4. ~~Phase 4 — provider fallback logic~~ done
5. Phase 5 — OpenTelemetry + Prometheus + Grafana instrumentation

When implementing Phase 5, the OTel/Prometheus deps are already in
`pyproject.toml` and Prometheus/Grafana are already provisioned in
`docker-compose.yml` / `monitoring/`; the job is wiring
`opentelemetry-instrumentation-fastapi` into `create_app()` and adding a
`/metrics` endpoint. `ChatCompletionResponse` already carries `usage` and
`provider`, which is most of what a per-provider request/token/cost metric
needs.

## Non-obvious gotchas

- `docker-compose.yml` mounts `./config` read-write into the container
  specifically so hot-reload can be exercised by editing the host file
  while the container runs.
- Provider mocks (`ollama-mock` etc.) are commented out in
  `docker-compose.yml` — intentionally not active yet, left as a pattern to
  copy for testing fallback against real network failures rather than a
  `ProviderError` raised in-process. This is a different thing from
  `LLM_GATEWAY_MOCK_PROVIDERS`, which mocks provider *responses* inside the
  app itself and is on by default.
- Fallback (Phase 4) only retries across the providers a *team* is allowed
  to use for that model (`TeamConfig.allowed_providers`) — it never widens
  scope to a provider the team isn't configured for, even if that provider
  could serve the model. Only tokens/spend from whichever provider actually
  served the request get recorded; failed attempts consume no budget/token
  credit (the request-count credit from `enforce_rate_limit` is still
  charged once, pre-call, regardless of how many providers it takes).
- `GatewayConfig.team_by_api_key()` is now actually used, by
  `api/auth.py::require_team`.
- Each provider's `_MOCK` flag is read from the env once at **module import
  time**, not per-request — changing `LLM_GATEWAY_MOCK_PROVIDERS` at runtime
  (e.g. via the hot-reloadable config file) would *not* work even if it were
  wired into config; it's a process-level env var by design, unrelated to
  `config/config.yaml`.
- A team's `allowed_models` and `allowed_providers` are independent lists in
  config — nothing in the schema maps a model to a provider. That mapping
  lives in code, in each `ProviderClient.supported_models`. A model listed
  in `allowed_models` that no allowed provider's client supports will 403
  at request time, not at config-load time — the schema can't catch that
  mismatch.
