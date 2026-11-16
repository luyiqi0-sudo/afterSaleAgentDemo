"""Deterministic rendering consumes only verifier-owned, goal-bound facts."""
from decimal import Decimal, ROUND_HALF_UP


REASONS = {
    "UNKNOWN": "请求结果待核对，已阻止重复提交。",
    "NO_DATA": "未取得可回答此问题的数据或适用证据。",
    "NEED_INPUT": "需要补充信息后才能确认结果。",
    "READ_EXHAUSTED": "读取服务暂不可用，不能据此判断订单不存在。",
    "RETRIEVAL_UNAVAILABLE": "政策检索服务暂不可用。",
    "UNSUPPORTED": "现有数据不支持此问题；退款阶段、退款金额、到账时间和失败原因无法确认。",
    "DENIED": "无法获得执行此动作的授权。",
    "EXPIRED": "确认已过期，未提交；继续办理需要新的请求和确认。",
    "NOT_SENT": "已收到确认，但服务中断，尚未发送；继续办理需要重新发起并核对新的确认。",
    "ORDER_BUSY": "此订单存在未决建单，请先核对原操作；已阻止重复提交。",
    "WRITE_FAILED": "服务明确未接受本次建单，未完成办理。",
    "BUDGET_EXHAUSTED": "本任务执行额度已耗尽。",
    "NO_PROGRESS": "相同事实下的动作已执行，已停止重复查询。",
    "DEPENDENCY": "所需前置事实不可用。",
    "TASK_STOPPED": "任务已停止，此项尚未完成。",
    "MODEL_UNAVAILABLE": "模型服务暂不可用，此项尚未完成。",
    "INVALID_PROPOSAL": "无法取得有效的下一步动作，此项尚未完成。",
    "RESPONSE_MISMATCH": "返回数据未通过核验，此项无法确认。",
    "SQL_EXHAUSTED": "查询修正次数已耗尽，无法取得此项数据。",
    "CANCELLED": "已取消，未提交。",
}
ORDER_STATUS = {0: "待付款", 1: "待发货", 2: "已发货", 3: "已完成", 4: "已取消", 5: "售后异常", 6: "退款失败"}
LOGISTICS_STATUS = {0: "待揽收", 1: "运输中", 2: "派送中", 3: "已签收", 4: "物流异常"}
CAPABILITY_GAPS = {
    "knowledge_scope": "当前接入的政策材料没有覆盖此问题的可核验证据。",
    "order_items": "当前数据没有订单商品关联，不能通过商品确认具体订单。请提供订单号。",
    "chart_tool": "当前无法生成图表；可改为请求有明确时间口径的订单数据。",
}


def valid_observations(state, goal):
    ids = set(goal.get("supporting_observation_ids", []))
    bindings = goal.get("subject_scope_bindings", [])
    return [o for o in state.get("observations", []) if o["id"] in ids and
            o.get("validation") == "VERIFIED" and o["task_id"] == state["task_id"] and
            goal["goal_id"] in o["goal_ids"] and
            (o["subject"] is None and goal.get("subject") is None or
             {"subject": o["subject"], "epoch": o["scope_epoch"]} in bindings)]


