import asyncio
import hmac
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from database.business_snapshot import API_VERSION, SHANGHAI, BusinessSnapshot
from mock_service.contracts import ID_PATTERN, CursorCodec, TicketRequest
from mock_service.faults import Fault, FaultScript


def _error(status: int, code: str, **kwargs) -> JSONResponse:
    body = {"code": code}
    if status in {400, 401, 403, 404, 405, 422}:
        body["accepted"] = False
    return JSONResponse({"error": body}, status_code=status, **kwargs)


def _stamp() -> str:
    return datetime.now(SHANGHAI).isoformat()


def _init_tickets(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("""CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL, user_id TEXT NOT NULL,
            reason TEXT NOT NULL, accepted_at TEXT NOT NULL
        )""")


def _insert_ticket(path: Path, payload: TicketRequest) -> str:
    ticket_id = "T-" + uuid4().hex
    with closing(sqlite3.connect(path, timeout=0.5)) as con, con:
        con.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?)", (
            ticket_id, payload.order_id, payload.user_id, payload.reason, _stamp(),
        ))
    return ticket_id


async def _before(fault: Fault) -> JSONResponse | None:
    if fault.action == "429":
        return _error(429, "rate_limited", headers={"Retry-After": str(fault.retry_after)})
    if fault.action == "timeout_before":
        await asyncio.sleep(fault.delay_seconds)
    if fault.action in {"500", "timeout_before"}:
        return _error(500, "service_error")
    return None


def create_app(dataset_root, ticket_db, service_token, faults=None) -> FastAPI:
    """Configuration is caller-owned; no request can mutate the dataset or fault script."""
    dataset_root, ticket_db = Path(dataset_root).resolve(), Path(ticket_db).resolve()

    @asynccontextmanager
    async def lifespan(app):
        if not isinstance(service_token, str) or not service_token or not re.fullmatch(r"[!-~]+", service_token):
            raise ValueError("MOCK_SERVICE_TOKEN_REQUIRED")
        if ticket_db.is_relative_to(dataset_root):
            raise ValueError("MOCK_TICKET_DB_MUST_BE_PRIVATE")
        app.state.snapshot = BusinessSnapshot(dataset_root)
        app.state.faults = FaultScript(faults)
        app.state.cursors = CursorCodec(service_token.encode(), app.state.snapshot.dataset_id)
        _init_tickets(ticket_db)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.method == "GET" and request.url.path == "/health":
            return await call_next(request)
        credentials = request.headers.getlist("authorization")
        if len(credentials) != 1 or not hmac.compare_digest(credentials[0].encode(), ("Bearer " + service_token).encode()):
            return _error(401, "unauthorized")
        actors = request.headers.getlist("x-actor-id")
        if len(actors) != 1 or not re.fullmatch(r"[!-~]{1,128}", actors[0]):
            return _error(400, "actor_required")
        request.state.actor = actors[0]
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _exc):
        return _error(422, "invalid_parameters")

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return _error(exc.status_code, {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "invalid_request"))

    def meta() -> dict:
        return dict(app.state.snapshot.metadata, observed_at=_stamp())

    async def lookup(request: Request, order_id: str, logistics: bool):
        if not re.fullmatch(ID_PATTERN, order_id):
            return _error(422, "invalid_parameters")
        snapshot, actor = app.state.snapshot, request.state.actor
        record = snapshot.order(order_id, actor)
        if record is None:
            return _error(404, "order_unavailable")
        if logistics:
            record = snapshot.logistics(order_id, actor)
            if record is None:
                return _error(404, "logistics_unavailable")
        failure = await _before(app.state.faults.next(request.method, request.url.path))
        return failure or dict(record, _meta=meta())

    @app.get("/health")
    async def health():
        return {"status": "ok", "api_version": API_VERSION, "dataset_id": app.state.snapshot.dataset_id}

    @app.get("/orders/{order_id}")
    async def order(request: Request, order_id: str):
        return await lookup(request, order_id, logistics=False)

    @app.get("/logistics/{order_id}")
    async def logistics(request: Request, order_id: str):
        return await lookup(request, order_id, logistics=True)

    @app.get("/orders")
    async def orders(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50, cursor: str | None = None):
        actor, codec = request.state.actor, app.state.cursors
        try:
            offset = codec.decode(cursor, actor) if cursor is not None else 0
        except ValueError:
            return _error(400, "invalid_cursor")
        failure = await _before(app.state.faults.next(request.method, request.url.path))
        if failure is not None:
            return failure
        rows = app.state.snapshot.orders(actor, offset, limit + 1)
        next_cursor = codec.encode(actor, offset + limit) if len(rows) > limit else None
        return {"items": rows[:limit], "next_cursor": next_cursor, "_meta": meta()}

    @app.post("/tickets", status_code=201)
    async def create_ticket(request: Request, payload: TicketRequest):
        actor = request.state.actor
        if payload.user_id != actor:
            return _error(403, "actor_mismatch")
        if app.state.snapshot.order(payload.order_id, actor) is None:
            return _error(404, "order_unavailable")
        fault = app.state.faults.next(request.method, request.url.path)
        failure = await _before(fault)
        if failure is not None:
            return failure
        try:
            ticket_id = await asyncio.to_thread(_insert_ticket, ticket_db, payload)
        except sqlite3.Error:
            return _error(500, "service_error")
        if fault.action == "500_after":
            return _error(500, "service_error")
        if fault.action == "timeout_after":
            await asyncio.sleep(fault.delay_seconds)
        return {"ticket_id": ticket_id, "order_id": payload.order_id, "status": "ACCEPTED", "_meta": meta()}

    return app
