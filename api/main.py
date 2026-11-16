"""Authenticated SSE, dependency assembly, and single-worker lifetime."""
import asyncio
import json
import re
import sqlite3
import time
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from agent.config import Settings
from agent.controller import AgentController, ChatRequest
from agent.events import PUBLIC_FIELDS, PrivateTrace, public_frame
from agent.provider import HTTPModelProvider
from agent.runtime_store import RuntimeStore
from agent.runtime_types import RuntimeConflict
from agent.snapshot import SnapshotVerifier
from agent.sql import SQLService
from agent.tools import BusinessTools
from api.auth import demo_identity_ready, demo_principal
from database.business_snapshot import API_VERSION
from database.source import verify_database_source
from knowledge.ingest import ingest_knowledge, open_retriever
from knowledge.source import verify_knowledge_source


CONFLICT_CODES = frozenset({"RUN_BUSY", "MESSAGE_CONFLICT", "STALE_INTERRUPT", "STALE_REVISION",
                            "INVALID_DECISION", "SERVICE_STOPPING", "SESSION_UNAVAILABLE"})
STARTUP_CODES = frozenset({"SNAPSHOT_INVALID", "SNAPSHOT_PATH_MISMATCH", "BUSINESS_SERVICE_TOKEN_REQUIRED",
                          "KNOWLEDGE_SNAPSHOT_MISMATCH", "DATABASE_SNAPSHOT_MISMATCH",
                          "RUNTIME_MUST_BE_PRIVATE", "WORKER_ALREADY_RUNNING", "PROVIDER_CONFIGURATION_REQUIRED",
                          "TIMEOUT", "INVALID_OUTPUT", "HTTP_FAILURE", "PGVECTOR_UNAVAILABLE"})
REQUEST_STATUSES = frozenset({"waiting_input", "completed", "terminated"})
TRACE_ID = ContextVar("trace_correlation_id", default=None)


