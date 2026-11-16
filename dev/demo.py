"""Prepare or use isolated local Demo credentials without echoing them."""
import argparse
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from pydantic import SecretStr

from agent.config import Settings
from api.main import create_app as create_api
from mock_service.app import create_app as create_mock


ROOT = Path("data/c004-r01/baseline").resolve()
DATABASE = Path("database").resolve()
KNOWLEDGE = Path("knowledge").resolve()
RUNTIME = Path("runtime/local-demo")
PRIVATE = RUNTIME / "credentials.json"


def _credentials():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(PRIVATE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return json.loads(PRIVATE.read_text(encoding="utf-8"))
    data = {
        "business": secrets.token_urlsafe(32),
        "demo": secrets.token_urlsafe(32),
        "other_demo": secrets.token_urlsafe(32),
    }
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file)
    print("Created private Demo credentials under runtime/local-demo.")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    mock = commands.add_parser("mock")
    mock.add_argument("--faults", type=Path)
    api = commands.add_parser("api")
    api.add_argument("--vector", choices=("memory", "pgvector"), required=True)
    chat = commands.add_parser("chat")
    chat.add_argument("message")
    chat.add_argument("--actor", choices=("owner", "other"), default="owner")
    chat.add_argument("--id", default=None)
    chat.add_argument("--session")
    chat.add_argument("--interrupt")
    chat.add_argument("--revision", type=int)
    chat.add_argument("--base", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    cust = _credentials()
    if args.command == "init":
        return
    if args.command == "mock":
        uvicorn.run(create_mock(ROOT, RUNTIME / "mock" / "tickets.sqlite", cust["business"], args.faults),
                    host="127.0.0.1", port=8090, access_log=False)
    elif args.command == "api":
        settings = Settings().model_copy(update={
            "dataset_root": ROOT, "database_path": DATABASE / "ecommerce.db",
            "dictionary_path": DATABASE / "dictionary.json", "knowledge_path": KNOWLEDGE,
            "runtime_dir": RUNTIME / "api", "business_base_url": "http://127.0.0.1:8090",
            "business_service_token": SecretStr(cust["business"]),
            "demo_mode": True, "auth_tokens": {cust["demo"]: "eval-u01", cust["other_demo"]: "eval-u02"},
            "vector_backend": args.vector,
        })
        uvicorn.run(create_api(settings), host="127.0.0.1", port=8080, access_log=False)
    elif args.command == "chat":
        body = {"message": args.message, "client_message_id": args.id or uuid4().hex}
        body.update({k: v for k, v in {"session_id": args.session, "interrupt_id": args.interrupt,
                                     "expected_revision": args.revision}.items() if v is not None})
        token = cust["demo" if args.actor == "owner" else "other_demo"]
        # Only send the local handoff credential to this fixed loopback service.
        if args.base != "http://127.0.0.1:8080":
            parser.error("Demo helper uses only http://127.0.0.1:8080")
        with httpx.Client(base_url=args.base, timeout=35, trust_env=False) as client:
            with client.stream("POST", "/chat", json=body, headers={"Authorization": "Bearer " + token}) as response:
                print(f"HTTP {response.status_code}")
                for line in response.iter_lines():
                    if line.startswith("data: ") and response.status_code == 200:
                        wire = json.loads(line[6:])
                        print(json.dumps(wire, ensure_ascii=False))
                    else:
                        print(line)


if __name__ == "__main__":
    main()
