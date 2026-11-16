"""Verify the candidate-owned Mock before running its formal HTTP tests."""

import argparse
import json
from pathlib import Path

from database.business_snapshot import API_VERSION, BASELINE_FILE, DATABASE_FILE
from database.contracts import VERSION, jsonl_rows, sha256
from database.snapshot import dataset_id


DATASET_PATH = "data/c004-r01/baseline"
START_COMMAND = "python -m uvicorn mock_server:app --host 127.0.0.1 --port 8090 --workers 1"
FROZEN_FILES = {
    "docs/Mock接口契约-v1.md",
    "mock_server.py",
    "mock/__init__.py",
    "mock/verify_freeze.py",
    "mock/faults/baseline-http.json",
    "mock/faults/read-429-then-ok.json",
    "mock/faults/read-500-three-times.json",
    "mock/faults/runtime-http.json",
    "mock_service/__init__.py",
    "mock_service/app.py",
    "mock_service/contracts.py",
    "mock_service/faults.py",
    "database/__init__.py",
    "database/business_snapshot.py",
    "database/contracts.py",
    "database/normalize.py",
    "database/snapshot.py",
} | {f"{DATASET_PATH}/{name}" for name in ("manifest.json", DATABASE_FILE, BASELINE_FILE, "normalized/orders.jsonl")}


def verify_freeze(path: Path) -> dict:
    """Reject a change to any frozen input; do not regenerate this baseline implicitly."""
    path = Path(path).resolve()
    root = path.parent.parent
    try:
        freeze = json.loads(path.read_text(encoding="utf-8"))
        if (
            freeze["freeze_version"] != 1 or freeze["api_version"] != API_VERSION
            or freeze["data_contract_version"] != VERSION or freeze["dataset_root"] != DATASET_PATH
            or freeze["start_command"] != START_COMMAND or freeze["files"].keys() != FROZEN_FILES
        ):
            raise ValueError
        for name, expected in freeze["files"].items():
            file = root / name
            if not file.resolve().is_relative_to(root) or sha256(file) != expected:
                raise ValueError(name)
        normalized = root / DATASET_PATH / "normalized/orders.jsonl"
        if freeze["dataset_id"] != dataset_id(jsonl_rows(normalized)):
            raise ValueError
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        raise ValueError("MOCK_FREEZE_MISMATCH") from None
    return freeze


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, default=Path(__file__).with_name("freeze.json"))
    baseline = verify_freeze(parser.parse_args().freeze)
    print(f"PASS {baseline['api_version']} {baseline['dataset_id']} {len(baseline['files'])} frozen files")


if __name__ == "__main__":
    main()
