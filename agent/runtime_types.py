"""Trusted runtime values. These objects are never accepted from HTTP/checkpoints."""
from dataclasses import dataclass

from agent.contracts import ToolResult


class RuntimeConflict(RuntimeError):
    """Fail-closed protocol, permission, fence or budget rejection."""


@dataclass(frozen=True)
class GateGrant:
    nonce: str
    run_id: str
    invocation_id: str
    receipt_id: str
    operation_id: str
    deadline: float


@dataclass(frozen=True)
class SendToken:
    nonce: str
    operation_id: str
    grant: GateGrant


@dataclass(frozen=True)
class ClaimResult:
    token: SendToken | None
    result: ToolResult
