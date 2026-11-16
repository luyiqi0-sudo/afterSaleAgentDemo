"""ADR-001 section 6 limits; reservations survive checkpoint rollback."""
import time
import uuid

from agent.runtime_types import RuntimeConflict


TASK_LIMITS = {"messages": 6, "active_ms": 120000, "model_calls": 12,
               "input_tokens": 32768, "output_tokens": 8192,
               "business_nodes": 64, "logical_actions": 8,
               "http_attempts": 12, "write_attempts": 1}
REQUEST_LIMITS = {"active_ms": 30000, "model_calls": 6, "business_nodes": 32}
TASK_TTL_SECONDS = 86400
CONFIRM_TTL_SECONDS = 900
RECURSION_LIMIT = 48
SOFT_FINALIZATION_SECONDS = 3
OUTER_FINALIZATION_SECONDS = 2


class BudgetScope:
    """One invocation's provider callback, with durable event IDs and usage settlement."""

    def __init__(self, store, run_id, invocation_id, deadline=None):
        self.store, self.run_id, self.invocation_id = store, run_id, invocation_id
        self.deadline = deadline if deadline is not None else time.monotonic() + 30
        self.pending = {}

    def __call__(self, operation, input_upper_bound, output_limit):
        remaining = self.deadline - time.monotonic() - SOFT_FINALIZATION_SECONDS
        if remaining <= 0:
            raise RuntimeConflict("BUDGET_EXHAUSTED")
        event_id = str(uuid.uuid4())
        self.store.reserve_budget(self.run_id, self.invocation_id, event_id,
                                  model_calls=1, input_tokens=input_upper_bound,
                                  output_tokens=output_limit)
        self.pending.setdefault(operation, []).append(event_id)
        remaining = self.deadline - time.monotonic() - SOFT_FINALIZATION_SECONDS
        if remaining <= 0:
            raise RuntimeConflict("BUDGET_EXHAUSTED")
        return remaining

    def settle(self, operation, usage):
        events = self.pending.get(operation, [])
        if not events:
            return
        event = events.pop(0)
        if usage is not None:
            self.store.settle_budget(event, usage)

    def emit(self, event):
        self.settle(event.get("operation"), event.get("usage"))
