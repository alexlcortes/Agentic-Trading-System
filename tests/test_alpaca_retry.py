"""Retries for Alpaca and yfinance calls.

Regression for 2026-10-08 and 10-09: a GOOGL buy reached Alpaca, then the
status check that follows it hit RemoteDisconnected. The exception discarded
the order id, so both orders (8 and 1 shares) filled without being tracked.
"""

import json
from types import SimpleNamespace

import pandas as pd
import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderStatus

from agents import market_data_agent
from agents.market_data_agent import TickerDataError
from execution import alpaca_executor

DROPPED = requests.exceptions.ConnectionError(
    "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
)


def _not_found():
    return APIError("order not found", SimpleNamespace(response=SimpleNamespace(status_code=404)))


def _order(status=OrderStatus.ACCEPTED, order_id="order-1"):
    return SimpleNamespace(id=order_id, status=status, filled_qty=None, filled_avg_price=None)


class _FakeAlpaca:
    """Each method raises its queued exceptions in turn, then returns normally."""

    def __init__(self, submit=(), lookup_by_client_id=(), status=(), account=()):
        self.failures = {
            "submit": list(submit), "client_id": list(lookup_by_client_id),
            "status": list(status), "account": list(account),
        }
        self.submitted = []
        self.received = None  # the order Alpaca "has", once any submit lands

    def _maybe_fail(self, name):
        if self.failures[name]:
            raise self.failures[name].pop(0)

    def get_clock(self):
        return SimpleNamespace(is_open=False)

    def submit_order(self, order_data):
        self.submitted.append(order_data.client_order_id)
        self._maybe_fail("submit")
        self.received = _order()
        return self.received

    def get_order_by_client_id(self, client_id):
        self._maybe_fail("client_id")
        if self.received is None:
            raise _not_found()
        return self.received

    def get_order_by_id(self, order_id):
        self._maybe_fail("status")
        return _order(order_id=order_id)

    def get_account(self):
        self._maybe_fail("account")
        return SimpleNamespace(equity="100000", last_equity="100000")

    def get_all_positions(self):
        return []


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(alpaca_executor.time, "sleep", sleeps.append)
    monkeypatch.setattr(market_data_agent.time, "sleep", sleeps.append)
    monkeypatch.setattr(alpaca_executor, "PENDING_ORDERS_PATH", tmp_path / "pending_orders.json")
    monkeypatch.setattr(alpaca_executor, "_position_details", lambda positions: {})
    return sleeps


def _use(monkeypatch, client):
    monkeypatch.setattr(alpaca_executor, "_get_client", lambda: client)
    return client


def _submit():
    return alpaca_executor.submit_order(
        "GOOGL", "buy", 1, run_id="run-1", reference_price=352.72
    )


def test_status_check_failure_after_submit_keeps_tracking_the_order(monkeypatch):
    attempts = alpaca_executor.RETRY_ATTEMPTS
    _use(monkeypatch, _FakeAlpaca(status=[DROPPED] * attempts))

    result = _submit()

    assert result["status"] == "status_unknown"
    assert result["order_id"] == "order-1"
    pending = json.loads(alpaca_executor.PENDING_ORDERS_PATH.read_text())
    assert pending["order-1"]["ticker"] == "GOOGL"
    assert pending["order-1"]["reference_price"] == 352.72


def test_status_check_recovers_after_a_dropped_connection(monkeypatch):
    client = _use(monkeypatch, _FakeAlpaca(status=[DROPPED]))
    result = _submit()
    assert result["status"] == "queued_for_next_session"
    assert len(client.submitted) == 1


def test_submit_whose_response_was_lost_is_not_resubmitted(monkeypatch):
    class LostResponse(_FakeAlpaca):
        def submit_order(self, order_data):
            self.submitted.append(order_data.client_order_id)
            self.received = _order()  # Alpaca got it...
            raise DROPPED  # ...but the response never came back

    client = _use(monkeypatch, LostResponse())
    result = _submit()

    assert len(client.submitted) == 1
    assert result["order_id"] == "order-1"


def test_submit_that_never_arrived_is_resent_with_the_same_client_id(monkeypatch):
    client = _use(monkeypatch, _FakeAlpaca(submit=[DROPPED]))
    result = _submit()

    assert len(client.submitted) == 2
    assert client.submitted[0] == client.submitted[1]
    assert result["order_id"] == "order-1"


def test_duplicate_id_refusal_on_a_resend_returns_the_existing_order(monkeypatch):
    class RacedResend(_FakeAlpaca):
        def submit_order(self, order_data):
            self.submitted.append(order_data.client_order_id)
            if len(self.submitted) == 1:
                raise DROPPED  # not visible yet, so the lookup misses it
            self.received = _order()  # the first send shows up now...
            raise APIError("client_order_id must be unique")  # ...so the resend is refused

    client = _use(monkeypatch, RacedResend())

    result = _submit()

    assert len(client.submitted) == 2
    assert result["order_id"] == "order-1"


def test_submit_gives_up_after_every_attempt_fails(monkeypatch):
    attempts = alpaca_executor.RETRY_ATTEMPTS
    client = _use(monkeypatch, _FakeAlpaca(submit=[DROPPED] * attempts))
    result = _submit()

    assert result["status"] == "submission_failed"
    assert result["order_id"] is None
    assert len(client.submitted) == attempts


def test_api_errors_are_not_retried(monkeypatch):
    client = _use(monkeypatch, _FakeAlpaca(submit=[APIError("insufficient buying power")]))
    result = _submit()
    assert result["status"] == "submission_failed"
    assert len(client.submitted) == 1


def test_portfolio_state_retries_a_dropped_connection(monkeypatch, isolated):
    _use(monkeypatch, _FakeAlpaca(account=[DROPPED]))
    state = alpaca_executor.get_portfolio_state()
    assert state["equity"] == 100_000.0
    assert isolated == [alpaca_executor.RETRY_BACKOFF_SECONDS]


def _ohlcv():
    return pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1]}
    )


def test_empty_yfinance_result_is_retried(isolated):
    # LIN on 2026-10-08: yfinance returned nothing ("possibly delisted") once.
    responses = iter([pd.DataFrame(), _ohlcv()])
    data = market_data_agent._fetch_with_retry("LIN", lambda: next(responses))
    assert len(data) == 1
    assert len(isolated) == 1


def test_persistently_empty_yfinance_result_is_reported_as_no_data():
    with pytest.raises(TickerDataError, match="No price data returned for 'XXXX'"):
        market_data_agent._fetch_with_retry("XXXX", pd.DataFrame)


def test_rows_without_a_close_are_dropped(isolated):
    # 2026-09-30: yfinance ended SPY/GOOGL/XOM with a NaN close.
    index = pd.to_datetime(["2026-09-29", "2026-09-30"])
    frame = pd.DataFrame(
        {"Open": [1.0, None], "High": [1.0, None], "Low": [1.0, None],
         "Close": [670.0, None], "Volume": [1, None]},
        index=index,
    )
    data = market_data_agent._fetch_with_retry("SPY", lambda: frame)
    assert list(data["Close"]) == [670.0]
    assert isolated == []  # not an empty result, so no retry


def test_frame_with_only_missing_closes_is_retried_as_empty(isolated):
    nan_frame = pd.DataFrame(
        {"Open": [None], "High": [None], "Low": [None], "Close": [None], "Volume": [None]},
        index=pd.to_datetime(["2026-09-30"]),
    )
    responses = iter([nan_frame, _ohlcv()])
    data = market_data_agent._fetch_with_retry("SPY", lambda: next(responses))
    assert len(data) == 1
    assert len(isolated) == 1
