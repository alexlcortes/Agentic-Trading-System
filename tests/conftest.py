import pytest

from orchestration import replay


@pytest.fixture(autouse=True)
def no_real_replay_recordings(tmp_path, monkeypatch):
    """Any test that drives run_daily.run_once would otherwise record its
    fake day into logs/replay/<today>.json, overwriting the real one the
    golden test will be built from."""
    monkeypatch.setattr(replay, "REPLAY_DIR", tmp_path / "replay")
