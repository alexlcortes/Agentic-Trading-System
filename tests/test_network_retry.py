import pytest
import requests

import run_daily
from config import settings


class _FlakyClient:
    """get_clock() raises each queued exception in turn, then succeeds."""

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def get_clock(self):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return object()


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(run_daily.time, "sleep", sleeps.append)
    return sleeps


def _use(monkeypatch, client):
    monkeypatch.setattr(run_daily, "_get_client", lambda: client)


def test_succeeds_without_retrying_when_network_is_up(monkeypatch, no_sleep):
    client = _FlakyClient([])
    _use(monkeypatch, client)
    run_daily._wait_for_network()
    assert client.calls == 1
    assert no_sleep == []


def test_retries_through_dns_failures_then_continues(monkeypatch, no_sleep):
    client = _FlakyClient([
        requests.exceptions.ConnectionError("Failed to resolve 'paper-api.alpaca.markets'"),
        requests.exceptions.Timeout("read timed out"),
    ])
    _use(monkeypatch, client)
    run_daily._wait_for_network()
    assert client.calls == 3
    assert no_sleep == [run_daily.NETWORK_RETRY_DELAY_SECONDS] * 2


def test_gives_up_after_the_retry_window(monkeypatch, no_sleep):
    attempts = run_daily.NETWORK_RETRY_ATTEMPTS
    client = _FlakyClient([requests.exceptions.ConnectionError("offline")] * attempts)
    _use(monkeypatch, client)
    with pytest.raises(run_daily.NetworkUnavailableError):
        run_daily._wait_for_network()
    assert client.calls == attempts
    assert len(no_sleep) == attempts - 1  # no pointless sleep after the last attempt


def test_non_network_errors_are_not_retried(monkeypatch, no_sleep):
    client = _FlakyClient([ValueError("forbidden: bad API key")])
    _use(monkeypatch, client)
    with pytest.raises(ValueError):
        run_daily._wait_for_network()
    assert client.calls == 1
    assert no_sleep == []


def test_unreachable_network_skips_the_run_with_a_summary_line(monkeypatch, no_sleep, tmp_path):
    summary = tmp_path / "daily_summary.log"
    monkeypatch.setattr(run_daily, "SUMMARY_LOG_PATH", summary)
    monkeypatch.setattr(run_daily, "_woke_up_too_late", lambda: False)
    monkeypatch.setattr(run_daily, "_startup_safety_check", lambda: True)
    monkeypatch.setattr(settings, "auto_execute", settings.auto_execute)  # run_once sets it
    monkeypatch.setattr(run_daily, "reconcile_pending_orders", lambda: pytest.fail("ran past the gate"))
    _use(monkeypatch, _FlakyClient(
        [requests.exceptions.ConnectionError("offline")] * run_daily.NETWORK_RETRY_ATTEMPTS
    ))

    with pytest.raises(run_daily.NetworkUnavailableError):
        run_daily.run_once()
    assert "Network unreachable" in summary.read_text()
