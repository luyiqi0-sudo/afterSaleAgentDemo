"""Public execution notices; no model prose or unverified tool payloads."""
from copy import deepcopy

from agent.answers import REASONS, render_answer
from agent.guards import valid_order_id


MAX_SUMMARY_CHARS = 1600
TOOL_LABELS = {
    "query_order": "订单查询服务", "query_logistics": "物流查询服务",
    "query_sql": "业务数据查询", "retrieve_policy": "售后政策检索",
    "execute_confirmed_ticket": "售后工单服务",
}


def emit_stage(ctx, action, goal_ids, subject=None):
    order = f"订单 {subject}" if isinstance(subject, str) and valid_order_id(subject) else "该订单"
    summary = {
        "understand": "正在梳理你的问题，确认需要查询的信息和办理事项。",
        "query_order": f"正在查询{order}的状态和金额。",
        "query_logistics": f"正在查询{order}的物流编号、状态和最近更新时间。",
        "query_sql": "正在查询当前账号的业务数据，并核对统计口径。",
        "retrieve_policy": "正在检索售后政策，并核对适用日期、版本和来源。",
        "execute_confirmed_ticket": f"已收到你的确认，正在提交{order}的售后工单。",
        "request_ticket": f"已准备好{order}的售后工单，等待你确认；尚未提交。",
        "ask_user": "需要补充信息后才能继续处理。",
    }.get(action, "正在处理已核准的操作。")
    ctx.emit("step", {"action": action, "goal_ids": goal_ids, "summary": summary})


def begin_tool(ctx):
    action = ctx.action
    kind = action["action_type"]
    tool = "create_ticket" if kind == "execute_confirmed_ticket" else kind
    ctx.pending_tool = {"tool": tool, "action_ref": action["action_ref"]}
    ctx.emit("tool_start", {"tool": tool, "summary": f"正在调用{TOOL_LABELS[kind]}。"})


def finish_tool(ctx, state):
    """Called only after verify has projected AND saved this action's facts."""
    pending = ctx.pending_tool
    if pending is None:
        return  # Cached observation reuse is not another tool invocation.
    action, outcome = ctx.action, state["outcome"]
    if action is None or action.get("action_ref") != pending["action_ref"]:
        return  # Controller will deliver an interrupted-verification notice.
    code = outcome["class"]
    per_goal = outcome.get("goal_outcomes", {})
    codes = [per_goal.get(goal_id, code) for goal_id in outcome["goal_ids"]]
    failures = [value for value in codes if value != "VERIFIED_OK"]
    status = ("PARTIAL" if "VERIFIED_OK" in codes else failures[0]) if failures else code
    goals = []
    for goal in state["goals"]:
        if goal["goal_id"] not in outcome["goal_ids"]:
            continue
        local = deepcopy(goal)
        result = per_goal.get(goal["goal_id"], code)
        # Describe this tool's result, not completion of a broader goal. For
        # example, order lookup can succeed before logistics has been queried.
        local.update(status="FULFILLED" if result == "VERIFIED_OK" else "BLOCKED",
                     unmet_reason="UNKNOWN" if result == "WRITE_UNKNOWN" else result)
        goals.append(local)
    observations = [o for o in state["observations"] if o.get("action_ref") == pending["action_ref"]
                    and o["task_id"] == state["task_id"] and o["scope_epoch"] == state["scope_epoch"]]
    summary = render_answer({**state, "goals": goals, "observations": observations,
                             "unresolved_operation_ids": []}, include_descriptions=False)
    if len(summary) > MAX_SUMMARY_CHARS:
        # Never cut a policy claim away from its citation/conditions or cut a
        # number in half. Long results remain complete in the final answer.
        detail = ("已取得并核验本次结果。" if status == "VERIFIED_OK" else
                  "本次结果仍有未满足的问题。" + "".join(dict.fromkeys(
                      REASONS.get("UNKNOWN" if value == "WRITE_UNKNOWN" else value, "此项尚未完成。")
                      for value in failures)))
        summary = detail + "内容较长，此处省略明细；完整结果及来源将在最终答复中展示。"
    data = {"tool": pending["tool"], "status": status, "summary": summary}
    if "attempts" in outcome:
        data["attempts"] = outcome["attempts"]
    ctx.pending_tool = None
    ctx.emit("tool_end" if status in {"VERIFIED_OK", "PARTIAL"} else "tool_error", data)


def interrupt_tool(ctx, reason):
    """A hard failure cannot publish raw data or guess an unverified outcome."""
    if ctx.pending_tool:
        tool = ctx.pending_tool["tool"]
        summary = ("工单提交结果的核验未完成，请以最终操作状态为准，勿重复提交。" if tool == "create_ticket" else
                   "本次调用的结果核验未完成，无法交付已确认的结果。")
        ctx.pending_tool = None
        ctx.emit("tool_error", {"tool": tool, "status": reason, "summary": summary})
