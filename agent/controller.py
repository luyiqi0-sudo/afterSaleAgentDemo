"""Authenticated ingress, durable LangGraph waiting and conservative outer recovery."""
import asyncio
from contextlib import AsyncExitStack
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from pathlib import Path
import re
import sqlite3
import time
from typing import Literal

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.answers import render_answer
from agent.public_progress import interrupt_tool
from agent.budgets import BudgetScope, RECURSION_LIMIT
from agent.graph import build_graph
from agent.runtime_store import new_id
from agent.runtime_types import RuntimeConflict
from agent.state import RuntimeContext, TERMINAL_GOALS, aggregate, initial_state


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=4000)
    client_message_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)
    interrupt_id: str | None = Field(default=None, min_length=1, max_length=128)
    decision: Literal["APPROVE", "SUPPLY", "REPLACE", "AMBIGUOUS", "CANCEL"] | None = None

    @model_validator(mode="after")
    def resume_fields(self):
        if self.interrupt_id is not None and (self.session_id is None or self.expected_revision is None):
            raise ValueError("resume requires session_id and expected_revision")
        if self.interrupt_id is None and (self.decision is not None or self.expected_revision is not None):
            raise ValueError("resume fields require interrupt_id")
        return self


def classify_resume(request, intent):
    """A small approval language, deliberately conservative for free text."""
    text = request.message.strip()
    additions = []
    if re.search(r"同时|另外|也查|顺便|以及|并且", text):
        for match in re.finditer(r"(?:订单\s*|查(?:询)?\s*)([A-Za-z0-9][A-Za-z0-9_-]*)", text):
            subject = match.group(1)
            if subject != intent["payload"].get("order_id"):
                additions.append({"message_id": request.client_message_id, "text": text,
                                  "span": {"start": match.start(1), "end": match.end(1)},
                                  "requested_subject": subject, "requested_action": "查询", "relation": "ADDITIONAL"})
        if not additions:
            additions = [{"message_id": request.client_message_id, "text": text, "span": {"start": 0, "end": len(text)},
                          "requested_subject": None, "requested_action": "待澄清", "relation": "ADDITIONAL"}]
        return "AMBIGUOUS", additions
    if re.fullmatch(r"(?:取消|不办了|不要了|cancel|不用(?:创建|提交)(?:售后)?工单)[。！!]?", text, re.I):
        return "CANCEL", []
    if intent["kind"] == "CLARIFY" and intent["payload"].get("field") == "policy_query_date":
        return ("AMBIGUOUS" if request.decision == "AMBIGUOUS" else "SUPPLY"), []
    if re.search(r"不是.+[，, ](?:是|改为)|改成|换成|改为|换订单", text):
        return "REPLACE", []
    if intent["kind"] == "CLARIFY":
        return ("AMBIGUOUS" if request.decision == "AMBIGUOUS" else "SUPPLY"), []
    if request.decision not in (None, "APPROVE"):
        return "AMBIGUOUS", []
    subject = re.escape(intent["payload"].get("order_id", ""))
    if re.fullmatch(rf"(?:确认|同意|批准|yes|approve)(?:\s*(?:订单\s*)?{subject})?[。！!]?", text, re.I):
        return "APPROVE", []
    reason = intent["payload"].get("reason")
    if reason and re.fullmatch(rf"确认按展示的订单和\s*{re.escape(reason)}\s*原因创建工单[。！!]?", text):
        return "APPROVE", []
    return "AMBIGUOUS", []


