"""Cheap candidates for an explicitly linked order + logistics read.

This module never dispatches a tool or grants access. Its candidates use the
same ActionProposal and execution gate as model-generated candidates.
"""
import re


_QUERY_METHOD = r"\bSQL\b|\bJOIN\b|联表|多表|关联.{0,8}表"
_MODEL_CHOICE = re.compile(_QUERY_METHOD + r"|统计|合计|总额|平均|分组|如果|若|仅当|只有|否则|不要|不用|不查", re.I)


def linked_read_proposal(state, ready):
    # Keep an explicit SQL/Join request even if goal paraphrasing omitted it.
    if re.search(_QUERY_METHOD, state["question"], re.I):
        return None
    groups = {}
    for goal in state["goals"]:
        if (goal["kind"] in {"order", "logistics"} and goal.get("subject")
                and goal["status"] in {"OPEN", "FULFILLED"} and goal.get("required", True)):
            groups.setdefault(goal["subject"], []).append(goal)
    groups = {subject: group for subject, group in groups.items()
              if {g["kind"] for g in group} == {"order", "logistics"}
              and not any(_MODEL_CHOICE.search(g["description"]) for g in group)
              and any(g["goal_id"] in ready for g in group)}
    if len(groups) != 1:
        return None  # Leave multi-object choice and unlinked work to the model.
    subject, group = next(iter(groups.items()))
    current = [o for o in state["observations"] if o["task_id"] == state["task_id"]
               and o["scope_epoch"] == state["scope_epoch"] and o["validation"] == "VERIFIED"
               and o["subject"] == subject and o["action_type"] == "query_order"]
    targets = [g for g in group if g["goal_id"] in ready]
    logistics = [g for g in targets if g["kind"] == "logistics"]
    action = "query_logistics" if current and logistics else "query_order"
    if action == "query_logistics":
        targets = logistics
    obj = next((o for o in state["verified_objects"] if o["order_id"] == subject
                and o["task_id"] == state["task_id"] and o["scope_epoch"] == state["scope_epoch"]), None)
    if obj:
        source = {"type": "object", "ref_id": obj["ref_id"]}
    else:
        candidate = next((c for c in state["candidate_objects"] if c["order_id"] == subject and c.get("span")), None)
        if candidate is None:
            return None
        source = {"type": "message", "span": candidate["span"]}
    return {"action_type": action, "goal_ids": [g["goal_id"] for g in targets],
            "parameters": {"order_id": subject}, "parameter_sources": {"order_id": source},
            "based_on_observation_ids": [current[-1]["id"]] if current else [],
            "public_summary": "查询同一订单的物流信息" if action == "query_logistics" else "查询订单信息并核验物流前置条件"}
