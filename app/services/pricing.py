from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, DecimalException
from types import MappingProxyType

_MAX_MONETARY_AMOUNT = Decimal("999999.999999")
_ROUNDING_QUANTUM = Decimal("0.000001")
_TOKENS_PER_MILLION = Decimal(1_000_000)


class UnknownModelError(ValueError):
    """Raised when a provider/model pair has no pricing entry."""


class PricingValidationError(ValueError):
    """Raised when pricing or cost input is invalid."""


def _validate_identifier(label: str, value: str) -> None:
    if not isinstance(value, str):
        raise PricingValidationError(
            f"{label} must be a str, got {type(value).__name__}"
        )

    if not value.strip():
        raise PricingValidationError(f"{label} must not be blank")


def _validate_price_rate(label: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise PricingValidationError(
            f"{label} must be a Decimal, got {type(value).__name__}"
        )

    if not value.is_finite():
        raise PricingValidationError(
            f"{label} must be finite (not NaN or Infinity)"
        )

    if value < 0:
        raise PricingValidationError(
            f"{label} must not be negative"
        )


def _validate_token_count(label: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PricingValidationError(
            f"{label} must be a plain int, "
            f"got {type(value).__name__}"
        )

    if value < 0:
        raise PricingValidationError(
            f"{label} must not be negative"
        )


def _round_cost(value: Decimal) -> Decimal:
    try:
        rounded = value.quantize(
            _ROUNDING_QUANTUM,
            rounding=ROUND_CEILING,
        )
    except DecimalException as exc:
        raise PricingValidationError(
            "cost could not be represented with six decimal places"
        ) from exc

    if rounded < 0:
        raise PricingValidationError(
            "calculated cost must not be negative"
        )

    if rounded > _MAX_MONETARY_AMOUNT:
        raise PricingValidationError(
            f"calculated cost {rounded} exceeds NUMERIC(12,6) "
            f"maximum of {_MAX_MONETARY_AMOUNT}"
        )

    return rounded


@dataclass(frozen=True)
class ModelPricing:
    provider: str
    model: str
    input_price_per_million: Decimal
    output_price_per_million: Decimal

    def __post_init__(self) -> None:
        _validate_identifier("provider", self.provider)
        _validate_identifier("model", self.model)
        _validate_price_rate(
            "input_price_per_million",
            self.input_price_per_million,
        )
        _validate_price_rate(
            "output_price_per_million",
            self.output_price_per_million,
        )


def build_pricing_catalog(
    entries: Sequence[ModelPricing],
) -> Mapping[tuple[str, str], ModelPricing]:
    catalog: dict[tuple[str, str], ModelPricing] = {}

    for entry in entries:
        if not isinstance(entry, ModelPricing):
            raise PricingValidationError(
                "every pricing catalog entry must be a ModelPricing"
            )

        key = (entry.provider, entry.model)

        if key in catalog:
            raise PricingValidationError(
                "duplicate pricing entry for "
                f"provider={entry.provider!r} "
                f"model={entry.model!r}"
            )

        catalog[key] = entry

    return MappingProxyType(catalog)


# Synthetic nonzero prices used only by the free mock provider.
# Rates are dollars per 1,000,000 tokens.
#
# The "openai" / "gpt-5-mini" entry below is REAL pricing, verified
# against the official OpenAI model page
# (https://developers.openai.com/api/docs/models/gpt-5-mini) on
# 2026-09-07: $0.25 per 1,000,000 input tokens, $2.00 per 1,000,000
# output tokens (standard, non-batch rate). OpenAI pricing changes over
# time and is not guaranteed to remain current -- this entry MUST be
# re-verified against the live OpenAI pricing page
# (https://developers.openai.com/api/docs/pricing) before any production
# deployment, and periodically thereafter. Do not treat this hard-coded
# rate as permanently accurate.
_CATALOG_ENTRIES = (
    ModelPricing(
        provider="mock",
        model="mock-small",
        input_price_per_million=Decimal("0.01"),
        output_price_per_million=Decimal("0.02"),
    ),
    ModelPricing(
        provider="mock",
        model="mock-large",
        input_price_per_million=Decimal("0.05"),
        output_price_per_million=Decimal("0.10"),
    ),
    ModelPricing(
        provider="openai",
        model="gpt-5-mini",
        input_price_per_million=Decimal("0.25"),
        output_price_per_million=Decimal("2.00"),
    ),
)

_CATALOG = build_pricing_catalog(_CATALOG_ENTRIES)


def get_model_pricing(
    provider: str,
    model: str,
) -> ModelPricing:
    _validate_identifier("provider", provider)
    _validate_identifier("model", model)

    pricing = _CATALOG.get((provider, model))

    if pricing is None:
        raise UnknownModelError(
            f"no pricing entry for provider={provider!r} "
            f"model={model!r}"
        )

    return pricing


def calculate_cost_for_pricing(
    pricing: ModelPricing,
    prompt_tokens: int,
    completion_tokens: int,
) -> Decimal:
    if not isinstance(pricing, ModelPricing):
        raise PricingValidationError(
            "pricing must be a ModelPricing"
        )

    _validate_token_count("prompt_tokens", prompt_tokens)
    _validate_token_count("completion_tokens", completion_tokens)

    try:
        input_cost = (
            Decimal(prompt_tokens)
            * pricing.input_price_per_million
            / _TOKENS_PER_MILLION
        )
        output_cost = (
            Decimal(completion_tokens)
            * pricing.output_price_per_million
            / _TOKENS_PER_MILLION
        )
        total = input_cost + output_cost
    except DecimalException as exc:
        raise PricingValidationError(
            "cost calculation failed"
        ) from exc

    return _round_cost(total)


def estimate_cost_for_pricing(
    pricing: ModelPricing,
    prompt_token_estimate: int,
    max_tokens: int,
) -> Decimal:
    if not isinstance(pricing, ModelPricing):
        raise PricingValidationError(
            "pricing must be a ModelPricing"
        )

    _validate_token_count(
        "prompt_token_estimate",
        prompt_token_estimate,
    )
    _validate_token_count("max_tokens", max_tokens)

    if max_tokens < 1:
        raise PricingValidationError(
            "max_tokens must be >= 1"
        )

    try:
        input_cost = (
            Decimal(prompt_token_estimate)
            * pricing.input_price_per_million
            / _TOKENS_PER_MILLION
        )
        output_cost = (
            Decimal(max_tokens)
            * pricing.output_price_per_million
            / _TOKENS_PER_MILLION
        )
        total = input_cost + output_cost
    except DecimalException as exc:
        raise PricingValidationError(
            "cost estimation failed"
        ) from exc

    return _round_cost(total)


def calculate_actual_cost(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> Decimal:
    pricing = get_model_pricing(provider, model)

    return calculate_cost_for_pricing(
        pricing,
        prompt_tokens,
        completion_tokens,
    )


def estimate_worst_case_cost(
    provider: str,
    model: str,
    prompt_token_estimate: int,
    max_tokens: int,
) -> Decimal:
    pricing = get_model_pricing(provider, model)

    return estimate_cost_for_pricing(
        pricing,
        prompt_token_estimate,
        max_tokens,
    )


@dataclass(frozen=True)
class CostEstimateCandidate:
    pricing: ModelPricing
    prompt_token_estimate: int
    max_tokens: int


def select_max_estimated_cost(
    candidates: Sequence[CostEstimateCandidate],
) -> Decimal:
    if not candidates:
        raise PricingValidationError(
            "candidates must not be empty"
        )

    estimates: list[Decimal] = []

    for candidate in candidates:
        if not isinstance(candidate, CostEstimateCandidate):
            raise PricingValidationError(
                "every candidate must be a CostEstimateCandidate"
            )

        estimates.append(
            estimate_cost_for_pricing(
                candidate.pricing,
                candidate.prompt_token_estimate,
                candidate.max_tokens,
            )
        )

    return max(estimates)