class AgentController:
    def __init__(self, *, store, checkpoint_path: Path, model, sql, tools, retriever=None,
                 business_date=date(2026, 11, 15), request_seconds=30, clock=time.monotonic, trace_emit=None):
        self.store, self.checkpoint_path = store, Path(checkpoint_path)
        self.model, self.sql, self.tools, self.retriever = model, sql, tools, retriever
        self.business_date, self.request_seconds, self.clock = business_date, min(request_seconds, 30), clock
        self.trace_emit = trace_emit or (lambda event, data: None)
        self.graph = None
        self.trace = []
        self._stack = AsyncExitStack()
        self._locks = {}
        self._start_lock = asyncio.Lock()
        self._active = set()
        self._closing = False
        self._cleanup_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-local-cleanup")
        self._cleanup_futures = []

    def _config(self, run):
        return {"configurable": {"thread_id": run["thread_id"]}, "recursion_limit": RECURSION_LIMIT}

    async def start(self):
        async with self._start_lock:
            if self.graph is not None:
                return
            self.store.acquire_worker()
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                saver = await self._stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(self.checkpoint_path)))
                self.graph = build_graph(saver)
                # Acquiring the exclusive OS worker lock proves old process execution stopped.
                for run in self.store.list_runs():
                    if run["status"] == "LIVE":
                        await self._recover(run, "CRASH_RECOVERY")
                    elif run["status"] == "WAITING":
                        if self.store.clock() >= run["expires_at"]:
                            await self._recover(run, "TASK_EXPIRED")
                            continue
                        intent = self.store.get_intent(run.get("pending_intent_id"))
                        binding = self.store.get_binding(intent["intent_id"]) if intent else None
                        snapshot = await self.graph.aget_state(self._config(run))
                        actual = self._interrupt(snapshot)
                        if not intent or intent["status"] != "PENDING" or not binding or not actual or binding["framework_interrupt_id"] != actual.id or binding["checkpoint_id"] != snapshot.config["configurable"]["checkpoint_id"]:
                            await self._recover(run, "CRASH_RECOVERY")
            except BaseException:
                self.graph = None
                await self._stack.aclose()
                self.store.close()
                raise

    async def aclose(self):
        self._closing = True
        current = asyncio.current_task()
        active = [task for task in self._active if task is not current]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if any(not future.done() for future in self._cleanup_futures):
            return False
        if hasattr(self.tools, "aclose"):
            stopped = await self.tools.aclose(timeout=2)
            if not stopped:
                # A real settlement thread may still hold a transaction. Preserve
                # the OS worker lock/client/checkpointer until stopped is proven.
                return False
        elif hasattr(self.tools, "drain_sends"):
            await self.tools.drain_sends(timeout=2)
            remaining = self.tools.pending_sends()
            for pending in remaining:
                pending.cancel()
            if remaining:
                await asyncio.gather(*remaining, return_exceptions=True)
        await self._stack.aclose()
        self.store.close()
        self._cleanup_pool.shutdown(wait=False)
        self.graph = None
        return True

    async def preflight(self, principal, request):
        """Read-only early rejection; the locked ingress still owns the only CAS.

        Message identity takes precedence over the current run/interrupt revision:
        an exact authenticated replay does not become stale when a task advances.
        Expiry/budget settlement is intentionally left to handle's existing path.
        """
        if self._closing:
            raise RuntimeConflict("SERVICE_STOPPING")
        request = ChatRequest.model_validate(request)
        if not request.session_id:
            return
        replay = self.store.lookup_message(request.session_id, principal, request.client_message_id,
                                           self._request_content(request))
        if replay is not None:
            return
        session = self.store.get_session(request.session_id, principal)
        run = self.store.get_run(session["run_id"], principal)
        if not request.interrupt_id:
            if run["status"] != "CLOSED":
                raise RuntimeConflict("RUN_BUSY")
            return
        intent = self.store.get_intent(request.interrupt_id)
        binding = self.store.get_binding(request.interrupt_id)
        if (run["status"] != "WAITING" or run["state_revision"] != request.expected_revision or
                run.get("pending_intent_id") != request.interrupt_id or not intent or
                intent["status"] != "PENDING" or not binding):
            raise RuntimeConflict("STALE_INTERRUPT")
        for key in ("run_id", "task_id", "generation", "tenant", "actor", "state_revision", "goal_revision", "scope_epoch"):
            if intent.get(key) != run[key] or binding.get(key) != run[key]:
                raise RuntimeConflict("STALE_INTERRUPT")
        if binding.get("intent_id") != request.interrupt_id or binding.get("thread_id") != run["thread_id"]:
            raise RuntimeConflict("STALE_INTERRUPT")

    @staticmethod
    def _request_content(request):
        return request.model_dump(exclude={"session_id", "client_message_id"})

    @staticmethod
    def _interrupt(snapshot):
        values = [item for task in snapshot.tasks for item in task.interrupts]
        if len(values) > 1:
            raise RuntimeConflict("MULTIPLE_INTERRUPTS")
        return values[0] if values else None

    def _public(self, run, state, *, request_status="completed", interrupt_value=None, persisted=True, reason=None):
        return {"session_id": run["session_id"], "task_id": run["task_id"], "revision": run["state_revision"],
                "status": state.get("task_status", aggregate(state.get("goals", []), reason)),
                "request_status": request_status, "answer": state.get("answer") or render_answer(state),
                "goals": [{"goal_id": g["goal_id"], "description": g["description"], "subject": g.get("subject"),
                           "status": g["status"], "unmet_reason": g.get("unmet_reason")} for g in state.get("goals", [])],
                "interrupt": interrupt_value, "reason": reason or state.get("termination_reason"), "persisted": persisted,
                "operation_refs": list(state.get("unresolved_operation_ids") or [])}

    def _emit_result(self, emit, result):
        if result.get("interrupt"):
            emit("interrupt", result["interrupt"])
        emit("answer", {"text": result["answer"]})
        emit("done", result)
        return result

    async def handle(self, principal, request, emit=None):
        if self._closing:
            raise RuntimeConflict("SERVICE_STOPPING")
        request = ChatRequest.model_validate(request)
        emit = emit or (lambda event, data: None)
        await self.start()
        await self.preflight(principal, request)
        session_id = request.session_id or new_id()
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            task = asyncio.current_task()
            self._active.add(task)
            try:
                return await self._handle_locked(principal, request, session_id, emit)
            finally:
                self._active.discard(task)

    async def _handle_locked(self, principal, request, session_id, emit):
        receipt = None
        content = self._request_content(request)
        # Recheck under the session lock, not via a cached preflight verdict.
        # Old task receipts remain replayable even after this session starts a
        # new task; no current interrupt check or consume_resume is needed.
        if request.session_id:
            previous = self.store.lookup_message(session_id, principal, request.client_message_id, content)
            if previous is not None:
                original_run = self.store.get_run(previous["run_id"], principal)
                result = self.store.get_request_result(previous["request_id"])
                if original_run["status"] == "CLOSED" and (result is None or result.get("request_status") == "waiting_input"):
                    result = await self._recover(original_run, original_run.get("termination_reason") or "CRASH_RECOVERY")
                elif result is None:
                    # Interrupted old invocations are reconciled against runtime;
                    # the normal startup recovery already handles process death.
                    if original_run["status"] == "LIVE":
                        result = await self._recover(original_run, "CRASH_RECOVERY")
                    else:
                        result = self._waiting_result(original_run, original_run["verified_snapshot"])
                return self._emit_result(emit, result)
        if request.interrupt_id:
            session = self.store.get_session(session_id, principal)
            run = self.store.get_run(session["run_id"], principal)
            # Runtime consumption handles idempotent replay before old-revision rejection.
            binding = self.store.get_binding(request.interrupt_id)
            intent = self.store.get_intent(request.interrupt_id)
            if not intent or intent["run_id"] != run["run_id"] or not binding:
                raise RuntimeConflict("STALE_INTERRUPT")
            decision, additions = classify_resume(request, intent)
            snapshot = await self.graph.aget_state(self._config(run))
            actual = self._interrupt(snapshot)
            checkpoint_id = snapshot.config["configurable"]["checkpoint_id"] if snapshot.config else ""
            try:
                receipt = self.store.consume_resume(session_id, principal, request.interrupt_id, request.expected_revision,
                                                    request.client_message_id, content, decision, checkpoint_id,
                                                    addition_intents=additions)
            except RuntimeConflict as error:
                if str(error) in {"TASK_EXPIRED", "BUDGET_EXHAUSTED"}:
                    result = await self._recover(run, str(error))
                    return self._emit_result(emit, result)
                raise
            if receipt["replay"]:
                result = self.store.get_request_result(receipt["request_id"])
                if result is None or (run["status"] == "CLOSED" and result.get("request_status") == "waiting_input"):
                    result = await self._recover(self.store.get_run(receipt["run_id"]), "CRASH_RECOVERY")
                return self._emit_result(emit, result)
            run = self.store.get_run(receipt["run_id"], principal)
            if not actual or actual.id != receipt["framework_interrupt_id"]:
                result = await self._recover(run, "CRASH_RECOVERY")
                return self._emit_result(emit, result)
            graph_input = Command(resume={actual.id: receipt["receipt_id"]})
        else:
            run = self.store.start_run(session_id, principal, request.client_message_id, content)
            if run["replay"]:
                result = self.store.get_request_result(run["request_id"])
                if run["status"] == "CLOSED" and (result is None or result.get("request_status") == "waiting_input"):
                    result = await self._recover(run, run.get("termination_reason") or "CRASH_RECOVERY")
                elif result is None:
                    result = self._public(run, run["verified_snapshot"], request_status="terminated", reason=run.get("termination_reason"))
                return self._emit_result(emit, result)
            graph_input = initial_state(run, principal, request)
        budget = BudgetScope(self.store, run["run_id"], run["active_invocation_id"], self.clock() + self.request_seconds)
        def private(event, data):
            self.trace_emit(event, {"task_id": run["task_id"], "request_id": run["request_id"], **data})
        def model_event(event):
            budget.emit(event)
            private("model_usage", event)
        provider = self.model.with_emit(model_event) if hasattr(self.model, "with_emit") else self.model
        ctx = RuntimeContext(principal, run, self.store, provider, self.sql, self.tools, self.retriever, budget,
                             budget.deadline, self.business_date, request, emit, self.trace,
                             trace_emit=private, receipt_ref=receipt["receipt_id"] if receipt else None, last_verified=run["verified_snapshot"])
        self.clock()
        try:
            # Reserve the invocation's worst-case active time; waiting never consumes time.
            # Unknown interruption retains the reservation, so restart cannot create time.
            self.store.reserve_budget(run["run_id"], run["active_invocation_id"], new_id(), active_ms=int(self.request_seconds * 1000))
            async with asyncio.timeout(self.request_seconds):
                state = await self.graph.ainvoke(graph_input, self._config(run), context=ctx, durability="sync")
                snapshot = await self.graph.aget_state(self._config(run))
                actual = self._interrupt(snapshot)
                if actual:
                    intent_id = state["pending_interrupt_ref"]
                    binding = self.store.publish_interrupt(intent_id, run["active_invocation_id"],
                                                           snapshot.config["configurable"]["checkpoint_id"], actual.id)
                    intent = self.store.get_intent(intent_id)
                    run = self.store.get_run(run["run_id"])
                    state["task_status"] = "WAITING_CONFIRM" if intent["kind"] == "CONFIRM" else "WAITING_INPUT"
                    public = {"interrupt_id": intent_id, "kind": intent["kind"], "expected_revision": run["state_revision"], "expires_at": intent["expires_at"]}
                    public.update({k: v for k, v in intent["payload"].items() if k != "user_id"})
                    state["answer"] = (f"请确认是否为订单 {intent['payload']['order_id']} 创建工单：{intent['payload']['reason']}。尚未提交。" if intent["kind"] == "CONFIRM" else intent["payload"]["question"])
                    completed = [g for g in state["goals"] if g["status"] in TERMINAL_GOALS]
                    if completed:
                        facts = render_answer({**state, "goals": completed})
                        state["answer"] = facts + "\n\n" + state["answer"]
                    result = self._public(run, state, request_status="waiting_input", interrupt_value=public)
                else:
                    run = self.store.get_run(run["run_id"])
                    self.store.close_run(run["run_id"], run["active_invocation_id"], reason=state["task_status"], verified_snapshot=ctx.last_verified)
                    result = self._public(run, state)
                result = self.store.save_request_result(run["request_id"], result)
        except asyncio.CancelledError:
            result = await self._recover(run, "CLIENT_DISCONNECTED", ctx)
            # Caller may be disconnected; no terminal-delivery claim and no second done.
            raise
        except (TimeoutError, GraphRecursionError, RuntimeConflict, sqlite3.Error, OSError) as error:
            reason = "BUDGET_EXHAUSTED" if isinstance(error, (TimeoutError, GraphRecursionError)) or "BUDGET" in str(error) else "TASK_EXPIRED" if str(error) == "TASK_EXPIRED" else "DENIED" if str(error) == "DENIED" else "PERSISTENCE_FAILURE"
            result = await self._recover(run, reason, ctx)
        except Exception:
            # Unexpected framework/provider integration failures terminate conservatively.
            result = await self._recover(run, "PERSISTENCE_FAILURE", ctx)
        interrupt_tool(ctx, result.get("reason") or "RESULT_UNVERIFIED")
        return self._emit_result(emit, result)

    def _waiting_result(self, run, snapshot):
        """Rebuild a published waiting delivery without consuming or replaying the graph."""
        state = deepcopy(snapshot)
        intent = self.store.get_intent(run["pending_intent_id"])
        state["task_status"] = "WAITING_CONFIRM" if intent["kind"] == "CONFIRM" else "WAITING_INPUT"
        public = {"interrupt_id": intent["intent_id"], "kind": intent["kind"],
                  "expected_revision": run["state_revision"], "expires_at": intent["expires_at"]}
        public.update({key: value for key, value in intent["payload"].items() if key != "user_id"})
        state["answer"] = (f"请确认是否为订单 {intent['payload']['order_id']} 创建工单：{intent['payload']['reason']}。尚未提交。"
                           if intent["kind"] == "CONFIRM" else intent["payload"]["question"])
        completed = [goal for goal in state.get("goals", []) if goal["status"] in TERMINAL_GOALS]
        if completed:
            state["answer"] = render_answer({**state, "goals": completed}) + "\n\n" + state["answer"]
        return self._public(run, state, request_status="waiting_input", interrupt_value=public)

    async def _local_cleanup(self, call, deadline):
        """Bound the wait, tracking the real worker separately from its asyncio waiter."""
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError
        self._cleanup_futures = [future for future in self._cleanup_futures if not future.done()]
        future = self._cleanup_pool.submit(call)
        self._cleanup_futures.append(future)
        wrapped = asyncio.wrap_future(future)
        # Consume late errors without discarding the underlying Future's stopped proof.
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.wait_for(asyncio.shield(wrapped), remaining)

    def _recovery_records(self, run_id, reason):
        run = self.store.recover_run(run_id, executor_stopped=True, reason=reason)
        return run, self._operation_records(run_id)

    def _operation_records(self, run_id):
        records = []
        for operation in self.store.list_operations(run_id):
            receipt = self.store.get_receipt(operation["receipt_ref"])
            intent = self.store.get_intent(receipt["intent_id"])
            records.append((operation, intent))
        return records

    async def _recover(self, run, reason, ctx=None):
        cleanup_deadline = self.clock() + 2
        if ctx:
            ctx.gate_input = ctx.grant = None
        # Prefer the latest verifier snapshot over the ingress-era copy after a
        # long invocation. Checkpoint values are never a recovery authority.
        state = deepcopy((ctx.last_verified if ctx else None) or run.get("verified_snapshot") or {})
        state.setdefault("task_id", run["task_id"])
        state.setdefault("goals", [])
        state.setdefault("observations", [])
        state.setdefault("unresolved_operation_ids", [])
        persisted = True
        try:
            # All graph/pre-send coroutines have stopped before reaching this boundary.
            run, operations = await self._local_cleanup(lambda: self._recovery_records(run["run_id"], reason), cleanup_deadline)
            if hasattr(self.tools, "drain_sends"):
                await self.tools.drain_sends(run["run_id"], timeout=max(0, cleanup_deadline - self.clock() - 0.05))
                if self.clock() < cleanup_deadline:
                    operations = await self._local_cleanup(lambda: self._operation_records(run["run_id"]), cleanup_deadline)
            for operation, intent in operations:
                for goal in state["goals"]:
                    if goal["goal_id"] not in intent["goal_ids"]:
                        continue
                    if operation["state"] == "SUCCESS":
                        data = operation["result"]["data"]
                        obs = {"id": "recovered-" + operation["operation_id"], "task_id": run["task_id"], "scope_epoch": operation["scope_epoch"],
                               "goal_ids": intent["goal_ids"], "subject": operation["order_id"], "action_type": "execute_confirmed_ticket", "data": data, "validation": "VERIFIED"}
                        state["observations"].append(obs)
                        goal.update(status="FULFILLED", unmet_reason=None)
                        goal["supporting_observation_ids"].append(obs["id"])
                    elif operation["state"] in {"UNKNOWN", "IN_FLIGHT"}:
                        goal.update(status="BLOCKED", unmet_reason="UNKNOWN")
                        state["unresolved_operation_ids"].append(operation["operation_id"])
                    elif operation["state"] == "FAILED":
                        goal.update(status="BLOCKED", unmet_reason="NOT_SENT" if operation["dispatch_stage"] == "RESERVED" else "WRITE_FAILED")
        except (sqlite3.Error, OSError, RuntimeConflict, TimeoutError):
            persisted = False
        for goal in state["goals"]:
            if goal["status"] not in TERMINAL_GOALS:
                unmet = "BUDGET_EXHAUSTED" if reason == "BUDGET_EXHAUSTED" else "NOT_SENT" if goal["kind"] == "ticket" and persisted else "TASK_STOPPED"
                goal.update(status="BLOCKED", unmet_reason=unmet)
        state.update(termination_reason=reason, task_status=aggregate(state["goals"], reason))
        state["answer"] = render_answer(state)
        if not persisted:
            state["answer"] += "\n持久化未确认，操作结果待核对；不能声称终态已保存。"
        result = self._public(run, state, request_status="terminated", reason=reason, persisted=persisted)
        if persisted:
            try:
                previous = await self._local_cleanup(lambda: self.store.save_request_result(run["request_id"], result), cleanup_deadline)
                # An immutable old waiting delivery does not override a CLOSED run.
                if previous.get("request_status") != "waiting_input":
                    result = previous
            except (sqlite3.Error, OSError, TimeoutError):
                result["persisted"] = False
        return result
