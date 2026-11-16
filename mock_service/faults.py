import json
import math
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from mock_service.contracts import ID_PATTERN


ROUTE = re.compile(rf"(?:GET /orders(?:/{ID_PATTERN})?|GET /logistics/{ID_PATTERN}|POST /tickets)")
ACTIONS = {"ok", "429", "500", "timeout_before", "timeout_after", "500_after"}


@dataclass(frozen=True)
class Fault:
    action: str = "ok"
    delay_seconds: float = 0.0
    retry_after: int = 0


class FaultScript:
    def __init__(self, source: Path | dict | None):
        try:
            if isinstance(source, (str, Path)):
                source = json.loads(Path(source).read_text(encoding="utf-8"))
            if source is None:
                source = {}
            if not isinstance(source, dict):
                raise ValueError
            self.sequences = {}
            for route, actions in source.items():
                if not ROUTE.fullmatch(route) or not isinstance(actions, list):
                    raise ValueError
                self.sequences[route] = deque(self._parse(route, item) for item in actions)
        except (OSError, TypeError, ValueError, OverflowError):
            raise ValueError("MOCK_FAULTS_INVALID") from None

    @staticmethod
    def _parse(route, item):
        item = {"action": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or set(item) - {"action", "delay_seconds", "retry_after"}:
            raise ValueError
        action = item.get("action")
        if action not in ACTIONS or (action in {"timeout_after", "500_after"} and route != "POST /tickets"):
            raise ValueError
        delay = item.get("delay_seconds", 0.0)
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 60:
            raise ValueError
        if action.startswith("timeout_") != (delay > 0):
            raise ValueError
        retry_after = item.get("retry_after", 1 if action == "429" else 0)
        if type(retry_after) is not int or not 0 <= retry_after <= 60 or (action != "429" and retry_after != 0):
            raise ValueError
        return Fault(action, delay, retry_after)

    def next(self, method: str, path: str) -> Fault:
        sequence = self.sequences.get(f"{method} {path}")
        return sequence.popleft() if sequence else Fault()
