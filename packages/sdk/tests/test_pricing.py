from prompt_shields.pricing import estimate_cost, DEFAULT_PRICING


def test_openai_gpt4o_cost():
    # 1000 in @ 0.0025/1k + 2000 out @ 0.010/1k = 0.0025 + 0.020 = 0.0225
    cost = estimate_cost("openai", "gpt-4o", tokens_in=1000, tokens_out=2000)
    assert cost == 0.0225


def test_anthropic_sonnet_cost():
    # 1000 in @ 0.003/1k + 1000 out @ 0.015/1k = 0.003 + 0.015 = 0.018
    cost = estimate_cost("anthropic", "claude-sonnet-4-20250514",
                        tokens_in=1000, tokens_out=1000)
    assert cost == 0.018


def test_unknown_model_returns_none():
    assert estimate_cost("openai", "gpt-99-experimental",
                         tokens_in=100, tokens_out=200) is None


def test_unknown_vendor_returns_none():
    assert estimate_cost("custom", "any", tokens_in=100, tokens_out=200) is None


def test_missing_tokens_returns_none():
    assert estimate_cost("openai", "gpt-4o", tokens_in=None, tokens_out=200) is None
    assert estimate_cost("openai", "gpt-4o", tokens_in=100, tokens_out=None) is None


def test_missing_model_returns_none():
    assert estimate_cost("openai", None, tokens_in=100, tokens_out=200) is None


def test_custom_pricing_table_overrides_default():
    custom = {("openai", "gpt-4o"): (0.10, 0.20)}  # absurdly high to be obvious
    # 1000 in @ 0.10/1k + 1000 out @ 0.20/1k = 0.10 + 0.20 = 0.30
    cost = estimate_cost("openai", "gpt-4o", tokens_in=1000, tokens_out=1000,
                         pricing_table=custom)
    assert cost == 0.30


def test_zero_tokens_costs_zero():
    assert estimate_cost("openai", "gpt-4o", tokens_in=0, tokens_out=0) == 0.0


def test_default_table_has_common_models():
    """Smoke check — common models should always be priced."""
    assert ("openai", "gpt-4o") in DEFAULT_PRICING
    assert ("anthropic", "claude-sonnet-4-20250514") in DEFAULT_PRICING
    assert ("google", "gemini-1.5-pro") in DEFAULT_PRICING
