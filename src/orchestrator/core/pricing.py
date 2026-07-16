"""Pricing calculations for capability outcomes."""

# Static _PRICES table: published per-1M-token USD for brain providers.
_PRICES: dict[str, float] = {
    "openai/gpt-4": 30.0,
    "openai/gpt-4-turbo": 10.0,
    "openai/gpt-4o": 5.0,
    "anthropic/claude-3-opus-20240229": 15.0,
    "anthropic/claude-3-5-sonnet-20240620": 3.0,
    "anthropic/claude-3-haiku-20240307": 0.25,
    "google/gemini-1.5-pro": 3.5,
    "google/gemini-1.5-flash": 0.15,
}

DEFAULT_RATE: float = 5.0


def est_cost_avoided_usd(rows: list[dict]) -> float:
    """
    Estimate the USD cost avoided by workers, applying counterfactual brain prices.
    Sums worker-source char cost.
    Token estimate: chars / 4
    """
    total = 0.0
    for row in rows:
        if row.get("source") != "worker":
            continue

        chars = row.get("chars")
        if not chars:
            continue

        try:
            chars_val = float(chars)
        except (ValueError, TypeError):
            chars_val = 0.0

        if chars_val <= 0:
            continue

        provider = row.get("provider", "")
        model = row.get("model", "")
        key = f"{provider}/{model}"

        rate = _PRICES.get(key, DEFAULT_RATE)

        # chars/4 token estimate
        tokens = chars_val / 4.0

        # rate is per 1M tokens
        cost = (tokens / 1_000_000.0) * rate
        total += cost

    return total
