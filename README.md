# LLM Gateway

A production-style API gateway that sits in front of multiple LLM providers
(OpenAI, Anthropic, Ollama) and adds:

- Per-team rate limiting (Redis-backed)
- Budget enforcement (daily / monthly spend caps per team)
- Automatic provider fallback (e.g. OpenAI -> Anthropic -> Ollama on failure)
- Full observability: OpenTelemetry traces -> Prometheus metrics -> Grafana dashboards

> **Status:** Phase 1 — request routing + provider abstraction. Rate
> limiting, budget enforcement, and provider fallback are not implemented
> yet.

## Project layout

```
llm-gateway/
├── src/llm_gateway/       # application package (src layout)
│   ├── main.py            # FastAPI app factory + entrypoint
│   ├── config/             # YAML config schema + hot-reload loader
│   ├── api/                 # HTTP routes: health, auth dependency, chat completions
│   └── providers/           # ProviderClient abstraction: OpenAI, Anthropic, Ollama
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

## Tests

```bash
pytest
```

## Roadmap

- [x] **Phase 0** — Project scaffolding, config loader, Docker Compose stub, smoke test
- [x] **Phase 1** — Request routing, provider abstraction (OpenAI/Anthropic/Ollama), auth, `POST /v1/chat/completions`
- [ ] **Phase 2** — Rate limiting (Redis token bucket / sliding window)
- [ ] **Phase 3** — Budget enforcement
- [ ] **Phase 4** — Provider fallback logic
- [ ] **Phase 5** — OpenTelemetry instrumentation + Prometheus metrics + Grafana dashboards