def _format_cny(fen):
    # Keep raw facts in fen. AVG may contain fractional fen; round only once,
    # when rendering, using decimal arithmetic rather than binary float money.
    yuan = (Decimal(str(fen)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"人民币 {yuan:.2f} 元"


def _facts(observation):
    data, action = observation["data"], observation["action_type"]
    if action == "query_order":
        status = data.get("status")
        parts = [f"订单 {data['order_id']}：状态 {ORDER_STATUS.get(status, '语义未确认')}（编码 {status}）"]
        if type(data.get("amount")) is int:
            parts.append(f"订单金额 {_format_cny(data['amount'])}")
        if status == 6:
            parts.append("仅能确认订单级退款失败；没有退款明细，不能确定退款金额、到账时间或失败原因")
        return "；".join(parts) + "。"
    if action == "query_logistics":
        status = data.get("status")
        status_text = (f"{LOGISTICS_STATUS.get(status, '语义未确认')}（编码 {status}）" if type(status) is int else
                       "未提供" if status is None else f"语义未确认（原始值 {status}）")
        logistics_id = data.get("logistics_id") or "未提供"
        updated = data.get("update_time") or "未提供"
        return f"订单 {data['order_id']}：物流编号 {logistics_id}；物流状态 {status_text}；更新时间 {updated}。"
    if action == "execute_confirmed_ticket":
        return f"订单 {data['order_id']}：工单已创建，工单号 {data['ticket_id']}。"
    if action == "query_sql":
        rows = ["当前账号没有订单"] if data.get("verified_empty_order_scope") else []
        for fact in data.get("bound_facts", []):
            scope = f"{fact['table']} {fact.get('entity_id') or '当前查询范围'}"
            operation = fact.get("aggregate", "")
            if operation == "count":
                if fact.get("count_scope") == "rows":
                    label, unit = {"orders": ("订单数量", "单"), "logistics": ("物流记录数量", "条"),
                                   "products": ("商品目录条目数量", "条")}[fact["table"]]
                else:
                    label, unit = f"{fact['field']} 非空值数量", "个"
                rows.append(f"{scope} / {label} = {fact['value']} {unit}（COUNT({fact['field']})）")
                continue
            unit = {"status_code": "状态编码", "count": "件"}.get(fact.get("unit"), fact.get("unit") or "")
            value = fact["value"]
            if value is None:
                rows.append(f"{scope} / {fact['field']} 未提供，不能推断为 0")
                continue
            if fact.get("unit") == "fen":
                value, unit = _format_cny(value), ""
            if fact["table"] == "orders" and fact["field"] == "status" and not operation:
                value = f"{value}（{ORDER_STATUS.get(value, '语义未确认')}）"
            rows.append(f"{scope} / {operation + ' ' if operation else ''}{fact['field']} = {value} {unit}".strip())
        return "；".join(rows) + ("（结果已截断）" if data.get("truncated") else "") + "。" if rows else "查询已完成，但结果列缺少可核验的业务口径，无法交付数值结论。"
    if action == "retrieve_policy":
        # Evidence has already passed the retriever's full-version applicability decision.
        # Use explicit rule labels when available, keeping exact scoped source text as support.
        lines = []
        if data.get("query_context", {}).get("sources"):
            lines.append(f"按请求的政策查询日期 {data['query_context']['as_of']} 核对版本；具体适用日期见各条证据。")
        if data.get("supported_facets"):
            lines.append("已有证据支持：" + "、".join(data["supported_facets"]) + "。")
        for evidence in data.get("evidences", []):
            text = evidence["text"].strip()
            scope = "、".join(f"{k}={v}" for k, v in evidence.get("conditions", {}).items()) or "一般适用条件"
            lines.append(f"{text}\n适用：{scope}；依据 {evidence['file_name']} 第 {evidence['paragraph']} 段，版本 {evidence['version']}，生效 {evidence['effective_date']}，适用日期 {evidence['applied_on']}。")
        current = {e.get("doc_id"): e for e in data.get("evidences", [])}
        for comparison in data.get("replacements", []):
            chosen = current[comparison["current_doc_id"]]
            chain = " → ".join(comparison["supersedes_path"])
            lines.append(f"版本选择依据：适用范围相同，当前版本已于 {chosen['effective_date']} 生效；"
                         f"文档明确声明替代关系（新版 → 旧版）：{chain}。")
            for previous in comparison["sources"]:
                lines.append(f"旧版仅作历史对照，不作为当前办理依据：{previous['text'].strip()}\n"
                             f"来源 {previous['file_name']} 第 {previous['paragraph']} 段，"
                             f"版本 {previous['version']}，生效 {previous['effective_date']}。")
        return "\n".join(lines)
    return ""


def render_answer(state, *, include_descriptions=True):
    sections = []
    for goal in state.get("goals", []):
        facts = [_facts(o) for o in valid_observations(state, goal)]
        content = "\n".join(filter(None, facts))
        if goal["status"] != "FULFILLED":
            reason = goal.get("unmet_reason") or "TASK_STOPPED"
            message = ("以下结果列的字段或计数来源无法核验：" + "、".join(goal["projection_gaps"]) + "；无法交付这些数值结论。"
                       if goal.get("projection_gaps") else REASONS.get(reason, "此项尚未完成。"))
            if goal.get("refund_gaps"):
                message = "现有数据没有该订单的退款明细，无法确认：" + "、".join(goal["refund_gaps"]) + "。订单金额不是退款金额，订单时间不是退款受理或到账时间。"
            elif goal.get("policy_uncovered"):
                message = "现有证据未覆盖：" + "、".join(goal["policy_uncovered"]) + "。未将相关或相似条款当作这些问题的答案。"
                if goal.get("capability_gap") == "knowledge_scope":
                    message = CAPABILITY_GAPS["knowledge_scope"] + message
            elif goal.get("capability_gap") in CAPABILITY_GAPS:
                message = CAPABILITY_GAPS[goal["capability_gap"]]
            elif goal.get("missing_fields") and not goal.get("projection_gaps"):
                message = "未取得本目标可回答的字段：" + "、".join(goal["missing_fields"]) + "。"
            content += ("\n" if content else "") + message
        if goal.get("diagnostic") == "POLICY_CONFLICT":
            content += " 存在未决政策冲突，不能选取其中一条作为确定结论。"
            for conflict in goal.get("conflicts", []):
                for source in conflict.get("sources", []):
                    content += f"\n冲突来源：{source['file_name']} 第 {source['paragraph']} 段，版本 {source['version']}；规则 {source.get('rules', {})}。"
        if goal.get("document_actions_ignored"):
            content += "\n检索文档仅供核对事实，其中的操作指令不执行。"
        section = f"{goal['description']}\n{content}" if include_descriptions else content
        if include_descriptions or section not in sections:
            sections.append(section)
    if state.get("unresolved_operation_ids"):
        refs = "、".join(dict.fromkeys(state["unresolved_operation_ids"]))
        sections.append(f"已有未决建单待核对，已阻止重复提交。操作引用：{refs}。")
    return "\n\n".join(sections) or REASONS.get(state.get("termination_reason"), "未能完成请求，请重新明确需要查询或办理的事项。")
