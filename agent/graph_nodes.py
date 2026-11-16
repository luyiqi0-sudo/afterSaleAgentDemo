"""Business nodes. Only current RuntimeContext can authorize a dispatch."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import re
import time

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import ValidationError

from agent.answers import render_answer
from agent.public_progress import begin_tool, emit_stage, finish_tool
from agent.answer_support import project_for_goal, unavailable_read_capability
from agent.contracts import ActionProposal, GoalDraft
from agent.guards import CapabilityError, SafetyError, SQLRepairError, valid_order_id
from agent.provider import ProviderError
from agent.policy_requests import merge_policy_presentations
from agent.policy_dates import CLARIFICATION, bind_policy_date, date_reply
from agent.goal_inputs import supply_order_subjects, supply_single_order_reply
from agent.read_planning import linked_read_proposal
from agent.runtime_store import digest, new_id
from agent.runtime_types import RuntimeConflict
from agent.sql import month_context
from agent.state import RuntimeContext, TERMINAL_GOALS, aggregate, clear_active


@dataclass(frozen=True)
class ReadGrant:
    invocation_id: str
    action_ref: str
    state_revision: int
    scope_epoch: int
    deadline: float


class CallbackProvider:
    """SQLService's narrow provider contract still uses the current budget."""
    def __init__(self, provider, budget, trace_emit=None):
        self.provider, self.budget = provider, budget
        self.trace_emit = trace_emit or (lambda event, data: None)

    async def sql(self, question, schema, error=None):
        candidate = await self.provider.sql(question, schema, error, budget=self.budget)
        self.trace_emit("sql_candidate", {"sql": candidate, "previous_error": error})
        return candidate


def _failure(state, code, goal_ids=None, diagnostic=None):
    state["outcome"] = {"class": code, "goal_ids": goal_ids or [g["goal_id"] for g in state["goals"] if g["status"] not in TERMINAL_GOALS],
                        "diagnostic": diagnostic, "data": None}
    return state


def _business_context(ctx):
    # Inform planning of facts already owned by the server, without exposing an
    # actor ID or allowing model output to replace the runtime principal.
    return {"date": month_context(ctx.business_date),
            "identity": {"source": "trusted_request_context", "scope": "current_user",
                         "user_id_required": False}}


def _propose(state, ctx, proposal):
    # MODEL_PROPOSAL denotes the non-authorizing candidate ingress contract.
    # Deterministic read candidates use it too; their origin is traced separately.
    state["proposed_action"] = proposal.model_dump(mode="json")
    ctx.gate_input = {"kind": "MODEL_PROPOSAL", "action_ref": new_id(), "invocation_id": ctx.run["active_invocation_id"], "proposal": proposal}
    state.update(gate_input_kind="MODEL_PROPOSAL", route="gate")
    return state


def _mark(state, ids, status, reason):
    for goal in state["goals"]:
        if goal["goal_id"] in ids and goal["status"] not in TERMINAL_GOALS:
            goal.update(status=status, unmet_reason=reason)


def _snapshot(state, ctx):
    snapshot = {key: deepcopy(state.get(key)) for key in (
        "schema_version", "task_id", "run_id", "conversation_id", "thread_id", "run_generation",
        "state_revision", "goal_revision", "scope_epoch", "goals", "goal_history", "observations", "verified_objects",
        "closed_capabilities", "unresolved_operation_ids", "task_status", "termination_reason")}
    ctx.store.save_verified_snapshot(ctx.run["run_id"], ctx.run["active_invocation_id"], ctx.run["state_revision"], snapshot)
    ctx.last_verified = snapshot


def _prepare(state, ctx, kind, payload, goals, source_id=None, objects=None, sources=None):
    record = ctx.store.save_preparation(ctx.run["run_id"], ctx.run["active_invocation_id"], source_id or new_id(),
                                        kind, payload, goals, object_refs=objects, sources=sources)
    # The durable candidate is created BEFORE these former proposals are cleared.
    clear_active(state)
    state["prepare_request_ref"] = record["prepare_request_id"]
    return state


def _question(state, ctx, question, goals=None, field="order_id", source_id=None, fields=None):
    ids = goals or [g["goal_id"] for g in state["goals"] if g["status"] not in TERMINAL_GOALS]
    _mark(state, ids, "NEEDS_INPUT", None)
    payload = {"question": question, "field": field}
    if fields:
        payload["fields"] = fields
    _prepare(state, ctx, "CLARIFY", payload, ids, source_id)
    state.update(clarification=question, route="prepare_clarify")
    return state


def _current_observations(state, subject=None):
    return [o for o in state["observations"] if o["task_id"] == state["task_id"] and o["validation"] == "VERIFIED"
            and o["scope_epoch"] == state["scope_epoch"] and (subject is None or o["subject"] == subject)]


def _dependencies_met(goal, by_id):
    return all(dep in by_id and by_id[dep]["status"] == "FULFILLED" for dep in goal["depends_on"])


def _block_failed_dependencies(by_id):
    # A failed prerequisite can block a chain even when goals appear in reverse order.
    while True:
        blocked = [g for g in by_id.values() if g["status"] == "OPEN" and any(
            dep not in by_id or by_id[dep]["status"] in {"BLOCKED", "DENIED", "CANCELLED"}
            for dep in g["depends_on"])]
        if not blocked:
            return
        for goal in blocked:
            goal.update(status="BLOCKED", unmet_reason="DEPENDENCY")


def _draft_goal_ids(candidates, existing):
    retained = {(g["kind"], g.get("subject")): g["goal_id"] for g in existing}
    return {g["goal_id"]: retained.get((g["kind"], g.get("subject"))) or "g" + new_id()[:12] for g in candidates}


async def _record_pending_ticket(state, ctx, subject):
    ref = await asyncio.to_thread(ctx.store.pending_ticket_ref, ctx.principal, subject)
    if ref and ref not in state["unresolved_operation_ids"]:
        state["unresolved_operation_ids"].append(ref)
    return ref


