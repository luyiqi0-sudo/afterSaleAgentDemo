"""C-004: assertions on data meaning, never on generator helper output."""
import json
import subprocess
import sys
from datetime import date

import pytest

from database.contracts import jsonl_rows
from database.seed import generate_dataset
from database.validate import DataValidationError, validate_dataset


def raw(source_id="r1", **changes):
    row = {"order_id": "O1", "user_id": "alice", "amount": "19.99",
           "status": 2, "create_time": "2026/10/31 23:59:59"}
    row.update(changes)
    return {"source_id": source_id, "amount_unit": "yuan",
            "time_format": "slash_datetime", "timezone": "Asia/Shanghai",
            "record": row}


def test_normalization_preserves_exact_money_time_and_lineage():
    from database.normalize import normalize_records
    result = normalize_records([raw()])
    assert result["records"] == [{"order_id": "O1", "user_id": "alice", "amount": 1999,
                                 "status": 2, "create_time": "2026-10-31 23:59:59"}]
    assert result["rejected"] == []
    assert result["lineage"][0]["source_id"] == "r1"
    assert result["lineage"][0]["order_id"] == "O1"
    assert set(result["lineage"][0]["transformations"]) >= {"yuan_to_fen", "slash_datetime"}


@pytest.mark.parametrize("change", [
    {"amount": "19.999"}, {"amount": float("nan")}, {"amount": True},
    {"amount": None}, {"amount": "-1"}, {"status": 99},
    {"user_id": None}, {"create_time": "31/10/2026"},
])
def test_bad_raw_records_are_quarantined_not_guessed(change):
    from database.normalize import normalize_records
    result = normalize_records([raw(**change)])
    assert result["records"] == []
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["source_id"] == "r1"
    assert result["rejected"][0]["reason"]


def test_ambiguous_money_unit_is_not_assumed():
    from database.normalize import normalize_records
    row = raw()
    del row["amount_unit"]
    assert normalize_records([row])["records"] == []


def test_optional_missing_fields_normalize_to_null():
    from database.normalize import normalize_records
    row = raw()
    del row["record"]["status"]
    del row["record"]["create_time"]
    result = normalize_records([row])
    assert result["records"][0]["status"] is None
    assert result["records"][0]["create_time"] is None


def test_duplicate_order_ids_quarantine_both_rows():
    from database.normalize import normalize_records
    result = normalize_records([raw(), raw("r2", amount="88.88")])
    assert result["records"] == []
    assert {r["source_id"] for r in result["rejected"]} == {"r1", "r2"}


@pytest.mark.parametrize("bad", [
    {"source_id": [], "record": {"order_id": "bad"}},
    {"source_id": "bad", "record": {"order_id": []}},
    {"source_id": {}, "record": {"order_id": "bad"}},
])
def test_malformed_raw_ids_do_not_abort_neighboring_valid_rows(bad):
    from database.normalize import normalize_records
    result = normalize_records([bad, raw()])
    assert len(result["records"]) == 1 and result["records"][0]["order_id"] == "O1"
    assert len(result["rejected"]) == 1 and result["rejected"][0]["source_line"] == 1


def test_new_dataset_has_auditable_layers_and_explicit_snapshot_changes(tmp_path):
    from database.snapshot import validate_snapshot
    root = tmp_path / "dataset"
    generate_dataset(root, anchor_date=date(2027, 1, 1))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["anchor_date"] == manifest["as_of"] == "2027-01-01"
    assert len(jsonl_rows(root / "canonical/orders.jsonl")) == 200
    assert len(jsonl_rows(root / "normalized/orders.jsonl")) == 200
    assert len(jsonl_rows(root / "raw/orders.jsonl")) > 200
    assert len(jsonl_rows(root / "lineage/rejected.jsonl")) >= 3
    baseline = validate_snapshot(root, root / "snapshots/baseline")
    evolved = validate_snapshot(root, root / "snapshots/evolved")
    assert baseline["changed_orders"] == 0
    assert evolved["changed_orders"] == 5
    assert evolved["formal_mock_integration"] is False


def test_snapshot_cannot_change_amount_or_ownership_without_contract(tmp_path):
    from database.snapshot import validate_snapshot
    root = tmp_path / "dataset"
    generate_dataset(root)
    path = root / "snapshots/evolved/orders.jsonl"
    rows = jsonl_rows(path)
    rows[0]["amount"] += 1
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_snapshot(root, root / "snapshots/evolved")
    with pytest.raises(DataValidationError):
        validate_dataset(root, verify_hashes=False)


def test_anchor_conflict_does_not_create_output(tmp_path):
    root = tmp_path / "dataset"
    with pytest.raises(ValueError):
        generate_dataset(root, as_of=date(2026, 11, 15), anchor_date=date(2027, 1, 1))
    assert not root.exists()


def test_anchor_date_cli_and_standalone_normalizer(tmp_path):
    root = tmp_path / "dataset"
    made = subprocess.run([sys.executable, "-m", "database.seed", "--output", str(root),
                           "--anchor-date", "2027-01-01"], capture_output=True, text=True, encoding="utf-8")
    assert made.returncode == 0, made.stderr
    out = tmp_path / "normalized"
    result = subprocess.run([sys.executable, "-m", "database.normalize", "--input",
                             str(root / "raw/orders.jsonl"), "--output", str(out)],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert len(jsonl_rows(out / "orders.jsonl")) == 200
    assert len(jsonl_rows(out / "rejected.jsonl")) >= 3


def test_perturbation_changes_evaluation_ids_without_mutating_acceptance(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate_dataset(a)
    generate_dataset(b, seed=999999, variant="perturbation")
    left, right = jsonl_rows(a / "orders.jsonl"), jsonl_rows(b / "orders.jsonl")
    assert [x for x in left if x["order_id"].startswith("ACC-")] == [
        x for x in right if x["order_id"].startswith("ACC-")]
    assert {x["order_id"] for x in left if x["order_id"].startswith("EVAL-")}.isdisjoint(
        {x["order_id"] for x in right if x["order_id"].startswith("EVAL-")})
