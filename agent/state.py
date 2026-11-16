"""Checkpoint projections and request-local authority are deliberately separate."""
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypedDict

from agent.contracts import Principal


OUTCOMES = frozenset("VERIFIED_OK NO_DATA NEED_INPUT UNSUPPORTED DENIED SQL_EXHAUSTED READ_EXHAUSTED RETRIEVAL_UNAVAILABLE RESPONSE_MISMATCH WRITE_FAILED WRITE_UNKNOWN ORDER_BUSY NOT_SENT CANCELLED EXPIRED MODEL_UNAVAILABLE INVALID_PROPOSAL NO_PROGRESS BUDGET_EXHAUSTED PERSISTENCE_FAILURE CRASH_RECOVERY".split())
TERMINAL_GOALS = frozenset({"FULFILLED", "BLOCKED", "DENIED", "CANCELLED"})


class AgentState(TypedDict, total=False):
    schema_version: int
    run_id: str
    task_id: str
    thread_id: str
    conversation_id: str
    run_generation: int
    state_revision: int
    goal_revision: int
    scope_epoch: int
    actor_id_assertion: str
    tenant_id_assertion: str
    question: str
    user_inputs: list[dict]
    goals: list[dict]
    goal_history: list[dict]
    candidate_objects: list[dict]
    verified_objects: list[dict]
    focus_order_id: str | None
    observations: list[dict]
    closed_capabilities: list[dict]
    proposed_action: dict | None
    gate_input_kind: str | None
    internal_action_ref: dict | None
    prepare_request_ref: str | None
    pending_interrupt_ref: str | None
    pending_ticket_ref: str | None
    confirmed_operation_id: str | None
    confirmation_receipt_ref: str | None
    applied_receipt_id: str | None
    pending_addition_intents: list[dict]
    unresolved_operation_ids: list[str]
    task_status: str
    termination_reason: str | None
    outcome: dict | None
    route: str
    answer: str
    clarification: str | None
    task_budget: dict
    resume_mode: str | None
    replaced_subject: str | None


@dataclass
class RuntimeContext:
    """Passed via LangGraph context=, never serialized into AgentState."""
    principal: Principal
    run: dict
    store: Any
    model: Any
    sql: Any
    tools: Any
    retriever: Any
    budget: Any
    deadline: float
    business_date: date
    request: Any
    emit: Any
    trace: list[str]
    trace_emit: Any = None
    receipt_ref: str | None = None
    gate_input: dict | None = None
    grant: Any = None
    action: dict | None = None
    last_verified: dict = field(default_factory=dict)
    final_sent: bool = False
    pending_tool: dict | None = None


def aggregate(goals, reason=None):
    if reason == "DENIED":
        return "REJECTED"
    if reason == "TASK_EXPIRED":
        return "EXPIRED"
    required = [g for g in goals if g.get("required", True)]
    if required and all(g["status"] == "FULFILLED" for g in required):
        return "COMPLETED"
    if any(g["status"] == "FULFILLED" for g in required):
        return "PARTIAL"
    if required and all(g["status"] == "CANCELLED" for g in required):
        return "CANCELLED"
    return "FAILED"


def clear_active(state):
    state.update(proposed_action=None, gate_input_kind=None, internal_action_ref=None,
                 confirmed_operation_id=None, confirmation_receipt_ref=None,
                 prepare_request_ref=None, pending_interrupt_ref=None, pending_ticket_ref=None)


def initial_state(run, principal, request):
    return AgentState(schema_version=1, run_id=run["run_id"], task_id=run["task_id"],
                      thread_id=run["thread_id"], conversation_id=run["session_id"],
                      run_generation=run["generation"], state_revision=run["state_revision"],
                      goal_revision=run["goal_revision"], scope_epoch=run["scope_epoch"],
                      actor_id_assertion=principal.user_id, tenant_id_assertion=principal.tenant,
                      question=request.message, user_inputs=[{"message_id": request.client_message_id, "content": request.message}],
                      goals=[], goal_history=[], observations=[], candidate_objects=[], verified_objects=[],
                      closed_capabilities=[], pending_addition_intents=[], unresolved_operation_ids=[],
                      task_status="ACTIVE", termination_reason=None, focus_order_id=None)