class GraphNodes:
    def bound(self, name):
        async def node(state, runtime: Runtime[RuntimeContext]):
            ctx = runtime.context
            ctx.trace.append(name)
            ctx.trace_emit("node", {"node": name})
            finalization = name in {"halt", "answer", "route_goals", "seal_unknown", "degrade"}
            if time.monotonic() >= ctx.deadline - (0 if finalization else 3):
                raise RuntimeConflict("BUDGET_EXHAUSTED")
            ctx.store.reserve_budget(ctx.run["run_id"], ctx.run["active_invocation_id"], new_id(),
                                     business_nodes=1, finalization=finalization)
            if time.monotonic() >= ctx.deadline - (0 if finalization else 3):
                raise RuntimeConflict("BUDGET_EXHAUSTED")
            state = deepcopy(state)
            if (state["actor_id_assertion"], state["tenant_id_assertion"], state["run_generation"]) != (
                    ctx.principal.user_id, ctx.principal.tenant, ctx.run["generation"]):
                raise RuntimeConflict("DENIED")
            result = await getattr(self, name)(state, ctx)
            result["task_budget"] = ctx.store.get_budget(ctx.run["run_id"])
            return result
        return node

    async def understand(self, state, ctx):
        emit_stage(ctx, "understand", [])
        ctx.gate_input = ctx.grant = None
        clear_active(state)
        mode = state.get("resume_mode")
        order_supply = False
        if mode == "SUPPLY" and not state.get("pending_addition_intents"):
            receipt = ctx.store.get_receipt(ctx.receipt_ref)
            intent = ctx.store.get_intent(receipt["intent_id"])
            supplied = supply_single_order_reply(state["goals"], intent, ctx.request.message,
                                                  state["question"], ctx.request.client_message_id)
            if supplied:
                # Only the slot is filled. The original goal and execution gate
                # retain control over query scope, dependencies and ownership.
                state["candidate_objects"] = [{"order_id": supplied["order_id"], "span": supplied["span"]}]
                ctx.run = ctx.store.advance_goals(ctx.run["run_id"], ctx.run["active_invocation_id"], ctx.receipt_ref)
                state.update(goal_revision=ctx.run["goal_revision"], resume_mode=None, route="decide")
                ctx.trace_emit("clarification_supply", {**supplied, "field": "order_id",
                               "source_message_id": ctx.request.client_message_id})
                _snapshot(state, ctx)
                return state
        waiting_dates = [g for g in state["goals"] if g["status"] == "NEEDS_INPUT"
                         and g.get("policy_query", {}).get("error")]
        if mode == "SUPPLY" and waiting_dates:
            supplied = date_reply(ctx.request.message)
            if supplied is None:
                return _question(state, ctx, CLARIFICATION, [g["goal_id"] for g in waiting_dates], "policy_query_date")
            for goal in waiting_dates:
                query = goal["policy_query"]
                query.update(as_of=supplied, error=None, source_message_id=ctx.request.client_message_id,
                             source_text=ctx.request.message)
                goal.update(status="OPEN", unmet_reason=None, description=query["business_question"])
                goal["input_refs"].append(ctx.request.client_message_id)
            ctx.run = ctx.store.advance_goals(ctx.run["run_id"], ctx.run["active_invocation_id"], ctx.receipt_ref)
            state.update(goal_revision=ctx.run["goal_revision"], resume_mode=None, route="decide")
            _snapshot(state, ctx)
            return state
        try:
            draft = merge_policy_presentations(GoalDraft.model_validate(await ctx.model.understand_goal(
                state["question"], {"existing_goals": [{"id": g["goal_id"], "description": g["description"], "kind": g["kind"]}
                                                       for g in state["goals"]],
                                    "business_context": _business_context(ctx)}, budget=ctx.budget)))
            if (not state["goals"] and mode is None and len(draft.goals) == 1
                    and draft.goals[0].kind == "policy" and len(state["question"]) <= 300):
                # For one initial policy goal the exact user text is unambiguous.
                # Verify that request, not a model paraphrase which can add
                # presentation jargon or silently omit an unsupported facet.
                draft.goals[0].description = state["question"]
        except (ProviderError, ValidationError) as error:
            code = "BUDGET_EXHAUSTED" if getattr(error, "code", None) == "BUDGET_EXHAUSTED" else "DENIED" if getattr(error, "code", None) == "UNSAFE_PROPOSAL" else "MODEL_UNAVAILABLE" if isinstance(error, ProviderError) and error.code != "INVALID_OUTPUT" else "INVALID_PROPOSAL"
            _failure(state, code)
            state["route"] = "halt"
            return state
        candidates = []
        for candidate in draft.goals:
            subject = candidate.subject_candidate
            span = candidate.source_span
            if subject and (not span or span.end > len(state["question"]) or state["question"][span.start:span.end] != subject):
                # Exact unique mention is only a candidate; gate still checks object semantics/owner.
                if state["question"].count(subject) != 1:
                    return _question(state, ctx, "请明确订单号及需要办理的事项。")
                start = state["question"].index(subject)
                span = {"start": start, "end": start + len(subject)}
            else:
                span = span.model_dump() if span else None
            candidates.append({"goal_id": candidate.id, "description": candidate.description,
                               "kind": candidate.kind, "subject": subject, "candidate_span": span,
                               "required": True, "status": "OPEN", "unmet_reason": None,
                               "depends_on": candidate.deps, "supporting_observation_ids": [],
                               "subject_scope_bindings": [], "input_refs": [ctx.request.client_message_id]})
            if candidate.kind == "policy":
                candidates[-1]["policy_query"] = bind_policy_date(
                    candidate.description, state["question"], ctx.request.client_message_id,
                    single_goal=len(draft.goals) == 1)
        old = state["goals"]
        if mode == "REPLACE":
            # Keep superseded goal results for audit but close explicitly replaced requirements.
            for goal in old:
                if goal["status"] not in TERMINAL_GOALS and goal.get("subject") == state.get("replaced_subject"):
                    goal.update(status="CANCELLED", unmet_reason="CANCELLED", required=False)
            archived = [goal for goal in old if goal["status"] == "CANCELLED" and not goal.get("required", True)]
            state.setdefault("goal_history", []).extend(archived)
            old = [goal for goal in old if goal not in archived]
            id_map = _draft_goal_ids(candidates, old)
            candidates = [candidate for candidate in candidates if not any((g["kind"], g.get("subject")) == (candidate["kind"], candidate.get("subject")) for g in old)]
            if len(old) + len(candidates) > 4:
                state["goals"] = old
                return _question(state, ctx, "最多同时保留四个目标，请明确替换后的优先范围。", field="scope")
            for candidate in candidates:
                candidate["goal_id"] = id_map[candidate["goal_id"]]
                candidate["depends_on"] = [id_map.get(dep, dep) for dep in candidate["depends_on"]]
            state["goals"] = old + candidates
        elif mode == "SUPPLY" and old:
            additions = state.get("pending_addition_intents", [])
            if additions:
                # Explicit selection resolves the preserved mixed intent; model output cannot erase A.
                text = ctx.request.message.strip()
                if not re.search(r"都|两项|全部|同时|只", text):
                    return _question(state, ctx, "请明确两项都保留，或只保留其中一项。", field="scope")
                id_map = _draft_goal_ids(candidates, old)
                new_candidates = [g for g in candidates if not any((o["kind"], o.get("subject")) == (g["kind"], g.get("subject")) for o in old)]
                wanted = {item["requested_subject"] for item in additions if item.get("requested_subject")}
                if re.search(r"都|两项|全部|同时", text) and not wanted <= {g.get("subject") for g in candidates}:
                    return _question(state, ctx, "追加的订单查询尚未明确，请重新列出需要保留的两项请求。", field="scope")
                if len([g for g in old if g.get("required")]) + len(new_candidates) > 4:
                    return _question(state, ctx, "最多同时处理四个目标，请明确优先范围。", field="scope")
                for goal in old:
                    if goal["status"] == "NEEDS_INPUT":
                        goal.update(status="OPEN", unmet_reason=None)
                if "只" in text:
                    possible = {g.get("subject") for g in old + candidates if g.get("subject")}
                    keep_subjects = {subject for subject in possible if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(subject) + r"(?![A-Za-z0-9_-])", text)}
                    if len(keep_subjects) != 1:
                        return _question(state, ctx, "请明确仅保留哪一个订单及办理事项。", field="scope")
                    new_candidates = [candidate for candidate in new_candidates if candidate.get("subject") in keep_subjects]
                    for goal in old:
                        if goal.get("subject") not in keep_subjects and goal["status"] not in TERMINAL_GOALS:
                            goal.update(status="CANCELLED", unmet_reason="CANCELLED", required=False)
                for candidate in new_candidates:
                    if candidate.get("subject") and not await asyncio.to_thread(ctx.sql.owns_order, candidate["subject"], ctx.principal):
                        _failure(state, "DENIED")
                        state["route"] = "halt"
                        return state
                    candidate["goal_id"] = id_map[candidate["goal_id"]]
                    candidate["depends_on"] = [id_map.get(dep, dep) for dep in candidate["depends_on"]]
                state["goals"] = old + new_candidates
                state["pending_addition_intents"] = []
            else:
                receipt = ctx.store.get_receipt(ctx.receipt_ref)
                intent = ctx.store.get_intent(receipt["intent_id"])
                if intent["payload"].get("field") == "order_id":
                    order_supply = True
                    supply_order_subjects(old, candidates, ctx.request.message, ctx.request.client_message_id)
                else:
                    waiting = [g for g in old if g["status"] == "NEEDS_INPUT"]
                    for goal in waiting:
                        update = next((g for g in candidates if g["goal_id"] == goal["goal_id"] and g["kind"] == goal["kind"]), None)
                        if update:
                            goal.update(subject=update["subject"], candidate_span=update["candidate_span"], status="OPEN", unmet_reason=None)
                state["goals"] = old
            ctx.run = ctx.store.advance_goals(ctx.run["run_id"], ctx.run["active_invocation_id"], ctx.receipt_ref)
            state["goal_revision"] = ctx.run["goal_revision"]
        else:
            state["goals"] = candidates
        state["resume_mode"] = None
        state["candidate_objects"] = [{"order_id": g["subject"], "span": g["candidate_span"]} for g in candidates if g["subject"]]
        _snapshot(state, ctx)
        invalid_dates = [g["goal_id"] for g in state["goals"] if g["status"] == "OPEN"
                         and g.get("policy_query", {}).get("error")]
        if invalid_dates:
            return _question(state, ctx, CLARIFICATION, invalid_dates, "policy_query_date")
        if order_supply and (unresolved := [g["goal_id"] for g in state["goals"] if g["status"] == "NEEDS_INPUT"]):
            return _question(state, ctx, "请明确需要补充的订单号及对应查询事项。", unresolved)
        if draft.clarification or not candidates:
            return _question(state, ctx, draft.clarification or "请明确需要办理的事项。")
        state["route"] = "decide"
        return state

    async def decide(self, state, ctx):
        ctx.gate_input = ctx.grant = ctx.action = None
        clear_active(state)
        by_id = {g["goal_id"]: g for g in state["goals"]}
        _block_failed_dependencies(by_id)
        ready = {id_: g for id_, g in by_id.items() if g["status"] == "OPEN" and _dependencies_met(g, by_id)}
        # The recovery offer is produced from an actual exhausted read, not a
        # model claim of an abnormal order. It still goes through the normal
        # ownership gate, frozen confirmation and receipt-bound write path.
        for goal in ready.values():
            if goal["kind"] != "ticket" or not goal.get("read_failure_reason"):
                continue
            obj = next((o for o in state["verified_objects"] if o["order_id"] == goal["subject"]
                        and o["task_id"] == state["task_id"] and o["scope_epoch"] == state["scope_epoch"]), None)
            if obj:
                return _propose(state, ctx, ActionProposal.model_validate({
                    "action_type": "request_ticket", "goal_ids": [goal["goal_id"]],
                    "parameters": {"order_id": goal["subject"], "reason": goal["read_failure_reason"]},
                    "parameter_sources": {"order_id": {"type": "object", "ref_id": obj["ref_id"]}},
                    "based_on_observation_ids": [], "public_summary": "读取服务持续失败，提议人工核查并等待确认",
                }))
        if candidate := linked_read_proposal(state, ready):
            proposal = ActionProposal.model_validate(candidate)
            ctx.trace_emit("read_plan", {"origin": "linked_read_rule", "action_type": proposal.action_type,
                                         "goal_ids": proposal.goal_ids, "order_id": proposal.parameters.order_id})
            return _propose(state, ctx, proposal)
        allowed = {"ask_user"}
        for goal in ready.values():
            if goal["kind"] == "logistics":
                has_order = goal.get("subject") and any(o["action_type"] == "query_order" for o in _current_observations(state, goal["subject"]))
                allowed.add("query_logistics" if has_order else "query_order")
                continue
            allowed.update({"policy": ["retrieve_policy"], "sql": ["query_sql"], "order": ["query_order", "query_sql"],
                            "ticket": ["query_order", "request_ticket"], "other": ["query_sql"]}[goal["kind"]])
        visible = ready.values()
        if all(g["status"] in TERMINAL_GOALS for g in state["goals"]):
            allowed.add("finish")
            visible = by_id.values()
        context = {"question": state["question"], "goals": [{k: g[k] for k in ("goal_id", "kind", "description", "subject", "status", "depends_on")} for g in visible],
                   "business_context": _business_context(ctx),
                   "allowed_actions": sorted(allowed),
                   "message_candidates": [{"value": candidate["order_id"], "span": candidate["span"]}
                                          for candidate in state["candidate_objects"]
                                          if candidate.get("span") and candidate["span"]["end"] <= len(state["question"])
                                          and state["question"][candidate["span"]["start"]:candidate["span"]["end"]] == candidate["order_id"]],
                   "message_candidates_authority": "原消息中的未授权候选；仅供复制精确字符坐标，不是已核验对象或执行许可。",
                   "observations": [{"id": o["id"], "subject": o["subject"], "action_type": o["action_type"], "data": o["data"] if o["action_type"] in {"query_order", "query_logistics"} else {"verified": True}} for o in _current_observations(state)],
                   "objects": [{"ref_id": o["ref_id"], "order_id": o["order_id"]} for o in state["verified_objects"] if o["scope_epoch"] == state["scope_epoch"]]}
        for goal in context["goals"]:
            goal["policy_query_date"] = by_id[goal["goal_id"]].get("policy_query", {}).get("as_of")
        for attempt in range(2):
            try:
                raw = await ctx.model.decide_next(context, budget=ctx.budget)
                if isinstance(raw, dict) and (raw.get("action_type") == "execute_confirmed_ticket" or any(k in str(raw) for k in ("receipt_ref", "operation_id", "principal", "grant"))):
                    raise ProviderError("UNSAFE_PROPOSAL")
                proposal = ActionProposal.model_validate(raw)
                if proposal.action_type != "finish" and any(
                    goal_id in by_id and by_id[goal_id]["status"] == "OPEN" and goal_id not in ready
                    for goal_id in proposal.goal_ids
                ):
                    raise ProviderError("INVALID_OUTPUT")
                break
            except (ProviderError, ValidationError) as error:
                unsafe = isinstance(error, ProviderError) and error.code == "UNSAFE_PROPOSAL"
                invalid = isinstance(error, ValidationError) or getattr(error, "code", None) == "INVALID_OUTPUT"
                if invalid and attempt == 0:
                    context["previous_error"] = "INVALID_PROPOSAL"
                    continue
                _failure(state, "BUDGET_EXHAUSTED" if getattr(error, "code", None) == "BUDGET_EXHAUSTED" else "DENIED" if unsafe else "INVALID_PROPOSAL" if invalid else "MODEL_UNAVAILABLE")
                state["route"] = "halt"
                return state
        return _propose(state, ctx, proposal)

    def _source(self, state, value, source):
        if source.type == "message":
            span = source.span
            text = state["question"]
            if span.end > len(text) or text[span.start:span.end] != value:
                return False
            before, after = text[:span.start], text[span.end:]
            if re.search(r"[A-Za-z0-9_-]$", before) or re.match(r"[A-Za-z0-9_-]", after):
                return False
            if value.isdecimal() and re.match(r"\s*(?:[.．]\d|[元圆分块角%％]|(?:RMB|USD)\b)", after, re.I):
                return False
            # The object must be a draft candidate in an order/query context. A
            # bare number after a query verb needs an explicit order reference.
            order_context = re.search(r"订单|单号|工单|order|物流|不是.+是", text, re.I)
            short_lookup = (not value.isdecimal() and (
                re.search(r"(?:查(?:询|看|一下)?|看看|核查)\s*$", before)
                or (re.search(r"(?:确认|为|给)\s*$", before) and re.match(r"\s*(?:创建|提交|建单|建工单|办理)", after))
            ))
            return any(c["order_id"] == value for c in state["candidate_objects"]) and bool(order_context or short_lookup)
        if source.type == "object":
            return any(o["ref_id"] == source.ref_id and o["order_id"] == value and o["task_id"] == state["task_id"] and o["scope_epoch"] == state["scope_epoch"] for o in state["verified_objects"])
        return any(o["id"] == source.ref_id and o["data"].get(source.field) == value and o["subject"] == value for o in _current_observations(state))

    async def gate(self, state, ctx):
        gate_input, ctx.gate_input = ctx.gate_input, None
        state["gate_input_kind"] = None
        if not gate_input or gate_input["invocation_id"] != ctx.run["active_invocation_id"]:
            _failure(state, "DENIED")
            state["route"] = "halt"
            return state
        goals = {g["goal_id"]: g for g in state["goals"]}
        if gate_input["kind"] == "CONFIRMED_TICKET":
            intent = ctx.store.get_intent(gate_input["intent_id"])
            ids = intent["goal_ids"]
            clear_active(state)
            if any(g not in goals or not _dependencies_met(goals[g], goals) for g in ids):
                _failure(state, "INVALID_PROPOSAL", ids)
                _mark(state, ids, "BLOCKED", "DEPENDENCY")
                state["route"] = "halt"
                return state
            if not await asyncio.to_thread(ctx.sql.owns_order, intent["payload"]["order_id"], ctx.principal):
                _failure(state, "DENIED", ids)
                state["route"] = "halt"
                return state
            try:
                ctx.grant = ctx.store.issue_grant(ctx.run["run_id"], ctx.run["active_invocation_id"], ctx.receipt_ref, ctx.principal, deadline=ctx.deadline - 3)
            except RuntimeConflict as error:
                code = "EXPIRED" if str(error) == "EXPIRED_OR_CHANGED" else "BUDGET_EXHAUSTED" if "BUDGET" in str(error) else "DENIED"
                _failure(state, code, ids)
                state["route"] = "verify" if code == "EXPIRED" else "halt"
                return state
            ctx.action = {"action_type": "execute_confirmed_ticket", "goal_ids": ids, "parameters": intent["payload"], "operation_id": intent["operation_id"], "action_ref": gate_input["action_ref"]}
            state["route"] = "dispatch"
            return state
        proposal = gate_input["proposal"]
        ids, action, params = proposal.goal_ids, proposal.action_type, proposal.parameters.model_dump(exclude_none=True)
        if not set(ids) <= goals.keys() or (action != "finish" and any(goals[g]["status"] != "OPEN" for g in ids)):
            _failure(state, "DENIED", ids)
            state["route"] = "halt"
            return state
        if action != "finish" and any(not _dependencies_met(goals[g], goals) for g in ids):
            _failure(state, "INVALID_PROPOSAL", ids)
            state["route"] = "halt"
            return state
        observed = {o["id"] for o in _current_observations(state)}
        if not set(proposal.based_on_observation_ids) <= observed:
            _failure(state, "DENIED", ids)
            state["route"] = "halt"
            return state
        capability_kinds = {"retrieve_policy": {"policy"}, "query_order": {"order", "logistics", "ticket", "other"},
                            "query_logistics": {"logistics", "other"}, "query_sql": {"sql", "order", "other"},
                            "request_ticket": {"ticket", "other"}}
        if action in capability_kinds and any(goals[g]["kind"] not in capability_kinds[action] for g in ids):
            _failure(state, "DENIED", ids)
            state["route"] = "halt"
            return state
        if action == "finish":
            if not {g["goal_id"] for g in state["goals"] if g.get("required")} <= set(ids) or any(g["status"] not in TERMINAL_GOALS for g in state["goals"] if g.get("required")):
                _failure(state, "INVALID_PROPOSAL")
                state["route"] = "halt"
            else:
                state["route"] = "answer"
            return state
        if action == "query_sql":
            gaps = {g: unavailable_read_capability(goals[g]) for g in ids}
            if gap := next((value for value in gaps.values() if value), None):
                affected = [g for g, value in gaps.items() if value == gap]
                clarify = [g for g in affected if "物流" in goals[g]["description"]]
                if gap == "order_items" and clarify:
                    for g in clarify:
                        goals[g]["capability_gap"] = gap
                    _question(state, ctx, "现有数据没有订单商品关联，不能确定所指订单。请提供订单号。", clarify)
                    _snapshot(state, ctx)
                    return state
                _failure(state, "UNSUPPORTED", affected)
                state["outcome"]["capability_gap"] = gap
                state["route"] = "verify"
                return state
            missing = [g for g in ids if goals[g]["kind"] == "order" and not goals[g].get("subject")]
            if missing:
                # An entity read cannot silently become an account-wide SQL
                # query. Lists and aggregates are separate `sql` goals.
                return _question(state, ctx, "请提供需要查询的订单号。", missing)
        subject = params.get("order_id")
        if action in {"query_sql", "retrieve_policy"}:
            for goal_id in ids:
                goal_subject = goals[goal_id].get("subject")
                if goal_subject:
                    if not await asyncio.to_thread(ctx.sql.owns_order, goal_subject, ctx.principal):
                        _failure(state, "DENIED", ids)
                        state["route"] = "halt"
                        return state
                    binding = {"subject": goal_subject, "epoch": state["scope_epoch"]}
                    if binding not in goals[goal_id]["subject_scope_bindings"]:
                        goals[goal_id]["subject_scope_bindings"].append(binding)
        if subject:
            if not valid_order_id(subject) or not self._source(state, subject, proposal.parameter_sources["order_id"]):
                return _question(state, ctx, "请明确需要查询或办理的订单号。", ids)
            if any(goals[g].get("subject") not in (None, subject) for g in ids):
                _failure(state, "DENIED", ids)
                state["route"] = "halt"
                return state
            if not await asyncio.to_thread(ctx.sql.owns_order, subject, ctx.principal):
                _failure(state, "DENIED", ids)
                state["route"] = "halt"
                return state
            obj = {"ref_id": "obj-" + digest([state["task_id"], state["scope_epoch"], subject])[:20], "order_id": subject,
                   "task_id": state["task_id"], "scope_epoch": state["scope_epoch"], "actor": ctx.principal.user_id}
            if obj not in state["verified_objects"]:
                state["verified_objects"].append(obj)
            state["focus_order_id"] = subject
            for goal_id in ids:
                goals[goal_id]["subject"] = subject
                binding = {"subject": subject, "epoch": state["scope_epoch"]}
                if binding not in goals[goal_id]["subject_scope_bindings"]:
                    goals[goal_id]["subject_scope_bindings"].append(binding)
            if action == "query_logistics" and not any(o["action_type"] == "query_order" for o in _current_observations(state, subject)):
                _failure(state, "INVALID_PROPOSAL", ids)
                state["route"] = "halt"
                return state
        if action == "ask_user":
            # Re-asking an already answered field with no changed requirement is no progress.
            if state.get("applied_receipt_id") and all(goals[g].get("subject") for g in ids) and params["field"] == "order_id":
                _failure(state, "NO_PROGRESS", ids)
                state["route"] = "halt"
                return state
            fields = None
            if (params["field"] == "order_id" and all(goals[g]["kind"] == "ticket" and not goals[g].get("subject") for g in ids)
                    and re.search(r"订单号.{0,15}(?:问题|原因|理由)", params["question"])):
                fields = ["order_id", "reason"]
            return _question(state, ctx, params["question"], ids, params["field"], gate_input["action_ref"], fields)
        if action in {"query_order", "query_logistics"} and subject:
            cached = [o for o in _current_observations(state, subject) if o["action_type"] == action
                      and o["data"].get("order_id") == subject]
            covered = {g for o in cached for g in o["goal_ids"]}
            if cached and not (set(ids) & covered):
                # Authorization above still applies. Share verified read facts
                # only with new goals; repeating a served goal still reaches
                # the existing NO_PROGRESS guard. Never reuse a write result.
                ctx.action = {"action_type": action, "goal_ids": ids, "parameters": params, "action_ref": gate_input["action_ref"]}
                state["outcome"] = {"class": "VERIFIED_OK", "goal_ids": ids, "data": deepcopy(cached[-1]["data"])}
                state.update(route="verify", proposed_action=None)
                ctx.trace_emit("read_reuse", {"tool": action, "order_id": subject, "goal_ids": ids,
                                               "source_observation_id": cached[-1]["id"]})
                return state
        # Action fingerprints use pre-existing relevant facts, not freshly minted IDs/timestamps.
        facts = sorted({o["fact_version"] for o in _current_observations(state, subject) if o["action_type"] != action})
        try:
            ctx.store.reserve_action(ctx.run["run_id"], ctx.run["active_invocation_id"], gate_input["action_ref"], action, params, facts)
        except RuntimeConflict as error:
            _failure(state, "NO_PROGRESS" if str(error) == "NO_PROGRESS" else "BUDGET_EXHAUSTED", ids)
            state["route"] = "halt"
            return state
        if action == "request_ticket":
            # Explicit ticket request or verified abnormal order supports suggestion; never approval.
            if not any(goals[g]["kind"] == "ticket" for g in ids) and not any(o["data"].get("status") in (5, 6) for o in _current_observations(state, subject)):
                _failure(state, "DENIED", ids)
                state["route"] = "halt"
                return state
            if await _record_pending_ticket(state, ctx, subject):
                _failure(state, "ORDER_BUSY", ids)
                state["route"] = "verify"
                return state
            reason = (params.get("reason") or "用户请求售后处理").strip()
            payload = {"order_id": subject, "user_id": ctx.principal.user_id, "reason": reason}
            _prepare(state, ctx, "CONFIRM", payload, ids, gate_input["action_ref"], [obj["ref_id"]],
                     [source.model_dump(mode="json") for source in proposal.parameter_sources.values()])
            _mark(state, ids, "WAITING_CONFIRM", None)
            _snapshot(state, ctx)
            state["route"] = "prepare_ticket"
            return state
        ctx.action = {"action_type": action, "goal_ids": ids, "parameters": params, "action_ref": gate_input["action_ref"]}
        ctx.grant = ReadGrant(ctx.run["active_invocation_id"], gate_input["action_ref"], ctx.run["state_revision"], ctx.run["scope_epoch"], ctx.deadline - 3)
        state["proposed_action"] = None
        state["route"] = "dispatch"
        return state

    async def dispatch(self, state, ctx):
        grant = ctx.grant
        current = ctx.store.get_run(ctx.run["run_id"], ctx.principal)
        if (not grant or not ctx.action or grant.invocation_id != current["active_invocation_id"] or
                current["status"] != "LIVE" or time.monotonic() >= grant.deadline):
            _failure(state, "DENIED")
            state["route"] = "halt"
        elif isinstance(grant, ReadGrant) and (grant.state_revision != current["state_revision"] or
                grant.scope_epoch != current["scope_epoch"] or grant.action_ref != ctx.action["action_ref"]):
            _failure(state, "DENIED")
            state["route"] = "halt"
        else:
            state["route"] = {"retrieve_policy": "rag", "query_sql": "sql", "query_order": "read_tool", "query_logistics": "read_tool", "execute_confirmed_ticket": "write_ticket"}[ctx.action["action_type"]]
            emit_stage(ctx, ctx.action["action_type"], ctx.action["goal_ids"], ctx.action["parameters"].get("order_id"))
        return state

    async def rag(self, state, ctx):
        begin_tool(ctx)
        if ctx.retriever is None:
            return _failure(state, "RETRIEVAL_UNAVAILABLE", ctx.action["goal_ids"])
        selected = [g for g in state["goals"] if g["goal_id"] in ctx.action["goal_ids"]]
        subject = selected[0].get("subject") if len(selected) == 1 else None
        order_date = None
        for observation in _current_observations(state, subject) if subject else []:
            if observation["action_type"] == "query_order" and observation["data"].get("create_time"):
                order_date = date.fromisoformat(observation["data"]["create_time"][:10])
        dates = {g.get("policy_query", {}).get("as_of") or ctx.business_date.isoformat() for g in selected}
        if len(dates) != 1:
            return _failure(state, "INVALID_PROPOSAL", ctx.action["goal_ids"])
        query_date = date.fromisoformat(next(iter(dates)))
        query_text = (selected[0]["policy_query"]["business_question"]
                      if len(selected) == 1 and selected[0].get("policy_query", {}).get("as_of")
                      else ctx.action["parameters"]["question"])
        result = await ctx.retriever.retrieve(query_text, query_date=query_date,
                                              conditions=ctx.principal.conditions, order_create_date=order_date,
                                              budget=ctx.budget, provider=ctx.model)
        data = result.model_dump(mode="json")
        data["query_context"] = {"as_of": query_date.isoformat(), "server_date": ctx.business_date.isoformat(),
                                 "order_create_date": order_date.isoformat() if order_date else None,
                                 "sources": [g["policy_query"] for g in selected if g.get("policy_query", {}).get("as_of")]}
        ctx.trace_emit("policy_retrieval", data)
        outcome = {"SUCCESS": "VERIFIED_OK", "NEED_INPUT": "NEED_INPUT", "NO_DATA": "NO_DATA", "CONFLICT": "NO_DATA", "RETRIEVAL_UNAVAILABLE": "RETRIEVAL_UNAVAILABLE"}[result.status]
        if result.status == "RETRIEVAL_UNAVAILABLE" and data.get("error_code") == "BUDGET_EXHAUSTED":
            outcome = "BUDGET_EXHAUSTED"
        state["outcome"] = {"class": outcome, "goal_ids": ctx.action["goal_ids"], "data": data,
                            "diagnostic": "POLICY_CONFLICT" if result.status == "CONFLICT" else None}
        if result.status == "NO_DATA":
            state["outcome"]["capability_gap"] = "knowledge_scope"
        return state

    async def sql(self, state, ctx):
        begin_tool(ctx)
        try:
            result = await ctx.sql.answer(ctx.action["parameters"]["question"], ctx.principal, CallbackProvider(ctx.model, ctx.budget, ctx.trace_emit))
            data = result.model_dump(mode="json")
            data["bound_facts"] = ctx.sql.bound_facts(result)
            data["projection_gaps"] = ctx.sql.unbound_columns(result) if hasattr(ctx.sql, "unbound_columns") else []
            outcome = "VERIFIED_OK" if result.rows and data["bound_facts"] else "NO_DATA"
            if hasattr(ctx.sql, "verified_empty_orders") and ctx.sql.verified_empty_orders(result):
                data["verified_empty_order_scope"] = True
                data["bound_facts"] = [{"entity_id": None, "table": "orders", "field": "amount", "unit": "fen",
                                        "value": 0, "aggregate": "sum"}]
                outcome = "VERIFIED_OK"
            if result.rows and data["projection_gaps"] and not data["bound_facts"]:
                outcome = "UNSUPPORTED"
            subjects = {g.get("subject") for g in state["goals"] if g["goal_id"] in ctx.action["goal_ids"] and g.get("subject")}
            if subjects and any(f["entity_id"] not in subjects for f in data["bound_facts"]):
                outcome = "RESPONSE_MISMATCH"
                data = None
            state["outcome"] = {"class": outcome, "goal_ids": ctx.action["goal_ids"], "data": data}
            ctx.trace_emit("sql_execution", {"outcome": outcome, "result": data,
                                             "action_ref": ctx.action["action_ref"], "scope_epoch": state["scope_epoch"]})
        except (SafetyError, CapabilityError, SQLRepairError, ProviderError) as error:
            code = "BUDGET_EXHAUSTED" if isinstance(error, ProviderError) and error.code == "BUDGET_EXHAUSTED" else "DENIED" if isinstance(error, SafetyError) else "UNSUPPORTED" if isinstance(error, CapabilityError) else "SQL_EXHAUSTED" if isinstance(error, SQLRepairError) else "MODEL_UNAVAILABLE"
            _failure(state, code, ctx.action["goal_ids"])
            ctx.trace_emit("sql_error", {"outcome": code, "error_class": type(error).__name__})
        return state

    def _http_budget(self, ctx):
        def reserve():
            remaining = ctx.deadline - time.monotonic() - 3
            if remaining <= 0:
                raise RuntimeConflict("BUDGET_EXHAUSTED")
            ctx.store.reserve_budget(ctx.run["run_id"], ctx.run["active_invocation_id"], new_id(), http_attempts=1)
            ctx.trace_emit("http_attempt", {"tool": ctx.action["action_type"], "order_id": ctx.action["parameters"].get("order_id")})
            return remaining
        return reserve

    async def read_tool(self, state, ctx):
        begin_tool(ctx)
        result = await getattr(ctx.tools, ctx.action["action_type"])(ctx.action["parameters"]["order_id"], ctx.principal,
                                 reserve_http=self._http_budget(ctx), deadline=ctx.deadline - 3)
        if result.status == "SUCCESS":
            code = "VERIFIED_OK" if result.data else "NO_DATA"
            if result.data and result.data.get("order_id") != ctx.action["parameters"]["order_id"]:
                code = "RESPONSE_MISMATCH"
        else:
            code = {"order_unavailable": "DENIED", "authorization_unavailable": "DENIED", "RESPONSE_MISMATCH": "RESPONSE_MISMATCH",
                    "invalid_response": "RESPONSE_MISMATCH", "BUDGET_EXHAUSTED": "BUDGET_EXHAUSTED", "NO_DATA": "NO_DATA", "not_found": "NO_DATA"}.get(result.error_code, "READ_EXHAUSTED")
        state["outcome"] = {"class": code, "goal_ids": ctx.action["goal_ids"], "data": result.data, "attempts": result.attempts}
        ctx.trace_emit("tool_result", {"tool": ctx.action["action_type"], "result": result.model_dump(mode="json")})
        return state

    async def write_ticket(self, state, ctx):
        action = ctx.action
        begin_tool(ctx)
        result = await ctx.tools.create_ticket(action["parameters"]["order_id"], action["parameters"]["reason"], action["operation_id"], ctx.principal, grant=ctx.grant)
        ctx.grant = None
        if result.error_code in {"STORAGE_UNAVAILABLE", "SETTLEMENT_NOT_PERSISTED"}:
            raise RuntimeConflict("PERSISTENCE_FAILURE")
        code = {"SUCCESS": "VERIFIED_OK", "UNKNOWN": "WRITE_UNKNOWN", "FAILED": "WRITE_FAILED"}[result.status]
        if result.error_code in {"ORDER_BUSY", "BUDGET_EXHAUSTED"}:
            code = result.error_code
        if result.error_code in {"order_unavailable", "authorization_unavailable", "INVALID_GRANT", "STALE_RECEIPT", "STALE_FENCE"}:
            code = "DENIED"
        if result.error_code == "EXPIRED_OR_CHANGED":
            code = "EXPIRED"
        operation = ctx.store.get_operation(action["operation_id"])
        if result.status == "SUCCESS" and (not operation or operation["state"] != "SUCCESS" or not result.data or result.data.get("order_id") != action["parameters"]["order_id"]):
            code = "WRITE_UNKNOWN"
        state["outcome"] = {"class": code, "goal_ids": action["goal_ids"], "data": result.data,
                            "operation_id": action["operation_id"], "attempts": result.attempts}
        ctx.trace_emit("operation", {"operation": operation, "outcome": code})
        return state

    async def prepare_ticket(self, state, ctx):
        intent = ctx.store.freeze_preparation(state["prepare_request_ref"], ctx.run["active_invocation_id"])
        state.update(prepare_request_ref=None, pending_interrupt_ref=intent["intent_id"], pending_ticket_ref=intent["operation_id"])
        emit_stage(ctx, "request_ticket" if intent["kind"] == "CONFIRM" else "ask_user", intent["goal_ids"], intent["payload"].get("order_id"))
        return state

    async def prepare_clarify(self, state, ctx):
        if not state.get("prepare_request_ref"):
            _question(state, ctx, state.get("clarification") or "请补充需要的信息。")
        return await self.prepare_ticket(state, ctx)

    async def confirm(self, state, ctx):
        intent = ctx.store.get_intent(state["pending_interrupt_ref"])
        receipt_ref = interrupt({"intent_id": intent["intent_id"]})
        state["applied_receipt_id"] = receipt_ref
        return state

    async def clarify(self, state, ctx):
        return await self.confirm(state, ctx)

    async def resume_gate(self, state, ctx):
        ref = state.get("applied_receipt_id")
        if not ref or ref != ctx.receipt_ref:
            _failure(state, "DENIED")
            state["route"] = "halt"
            return state
        receipt = ctx.store.get_receipt(ref)
        intent = ctx.store.get_intent(receipt["intent_id"])
        if receipt["run_id"] != ctx.run["run_id"] or receipt["invocation_id"] != ctx.run["active_invocation_id"] or receipt["new_revision"] != ctx.run["state_revision"]:
            _failure(state, "DENIED")
            state["route"] = "halt"
            return state
        ctx.store.project_receipt(ref, ctx.run["active_invocation_id"])
        clear_active(state)
        ctx.gate_input = ctx.grant = None
        state.update(state_revision=receipt["new_revision"], goal_revision=receipt["goal_revision"], scope_epoch=receipt["scope_epoch"],
                     applied_receipt_id=ref, task_status="ACTIVE", question=ctx.request.message,
                     user_inputs=state["user_inputs"] + [{"message_id": ctx.request.client_message_id, "content": ctx.request.message}])
        decision = receipt["decision"]
        if decision == "APPROVE":
            action_ref = digest([ref, "execute_confirmed_ticket"])
            ctx.gate_input = {"kind": "CONFIRMED_TICKET", "action_ref": action_ref, "intent_id": intent["intent_id"], "invocation_id": ctx.run["active_invocation_id"]}
            state.update(gate_input_kind="CONFIRMED_TICKET", internal_action_ref={"action_ref": action_ref, "receipt_ref": ref,
                         "operation_id": intent["operation_id"], "payload_hash": intent["payload_hash"]}, route="gate")
        elif decision in {"SUPPLY", "REPLACE"}:
            state.update(resume_mode=decision, route="understand")
            if decision == "REPLACE":
                state["focus_order_id"] = None
                state["replaced_subject"] = intent["payload"].get("order_id")
            else:
                # Raw original input remains available for source spans after a short supplied answer.
                state["question"] = "\n".join(item["content"] for item in state["user_inputs"])
        elif decision == "AMBIGUOUS":
            state["pending_addition_intents"] = receipt["addition_intents"]
            _question(state, ctx, "两项请求已保留；原确认已失效，尚未提交。是否都保留？" if receipt["addition_intents"] else "原确认已失效，尚未提交。请明确咨询或办理意图。", intent["goal_ids"], "scope", ref)
        else:
            _mark(state, intent["goal_ids"], "CANCELLED" if decision == "CANCEL" else "BLOCKED", "CANCELLED" if decision == "CANCEL" else "EXPIRED")
            state["route"] = "route_goals"
        _snapshot(state, ctx)
        return state

    async def verify(self, state, ctx):
        outcome = state["outcome"]
        code, ids = outcome["class"], outcome["goal_ids"]
        action = ctx.action or {"action_type": "none", "parameters": {}, "goal_ids": ids}
        if code == "VERIFIED_OK":
            observations = []
            outcome["goal_outcomes"] = {}
            for goal in list(state["goals"]):
                if goal["goal_id"] not in ids:
                    continue
                subject = goal.get("subject")
                data, unmet, coverage = project_for_goal(action["action_type"], goal, outcome["data"])
                outcome["goal_outcomes"][goal["goal_id"]] = unmet or "VERIFIED_OK"
                goal.pop("capability_gap", None)
                goal.update(coverage)
                if unmet:
                    goal.update(status="BLOCKED", unmet_reason=unmet)
                    state["closed_capabilities"].append({"goal_id": goal["goal_id"], "capability": action["action_type"],
                                                         "epoch": state["scope_epoch"], "reason": unmet})
                if data is None:
                    continue
                observation = {"id": "obs-" + new_id(), "task_id": state["task_id"], "scope_epoch": state["scope_epoch"],
                               "goal_ids": [goal["goal_id"]], "subject": subject, "action_type": action["action_type"],
                               "action_ref": action.get("action_ref"), "data": data,
                               "fact_version": digest([subject, action["action_type"], data]), "outcome_class": code,
                               "validation": "VERIFIED", "operation_id": outcome.get("operation_id")}
                state["observations"].append(observation)
                observations.append(observation)
                goal["supporting_observation_ids"].append(observation["id"])
                if subject is not None and {"subject": subject, "epoch": state["scope_epoch"]} not in goal["subject_scope_bindings"]:
                    goal["subject_scope_bindings"].append({"subject": subject, "epoch": state["scope_epoch"]})
                satisfied = {"order": {"query_order", "query_sql"}, "logistics": {"query_logistics"}, "policy": {"retrieve_policy"},
                             "sql": {"query_sql"}, "ticket": {"execute_confirmed_ticket"}, "other": set()}[goal["kind"]]
                if action["action_type"] in satisfied and not unmet:
                    goal.update(status="FULFILLED", unmet_reason=None)
                # Orders outside shipment lifecycle don't require a pointless logistics lookup.
                if not unmet and goal["kind"] == "logistics" and action["action_type"] == "query_order" and data.get("status") in (0, 1, 4):
                    goal.update(status="FULFILLED", unmet_reason=None)
            for observation in observations:
                await self._suggest_abnormal(state, ctx, observation)
            state["route"] = "route_goals"
        elif code == "NEED_INPUT":
            missing = outcome.get("data", {}).get("missing", [])
            _question(state, ctx, "请补充已核验的适用信息：" + "、".join(missing), ids, missing[0] if missing else "scope")
        else:
            reason = "UNKNOWN" if code == "WRITE_UNKNOWN" else code
            _mark(state, ids, "DENIED" if code == "DENIED" else "BLOCKED", reason)
            for goal in state["goals"]:
                if goal["goal_id"] in ids and outcome.get("capability_gap"):
                    goal["capability_gap"] = outcome["capability_gap"]
                if goal["goal_id"] in ids and action["action_type"] == "query_sql" and (outcome.get("data") or {}).get("projection_gaps"):
                    goal["projection_gaps"] = outcome["data"]["projection_gaps"]
                if goal["goal_id"] in ids and outcome.get("diagnostic"):
                    goal["diagnostic"] = outcome["diagnostic"]
                    if outcome["diagnostic"] == "POLICY_CONFLICT":
                        goal["conflicts"] = outcome.get("data", {}).get("conflicts", [])
            state["closed_capabilities"].extend({"goal_id": goal, "capability": action["action_type"], "epoch": state["scope_epoch"], "reason": reason} for goal in ids)
            state["route"] = "seal_unknown" if code == "WRITE_UNKNOWN" else "degrade" if code in {"READ_EXHAUSTED", "RETRIEVAL_UNAVAILABLE"} else "halt" if code in {"DENIED", "RESPONSE_MISMATCH", "MODEL_UNAVAILABLE", "BUDGET_EXHAUSTED"} else "route_goals"
        _snapshot(state, ctx)
        finish_tool(ctx, state)
        return state

    async def _suggest_abnormal(self, state, ctx, observation):
        """A verified abnormal fact supports an optional, separately confirmed offer.

        This never changes required query predicates or grants write permission.
        """
        read_only = re.search(r"只查|仅查|不要.*(?:建单|工单|办理)|不(?:要)?建", state["question"])
        data, action = observation["data"], observation["action_type"]
        subjects = []
        if action == "query_order" and data.get("status") in (5, 6):
            subjects = [data["order_id"]]
        elif action == "query_sql":
            subjects = [f["entity_id"] for f in data.get("bound_facts", []) if f["table"] == "orders" and
                        f["field"] == "status" and f["value"] in (5, 6) and f["entity_id"]]
        for subject in dict.fromkeys(subjects):
            if not await asyncio.to_thread(ctx.sql.owns_order, subject, ctx.principal):
                continue
            if await _record_pending_ticket(state, ctx, subject):
                continue
            if read_only or len(state["goals"]) >= 4 or any(g["kind"] == "ticket" and g.get("subject") == subject for g in state["goals"]):
                continue
            obj = {"ref_id": "obj-" + digest([state["task_id"], state["scope_epoch"], subject])[:20], "order_id": subject,
                   "task_id": state["task_id"], "scope_epoch": state["scope_epoch"], "actor": ctx.principal.user_id}
            if obj not in state["verified_objects"]:
                state["verified_objects"].append(obj)
            state["goals"].append({"goal_id": "offer-" + digest([observation["id"], subject])[:12],
                                  "description": f"为异常订单 {subject} 创建售后工单（需确认）", "kind": "ticket", "subject": subject,
                                  "candidate_span": None, "required": False, "status": "OPEN", "unmet_reason": None,
                                  "depends_on": [], "supporting_observation_ids": [],
                                  "subject_scope_bindings": [{"subject": subject, "epoch": state["scope_epoch"]}],
                                  "input_refs": [], "suggestion_source": observation["id"]})
            ctx.run = ctx.store.advance_goals(ctx.run["run_id"], ctx.run["active_invocation_id"], "offer:" + observation["id"] + ":" + subject)
            state["goal_revision"] = ctx.run["goal_revision"]

    async def degrade(self, state, ctx):
        outcome, action = state.get("outcome") or {}, ctx.action or {}
        if outcome.get("class") != "READ_EXHAUSTED" or action.get("action_type") not in {"query_order", "query_logistics"}:
            return state
        # Respect explicit read-only intent; never replace an existing ticket
        # goal (including a conditional one whose prerequisites failed).
        if re.search(r"只查|仅查|(?:不要|不用|无需|不需要|不想).{0,16}(?:建|工单|办理)|不建", state["question"]):
            return state
        subject = action.get("parameters", {}).get("order_id")
        obj = next((o for o in state["verified_objects"] if o["order_id"] == subject
                    and o["task_id"] == state["task_id"] and o["scope_epoch"] == state["scope_epoch"]), None)
        if not obj or len(state["goals"]) >= 4 or any(
            g["kind"] == "ticket" and g.get("subject") in (None, subject) for g in state["goals"]
        ):
            return state
        if await _record_pending_ticket(state, ctx, subject):
            _snapshot(state, ctx)
            return state
        label = "订单" if action["action_type"] == "query_order" else "物流"
        reason = f"{label}查询服务持续失败，请人工核查"
        offer_id = "recovery-" + digest([state["task_id"], state["scope_epoch"], subject])[:12]
        state["goals"].append({
            "goal_id": offer_id, "description": f"为订单 {subject} 的查询服务故障创建人工核查工单（需确认）",
            "kind": "ticket", "subject": subject, "candidate_span": None, "required": False,
            "status": "OPEN", "unmet_reason": None, "depends_on": [], "supporting_observation_ids": [],
            "subject_scope_bindings": [{"subject": subject, "epoch": state["scope_epoch"]}],
            "input_refs": [], "read_failure_reason": reason,
        })
        ctx.run = ctx.store.advance_goals(ctx.run["run_id"], ctx.run["active_invocation_id"], offer_id)
        state["goal_revision"] = ctx.run["goal_revision"]
        _snapshot(state, ctx)
        return state

    async def seal_unknown(self, state, ctx):
        operation_id = state["outcome"].get("operation_id")
        if operation_id and operation_id not in state["unresolved_operation_ids"]:
            state["unresolved_operation_ids"].append(operation_id)
        return state

    async def route_goals(self, state, ctx):
        by_id = {g["goal_id"]: g for g in state["goals"]}
        _block_failed_dependencies(by_id)
        if any(g["status"] == "NEEDS_INPUT" for g in state["goals"]):
            state["route"] = "prepare_clarify"
        elif any(g["status"] == "OPEN" and _dependencies_met(g, by_id) for g in state["goals"]):
            state["route"] = "decide"
        else:
            _mark(state, list(by_id), "BLOCKED", "DEPENDENCY")
            state["route"] = "answer"
        return state

    async def halt(self, state, ctx):
        outcome = state.get("outcome") or {"class": "INVALID_PROPOSAL", "goal_ids": []}
        code = outcome["class"]
        if code == "DENIED":
            _mark(state, outcome["goal_ids"], "DENIED", "DENIED")
        _block_failed_dependencies({g["goal_id"]: g for g in state["goals"]})
        reason = code if code in {"NO_PROGRESS", "BUDGET_EXHAUSTED", "INVALID_PROPOSAL", "MODEL_UNAVAILABLE"} else "TASK_STOPPED"
        _mark(state, [g["goal_id"] for g in state["goals"]], "BLOCKED", reason)
        state["termination_reason"] = code
        ctx.gate_input = ctx.grant = None
        clear_active(state)
        return state

    async def answer(self, state, ctx):
        _mark(state, [g["goal_id"] for g in state["goals"]], "BLOCKED", "TASK_STOPPED")
        state["task_status"] = aggregate(state["goals"], state.get("termination_reason"))
        state["answer"] = render_answer(state)
        clear_active(state)
        ctx.gate_input = ctx.grant = None
        _snapshot(state, ctx)
        return state
