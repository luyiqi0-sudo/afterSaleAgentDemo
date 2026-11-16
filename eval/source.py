"""Load published Golden cases without allowing drift from the selected dataset."""
import hashlib
import json
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT / "data/c004-r01/baseline"
DELIVERY_CASES = PROJECT / "eval/golden_dataset.jsonl"


def read_cases(root: Path, cases_path: Path | None = None) -> list[dict]:
    root = Path(root)
    path = Path(cases_path) if cases_path is not None else (
        DELIVERY_CASES if root.resolve() == BASELINE.resolve() else root / "eval/golden_dataset.jsonl"
    )
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if not path.is_file() or path.is_symlink():
            raise ValueError
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest["files"]["eval/golden_dataset.jsonl"]:
            raise ValueError
        cases = [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
        if not cases or not all(isinstance(case, dict) for case in cases):
            raise ValueError
        return cases
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError("EVAL_SNAPSHOT_MISMATCH") from None
