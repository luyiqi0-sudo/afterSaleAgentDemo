import hashlib
import json
import sqlite3
from pathlib import Path

from agent.contracts import ToolResult


class OperationStore:
    """Legacy unit-fixture facade, not the production authorization boundary.

    Existing fixtures predate authenticated run/receipt/grant records. Production
    BusinessTools requires RuntimeStore and refuses this facade in strict mode.
    RuntimeStore conservatively imports these historical results on migration.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result TEXT NOT NULL)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=2)

    def claim(self, operation_id: str, payload: dict) -> tuple[bool, ToolResult]:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        pending = ToolResult(status="UNKNOWN", error_code="pending_reconciliation", operation_id=operation_id)
        with self._connect() as con:
            cursor = con.execute("INSERT OR IGNORE INTO operations VALUES (?,?,?)", (operation_id, digest, pending.model_dump_json()))
            if cursor.rowcount == 1:
                return True, pending
            stored_hash, result = con.execute("SELECT payload_hash,result FROM operations WHERE id=?", (operation_id,)).fetchone()
            if digest != stored_hash:
                return False, ToolResult(status="FAILED", error_code="operation_conflict", operation_id=operation_id)
            return False, ToolResult.model_validate_json(result)

    def finish(self, operation_id: str, result: ToolResult) -> None:
        with self._connect() as con:
            con.execute("UPDATE operations SET result=? WHERE id=?", (result.model_dump_json(), operation_id))
