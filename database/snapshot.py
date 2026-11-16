"""Versioned fixture exports, not an HTTP service or a remote success receipt."""

import argparse
import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from database.contracts import jsonl_rows, normalize_order, stable_json
from database.normalize import write_rows


def dataset_id(rows: list[dict]) -> str:
    return hashlib.sha256(stable_json(rows).encode()).hexdigest()


def build_snapshots(root: Path, anchor: date):
    base = jsonl_rows(root / "normalized/orders.jsonl")
    logistics = jsonl_rows(root / "logistics.jsonl")
    eligible = [r["order_id"] for r in base if r["status"] in (3, 5) and r["create_time"]][:5]
    for profile in ("baseline", "evolved"):
        rows, changes = [], []
        observed = datetime.combine(anchor, time(23, 59, 59))
        if profile == "evolved":
            observed += timedelta(days=1)
        for original in base:
            row = dict(original)
            if profile == "evolved" and row["order_id"] in eligible:
                row["status"] = {3: 5, 5: 6}[original["status"]]
                changes.append({"order_id": row["order_id"], "field": "status",
                                "from": original["status"], "to": row["status"],
                                "effective_at": observed.isoformat() + "+08:00"})
            rows.append(row)
        target = root / "snapshots" / profile
        write_rows(target / "orders.jsonl", rows)
        write_rows(target / "logistics.jsonl", logistics)
        meta = {"version": "snapshot-v1", "profile": profile, "base_dataset_id": dataset_id(base),
                "as_of": observed.isoformat() + "+08:00", "changes": changes,
                "formal_mock_integration": False}
        (target / "snapshot.json").write_text(stable_json(meta), encoding="utf-8", newline="\n")


def validate_snapshot(root: Path, snapshot: Path) -> dict:
    base = jsonl_rows(root / "normalized/orders.jsonl")
    rows = jsonl_rows(snapshot / "orders.jsonl")
    metadata = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
    if metadata.get("version") != "snapshot-v1" or metadata.get("base_dataset_id") != dataset_id(base):
        raise ValueError("snapshot provenance mismatch")
    if metadata.get("formal_mock_integration") is not False:
        raise ValueError("snapshot is not formal Mock integration")
    profile = metadata.get("profile")
    if profile not in ("baseline", "evolved"):
        raise ValueError("unknown snapshot profile")
    anchor = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["anchor_date"]
    moment = datetime.combine(date.fromisoformat(anchor), time(23, 59, 59))
    if profile == "evolved":
        moment += timedelta(days=1)
    stamp = moment.isoformat() + "+08:00"
    if metadata["as_of"] != stamp:
        raise ValueError("snapshot time mismatch")
    if len(rows) != len(base) or {r["order_id"] for r in rows} != {r["order_id"] for r in base}:
        raise ValueError("snapshot key set mismatch")
    by_id = {r["order_id"]: r for r in base}
    changes = []
    for row in rows:
        if normalize_order(row) != row:
            raise ValueError("snapshot schema mismatch")
        original = by_id[row["order_id"]]
        if any(row[k] != original[k] for k in original if k != "status"):
            raise ValueError("undeclared snapshot field change")
        if row["status"] != original["status"]:
            if (original["status"], row["status"]) not in ((3, 5), (5, 6)):
                raise ValueError("unsupported state transition")
            changes.append({"order_id": row["order_id"], "field": "status", "from": original["status"],
                            "to": row["status"], "effective_at": stamp})
    if changes != metadata["changes"] or len(changes) != (5 if profile == "evolved" else 0):
        raise ValueError("snapshot change ledger mismatch")
    if jsonl_rows(snapshot / "logistics.jsonl") != jsonl_rows(root / "logistics.jsonl"):
        raise ValueError("undeclared logistics change")
    return {"status": "PASS", "profile": profile, "changed_orders": len(changes),
            "formal_mock_integration": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    print(stable_json(validate_snapshot(args.root, args.snapshot)))


if __name__ == "__main__":
    main()
