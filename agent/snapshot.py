"""Independent verification of Mock facts using the local business snapshot."""

from datetime import datetime
from pathlib import Path

from database.business_snapshot import BusinessSnapshot
from database.business_snapshot import SnapshotValidationError as SnapshotValidationError


class SnapshotMismatch(ValueError):
    """A remote response cannot be used as verified business evidence."""


def _actor(principal_or_trusted_actor) -> str:
    if isinstance(principal_or_trusted_actor, str):
        return principal_or_trusted_actor
    if getattr(principal_or_trusted_actor, "tenant", "demo") == "demo":
        return getattr(principal_or_trusted_actor, "user_id", "")
    return ""


def _observed_stamp(value) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value)
        return parsed.utcoffset() is not None
    except ValueError:
        return False


class SnapshotVerifier:
    def __init__(self, dataset_root: Path):
        self._snapshot = BusinessSnapshot(dataset_root)
        self.dataset_id = self._snapshot.dataset_id

    @property
    def metadata(self) -> dict:
        return self._snapshot.metadata

    def _verify(self, data, expected):
        if not isinstance(data, dict) or expected is None or data.keys() != expected.keys() | {"_meta"}:
            raise SnapshotMismatch("RESPONSE_MISMATCH")
        if any(type(data[key]) is not type(value) or data[key] != value for key, value in expected.items()):
            raise SnapshotMismatch("RESPONSE_MISMATCH")
        meta = data["_meta"]
        required = self.metadata
        if (
            not isinstance(meta, dict) or meta.keys() != required.keys() | {"observed_at"}
            or any(meta[key] != value for key, value in required.items())
            or not _observed_stamp(meta["observed_at"])
        ):
            raise SnapshotMismatch("RESPONSE_MISMATCH")
        return dict(data, _meta=dict(meta))

    def validate_order(self, data, order_id: str, principal_or_trusted_actor) -> dict:
        return self._verify(data, self._snapshot.order(order_id, _actor(principal_or_trusted_actor)))

    def validate_logistics(self, data, order_id: str, principal_or_trusted_actor) -> dict:
        return self._verify(data, self._snapshot.logistics(order_id, _actor(principal_or_trusted_actor)))
