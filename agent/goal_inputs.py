"""Fill an order clarification without replacing the existing task graph."""
import re

from agent.guards import valid_order_id
from agent.state import TERMINAL_GOALS


def supply_single_order_reply(goals, intent, message, full_question, message_id):
    """Fill one frozen read clarification from a complete, unambiguous reply.

    No task replanning, ownership assertion or tool grant is produced here.
    Multi-goal tasks and replies carrying more than this slot use the existing
    model path; an unrelated outstanding request must not receive the same ID.
    """
    payload = intent["payload"]
    active = [g for g in goals if g.get("required", True) and g["status"] not in TERMINAL_GOALS]
    if (intent["kind"] != "CLARIFY" or payload.get("field") != "order_id"
            or payload.get("fields", ["order_id"]) != ["order_id"] or len(active) != 1):
        return None
    goal = active[0]
    if (intent["goal_ids"] != [goal["goal_id"]] or goal["status"] != "NEEDS_INPUT"
            or goal.get("subject") or goal["kind"] not in {"sql", "order", "logistics"}):
        return None
    match = re.fullmatch(
        r"\s*(?:(?:订单(?:号|编号)?|单号)\s*(?:是|为|[:：])?\s*)?"
        r"(?P<subject>[A-Za-z0-9][A-Za-z0-9_-]*)"
        r"(?:\s*[，,]?\s*(?:请)?继续(?:查询)?)?[。.!！]?\s*", message)
    if not match or not valid_order_id(match["subject"]) or not full_question.endswith(message):
        return None
    offset = len(full_question) - len(message)
    span = {"start": offset + match.start("subject"), "end": offset + match.end("subject")}
    goal.update(subject=match["subject"], candidate_span=span, status="OPEN", unmet_reason=None)
    if message_id not in goal["input_refs"]:
        goal["input_refs"].append(message_id)
    return {"goal_id": goal["goal_id"], "order_id": match["subject"], "span": span}


def supply_order_subjects(goals, candidates, message, message_id):
    """Bind waiting goals and explicitly matched, same-order dependents.

    IDs/kinds identify goals, never list positions. Independent missing objects,
    known objects, terminal goals and dependency edges are not rewritten. These
    remain candidates: the execution gate still validates source and ownership.
    """
    proposed = {g["goal_id"]: g for g in candidates}
    supplied = {}

    def match(goal):
        candidate = proposed.get(goal["goal_id"])
        if candidate is None or candidate["kind"] != goal["kind"] or not candidate.get("subject"):
            return None
        subject = candidate["subject"]
        if goal.get("subject") not in (None, subject):
            return None
        if not re.search(r"(?<![A-Za-z0-9_-])" + re.escape(subject) + r"(?![A-Za-z0-9_-])", message):
            return None
        return candidate

    def apply(goal, candidate):
        goal.update(subject=candidate["subject"], candidate_span=candidate["candidate_span"], status="OPEN", unmet_reason=None)
        if message_id not in goal["input_refs"]:
            goal["input_refs"].append(message_id)
        supplied[goal["goal_id"]] = candidate["subject"]

    for goal in goals:
        if goal["status"] == "NEEDS_INPUT" and (candidate := match(goal)):
            apply(goal, candidate)
    for _ in range(len(goals)):
        changed = False
        for goal in goals:
            deps = goal["depends_on"]
            if (goal["status"] != "OPEN" or goal.get("subject") or goal["kind"] not in {"order", "logistics"}
                    or not deps or not all(dep in supplied for dep in deps)):
                continue
            subjects = {supplied[dep] for dep in deps}
            candidate = match(goal)
            if candidate and subjects == {candidate["subject"]}:
                apply(goal, candidate)
                changed = True
        if not changed:
            break
