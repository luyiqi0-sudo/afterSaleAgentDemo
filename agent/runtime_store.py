"""Authoritative control records and once-only send ledger in one SQLite DB.

Checkpoints remain separate. All mutations use short BEGIN IMMEDIATE transactions;
there is never network I/O inside a transaction. Tokens are local object identities,
not serialized capabilities. Only the authenticated application controller uses this
store; HTTP input must never be passed directly to prepare/issue_grant.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from agent.budgets import CONFIRM_TTL_SECONDS, REQUEST_LIMITS, TASK_LIMITS, TASK_TTL_SECONDS
from agent.contracts import ToolResult
from agent.runtime_types import ClaimResult, GateGrant, RuntimeConflict, SendToken


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def new_id():
    return uuid.uuid4().hex


def identity(principal):
    return getattr(principal, "tenant", "demo"), principal.user_id


class _DeadlineConnection:
    """One absolute resource-wait deadline, refreshed at every SQL/commit boundary."""

    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline

    def _refresh(self):
        milliseconds = max(0, int((self.deadline - time.monotonic()) * 1000))
        self.connection.execute(f"PRAGMA busy_timeout={milliseconds}")

    def execute(self, *args, **kwargs):
        self._refresh()
        return self.connection.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._refresh()
        return self.connection.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self._refresh()
        return self.connection.executescript(*args, **kwargs)

    def commit(self):
        self._refresh()
        return self.connection.commit()

    def __getattr__(self, name):
        return getattr(self.connection, name)


class RuntimeStore:
    def __init__(self, path: Path, *, clock=time.time, busy_timeout_seconds=10.0):
        if not 0 < busy_timeout_seconds <= 10:
            raise ValueError("busy_timeout_seconds must be in (0, 10]")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        self._grants, self._tokens = {}, {}
        self._mutex = threading.RLock()
        self._worker_file = None
        with closing(self._connect()) as con, con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS rt_records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS rt_messages (
                    session_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL, result_kind TEXT NOT NULL, result_id TEXT NOT NULL,
                    PRIMARY KEY(session_id,message_id));
                CREATE TABLE IF NOT EXISTS rt_operations (
                    operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    tenant TEXT NOT NULL, actor TEXT NOT NULL, order_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL CHECK(operation_type='CREATE_TICKET'),
                    payload_hash TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rt_reservations (
                    tenant TEXT NOT NULL, actor TEXT NOT NULL, order_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL CHECK(operation_type='CREATE_TICKET'),
                    operation_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(tenant,actor,order_id,operation_type));
            """)
            self._migrate_legacy(con)

    def _connect(self, *, timeout=None):
        con = sqlite3.connect(self.path, timeout=self.busy_timeout_seconds if timeout is None else timeout)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def _commit(self, con):
        con.commit()

    @contextmanager
    def _transaction(self, *, wait_seconds=None):
        wait_seconds = self.busy_timeout_seconds if wait_seconds is None else wait_seconds
        deadline = time.monotonic() + wait_seconds
        acquired = self._mutex.acquire(timeout=max(0, wait_seconds))
        if not acquired:
            raise sqlite3.OperationalError("runtime transaction wait exhausted")
        con = None
        try:
            remaining = max(0, deadline - time.monotonic())
            con = _DeadlineConnection(self._connect(timeout=remaining), deadline)
            con.execute("BEGIN IMMEDIATE")
            yield con
            self._commit(con)
        except BaseException:
            if con is not None:
                con.rollback()
            raise
        finally:
            if con is not None:
                con.close()
            self._mutex.release()

    @staticmethod
    def _get(con, kind, key):
        row = con.execute("SELECT data FROM rt_records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(con, kind, key, value):
        con.execute("INSERT INTO rt_records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (kind, key, canonical(value)))

    def _migrate_legacy(self, con):
        # Old schema omitted identity/payload. Preserve each result and block new
        # writes globally if uncertain legacy identity cannot be reconstructed.
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'").fetchone()
        if not exists:
            return
        for row in con.execute("SELECT id,payload_hash,result FROM operations"):
            if self._get(con, "legacy_operation", row["id"]):
                continue
            result = json.loads(row["result"])
            self._put(con, "legacy_operation", row["id"], {"payload_hash": row["payload_hash"], "result": result})
            if result.get("status") == "UNKNOWN":
                self._put(con, "readiness", "legacy_unknown", {"blocked": True, "reason": "LEGACY_IDENTITY_UNAVAILABLE"})

    def acquire_worker(self):
        """Hold an OS advisory lock for the process lifetime. No timeout takeover."""
        if self._worker_file is not None:
            return
        handle = open(str(self.path) + ".worker.lock", "a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeConflict("WORKER_ALREADY_RUNNING") from exc
        self._worker_file = handle

    def close(self):
        if self._worker_file:
            self._worker_file.close()
            self._worker_file = None
        self._grants.clear()
        self._tokens.clear()

    def _owner(self, session, principal):
        if not session or (session["tenant"], session["actor"]) != identity(principal):
            raise RuntimeConflict("SESSION_UNAVAILABLE")

    def _live(self, con, run_id, invocation_id):
        run = self._get(con, "run", run_id)
        if not run or run["status"] != "LIVE" or run["active_invocation_id"] != invocation_id:
            raise RuntimeConflict("STALE_FENCE")
        session = self._get(con, "session", run["session_id"])
        if (session["run_id"] != run_id or session["generation"] != run["generation"] or
            session["state_revision"] != run["state_revision"]):
            raise RuntimeConflict("STALE_FENCE")
        if self.clock() >= run["expires_at"]:
            raise RuntimeConflict("TASK_EXPIRED")
        if run.get("budget_blocked"):
            raise RuntimeConflict("BUDGET_EXHAUSTED")
        return run

    def start_run(self, session_id, principal, message_id, content, *, invocation_id=None, request_id=None):
        with self._transaction() as con:
            session = self._get(con, "session", session_id)
            if session:
                self._owner(session, principal)
            replay = self._message(con, session_id, message_id, content)
            if replay:
                result = self._get(con, replay["result_kind"], replay["result_id"])
                return dict(result, replay=True)
            if session and self._get(con, "run", session["run_id"])["status"] != "CLOSED":
                raise RuntimeConflict("RUN_BUSY")
            tenant, actor = identity(principal)
            generation = session["generation"] + 1 if session else 1
            run_id = new_id()
            run = {"run_id": run_id, "task_id": new_id(), "thread_id": new_id(), "session_id": session_id,
                   "tenant": tenant, "actor": actor, "generation": generation, "run_generation": generation,
                   "state_revision": (session["state_revision"] + 1) if session else 1,
                   "goal_revision": 1, "scope_epoch": 1, "status": "LIVE",
                   "active_invocation_id": invocation_id or new_id(), "request_id": request_id or new_id(),
                   "started_at": self.clock(), "expires_at": self.clock() + TASK_TTL_SECONDS,
                   "verified_snapshot": {}, "termination_reason": None}
            self._put(con, "session", session_id, {k: run[k] for k in ("run_id", "tenant", "actor", "generation", "state_revision")})
            self._put(con, "run", run_id, run)
            self._put(con, "budget", run_id, {k: int(k == "messages") for k in TASK_LIMITS})
            self._put(con, "invocation_budget", run["active_invocation_id"], {k: 0 for k in TASK_LIMITS})
            self._record_message(con, session_id, message_id, content, "run", run_id)
        return dict(run, replay=False)

    def _message(self, con, session, message, content):
        row = con.execute("SELECT * FROM rt_messages WHERE session_id=? AND message_id=?", (session, message)).fetchone()
        if row and row["content_hash"] != digest(content):
            raise RuntimeConflict("MESSAGE_CONFLICT")
        return row

    def lookup_message(self, session_id, principal, message_id, content):
        """Authenticated read-only ingress preflight; the later ingress owns CAS."""
        with closing(self._connect()) as con:
            con.execute("BEGIN")
            self._owner(self._get(con, "session", session_id), principal)
            row = self._message(con, session_id, message_id, content)
            if row is None:
                return None
            result = self._get(con, row["result_kind"], row["result_id"])
            if result is None:
                raise RuntimeConflict("MESSAGE_RESULT_UNAVAILABLE")
            return dict(result, replay=True)

    @staticmethod
    def _record_message(con, session, message, content, kind, ref):
        con.execute("INSERT INTO rt_messages VALUES (?,?,?,?,?)", (session, message, digest(content), kind, ref))

    def get_run(self, run_id, principal=None):
        with closing(self._connect()) as con, con:
            run = self._get(con, "run", run_id)
            if principal:
                self._owner(self._get(con, "session", run["session_id"]) if run else None, principal)
            return run

    def get_session(self, session_id, principal):
        with closing(self._connect()) as con, con:
            session = self._get(con, "session", session_id)
            self._owner(session, principal)
            return session

    def list_runs(self, status=None):
        """Server lifecycle use only; never expose a cross-user listing endpoint."""
        with closing(self._connect()) as con, con:
            runs = [json.loads(row[0]) for row in con.execute("SELECT data FROM rt_records WHERE kind='run'")]
        return [run for run in runs if status is None or run["status"] == status]

    def get_binding(self, intent_id):
        with closing(self._connect()) as con, con:
            return self._get(con, "binding", intent_id)

    def save_verified_snapshot(self, run_id, invocation_id, expected_revision, snapshot):
        """Only verifier output, never model/state input, enters this fallback snapshot."""
        with self._transaction() as con:
            run = self._live(con, run_id, invocation_id)
            if run["state_revision"] != expected_revision:
                raise RuntimeConflict("STALE_REVISION")
            run["verified_snapshot"] = snapshot
            self._put(con, "run", run_id, run)
        return run

    def advance_goals(self, run_id, invocation_id, event_id, *, scope_changed=False):
        """Trusted goal-commit dedup, used for SUPPLY/additions (REPLACE already did it)."""
        with self._transaction() as con:
            run = self._live(con, run_id, invocation_id)
            key = run_id + ":" + event_id
            if not self._get(con, "goal_event", key):
                run["goal_revision"] += 1
                run["scope_epoch"] += int(scope_changed)
                self._put(con, "goal_event", key, {"scope_changed": scope_changed})
                self._put(con, "run", run_id, run)
        self._revoke(run_id)
        return run

    def get_intent(self, intent_id):
        with closing(self._connect()) as con, con:
            return self._get(con, "intent", intent_id)

    def get_receipt(self, receipt_id):
        with closing(self._connect()) as con, con:
            return self._get(con, "receipt", receipt_id)

    def prepare(self, run_id, invocation_id, source_id, kind, payload, goal_ids=None, *, ttl=CONFIRM_TTL_SECONDS):
        """Convenience for component fixtures; graph uses the two explicit phases."""
        preparation = self.save_preparation(run_id, invocation_id, source_id, kind, payload, goal_ids)
        return self.freeze_preparation(preparation["prepare_request_id"], invocation_id, ttl=ttl)

    def get_preparation(self, prepare_request_id):
        with closing(self._connect()) as con, con:
            return self._get(con, "preparation", prepare_request_id)

    def save_preparation(self, run_id, invocation_id, source_id, kind, payload, goal_ids=None, *, object_refs=None, sources=None):
        """Gate commits its verified candidate before clearing the model proposal.

        There is no operation ID, intent or confirmation TTL at this boundary.
        The prepare node freezes this record only after its checkpointed reference
        is available, retaining separate authoritative node boundaries.
        """
        if kind not in ("CONFIRM", "CLARIFY"):
            raise RuntimeConflict("INVALID_PREPARATION")
        with self._transaction() as con:
            run = self._live(con, run_id, invocation_id)
            key = digest([run_id, source_id, kind])
            bindings = {"run_id": run_id, "task_id": run["task_id"], "generation": run["generation"],
                        "tenant": run["tenant"], "actor": run["actor"], "state_revision": run["state_revision"],
                        "scope_epoch": run["scope_epoch"], "goal_revision": run["goal_revision"],
                        "kind": kind, "payload": payload, "goal_ids": goal_ids or [],
                        "object_refs": object_refs or [], "sources": sources or []}
            content_hash = digest(bindings)
            previous = self._get(con, "preparation", key)
            if previous:
                if previous["content_hash"] != content_hash:
                    raise RuntimeConflict("PREPARATION_CONFLICT")
                return previous
            if kind == "CONFIRM" and (payload.get("user_id") != run["actor"] or not payload.get("order_id") or not payload.get("reason")):
                raise RuntimeConflict("INVALID_PAYLOAD")
            preparation = dict(bindings, prepare_request_id=key, source_id=source_id,
                               content_hash=content_hash, created_at=self.clock())
            self._put(con, "preparation", key, preparation)
        return preparation

    def freeze_preparation(self, prepare_request_id, invocation_id, *, ttl=CONFIRM_TTL_SECONDS):
        if not 0 < ttl <= CONFIRM_TTL_SECONDS:
            raise RuntimeConflict("INVALID_PREPARATION")
        with self._transaction() as con:
            preparation = self._get(con, "preparation", prepare_request_id)
            if not preparation:
                raise RuntimeConflict("PREPARATION_UNAVAILABLE")
            run = self._live(con, preparation["run_id"], invocation_id)
            for key in ("task_id", "generation", "tenant", "actor", "state_revision", "scope_epoch", "goal_revision"):
                if preparation[key] != run[key]:
                    raise RuntimeConflict("PREPARATION_CONFLICT")
            frozen_ref = self._get(con, "preparation_frozen", prepare_request_id)
            if frozen_ref:
                return self._get(con, "intent", frozen_ref["intent_id"])
            payload = preparation["payload"]
            intent = {"intent_id": new_id(), "prepare_request_id": prepare_request_id, "kind": preparation["kind"], "run_id": run["run_id"],
                      "task_id": run["task_id"], "generation": run["generation"], "tenant": run["tenant"], "actor": run["actor"],
                      "scope_epoch": run["scope_epoch"], "goal_revision": run["goal_revision"], "state_revision": run["state_revision"],
                      "operation_id": new_id() if preparation["kind"] == "CONFIRM" else None, "goal_ids": preparation["goal_ids"],
                      "object_refs": preparation["object_refs"], "sources": preparation["sources"],
                      "payload": payload, "payload_hash": digest(payload), "issued_at": self.clock(),
                      "expires_at": min(self.clock() + ttl, run["expires_at"]), "status": "UNPUBLISHED"}
            intent["business_interrupt_id"] = intent["intent_id"]
            self._put(con, "preparation_frozen", prepare_request_id, {"intent_id": intent["intent_id"]})
            self._put(con, "intent", intent["intent_id"], intent)
        return intent

    def publish_interrupt(self, intent_id, invocation_id, checkpoint_id, framework_interrupt_id, checkpoint_ns=""):
        if not checkpoint_id or not framework_interrupt_id:
            raise RuntimeConflict("CHECKPOINT_REQUIRED")
        with self._transaction() as con:
            intent = self._get(con, "intent", intent_id)
            if not intent or intent["status"] != "UNPUBLISHED":
                raise RuntimeConflict("STALE_INTERRUPT")
            run = self._live(con, intent["run_id"], invocation_id)
            self._check_frozen_versions(intent, run)
            binding = {"intent_id": intent_id, "framework_interrupt_id": framework_interrupt_id,
                       "checkpoint_id": checkpoint_id, "checkpoint_ns": checkpoint_ns,
                       "thread_id": run["thread_id"], **{key: run[key] for key in
                       ("run_id", "task_id", "generation", "tenant", "actor", "state_revision", "goal_revision", "scope_epoch")}}
            intent["status"] = "PENDING"
            run.update(status="WAITING", active_invocation_id=None, pending_intent_id=intent_id)
            self._put(con, "binding", intent_id, binding)
            self._put(con, "intent", intent_id, intent)
            self._put(con, "run", run["run_id"], run)
        return dict(binding, expected_revision=run["state_revision"])

    @staticmethod
    def _check_frozen_versions(intent, run, binding=None, *, approved_revision=None):
        for key in ("run_id", "task_id", "generation", "tenant", "actor", "state_revision", "goal_revision", "scope_epoch"):
            expected = approved_revision if key == "state_revision" and approved_revision is not None else run[key]
            if intent.get(key) != expected or (binding is not None and binding.get(key) != expected):
                raise RuntimeConflict("STALE_INTERRUPT")
        if binding is not None and (binding.get("intent_id") != intent["intent_id"] or binding.get("thread_id") != run["thread_id"]):
            raise RuntimeConflict("STALE_INTERRUPT")

    def consume_resume(self, session_id, principal, intent_id, expected_revision, message_id, content, decision,
                       checkpoint_id, *, invocation_id=None, request_id=None, addition_intents=None):
        statuses = {"APPROVE": "CONSUMED", "SUPPLY": "CONSUMED", "REPLACE": "SUPERSEDED",
                    "AMBIGUOUS": "SUPERSEDED", "CANCEL": "CANCELLED", "EXPIRE": "EXPIRED"}
        if decision not in statuses:
            raise RuntimeConflict("INVALID_DECISION")
        with self._transaction() as con:
            session = self._get(con, "session", session_id)
            self._owner(session, principal)
            replay = self._message(con, session_id, message_id, content)
            if replay:
                return dict(self._get(con, replay["result_kind"], replay["result_id"]), replay=True)
            run = self._get(con, "run", session["run_id"])
            intent, binding = self._get(con, "intent", intent_id), self._get(con, "binding", intent_id)
            if (run["status"] != "WAITING" or run["state_revision"] != expected_revision or not intent or
                intent["run_id"] != run["run_id"] or intent["status"] != "PENDING" or
                run.get("pending_intent_id") != intent_id or not binding or binding["checkpoint_id"] != checkpoint_id):
                raise RuntimeConflict("STALE_INTERRUPT")
            if self.clock() >= run["expires_at"]:
                raise RuntimeConflict("TASK_EXPIRED")
            self._check_frozen_versions(intent, run, binding)
            budget = self._get(con, "budget", run["run_id"])
            if budget["messages"] >= TASK_LIMITS["messages"]:
                raise RuntimeConflict("BUDGET_EXHAUSTED")
            if self.clock() >= intent["expires_at"]:
                decision = "EXPIRE"
            elif addition_intents and decision == "APPROVE":
                decision = "AMBIGUOUS"
            if decision == "APPROVE" and intent["kind"] != "CONFIRM":
                raise RuntimeConflict("INVALID_DECISION")
            old_revision = run["state_revision"]
            run.update(status="LIVE", active_invocation_id=invocation_id or new_id(), request_id=request_id or new_id(),
                       state_revision=old_revision + 1, pending_intent_id=None)
            if decision == "REPLACE":
                run["scope_epoch"] += 1
                run["goal_revision"] += 1
            receipt = {"receipt_id": new_id(), "run_id": run["run_id"], "request_id": run["request_id"],
                       "invocation_id": run["active_invocation_id"], "client_message_id": message_id,
                       "message_hash": digest(content), "decision": decision, "intent_id": intent_id,
                       "business_interrupt_id": intent_id, **binding, "operation_id": intent["operation_id"],
                       "payload_hash": intent["payload_hash"], "tenant": run["tenant"], "actor": run["actor"],
                       "old_revision": old_revision, "new_revision": run["state_revision"],
                       "scope_epoch": run["scope_epoch"], "goal_revision": run["goal_revision"],
                       "addition_intents": addition_intents or [], "accepted_at": self.clock(),
                       "progress": "RECORDED", "settlement_reason": None}
            intent["status"] = statuses[decision]
            session["state_revision"] = run["state_revision"]
            budget["messages"] += 1
            self._put(con, "session", session_id, session)
            self._put(con, "run", run["run_id"], run)
            self._put(con, "intent", intent_id, intent)
            self._put(con, "receipt", receipt["receipt_id"], receipt)
            self._put(con, "budget", run["run_id"], budget)
            self._put(con, "invocation_budget", run["active_invocation_id"], {k: 0 for k in TASK_LIMITS})
            self._record_message(con, session_id, message_id, content, "receipt", receipt["receipt_id"])
        return dict(receipt, replay=False)

    def project_receipt(self, receipt_id, invocation_id):
        with self._transaction() as con:
            receipt = self._get(con, "receipt", receipt_id)
            self._live(con, receipt["run_id"], invocation_id)
            if receipt["invocation_id"] != invocation_id or receipt["progress"] == "SETTLED":
                raise RuntimeConflict("STALE_RECEIPT")
            if receipt["decision"] == "APPROVE":
                self._validate_receipt(con, receipt["run_id"], invocation_id, receipt_id, require_projected=False)
            receipt["progress"] = "PROJECTED"
            self._put(con, "receipt", receipt_id, receipt)
        return receipt

    def _validate_receipt(self, con, run_id, invocation_id, receipt_id, principal=None, *, require_projected=True):
        run = self._live(con, run_id, invocation_id)
        if principal and identity(principal) != (run["tenant"], run["actor"]):
            raise RuntimeConflict("DENIED")
        receipt = self._get(con, "receipt", receipt_id)
        if (not receipt or receipt["run_id"] != run_id or receipt["invocation_id"] != invocation_id or
            receipt["decision"] != "APPROVE" or receipt["progress"] == "SETTLED" or
            (require_projected and receipt["progress"] != "PROJECTED") or
            receipt["new_revision"] != run["state_revision"] or receipt["scope_epoch"] != run["scope_epoch"] or
            receipt["goal_revision"] != run["goal_revision"] or receipt["request_id"] != run["request_id"] or
            receipt["tenant"] != run["tenant"] or receipt["actor"] != run["actor"] or
            receipt["generation"] != run["generation"] or receipt["thread_id"] != run["thread_id"] or
            receipt["task_id"] != run["task_id"] or receipt["new_revision"] != receipt["old_revision"] + 1):
            raise RuntimeConflict("STALE_RECEIPT")
        intent = self._get(con, "intent", receipt["intent_id"])
        binding = self._get(con, "binding", receipt["intent_id"])
        if not intent or not binding:
            raise RuntimeConflict("STALE_RECEIPT")
        self._check_frozen_versions(intent, run, binding, approved_revision=receipt["old_revision"])
        if any(receipt.get(key) != binding.get(key) for key in ("checkpoint_id", "checkpoint_ns", "framework_interrupt_id", "state_revision")):
            raise RuntimeConflict("STALE_RECEIPT")
        if (intent["kind"] != "CONFIRM" or intent["operation_id"] != receipt["operation_id"] or
            intent["status"] != "CONSUMED" or intent["payload_hash"] != receipt["payload_hash"] or
            digest(intent["payload"]) != intent["payload_hash"] or self.clock() >= intent["expires_at"]):
            raise RuntimeConflict("EXPIRED_OR_CHANGED")
        return run, receipt, intent

    def issue_grant(self, run_id, invocation_id, receipt_id, principal, *, deadline=None):
        with self._transaction() as con:
            run, receipt, intent = self._validate_receipt(con, run_id, invocation_id, receipt_id, principal)
            self._reserve(con, run, invocation_id, "write-action:" + receipt_id, {"logical_actions": 1})
        grant = GateGrant(new_id(), run_id, invocation_id, receipt_id, intent["operation_id"],
                          deadline if deadline is not None else time.monotonic() + 27)
        self._grants[grant.nonce] = grant
        return grant

    def _check_grant(self, con, grant, principal=None):
        if self._grants.get(grant.nonce) is not grant or time.monotonic() >= grant.deadline:
            raise RuntimeConflict("INVALID_GRANT")
        return self._validate_receipt(con, grant.run_id, grant.invocation_id, grant.receipt_id, principal)

    def claim(self, grant):
        with self._transaction() as con:
            run, receipt, intent = self._check_grant(con, grant)
            prior = self._operation(con, grant.operation_id)
            if prior:
                if (prior["payload_hash"] != intent["payload_hash"] or prior["actor"] != run["actor"] or prior["tenant"] != run["tenant"]):
                    return ClaimResult(None, ToolResult(status="FAILED", error_code="operation_conflict", operation_id=grant.operation_id))
                return ClaimResult(None, ToolResult.model_validate(prior["result"]))
            if self._get(con, "legacy_operation", grant.operation_id) or self._get(con, "readiness", "legacy_unknown"):
                raise RuntimeConflict("LEGACY_RECONCILIATION_REQUIRED")
            key = (run["tenant"], run["actor"], intent["payload"]["order_id"], "CREATE_TICKET")
            if con.execute("SELECT 1 FROM rt_reservations WHERE tenant=? AND actor=? AND order_id=? AND operation_type=?", key).fetchone():
                return ClaimResult(None, ToolResult(status="FAILED", error_code="ORDER_BUSY", operation_id=grant.operation_id))
            result = ToolResult(status="UNKNOWN", error_code="pending_reconciliation", operation_id=grant.operation_id)
            operation = {"operation_id": grant.operation_id, "run_id": run["run_id"], "tenant": key[0], "actor": key[1],
                         "order_id": key[2], "operation_type": key[3], "payload": intent["payload"], "payload_hash": intent["payload_hash"],
                         "receipt_ref": receipt["receipt_id"], "claim_invocation": grant.invocation_id,
                         "task_id": run["task_id"], "scope_epoch": run["scope_epoch"], "state": "IN_FLIGHT",
                         "dispatch_stage": "RESERVED", "attempt_count": 0, "send_started_at": None,
                         "created_at": self.clock(), "result": result.model_dump()}
            con.execute("INSERT INTO rt_operations VALUES (?,?,?,?,?,?,?,?)", (grant.operation_id, run["run_id"], *key, intent["payload_hash"], canonical(operation)))
            con.execute("INSERT INTO rt_reservations VALUES (?,?,?,?,?)", (*key, grant.operation_id))
        token = SendToken(new_id(), grant.operation_id, grant)
        self._tokens[token.nonce] = token
        return ClaimResult(token, result)

    def start_send(self, token, principal):
        with self._mutex:
            if not isinstance(token, SendToken) or self._tokens.get(token.nonce) is not token:
                raise RuntimeConflict("INVALID_SEND_TOKEN")
            # Burning before the transaction is deliberately conservative on any failure.
            self._tokens.pop(token.nonce)
        with self._transaction() as con:
            run, receipt, intent = self._check_grant(con, token.grant, principal)
            operation = self._operation(con, token.operation_id)
            reservation = con.execute("SELECT operation_id FROM rt_reservations WHERE tenant=? AND actor=? AND order_id=? AND operation_type='CREATE_TICKET'",
                                      (run["tenant"], run["actor"], intent["payload"]["order_id"])).fetchone()
            if (not operation or operation["state"] != "IN_FLIGHT" or operation["dispatch_stage"] != "RESERVED" or
                not reservation or reservation[0] != token.operation_id or operation["claim_invocation"] != token.grant.invocation_id):
                raise RuntimeConflict("INVALID_SEND_STATE")
            self._reserve(con, run, token.grant.invocation_id, "send:" + token.operation_id, {"write_attempts": 1, "http_attempts": 1})
            operation.update(dispatch_stage="SEND_STARTED", send_started_at=self.clock(), attempt_count=1)
            self._put_operation(con, operation)
        return operation

    @staticmethod
    def _operation(con, operation_id):
        row = con.execute("SELECT data FROM rt_operations WHERE operation_id=?", (operation_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put_operation(con, operation):
        con.execute("UPDATE rt_operations SET data=? WHERE operation_id=?", (canonical(operation), operation["operation_id"]))

    def get_operation(self, operation_id):
        with closing(self._connect()) as con, con:
            return self._operation(con, operation_id)

    def pending_ticket_ref(self, principal, order_id):
        """Read the current actor's K for guidance only; claim stays authoritative."""
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT operation_id FROM rt_reservations WHERE tenant=? AND actor=? AND order_id=? AND operation_type='CREATE_TICKET'",
                (principal.tenant, principal.user_id, order_id),
            ).fetchone()
            return row[0] if row else None

    def list_operations(self, run_id):
        """Trusted lifecycle reader for ledger-authoritative recovery summaries."""
        with closing(self._connect()) as con:
            return [json.loads(row[0]) for row in con.execute("SELECT data FROM rt_operations WHERE run_id=?", (run_id,))]

    def _settle(self, con, operation, result):
        if operation["state"] in ("SUCCESS", "FAILED"):
            return
        operation.update(state=result.status, result=result.model_dump(), settled_at=self.clock())
        self._put_operation(con, operation)
        if result.status in ("SUCCESS", "FAILED"):
            con.execute("DELETE FROM rt_reservations WHERE operation_id=?", (operation["operation_id"],))

    def settle(self, operation_id, result, *, wait_seconds=1.5):
        """Trusted transport settlement only; never expose as a user/API mutation."""
        with self._transaction(wait_seconds=min(2.0, max(0.0, wait_seconds))) as con:
            operation = self._operation(con, operation_id)
            if not operation or operation["dispatch_stage"] != "SEND_STARTED":
                raise RuntimeConflict("NOT_SEND_STARTED")
            if result.operation_id not in (None, operation_id):
                raise RuntimeConflict("SETTLEMENT_CONFLICT")
            self._settle(con, operation, result.model_copy(update={"operation_id": operation_id}))

    def recover_run(self, run_id, *, executor_stopped=False, reason="INTERRUPTED"):
        if not executor_stopped:
            raise RuntimeConflict("EXECUTOR_STOP_NOT_PROVEN")
        with self._transaction() as con:
            run = self._get(con, "run", run_id)
            if not run:
                raise RuntimeConflict("RUN_UNAVAILABLE")
            operations = [json.loads(r[0]) for r in con.execute("SELECT data FROM rt_operations WHERE run_id=?", (run_id,))]
            for operation in operations:
                if operation["state"] == "IN_FLIGHT":
                    sent = operation["dispatch_stage"] == "SEND_STARTED"
                    self._settle(con, operation, ToolResult(status="UNKNOWN" if sent else "FAILED", error_code="pending_reconciliation" if sent else "NOT_SENT", operation_id=operation["operation_id"], attempts=operation["attempt_count"]))
            result_reason = "UNKNOWN" if any(op["state"] == "UNKNOWN" for op in operations) else ("SETTLED" if any(op["state"] == "SUCCESS" for op in operations) else "NOT_SENT")
            self._close(con, run, reason, result_reason)
        self._revoke(run_id)
        return self.get_run(run_id)

    def _close(self, con, run, reason, receipt_reason):
        run.update(status="CLOSED", active_invocation_id=None, pending_intent_id=None, termination_reason=reason)
        self._put(con, "run", run["run_id"], run)
        for row in con.execute("SELECT kind,id,data FROM rt_records WHERE kind IN ('intent','receipt')").fetchall():
            item = json.loads(row["data"])
            if item["run_id"] != run["run_id"]:
                continue
            if row["kind"] == "intent" and item["status"] in ("UNPUBLISHED", "PENDING"):
                item["status"] = "CANCELLED"
            elif row["kind"] == "receipt" and item["progress"] != "SETTLED":
                operation = self._operation(con, item["operation_id"]) if item["operation_id"] else None
                if operation and operation["receipt_ref"] == item["receipt_id"]:
                    outcome = operation["state"] if operation["dispatch_stage"] == "SEND_STARTED" else "NOT_SENT"
                    if outcome == "IN_FLIGHT":
                        outcome = "UNKNOWN"
                else:
                    if item["decision"] == "APPROVE":
                        outcome = "NOT_SENT"
                    else:
                        suffix = "APPLIED" if item["progress"] == "PROJECTED" else "NOT_PROJECTED"
                        outcome = item["decision"] + "_" + suffix
                item.update(progress="SETTLED", settlement_reason=outcome)
            self._put(con, row["kind"], row["id"], item)

    def close_run(self, run_id, invocation_id, *, reason="COMPLETED", verified_snapshot=None):
        with self._transaction() as con:
            run = self._live(con, run_id, invocation_id)
            if verified_snapshot is not None:
                run["verified_snapshot"] = verified_snapshot
            self._close(con, run, reason, reason)
        self._revoke(run_id)

    def _revoke(self, run_id):
        self._grants = {k: v for k, v in self._grants.items() if v.run_id != run_id}
        self._tokens = {k: v for k, v in self._tokens.items() if v.grant.run_id != run_id}

    def get_budget(self, run_id):
        with closing(self._connect()) as con, con:
            return self._get(con, "budget", run_id)

    def _reserve(self, con, run, invocation_id, event_id, amounts, finalization=False):
        prior = self._get(con, "budget_event", event_id)
        if prior:
            if prior["run_id"] != run["run_id"] or prior["amounts"] != amounts or prior["invocation_id"] != invocation_id:
                raise RuntimeConflict("BUDGET_EVENT_CONFLICT")
            return prior
        if any(k not in TASK_LIMITS or not isinstance(v, int) or isinstance(v, bool) or v < 0 for k, v in amounts.items()):
            raise RuntimeConflict("INVALID_BUDGET")
        if amounts.get("input_tokens", 0) > 8192 or amounts.get("output_tokens", 0) > 2048:
            raise RuntimeConflict("BUDGET_EXHAUSTED")
        task, request = self._get(con, "budget", run["run_id"]), self._get(con, "invocation_budget", invocation_id)
        if task is None or request is None:
            raise RuntimeConflict("BUDGET_UNAVAILABLE")
        if not finalization and (task["business_nodes"] >= 60 or request["business_nodes"] >= 28):
            raise RuntimeConflict("BUDGET_EXHAUSTED")
        for key, amount in amounts.items():
            if task[key] + amount > TASK_LIMITS[key] or (key in REQUEST_LIMITS and request[key] + amount > REQUEST_LIMITS[key]):
                raise RuntimeConflict("BUDGET_EXHAUSTED")
            task[key] += amount
            request[key] += amount
        event = {"run_id": run["run_id"], "invocation_id": invocation_id, "amounts": amounts, "settled": False}
        self._put(con, "budget", run["run_id"], task)
        self._put(con, "invocation_budget", invocation_id, request)
        self._put(con, "budget_event", event_id, event)
        return event

    def reserve_budget(self, run_id, invocation_id, event_id, *, finalization=False, **amounts):
        with self._transaction() as con:
            run = self._live(con, run_id, invocation_id)
            return self._reserve(con, run, invocation_id, event_id, amounts, finalization)

    def reserve_action(self, run_id, invocation_id, event_id, action_type, parameters, fact_versions):
        with self._transaction() as con:
            run = self._live(con, run_id, invocation_id)
            fingerprint = digest([run_id, action_type, parameters, run["goal_revision"], run["scope_epoch"], fact_versions])
            event_binding = self._get(con, "action_event", event_id)
            if event_binding and event_binding["fingerprint"] != fingerprint:
                raise RuntimeConflict("BUDGET_EVENT_CONFLICT")
            prior = self._get(con, "action", fingerprint)
            if prior:
                raise RuntimeConflict("NO_PROGRESS")
            self._reserve(con, run, invocation_id, event_id, {"logical_actions": 1})
            self._put(con, "action", fingerprint, {"event_id": event_id, "run_id": run_id})
            self._put(con, "action_event", event_id, {"fingerprint": fingerprint})
        return fingerprint

    def settle_budget(self, event_id, usage):
        overflow = False
        with self._transaction() as con:
            event = self._get(con, "budget_event", event_id)
            if not event or event["settled"]:
                return
            # Unknown usage retains full reservation. Actual usage is auditable;
            # maxima remain consumed, so rollback never grants extra capacity.
            actual = {}
            for name in ("prompt_tokens", "completion_tokens"):
                value = usage.get(name)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    return
                actual[name] = value
            if actual["prompt_tokens"] > event["amounts"].get("input_tokens", 0) or actual["completion_tokens"] > event["amounts"].get("output_tokens", 0):
                overflow = True
                run = self._get(con, "run", event["run_id"])
                run["budget_blocked"] = True
                self._put(con, "run", event["run_id"], run)
            event.update(settled=True, actual_usage=actual)
            self._put(con, "budget_event", event_id, event)
        if overflow:
            self._revoke(event["run_id"])
            raise RuntimeConflict("PROVIDER_USAGE_EXCEEDED_RESERVATION")

    def save_request_result(self, request_id, result):
        with self._transaction() as con:
            previous = self._get(con, "request_result", request_id)
            if previous is not None:
                return previous
            self._put(con, "request_result", request_id, result)
        return result

    def get_request_result(self, request_id):
        with closing(self._connect()) as con, con:
            return self._get(con, "request_result", request_id)
