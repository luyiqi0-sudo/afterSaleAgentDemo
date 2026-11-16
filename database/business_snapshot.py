"""Read and pin a verified C-004-v1 baseline for one process lifetime."""

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from database.contracts import LOGISTICS_FIELDS, ORDER_FIELDS, PRODUCT_FIELDS, SOURCE, VERSION, jsonl_rows, sha256
from database.snapshot import dataset_id


API_VERSION = "after-sales-mock-v1"
DATABASE_FILE = "database/ecommerce.db"
BASELINE_FILE = "snapshots/baseline/snapshot.json"
REQUIRED_FILES = {DATABASE_FILE, BASELINE_FILE, "normalized/orders.jsonl", "logistics.jsonl", "products.jsonl"}
SHANGHAI = ZoneInfo("Asia/Shanghai")


class SnapshotValidationError(ValueError):
    """Dataset failed initialization; callers must fail closed."""


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _require(condition):
    if not condition:
        raise SnapshotValidationError("SNAPSHOT_INVALID")


def _rows(con, table, fields):
    return [dict(row) for row in con.execute(f"SELECT {','.join(fields)} FROM {table}")]


def _check_rows(actual, exported, key, count):
    actual_by_id = {row[key]: row for row in actual}
    exported_by_id = {row[key]: row for row in exported}
    _require(len(actual) == len(exported) == len(actual_by_id) == len(exported_by_id) == count)
    _require(actual_by_id == exported_by_id)


class BusinessSnapshot:
    def __init__(self, dataset_root: Path):
        try:
            self._load(Path(dataset_root).resolve(strict=True))
        except (OSError, KeyError, TypeError, ValueError, OverflowError, sqlite3.Error):
            raise SnapshotValidationError("SNAPSHOT_INVALID") from None

    def _load(self, root):
        manifest = _read_json(root / "manifest.json")
        _require(isinstance(manifest, dict))
        _require(manifest.get("contract_version") == VERSION and manifest.get("source_type") == SOURCE)
        _require(manifest.get("timezone") == "Asia/Shanghai" and manifest.get("formal_mock_integration") is False)
        _require(type(manifest.get("seed")) is int and 0 <= manifest["seed"] < 2**63)
        _require(manifest.get("anchor_date") == manifest.get("as_of"))
        anchor = date.fromisoformat(manifest["anchor_date"])
        files = manifest.get("files")
        _require(isinstance(files, dict) and REQUIRED_FILES <= files.keys())
        paths = list(root.rglob("*"))
        _require(not any(path.is_symlink() or not path.resolve().is_relative_to(root) for path in paths))
        actual = {path.relative_to(root).as_posix() for path in paths if path.is_file()}
        _require(actual - {"manifest.json"} == files.keys())
        for filename, digest in files.items():
            _require(sha256(root / filename) == digest)

        orders = jsonl_rows(root / "normalized/orders.jsonl")
        self.dataset_id = dataset_id(orders)
        snapshot_as_of = datetime.combine(anchor, time(23, 59, 59), SHANGHAI).isoformat()
        baseline = _read_json(root / BASELINE_FILE)
        _require(baseline == {
            "version": "snapshot-v1", "profile": "baseline", "changes": [],
            "base_dataset_id": self.dataset_id, "as_of": snapshot_as_of, "formal_mock_integration": False,
        })
        db = root / DATABASE_FILE
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)) as con:
            con.row_factory = sqlite3.Row
            _require(con.execute("PRAGMA quick_check").fetchone()[0] == "ok")
            db_orders = _rows(con, "orders", ORDER_FIELDS)
            db_logistics = _rows(con, "logistics", LOGISTICS_FIELDS)
            db_products = _rows(con, "products", PRODUCT_FIELDS)
        _require(sha256(db) == files[DATABASE_FILE])
        counts = manifest["counts"]
        _check_rows(db_orders, orders, "order_id", counts["orders"])
        _check_rows(db_logistics, jsonl_rows(root / "logistics.jsonl"), "logistics_id", counts["logistics"])
        _check_rows(db_products, jsonl_rows(root / "products.jsonl"), "sku_id", counts["products"])
        self._orders = {row["order_id"]: row for row in sorted(db_orders, key=lambda r: r["order_id"])}
        self._logistics = {row["order_id"]: row for row in db_logistics}
        _require(len(self._logistics) == len(db_logistics) and self._logistics.keys() <= self._orders.keys())
        self._metadata = {
            "api_version": API_VERSION, "dataset_id": self.dataset_id,
            "data_contract_version": VERSION, "snapshot_as_of": snapshot_as_of,
        }

    @property
    def metadata(self) -> dict:
        return dict(self._metadata)

    def order(self, order_id: str, actor: str) -> dict | None:
        row = self._orders.get(order_id)
        return dict(row) if row is not None and row["user_id"] == actor else None

    def logistics(self, order_id: str, actor: str) -> dict | None:
        row = self._logistics.get(order_id)
        return dict(row) if row is not None and self.order(order_id, actor) is not None else None

    def orders(self, actor: str, offset: int, size: int) -> list[dict]:
        rows = [row for row in self._orders.values() if row["user_id"] == actor]
        return [dict(row) for row in rows[offset:offset + size]]
