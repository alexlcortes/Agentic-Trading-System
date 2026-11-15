"""Atomic writes for pending_orders.json and positions_meta.json: a run killed
mid-write must leave the previous file intact, never a truncated one that
fails every later run (execution/state_files.py)."""

import json

import pytest

from execution import alpaca_executor, position_meta
from execution.state_files import write_json_atomic


def test_writes_the_data(tmp_path):
    path = tmp_path / "state.json"
    write_json_atomic(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}


def test_creates_the_parent_directory(tmp_path):
    path = tmp_path / "logs" / "state.json"
    write_json_atomic(path, {})
    assert json.loads(path.read_text()) == {}


def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "state.json"
    write_json_atomic(path, {"order-1": {"ticker": "GOOGL"}})
    # json.dump fails partway through: the first key is already written to
    # the temp file when the unserializable value is reached.
    with pytest.raises(TypeError):
        write_json_atomic(path, {"order-2": {"ticker": "MSFT"}, "bad": object()})
    assert json.loads(path.read_text()) == {"order-1": {"ticker": "GOOGL"}}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_pending_orders_go_through_the_atomic_write(tmp_path, monkeypatch):
    monkeypatch.setattr(alpaca_executor, "PENDING_ORDERS_PATH", tmp_path / "pending_orders.json")
    alpaca_executor._save_pending({"order-1": {"ticker": "GOOGL"}})
    with pytest.raises(TypeError):
        alpaca_executor._save_pending({"order-1": object()})
    assert alpaca_executor._load_pending() == {"order-1": {"ticker": "GOOGL"}}


def test_position_meta_goes_through_the_atomic_write(tmp_path, monkeypatch):
    monkeypatch.setattr(position_meta, "POSITIONS_META_PATH", tmp_path / "positions_meta.json")
    position_meta.save_meta({"GOOGL": {"opened_at": "2026-11-02"}})
    with pytest.raises(TypeError):
        position_meta.save_meta({"GOOGL": object()})
    assert position_meta.load_meta() == {"GOOGL": {"opened_at": "2026-11-02"}}
