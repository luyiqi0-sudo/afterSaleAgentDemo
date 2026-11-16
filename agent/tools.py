import asyncio
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from agent.contracts import Principal, ToolResult
from agent.guards import valid_order_id
from agent.runtime_store import RuntimeStore, digest
from agent.runtime_types import GateGrant, RuntimeConflict


def retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class BusinessTools:
    """Frozen Mock adapter. Legacy facade remains available only for unit fixtures."""

    def __init__(self, sql, operations, client: httpx.AsyncClient, timeout=3.0, budget=8.0, backoff=0.2, emit=None,
                 *, snapshot_verifier=None, service_token=None, strict=False, reserve_http=None):
        self.sql = sql
        self.operations = operations
        self.client = client
        self.timeout = min(timeout, 3.0)
        self.budget = min(budget, 8.0)
        self.backoff = backoff
        self.emit = emit or (lambda *_args, **_kwargs: None)
        self.snapshot_verifier, self.service_token = snapshot_verifier, service_token
        self.strict = strict or isinstance(operations, RuntimeStore)
        self.reserve_http = reserve_http
        self._send_tasks = {}
        self._pre_send_tasks = {}
        self._settlement_executor = None
        self._settlements = {}
        self._settlement_lock = threading.Lock()
        self._closing = False
        if self.strict and (snapshot_verifier is None or not service_token or not isinstance(operations, RuntimeStore)):
            raise ValueError("strict tools require snapshot, service credentials and RuntimeStore")

    def _headers(self, principal):
        return {"Authorization": "Bearer " + self.service_token, "X-Actor-ID": principal.user_id} if self.strict else {}

    def _emit_http_timeout(self, tool, order_id, attempt):
        try:
            self.emit("http_timeout", {"tool": tool, "order_id": order_id, "attempt": attempt})
        except Exception:
            # A missing observation will fail evidence checks; never change
            # HTTP behavior or settlement because the observer failed.
            pass

    async def _authorize(self, order_id: str, principal: Principal) -> ToolResult | None:
        if self.strict and getattr(principal, "tenant", "demo") != "demo":
            return ToolResult(status="FAILED", error_code="order_unavailable")
        if not valid_order_id(order_id):
            return ToolResult(status="FAILED", error_code="invalid_parameters")
        try:
            owned = await asyncio.to_thread(self.sql.owns_order, order_id, principal)
        except Exception:
            return ToolResult(status="FAILED", error_code="authorization_unavailable")
        if not owned:
            return ToolResult(status="FAILED", error_code="order_unavailable")
        return None

    async def _query(self, path: str, order_id: str, principal: Principal, kind: str, *, reserve_http=None, deadline=None) -> ToolResult:
        denied = await self._authorize(order_id, principal)
        if denied:
            return denied
        deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + self.budget)
        attempts = 0
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempts += 1
            wait = self.backoff * 2**attempt + random.uniform(0, self.backoff)
            try:
                reservation = reserve_http or self.reserve_http
                if reservation:
                    allowed = reservation()
                    if allowed is not None:
                        remaining = min(remaining, allowed)
                remaining = min(remaining, deadline - time.monotonic())
                if remaining <= 0:
                    raise RuntimeConflict("BUDGET_EXHAUSTED")
                response = await asyncio.wait_for(self.client.get(path, headers=self._headers(principal), follow_redirects=False,
                                                                 timeout=min(self.timeout, remaining)), timeout=min(self.timeout, remaining))
                if response.status_code == 200:
                    if len(response.content) > 65536:
                        raise ValueError("oversized response")
                    data = response.json()
                    if self.strict:
                        validator = self.snapshot_verifier.validate_order if kind == "order" else self.snapshot_verifier.validate_logistics
                        try:
                            data = validator(data, order_id, principal)
                        except ValueError:
                            return ToolResult(status="FAILED", error_code="RESPONSE_MISMATCH", attempts=attempts)
                        return ToolResult(status="SUCCESS", data=data, attempts=attempts)
                    if not isinstance(data, dict) or data.get("order_id") != order_id:
                        raise ValueError("response order mismatch")
                    fields = ("order_id", "amount", "status", "create_time") if kind == "order" else ("order_id", "logistics_id", "status", "update_time")
                    if "status" not in data or (kind == "order" and "amount" not in data):
                        raise ValueError("missing fields")
                    return ToolResult(status="SUCCESS", data={k: data[k] for k in fields if k in data}, attempts=attempts)
                if self.strict and kind == "logistics" and response.status_code == 404:
                    # Authorization above is required; a bare 404 cannot prove no logistics.
                    if len(response.content) > 65536:
                        raise ValueError("oversized response")
                    body = response.json()
                    error = body.get("error") if isinstance(body, dict) and body.keys() == {"error"} else None
                    if not (isinstance(error, dict) and error.keys() == {"code", "accepted"} and
                            error["accepted"] is False and isinstance(error["code"], str)):
                        raise ValueError("invalid error envelope")
                    if error["code"] in {"logistics_unavailable", "order_unavailable"}:
                        code = "NO_DATA" if error["code"] == "logistics_unavailable" else "order_unavailable"
                        return ToolResult(status="FAILED", error_code=code, attempts=attempts)
                if response.status_code not in (429, 500, 502, 503, 504):
                    return ToolResult(status="FAILED", error_code="service_rejected", attempts=attempts)
                if response.status_code == 429:
                    wait = max(wait, retry_after(response.headers.get("Retry-After")))
            except (httpx.TimeoutException, asyncio.TimeoutError):
                self._emit_http_timeout("query_" + kind, order_id, attempts)
            except httpx.NetworkError:
                pass
            except (ValueError, TypeError):
                return ToolResult(status="FAILED", error_code="invalid_response", attempts=attempts)
            except RuntimeConflict:
                return ToolResult(status="FAILED", error_code="BUDGET_EXHAUSTED", attempts=attempts - 1)
            if attempt == 2 or wait >= deadline - time.monotonic():
                break
            self.emit("tool_retry", {"tool": "query_" + kind, "attempt": attempts, "delay_seconds": wait})
            await asyncio.sleep(wait)
        return ToolResult(status="FAILED", error_code="service_unavailable", attempts=attempts)

    async def query_order(self, order_id: str, principal: Principal, **runtime) -> ToolResult:
        return await self._query(f"/orders/{order_id}", order_id, principal, "order", **runtime)

    async def query_logistics(self, order_id: str, principal: Principal, **runtime) -> ToolResult:
        return await self._query(f"/logistics/{order_id}", order_id, principal, "logistics", **runtime)

    async def create_ticket(self, order_id: str, reason: str, operation_id: str, principal: Principal, *, grant=None) -> ToolResult:
        if self._closing:
            return ToolResult(status="FAILED", error_code="TOOLS_CLOSING", operation_id=operation_id)
        executor = asyncio.current_task()
        self._pre_send_tasks[executor] = grant.run_id if isinstance(grant, GateGrant) else None
        try:
            return await self._create_ticket(order_id, reason, operation_id, principal, grant=grant)
        finally:
            self._pre_send_tasks.pop(executor, None)

    async def _create_ticket(self, order_id, reason, operation_id, principal, *, grant):
        if self._closing:
            return ToolResult(status="FAILED", error_code="TOOLS_CLOSING", operation_id=operation_id)
        if self.strict and not isinstance(grant, GateGrant):
            return ToolResult(status="FAILED", error_code="CONFIRMATION_REQUIRED", operation_id=operation_id)
        if not reason.strip() or len(reason) > 500 or not valid_order_id(operation_id):
            return ToolResult(status="FAILED", error_code="invalid_parameters")
        denied = await self._authorize(order_id, principal)
        if self._closing:
            return ToolResult(status="FAILED", error_code="TOOLS_CLOSING", operation_id=operation_id)
        if denied:
            return denied
        payload = {"order_id": order_id, "reason": reason.strip(), "user_id": principal.user_id}
        if self.strict:
            try:
                intent = self.operations.get_intent(self.operations.get_receipt(grant.receipt_id)["intent_id"])
                if grant.operation_id != operation_id or digest(payload) != intent["payload_hash"]:
                    return ToolResult(status="FAILED", error_code="operation_conflict", operation_id=operation_id)
                claim = self.operations.claim(grant)
                if claim.token is None:
                    return claim.result
                # Current object ownership is checked again immediately before the
                # durable send marker; the store also rechecks fence/receipt/TTL.
                denied = await self._authorize(order_id, principal)
                if self._closing:
                    # This executor irrevocably exits without a send; its own
                    # RESERVED operation can therefore be closed as NOT_SENT.
                    future = self._submit_runtime_work(grant.run_id, operation_id,
                                                       self.operations.recover_run, grant.run_id,
                                                       executor_stopped=True, reason="TOOLS_CLOSING")
                    await asyncio.shield(self._settlement_waiter(future))
                    return ToolResult(status="FAILED", error_code="NOT_SENT", operation_id=operation_id)
                if denied:
                    self.operations.recover_run(grant.run_id, executor_stopped=True, reason="DENIED")
                    return denied
                self.operations.start_send(claim.token, principal)
            except RuntimeConflict as exc:
                return ToolResult(status="FAILED", error_code=str(exc), operation_id=operation_id)
            except (sqlite3.Error, OSError):
                return ToolResult(status="FAILED", error_code="STORAGE_UNAVAILABLE", operation_id=operation_id)
        else:
            claimed, existing = self.operations.claim(operation_id, payload)
            if not claimed:
                return existing
        if self.strict:
            # Ownership transfers ONLY after SEND_STARTED has committed. No await
            # occurs between this transfer and registration; pre-send work above
            # remains cancellable by the graph/controller.
            self._pre_send_tasks.pop(asyncio.current_task(), None)
            task = asyncio.create_task(self._send_and_settle(order_id, operation_id, payload, principal, grant))
            self._send_tasks[operation_id] = (grant.run_id, task)
            def completed(done):
                self._send_tasks.pop(operation_id, None)
                if not done.cancelled():
                    done.exception()  # retrieve any exceptional completion
            task.add_done_callback(completed)
            return await asyncio.shield(task)
        return await self._send_and_settle(order_id, operation_id, payload, principal, grant)

    def pending_sends(self, run_id=None):
        """Observe existing post-marker tasks; this method never creates a send."""
        return tuple(task for owner, task in self._send_tasks.values() if run_id is None or owner == run_id)

    def pending_pre_sends(self, run_id=None):
        return tuple(task for task, owner in self._pre_send_tasks.items() if run_id is None or owner == run_id)

    def pending_settlements(self, run_id=None):
        """Real settlement/recovery futures, independent of asyncio waiter cancellation."""
        with self._settlement_lock:
            return tuple(future for owner, future in self._settlements.values()
                         if (run_id is None or owner == run_id) and not future.done())

    def _submit_settlement(self, run_id, operation_id, result):
        return self._submit_runtime_work(run_id, operation_id, self.operations.settle,
                                         operation_id, result, wait_seconds=1.5)

    def _submit_runtime_work(self, run_id, operation_id, callback, *args, **kwargs):
        if self._settlement_executor is None:
            self._settlement_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ticket-settlement")
        future = self._settlement_executor.submit(callback, *args, **kwargs)
        with self._settlement_lock:
            self._settlements[operation_id] = (run_id, future)
        def completed(_future):
            with self._settlement_lock:
                self._settlements.pop(operation_id, None)
        future.add_done_callback(completed)
        return future

    @staticmethod
    def _settlement_waiter(future):
        waiter = asyncio.wrap_future(future)
        def observed(done):
            if not done.cancelled():
                done.exception()
        waiter.add_done_callback(observed)
        return waiter

    async def drain_sends(self, run_id=None, *, timeout=2.0):
        """Bounded settlement wait. Timeout does not cancel or resend existing I/O."""
        tasks = self.pending_pre_sends(run_id) + self.pending_sends(run_id) + tuple(self._settlement_waiter(future) for future in self.pending_settlements(run_id))
        if not tasks:
            return []
        done, _ = await asyncio.wait(tasks, timeout=min(2.0, max(0.0, timeout)))
        return [task.result() for task in done if not task.cancelled() and task.exception() is None and isinstance(task.result(), ToolResult)]

    async def aclose(self, *, timeout=2.0):
        """Stop admission; true only after pre-send, send and actual DB work ended.

        A false return requires keeping client/runtime alive and draining again.
        This object does not own or close the externally supplied httpx client.
        """
        self._closing = True
        await self.drain_sends(timeout=timeout)
        if self.pending_pre_sends() or self.pending_sends() or self.pending_settlements():
            return False
        if self._settlement_executor is not None:
            self._settlement_executor.shutdown(wait=False, cancel_futures=False)
            self._settlement_executor = None
        return True

    def _valid_ticket(self, body, order_id, response_status):
        if not isinstance(body, dict) or body.keys() != {"ticket_id", "order_id", "status", "_meta"}:
            return False
        if response_status != 201 or body["order_id"] != order_id or body["status"] != "ACCEPTED":
            return False
        if not isinstance(body["ticket_id"], str) or not valid_order_id(body["ticket_id"]):
            return False
        meta = body["_meta"]
        expected = self.snapshot_verifier.metadata
        if not isinstance(meta, dict) or meta.keys() != expected.keys() | {"observed_at"}:
            return False
        if any(type(meta[key]) is not type(value) or meta[key] != value for key, value in expected.items()):
            return False
        stamp = meta["observed_at"]
        if not isinstance(stamp, str) or "T" not in stamp:
            return False
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).utcoffset() is not None
        except ValueError:
            return False

    @staticmethod
    def _proves_rejected(status, body):
        codes = {400: {"actor_required"}, 401: {"unauthorized"}, 403: {"actor_mismatch"},
                 404: {"order_unavailable"}, 422: {"invalid_parameters"}}
        if not isinstance(body, dict) or body.keys() != {"error"}:
            return False
        error = body["error"]
        return (isinstance(error, dict) and error.keys() == {"code", "accepted"} and
                error["accepted"] is False and isinstance(error["code"], str) and error["code"] in codes.get(status, set()))

    async def _send_and_settle(self, order_id, operation_id, payload, principal, grant):
        result = ToolResult(status="UNKNOWN", error_code="pending_reconciliation", attempts=0, operation_id=operation_id)
        try:
            # No automatic retry: the server's idempotency contract is unverified.
            timeout = min(self.timeout, grant.deadline - time.monotonic()) if self.strict else self.timeout
            if timeout <= 0:
                result.error_code = "DEADLINE_AFTER_SEND_MARKER"
                raise asyncio.TimeoutError
            result.attempts = 1
            response = await asyncio.wait_for(self.client.post("/tickets", json=payload, headers=self._headers(principal),
                                                              follow_redirects=False, timeout=timeout), timeout=timeout)
            if response.status_code in (200, 201) and len(response.content) <= 65536:
                body = response.json()
                ticket_id = body.get("ticket_id") if isinstance(body, dict) else None
                valid = isinstance(ticket_id, str) and valid_order_id(ticket_id)
                if self.strict:
                    valid = self._valid_ticket(body, order_id, response.status_code)
                if valid:
                    result = ToolResult(status="SUCCESS", data={"ticket_id": ticket_id, "order_id": order_id}, attempts=1, operation_id=operation_id)
            elif response.status_code in (400, 401, 403, 404, 422):
                body = response.json() if self.strict else {}
                if not self.strict or self._proves_rejected(response.status_code, body):
                    result = ToolResult(status="FAILED", error_code="service_rejected", attempts=1, operation_id=operation_id)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            if result.attempts:
                self._emit_http_timeout("create_ticket", order_id, result.attempts)
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        finally:
            # On cancellation or process failure the durable UNKNOWN reservation survives.
            try:
                if self.strict:
                    future = self._submit_settlement(grant.run_id, operation_id, result)
                    await asyncio.shield(self._settlement_waiter(future))
                else:
                    self.operations.finish(operation_id, result)
            except (sqlite3.Error, OSError):
                # The persisted SEND_STARTED marker remains. Do not claim either
                # that settlement persisted or that a successful response failed.
                result = ToolResult(status="UNKNOWN", error_code="SETTLEMENT_NOT_PERSISTED", attempts=1, operation_id=operation_id)
        return result
