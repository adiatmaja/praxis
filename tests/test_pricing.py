def test_est_cost_avoided_usd_basic():
    from orchestrator.core.pricing import est_cost_avoided_usd

    rows = [
        {"source": "worker", "provider": "openai", "model": "gpt-4", "chars": 4000000},
        {"source": "brain", "provider": "openai", "model": "gpt-4", "chars": 100000},
    ]
    # chars=4,000,000 -> 1,000,000 tokens.
    # If price is 30.0 for openai/gpt-4, then cost is 30.0
    cost = est_cost_avoided_usd(rows)
    assert isinstance(cost, float)
    assert cost > 0.0


def test_est_cost_avoided_usd_unknown_model():
    from orchestrator.core.pricing import est_cost_avoided_usd

    rows = [
        {
            "source": "worker",
            "provider": "unknown",
            "model": "magic-model",
            "chars": 8000000,
        },
    ]
    # chars=8,000,000 -> 2,000,000 tokens.
    # With default rate of 5.0, cost is 10.0
    cost = est_cost_avoided_usd(rows)
    assert isinstance(cost, float)
    assert cost > 0.0


def test_est_cost_avoided_usd_empty():
    from orchestrator.core.pricing import est_cost_avoided_usd

    assert est_cost_avoided_usd([]) == 0.0
