# LLM Gateway

A production-style API gateway that sits in front of multiple LLM providers
(OpenAI, Anthropic, Ollama) and adds:

- Per-team rate limiting (Redis-backed)
- Budget enforcement (daily / monthly spend caps per team)
- Automatic provider fallback (e.g. OpenAI -> Anthropic -> Ollama on failure)
- Full observability: OpenTelemetry traces -> Prometheus metrics -> Grafana dashboards

> **Status:** Phase 4 request routing + provider abstraction + per-team
> Redis-backed rate limiting + per-team budget enforcement + automatic
> provider fallback.

## Project layout

```
llm-gateway/
├── src/llm_gateway/       # application package (src layout)
│   ├── main.py            # FastAPI app factory + entrypoint
│   ├── config/             # YAML config schema + hot-reload loader
│   ├── api/                 # HTTP routes: health, auth/rate-limit/budget dependencies, chat completions, budget status
│   ├── providers/           # ProviderClient abstraction: OpenAI, Anthropic, Ollama
│   ├── rate_limit/          # Redis-backed per-team requests/min + tokens/min limiter
│   ├── budget/               # per-model USD pricing table + Redis-backed daily/monthly spend tracker
│   ├── redis_client.py      # shared redis.asyncio connection, built once at startup
│   └── redis_lua.py         # shared atomic increment-and-expire Lua script (rate_limit + budget)
├── config/
│   └── config.yaml         # runtime config: teams, models, limits, budgets
├── tests/                  # pytest suite
├── monitoring/
│   ├── prometheus.yml      # Prometheus scrape config
│   └── grafana/            # Grafana provisioning (datasource stub)
├── docker-compose.yml       # gateway + redis + prometheus + grafana
├── Dockerfile
├── pyproject.toml
└── .env.example
```

## Trying the chat completions endpoint

Provider calls are mocked by default (`LLM_GATEWAY_MOCK_PROVIDERS=true`), so
this works with no provider API keys:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer team-platform-eng-devkey-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}'
```

Set `LLM_GATEWAY_MOCK_PROVIDERS=false` and the relevant provider API key
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) or `OLLAMA_BASE_URL` to hit a real
provider instead.

## Requirements

- Python 3.11+
- Docker + Docker Compose (for the full stack)

## Local development (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn llm_gateway.main:app --reload
```

The app boots on `http://localhost:8000`. Health check: `GET /healthz`.

## Running the full stack

```bash
docker compose up --build
```

| Service     | URL                          |
|-------------|-------------------------------|
| Gateway     | http://localhost:8000         |
| Prometheus  | http://localhost:9090         |
| Grafana     | http://localhost:3000 (admin/admin) |
| Redis       | localhost:6379                |

## Configuration

Runtime behavior (teams, allowed models/providers, rate limits, budgets) is
defined in `config/config.yaml` and validated against a Pydantic schema
(`src/llm_gateway/config/schema.py`). The config loader watches the file and
hot-reloads it into memory without restarting the process — see
`src/llm_gateway/config/loader.py`.

## Rate limiting

Each team's `rate_limit` in `config/config.yaml` (`requests_per_minute`,
`tokens_per_minute`, `burst`) is enforced via Redis-backed sliding-window
counters — see `src/llm_gateway/rate_limit/limiter.py`'s module docstring
for the algorithm and the trade-offs behind it (fixed-window vs. sliding
log, and why tokens/minute is checked pre-call but only recorded after a
successful provider response). Exceeding either dimension returns `429 Too
Many Requests` with a `Retry-After` header. `REDIS_URL` (see
`.env.example`) points the gateway at Redis; startup fails loudly if Redis
is unreachable. Limit changes in `config.yaml` take effect on the next
request via the existing hot-reload — no restart needed.

## Budget enforcement

Each team's `budget` in `config/config.yaml` (`daily_usd`, `monthly_usd`) is
enforced against Redis-backed spend counters, tracked over UTC calendar-day
and calendar-month windows — see `src/llm_gateway/budget/tracker.py`'s
module docstring for the windowing rationale (calendar vs. rolling) and the
check-before/record-after enforcement strategy, which deliberately mirrors
`rate_limit/limiter.py`'s tokens/minute design for the same reason (cost
isn't known until the provider responds). Cost per request is looked up in
`src/llm_gateway/budget/pricing.py`'s per-model USD table — a hand-maintained
table, documented there as a known manual-upkeep liability, not an
automated feed. Exceeding either window returns `402 Payment Required` with
a body naming which window, the current spend, limit, and reset time.
`GET /v1/teams/{team_name}/budget` (self-service only — a team can only see
its own budget) returns current daily/monthly spend, limit, and remaining
for self-monitoring. Like rate limits, budget changes in `config.yaml` take
effect on the next request via hot-reload; already-accumulated spend for a
window is kept as-is when a limit changes mid-window.

## Provider fallback

For a given request, `POST /v1/chat/completions` walks the calling team's
`allowed_providers` in the order configured in `config.yaml` and tries every
one whose `ProviderClient` claims to support the requested model, in that
order — see `src/llm_gateway/api/chat.py`. The first successful response
wins; a provider that raises `ProviderError` (network error, non-2xx,
unparseable response) is skipped in favor of the next candidate instead of
failing the request immediately. Only tokens/spend from the provider that
actually served the request are recorded. If every candidate fails, the
route returns `502 Bad Gateway` with each provider's failure reason
included. A team allowed only one provider for a model gets no fallback,
same behavior as before Phase 4.

## Tests

```bash
pytest
```

The default run excludes the real-Redis integration tests
(`@pytest.mark.redis`); everything else runs against `fakeredis`, no Redis
instance required. To also run the integration tests:

```bash
docker compose up redis -d
pytest -m redis
```

## Roadmap

- [x] **Phase 0** — Project scaffolding, config loader, Docker Compose stub, smoke test
- [x] **Phase 1** — Request routing, provider abstraction (OpenAI/Anthropic/Ollama), auth, `POST /v1/chat/completions`
- [x] **Phase 2** — Rate limiting (Redis-backed sliding-window counters, per-team requests/min + tokens/min)
- [x] **Phase 3** — Budget enforcement (per-model USD pricing, Redis-backed daily/monthly spend tracking, `GET /v1/teams/{team}/budget`)
- [x] **Phase 4** — Provider fallback logic (retry the next allowed provider on failure, first success wins, 502 only if all fail)
- [ ] **Phase 5** — OpenTelemetry instrumentation + Prometheus metrics + Grafana dashboards
