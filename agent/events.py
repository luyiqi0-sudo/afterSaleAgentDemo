"""Public event protocol and private evidence sink."""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


PUBLIC_FIELDS = {
    "step": ("action", "goal_ids", "summary"),
    "tool_start": ("tool", "summary"),
    "tool_end": ("tool", "status", "attempts", "summary"),
    "tool_error": ("tool", "status", "attempts", "summary"),
    "answer": ("text",),
    "interrupt": ("interrupt_id", "kind", "expected_revision", "expires_at", "order_id", "reason", "question", "field", "fields"),
    "done": ("session_id", "task_id", "revision", "status", "request_status", "answer", "goals", "interrupt",
             "reason", "persisted", "operation_refs"),
    "error": ("code",),
}
TRACE_FIELDS = {
    "node": ("node",),
    "model_usage": ("operation", "status", "usage", "http_status", "model", "latency_ms",
                    "request_messages", "structured_candidate", "timeout_ms", "timeout_source"),
    "sql_candidate": ("sql", "previous_error"),
    "sql_execution": ("outcome", "result", "action_ref", "scope_epoch"),
    "sql_error": ("outcome", "error_class"),
    "policy_retrieval": ("status", "evidences", "notes", "missing", "error_code", "conflicts", "quarantined", "query_context"),
    "http_attempt": ("tool", "order_id"),
    "tool_result": ("tool", "result"),
    "tool_retry": ("tool", "attempt", "delay_seconds"),
    "read_reuse": ("tool", "order_id", "goal_ids", "source_observation_id"),
    "read_plan": ("origin", "action_type", "order_id", "goal_ids"),
    "clarification_supply": ("goal_id", "field", "order_id", "span", "source_message_id"),
    "operation": ("operation", "outcome"),
    "http_send": ("method", "tool", "order_id", "reason"),
    "http_response": ("method", "route", "order_id", "http_status", "latency_ms", "ticket_id"),
    "http_timeout": ("tool", "order_id", "attempt"),
    "delivery": ("request_status", "status", "persisted", "reason", "latency_ms"),
}
_DROP = {
    "authorization", "apikey", "password", "headers", "thinking", "thinkingcontent", "reasoning",
    "reasoningcontent", "chainofthought", "analysis", "hiddenreasoning", "rawresponse", "rawrequest",
    "prompt", "userid", "actor", "tenant", "servicetoken", "dsn", "payloadhash", "owner", "digest",
    "fingerprint",
}
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_SQL_LITERALS = re.compile(r"'(?:''|[^'])*'|--[^\r\n]*|/\*.*?\*/", flags=re.DOTALL)
_CREDENTIAL_TEXT = re.compile(r"(?i)\b(?:bearer\s+|(?:api[_-]?key|password|token)\s*[=:]\s*)[\w.~+/=-]+")


def select_fields(data, fields):
    return {key: data[key] for key in fields if key in data}


def public_frame(event: str, data: dict, *, request_id: str, conversation_id: str | None) -> bytes:
    filtered = select_fields(data, PUBLIC_FIELDS[event])
    if event == "done":
        filtered = {"session_id": conversation_id, "task_id": None, "revision": None, "status": None,
                    "request_status": "terminated", "reason": None, "answer": "", "goals": [],
                    "interrupt": None, "operation_refs": [], "persisted": False, **filtered}
        filtered["task_status"] = filtered.get("status")
        if isinstance(filtered.get("interrupt"), dict):
            filtered["interrupt"] = select_fields(filtered["interrupt"], PUBLIC_FIELDS["interrupt"])
        if isinstance(filtered.get("goals"), list):
            filtered["goals"] = [select_fields(goal, ("goal_id", "description", "subject", "status", "unmet_reason"))
                                 for goal in filtered["goals"]]
    name = {"step": "stage", "answer": "answer_delta"}.get(event, event)
    envelope = {"id": uuid4().hex, "request_id": request_id, "conversation_id": conversation_id,
                "timestamp": datetime.now(timezone.utc).isoformat(), "type": name, "data": filtered}
    return f"event: {name}\ndata: {json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


class PrivateTrace:
    """Newline JSON in the private runtime directory; never an API response."""
    def __init__(self, path: Path, secrets=()):
        self.path = path
        self.secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))
        self.write_errors = 0

    def _redact(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return _CREDENTIAL_TEXT.sub("[REDACTED]", value)

    def _clean(self, value, key=""):
        if isinstance(value, dict):
            return {self._redact(k): self._clean(v, k) for k, v in value.items()
                    if isinstance(k, str) and _NON_ALNUM.sub("", k.lower()) not in _DROP}
        if isinstance(value, list):
            return [self._clean(item, key) for item in value]
        if isinstance(value, str):
            if key == "content" and value.lstrip().startswith(("{", "[")):
                try:
                    return json.dumps(self._clean(json.loads(value)), ensure_ascii=False)
                except (ValueError, RecursionError):
                    return "[UNPARSEABLE_JSON]"
            value = self._redact(value)
            if key in {"sql", "statement", "executed_sql"}:
                value = _SQL_LITERALS.sub("<redacted>", value)
            return value
        return value

    def __call__(self, event, data):
        if event not in TRACE_FIELDS:
            return
        try:
            fields = TRACE_FIELDS[event] + ("task_id", "runtime_request_id", "correlation_id")
            safe = select_fields(data, fields)
            if "request_messages" in safe:
                safe["request_messages"] = [select_fields(msg, ("role", "content"))
                                            for msg in safe["request_messages"]
                                            if isinstance(msg, dict) and msg.get("role") in {"system", "user"}
                                            and isinstance(msg.get("content"), str)]
            row = {"event": event, "timestamp": time.time(), **self._clean(safe)}
            path = self.path
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        except (OSError, ValueError, TypeError, RecursionError):
            self.write_errors += 1
