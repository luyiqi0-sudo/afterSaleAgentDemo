"""Run with: python -m uvicorn mock_server:app --host 127.0.0.1 --port 8090 --workers 1"""

import os
from pathlib import Path

from mock_service.app import create_app


ROOT = Path(__file__).resolve().parent
app = create_app(
    os.environ.get("DATASET_ROOT", ROOT / "data/c004-r01/baseline"),
    os.environ.get("MOCK_TICKET_DB", ROOT / "runtime/mock/tickets.sqlite"),
    os.environ.get("BUSINESS_API_TOKEN"),
    os.environ.get("MOCK_FAULTS") or None,
)
