import json
from types import SimpleNamespace

import pandas as pd
import pytest

from agents import technical_agent
from agents.technical_agent import (
    TechnicalSignal,
    _build_user_message,
    _crossover_state,
    _summarize_indicators,
    get_technical_signal,
)


def _amt_like_prices():
    # AMT 2026-10-19 shape: a 60-day slide, then a 15-day rally. Price is back
    # above both averages and MACD is positive, but SMA(20) is still under
    # SMA(50). The LLM read the old "below" label as an uptrend and said buy.
    return pd.DataFrame({"Close": [220 - i * 0.8 for i in range(60)] + [172.8 + i * 1.6 for i in range(1, 16)]})


def test_amt_rally_inside_a_downtrend_is_labelled_unambiguously():
    indicators = _summarize_indicators(_amt_like_prices())
    assert indicators["sma_20"] < indicators["sma_50"]
    assert indicators["sma_crossover_state"] == "sma20_below_sma50"
    assert indicators["close_vs_sma_20"] == "above"
    assert indicators["close_vs_sma_50"] == "above"
    assert indicators["macd_histogram"] > 0


def test_amt_prompt_separates_price_position_from_sma_order():
    message = _build_user_message(_summarize_indicators(_amt_like_prices()))
    assert "SMA crossover state: sma20_below_sma50 " in message
    assert "SMA(20) is below SMA(50), a downtrend" in message
    assert "(above SMA(20), above SMA(50))" in message
    assert "above/below" not in message


@pytest.mark.parametrize("short, long, expected", [
    ([1.0, 3.0], [2.0, 2.0], "golden_cross"),
    ([3.0, 1.0], [2.0, 2.0], "death_cross"),
    ([3.0, 3.0], [2.0, 2.0], "sma20_above_sma50"),
    ([1.0, 1.0], [2.0, 2.0], "sma20_below_sma50"),
])
def test_crossover_state_labels(short, long, expected):
    assert _crossover_state(pd.Series(short), pd.Series(long)) == expected


def test_signal_carries_the_indicators_the_model_was_given(monkeypatch):
    parsed = TechnicalSignal(signal="hold", confidence=0.74, reasoning="mixed")
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=lambda **kwargs: completion))
    )
    monkeypatch.setattr(technical_agent, "OpenAI", lambda **kwargs: client)

    result = get_technical_signal(_amt_like_prices())

    assert result["signal"] == "hold"
    assert result["indicators"] == _summarize_indicators(_amt_like_prices())
    assert result["indicators"]["sma_crossover_state"] == "sma20_below_sma50"
    # Logged with json.dumps: numbers must come back as numbers, not strings.
    logged = json.loads(json.dumps(result, default=str))
    assert isinstance(logged["indicators"]["sma_20"], float)
