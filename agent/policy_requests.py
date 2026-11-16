"""Bounded policy presentation requests, distinct from business evidence.

Only complete metadata clauses are removed from the business question. Unknown
qualifiers remain for coverage checking; numbers reported by users are not facts.
"""
import re


_PREFIX = r"(?:(?:请|并|同时|另外)\s*)*"
_CITATION = re.compile(_PREFIX + r"(?:引用|注明|标注|列出|提供|给出|说明)(?:来源|出处|依据|文档|文件|名|称|段落|锚点|和|与|及|的|、|\s)+")
_DIFFERENCE = re.compile(_PREFIX + r"(?:说明|比较|对比|解释)?(?:新旧|不同|两个)(?:政策|版本|规则)(?:的)?(?:差异|区别|变化|变更|替代关系)")
_CHOICE = re.compile(_PREFIX + r"(?:确认)?(?:当前|现在)?(?:应|应该)?以?(?:哪个|哪一版|哪个版本)(?:为准|适用|有效)(?:[（(][0-9一二三四五六七八九十百两]+个?(?:工作日|自然日|天|日|小时)(?:还是|或)[0-9一二三四五六七八九十百两]+个?(?:工作日|自然日|天|日|小时)[）)])?")
_REPORTED = re.compile(r"有的(?:政策|文档|规则)?说[0-9一二三四五六七八九十百两]+个?(?:工作日|自然日|天|日|小时)")


def policy_request(text):
    """Return the business clauses and required presentation capabilities."""
    clauses, requirements = [], set()
    for clause in re.split(r"[，,；;。？?！!\n]", text):
        clause = clause.strip()
        if not clause:
            continue
        if _CITATION.fullmatch(clause):
            requirements.add("citations")
        elif _CHOICE.fullmatch(clause):
            # Version selection needs applicable evidence, not necessarily a
            # predecessor. The first version can govern a historical date.
            requirements.add("selection")
        elif any(pattern.fullmatch(clause) for pattern in (_DIFFERENCE, _REPORTED)):
            requirements.add("versions")
        else:
            clauses.append(clause)
    return "；".join(clauses), requirements


def merge_policy_presentations(draft):
    """Attach pure output requirements to one explicit policy dependency only.

    Never infer a parent from ordering, or merge an independent business question.
    Every merged requirement remains in its parent's description for verification.
    """
    goals = {goal.id: goal.model_copy(deep=True) for goal in draft.goals}
    aliases = {}
    for _ in range(len(goals)):
        changed = False
        for goal in list(goals.values()):
            body, requirements = policy_request(goal.description)
            if goal.kind != "policy" or goal.subject_candidate or body or not requirements or len(goal.deps) != 1:
                continue
            parent_id = aliases.get(goal.deps[0], goal.deps[0])
            parent = goals.get(parent_id)
            if parent is None or parent.kind != "policy" or not policy_request(parent.description)[0]:
                continue
            combined = parent.description + "；" + goal.description
            if len(combined) > 300:
                continue
            parent.description = combined
            aliases[goal.id] = parent_id
            del goals[goal.id]
            changed = True
        if not changed:
            break
    for goal in goals.values():
        goal.deps = list(dict.fromkeys(aliases.get(dep, dep) for dep in goal.deps))
    # Revalidate the dependency graph after remapping; no derived authorization.
    return type(draft).model_validate({"goals": list(goals.values()), "clarification": draft.clarification})
