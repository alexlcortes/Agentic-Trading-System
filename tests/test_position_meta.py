import pytest

from execution import position_meta
from execution.position_meta import load_meta, record_entry, sync_meta


@pytest.fixture(autouse=True)
def tmp_meta_path(tmp_path, monkeypatch):
    monkeypatch.setattr(position_meta, "POSITIONS_META_PATH", tmp_path / "positions_meta.json")


def test_new_position_gets_entry():
    meta = sync_meta({}, {"GOOGL": 250.0}, "2026-09-22")
    assert meta == {"GOOGL": {"opened_at": "2026-09-22", "entry_run_id": None, "high_water_mark": 250.0}}


def test_high_water_mark_only_rises():
    meta = sync_meta({}, {"GOOGL": 250.0}, "2026-09-22")
    meta = sync_meta(meta, {"GOOGL": 260.0}, "2026-09-23")
    meta = sync_meta(meta, {"GOOGL": 240.0}, "2026-09-24")
    assert meta["GOOGL"]["high_water_mark"] == 260.0
    assert meta["GOOGL"]["opened_at"] == "2026-09-22"


def test_sold_position_is_dropped_and_rebuy_starts_fresh():
    meta = sync_meta({}, {"GOOGL": 250.0}, "2026-09-22")
    meta = sync_meta(meta, {}, "2026-09-23")
    assert meta == {}
    meta = sync_meta(meta, {"GOOGL": 200.0}, "2026-09-30")
    assert meta["GOOGL"] == {"opened_at": "2026-09-30", "entry_run_id": None, "high_water_mark": 200.0}


def test_sync_does_not_mutate_input():
    original = {"GOOGL": {"opened_at": "2026-09-22", "entry_run_id": "r1", "high_water_mark": 250.0}}
    sync_meta(original, {"GOOGL": 300.0}, "2026-09-23")
    assert original["GOOGL"]["high_water_mark"] == 250.0


def test_record_entry_then_sync_keeps_run_id():
    record_entry("GOOGL", "run-1", "2026-09-22")
    meta = sync_meta(load_meta(), {"GOOGL": 250.0}, "2026-09-22")
    assert meta["GOOGL"]["entry_run_id"] == "run-1"
    assert meta["GOOGL"]["high_water_mark"] == 250.0


def test_add_to_position_keeps_original_run_id():
    record_entry("GOOGL", "run-1", "2026-09-22")
    record_entry("GOOGL", "run-2", "2026-09-25")
    assert load_meta()["GOOGL"]["entry_run_id"] == "run-1"
    assert load_meta()["GOOGL"]["opened_at"] == "2026-09-22"
