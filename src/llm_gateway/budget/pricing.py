"""
Per-model USD pricing table and cost calculation.

## Where these numbers come from, and why they will drift

Prices are quoted by each provider as USD per 1M tokens; MODEL_PRICING below
converts to USD per 1K tokens only because that's the unit this module's
callers (budget amounts) are most naturally expressed in. Sources, as of
whenever this table was last hand-updated (see the comment above each
entry -- there is no automated sync):

  - OpenAI:    https://openai.com/api/pricing/
  - Anthropic: https://www.anthropic.com/pricing#api
  - Ollama:    self-hosted -- no per-token charge, see note below.

**This table is a known, deliberate manual-maintenance liability, not an
oversight.** Provider list prices change without notice (new model
revisions, promotional pricing, volume tiers this gateway doesn't model at
all). There is no CI check, webhook, or scheduled job that keeps
MODEL_PRICING in sync with reality -- whoever adds a model to
`config/config.yaml`'s `allowed_models` is responsible for adding (or
verifying) its entry here too, by hand, at the same time. Treat any budget
figure this module produces as an estimate for internal enforcement
purposes, not as a substitute for the provider's own billing/invoice.

## Adding a model

Add one `ModelPricing(...)` entry to MODEL_PRICING, keyed by the exact model
string used in `config/config.yaml` and by `ChatCompletionRequest.model`.
That's the entire change -- nothing else in this module, or in
budget/tracker.py, needs to know about a specific model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from llm_gateway.api.schemas import Usage

logger = logging.getLogger("llm_gateway.budget")


@dataclass(frozen=True)
class ModelPricing:
    input_usd_per_1k: float
    output_usd_per_1k: float


MODEL_PRICING: dict[str, ModelPricing] = {
    # OpenAI pricing page, gpt-4o row, $2.50 / 1M input + $10.00 / 1M output.
    "gpt-4o": ModelPricing(input_usd_per_1k=0.0025, output_usd_per_1k=0.010),
    # config.yaml's "claude-sonnet-4-6" is this sample repo's placeholder for
    # a Claude Sonnet-tier model rather than a real, currently-billed model
    # name -- priced here at Anthropic's Sonnet-tier list rate ($3 / 1M
    # input, $15 / 1M output) as a representative stand-in. Replace with the
    # real model's listed price once this points at an actually-billed model.
    "claude-sonnet-4-6": ModelPricing(input_usd_per_1k=0.003, output_usd_per_1k=0.015),
    # Ollama models run on infrastructure the team operating the gateway
    # already owns -- there's no per-token provider invoice, so this is
    # priced at $0. Budget enforcement is a no-op in practice for a team
    # (like `sandbox` in config.yaml) whose allowed_providers is ollama-only.
    "llama3": ModelPricing(input_usd_per_1k=0.0, output_usd_per_1k=0.0),
}


class PricingNotFoundError(KeyError):
    """Raised by get_pricing() for a model with no MODEL_PRICING entry."""


def get_pricing(model: str) -> ModelPricing:
    try:
        return MODEL_PRICING[model]
    except KeyError as exc:
        raise PricingNotFoundError(model) from exc


def compute_cost_usd(model: str, usage: Usage) -> float:
    """Returns the estimated USD cost of one completion. Fails open (logs an
    error, returns $0.0) rather than raising if `model` has no pricing entry
    -- budget enforcement is already a best-effort, soft-overage system (see
    budget/tracker.py), and a pricing-table gap for a newly allowed model
    should not turn an otherwise-successful completion into a 500. The gap
    itself is still visible in logs, and via GET /v1/teams/{team}/budget
    under-reporting spend for that model until the table is fixed."""
    try:
        pricing = get_pricing(model)
    except PricingNotFoundError:
        logger.error(
            "no pricing entry for model '%s' -- recording $0 cost; add an entry to "
            "budget/pricing.py::MODEL_PRICING",
            model,
        )
        return 0.0

    return (
        usage.prompt_tokens / 1000 * pricing.input_usd_per_1k
        + usage.completion_tokens / 1000 * pricing.output_usd_per_1k
    )