class EventStream(StreamingResponse):
    """Watch disconnect during idle generations on both ASGI 2.3 and 2.4+."""

    async def __call__(self, scope, receive, send):
        writer = asyncio.create_task(self.stream_response(send))
        disconnect = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            await asyncio.wait((writer, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if writer.done() and not writer.cancelled():
                try:
                    writer.result()
                except OSError:
                    pass
        finally:
            for task in (writer, disconnect):
                if not task.done():
                    task.cancel()
            await asyncio.gather(writer, disconnect, return_exceptions=True)
            await self.body_iterator.aclose()


def _stream(controller, principal, command, counts, trace, *, demo_mode):
    async def events():
        queue = asyncio.Queue()
        terminal = False
        correlation_id = uuid4().hex
        started = time.monotonic()
        delivery = {}

        def emit(event, data):
            nonlocal terminal, delivery
            if terminal or event not in PUBLIC_FIELDS:
                return
            frame = public_frame(event, data, request_id=correlation_id,
                                 conversation_id=data.get("session_id") if event == "done" else command.session_id)
            if event == "done":
                terminal = True
                delivery = data
                status = data.get("request_status")
                counts[status if status in REQUEST_STATUSES else "other"] += 1
                if data.get("persisted") is False:
                    counts["unpersisted"] += 1
            queue.put_nowait((event, frame))

        async def run():
            token = TRACE_ID.set(correlation_id)
            try:
                result = await controller.handle(principal, command, emit=emit)
                if not terminal:
                    emit("done", result)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = str(error) if isinstance(error, RuntimeConflict) and str(error) in CONFLICT_CODES else "REQUEST_FAILED"
                emit("error", {"code": code})
                # This snapshot was not returned by the Controller. Do not
                # claim task status or persistence for this delivery.
                emit("done", {"session_id": command.session_id, "request_status": "terminated",
                              "reason": code, "persisted": False})
            finally:
                if trace:
                    final_delivery = delivery or {"reason": "CLIENT_DISCONNECTED"}
                    trace("delivery", {**final_delivery, "correlation_id": correlation_id,
                                       "latency_ms": round((time.monotonic() - started) * 1000)})
                TRACE_ID.reset(token)

        task = asyncio.create_task(run())
        try:
            while True:
                event, frame = await queue.get()
                yield frame
                if event == "done":
                    break
        finally:
            if not task.done():
                if not terminal:
                    counts["disconnected"] += 1
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    return EventStream(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
        "X-Agent-Demo-Mode": "true" if demo_mode else "false",
    })


def _business_capture(trace):
    async def capture(response):
        # Only inspect bounded, already buffered response data for the frozen receipt.
        method = response.request.method
        path = response.request.url.path
        route = "/tickets" if path == "/tickets" else (
            "/orders/:id" if path.startswith("/orders/") else
            "/logistics/:id" if path.startswith("/logistics/") else "other")
        if route == "other":
            return
        data = {"method": method, "route": route, "http_status": response.status_code,
                "correlation_id": TRACE_ID.get()}
        if route in {"/orders/:id", "/logistics/:id"}:
            data["order_id"] = path.rsplit("/", 1)[-1]
        if method == "POST" and route == "/tickets" and response.status_code == 201:
            try:
                if int(response.headers.get("content-length", "65537")) <= 65536:
                    await response.aread()
                    if len(response.content) <= 65536:
                        receipt = response.json()
                        data.update({k: receipt[k] for k in ("order_id", "ticket_id")
                                     if isinstance(receipt.get(k), str)})
            except (ValueError, AttributeError):
                pass
        trace("http_response", data)

    return capture


def _business_send(trace):
    async def capture(request):
        path = request.url.path
        data = {"method": request.method, "correlation_id": TRACE_ID.get()}
        if path.startswith(("/orders/", "/logistics/")):
            tool, order = path.strip("/").split("/", 1)
            data.update(tool="query_order" if tool == "orders" else "query_logistics", order_id=order)
        elif path == "/tickets" and request.method == "POST":
            body = json.loads(request.content)
            data.update(tool="create_ticket", order_id=body.get("order_id"), reason=body.get("reason"))
        else:
            return
        trace("http_send", data)
    return capture


async def assemble(settings, business, trace, *, model=None, retriever=None):
    """Build verified dependencies; no fallback model or vector backend."""
    root = settings.dataset_root.resolve()
    snapshot = await asyncio.to_thread(SnapshotVerifier, root)
    dictionary_path = await asyncio.to_thread(verify_database_source, root, settings.database_path,
                                             settings.dictionary_path)
    await asyncio.to_thread(verify_knowledge_source, root, settings.knowledge_path)
    token = settings.business_service_token.get_secret_value()
    if not re.fullmatch(r"[!-~]+", token):
        raise ValueError("BUSINESS_SERVICE_TOKEN_REQUIRED")
    if model is None and not all((settings.model_name, settings.model_api_key.get_secret_value(),
                                  settings.embedding_model, settings.embedding_api_key.get_secret_value())):
        raise ValueError("PROVIDER_CONFIGURATION_REQUIRED")
    if (settings.runtime_dir.resolve().is_relative_to(root)
            or settings.runtime_dir.resolve().is_relative_to(settings.database_path.resolve().parent)):
        raise ValueError("RUNTIME_MUST_BE_PRIVATE")
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    model = model if model is not None else HTTPModelProvider(settings)
    if retriever is None:
        if settings.vector_backend == "memory":
            retriever = await ingest_knowledge(settings, model)
        else:
            retriever = open_retriever(settings, model)
    sql = SQLService(settings.database_path, dictionary_path=dictionary_path,
                     anchor_date=settings.anchor_date)
    store = RuntimeStore(settings.runtime_dir / "runtime.sqlite", busy_timeout_seconds=.1)

    def bound_trace(event, data):
        trace(event, {**data, "runtime_request_id": data.get("request_id"), "correlation_id": TRACE_ID.get()})

    tools = BusinessTools(sql, store, business, timeout=settings.http_timeout_seconds,
                          budget=settings.http_budget_seconds, snapshot_verifier=snapshot, service_token=token,
                          emit=bound_trace)

    controller = AgentController(store=store, checkpoint_path=settings.runtime_dir / "checkpoint.sqlite",
                                 model=model, sql=sql, tools=tools, retriever=retriever,
                                 business_date=settings.anchor_date, request_seconds=settings.request_budget_seconds,
                                 trace_emit=bound_trace)

    async def readiness():
        checks = {"frozen_sqlite": "ok", "provider_configuration": "ok",
                  "chat_provider_probe": "not_probed"}

        async def read_probe(path, *, headers=None, params=None):
            async with business.stream("GET", path, params=params, timeout=2, follow_redirects=False,
                                       headers=headers) as probe:
                if probe.status_code != 200:
                    return None
                raw = bytearray()
                async for piece in probe.aiter_bytes(chunk_size=8192):
                    if len(raw) + len(piece) > 8192:
                        return None
                    raw.extend(piece)
            return json.loads(raw)

        async def check_business():
            checks["business"] = "unavailable"
            try:
                async with asyncio.timeout(2):
                    if await read_probe("/health") != {
                        "status": "ok", "api_version": API_VERSION, "dataset_id": snapshot.dataset_id,
                    }:
                        return
                    # Use a dedicated probe actor with no business actions. The
                    # list endpoint authenticates even an empty order scope.
                    body = await read_probe("/orders", params={"limit": 1}, headers={
                        "Authorization": "Bearer " + token, "X-Actor-ID": "__readiness__",
                    })
                    meta = body.get("_meta") if isinstance(body, dict) else None
                    if (isinstance(meta, dict) and body.get("items") == []
                            and all(meta.get(k) == v for k, v in snapshot.metadata.items())):
                        checks["business"] = "ok"
            except (TimeoutError, httpx.HTTPError, ValueError):
                return

        async def check_vector():
            try:
                await asyncio.wait_for(retriever.check_index(), 4)
                checks["policy_index"] = "ok"
            except (TimeoutError, ValueError, RuntimeError):
                checks["policy_index"] = "unavailable"

        await asyncio.gather(check_business(), check_vector())
        return checks

    return controller, readiness


def create_app(settings: Settings | None = None, *, controller=None, readiness=None, model=None,
               retriever=None) -> FastAPI:
    configuration = settings if settings is not None else Settings()
    counts = Counter()
    trace = PrivateTrace(configuration.runtime_dir / "private-trace.jsonl", (
        configuration.model_api_key.get_secret_value(), configuration.embedding_api_key.get_secret_value(),
        configuration.business_service_token.get_secret_value(), configuration.pg_dsn.get_secret_value(),
        *configuration.auth_tokens,
    ))

    @asynccontextmanager
    async def lifespan(app):
        current, probe, business = controller, readiness, None
        app.state.controller = None
        app.state.readiness = None
        app.state.startup_code = None
        try:
            try:
                if current is None:
                    business = httpx.AsyncClient(base_url=configuration.business_base_url, event_hooks={
                        "response": [_business_capture(trace)], "request": [_business_send(trace)],
                    })
                    current, probe = await assemble(configuration, business, trace, model=model,
                                                    retriever=retriever)
                await current.start()
                app.state.controller, app.state.readiness = current, probe
            except Exception as error:
                # Keep /health/live available while this worker is unready.
                counts["startup_failure"] += 1
                app.state.startup_code = str(error) if str(error) in STARTUP_CODES else "STARTUP_FAILED"
            yield
        finally:
            app.state.controller = None
            if current is not None:
                while not await current.aclose():
                    # Actual sends/recovery workers still own resources. Wait for
                    # stopped proof rather than closing their client or lock.
                    await asyncio.sleep(.1)
            if business is not None:
                await business.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse({"detail": "INVALID_REQUEST"}, status_code=422)

    async def principal(req: Request):
        actor = demo_principal(configuration, req.headers.getlist("authorization"))
        if actor is None:
            raise HTTPException(401, "UNAUTHENTICATED", headers={"WWW-Authenticate": "Bearer"})
        return actor

    @app.post("/chat")
    async def chat(body: ChatRequest, req: Request, actor=Depends(principal)):
        current = req.app.state.controller
        if current is None:
            raise HTTPException(503, "SERVICE_UNAVAILABLE")
        try:
            await current.preflight(actor, body)
        except RuntimeConflict as error:
            if str(error) == "SESSION_UNAVAILABLE":
                raise HTTPException(404, "SESSION_UNAVAILABLE") from None
            raise HTTPException(409, str(error) if str(error) in CONFLICT_CODES else "REQUEST_CONFLICT") from None
        except (sqlite3.Error, OSError):
            raise HTTPException(503, "SERVICE_UNAVAILABLE") from None
        counts["accepted"] += 1
        return _stream(current, actor, body, counts, trace if controller is None else None,
                       demo_mode=configuration.demo_mode)

    @app.get("/health")
    async def health(req: Request, mode: Literal["live", "ready"] = "live"):
        state = req.app.state
        body = {"status": "ok", "mode": mode, "demo_mode": configuration.demo_mode}
        if mode == "ready":
            checks = {"controller": "ok" if state.controller else "unavailable",
                      "identity": "ok" if demo_identity_ready(configuration) else "unavailable"}
            if state.readiness:
                try:
                    checks.update(await state.readiness())
                except Exception:
                    checks["dependencies"] = "unavailable"
            body["checks"] = checks
            if state.startup_code:
                body["startup_code"] = state.startup_code
            if any(value not in {"ok", "not_probed"} for value in checks.values()):
                return JSONResponse({**body, "status": "unavailable"}, status_code=503)
        return body

    @app.get("/metrics")
    async def metrics():
        values = ("accepted", "completed", "waiting_input", "terminated", "disconnected", "unpersisted", "other")
        lines = ["# TYPE after_sales_chat_total counter"]
        lines.extend(f'after_sales_chat_total{{outcome="{value}"}} {counts[value]}' for value in values)
        lines.extend((f"after_sales_startup_failures_total {counts['startup_failure']}",
                      f"after_sales_trace_write_failures_total {trace.write_errors}"))
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    return app
