"""Explicit deterministic model candidates for mechanism tests; no expected facts."""
import re

_TOPICS = (
    r"拆封|卫生|特殊商品",
    r"承担.{0,4}运费|谁.{0,4}运费|退货运费",
    r"发票.{0,8}(?:时限|多久|开具)|电子发票",
    r"保修",
    r"首次响应|工单.{0,6}(?:响应|多久)",
    r"换货",
    r"普通商品.{0,6}退货|退货期限",
)
_ORDER = re.compile(r"(?:EVAL|ACC)-[0-9]{3}")


class ControlledModel:
    def __init__(self, case=None):
        self.case = case or {}

    def with_emit(self, callback):
        return self

    async def embed(self, texts, *, budget=None):
        result = []
        for text in texts:
            first = text.split("\n", 1)[0]
            i = next((j for j, p in enumerate(_TOPICS) if re.search(p, first)), 15)
            vector = [0.] * 16
            vector[i] = 1.
            result.append(vector)
        return result

    async def understand_goal(self, question, context, *, budget=None):
        kind = self.case["kind"]
        subject = _ORDER.search(question)
        group = {
            "policy": "policy", "policy_injection": "policy", "knowledge_gap": "policy",
            "order": "sql", "aggregate": "sql", "empty": "sql", "product": "sql",
            "join": "logistics", "tool_chain": "logistics", "retry_500": "logistics",
            "read_failure": "logistics", "cross_user_logistics": "logistics",
            "self_correction": "sql", "product_order_gap": "sql", "chart_gap": "sql",
            "sql_block": "sql", "confirm_ticket": "order", "decline_ticket": "order",
            "duplicate_ticket": "order", "unknown_write": "order", "clarify_order": "order",
            "clarify_ticket": "ticket",
        }.get(kind, "order")
        if self.case.get("id") == "D07":
            group = "ticket"
        if self.case.get("id") == "D05" and subject:
            span = {"start": subject.start(), "end": subject.end()}
            return {"goals": [
                {"id": "g1", "description": f"查询订单 {subject.group()} 的状态", "kind": "order",
                 "subject_candidate": subject.group(), "source_span": span},
                {"id": "g2", "description": f"为订单 {subject.group()} 创建工单", "kind": "ticket",
                 "subject_candidate": subject.group(), "source_span": span},
            ]}
        return {"goals": [{"id": "g1", "description": question[:300], "kind": group,
                           "subject_candidate": subject.group() if subject else None,
                           "source_span": {"start": subject.start(), "end": subject.end()} if subject else None}]}

    async def decide_next(self, context, *, budget=None):
        q = context["question"]
        goal = next(g for g in context["goals"] if g["status"] == "OPEN")
        subject, kind = goal["subject"], goal["kind"]
        sources, based = {}, []
        if kind == "policy":
            action, params = "retrieve_policy", {"question": q}
        elif kind == "sql":
            action, params = "query_sql", {"question": q}
        elif not subject:
            action, params = "ask_user", {"question": "请提供订单号及需要核查的问题。", "field": "order_id"}
        else:
            action = ("request_ticket" if kind == "ticket" else
                      "query_logistics" if kind == "logistics" and "query_logistics" in context["allowed_actions"] else
                      "query_order")
            params = {"order_id": subject}
            if action == "request_ticket":
                reason = self.case.get("inputs", {}).get("reason")
                if reason:
                    params["reason"] = reason
            if subject in q:
                start = q.index(subject)
                sources["order_id"] = {"type": "message", "span": {"start": start, "end": start + len(subject)}}
            else:
                obj = next(o for o in context["objects"] if o["order_id"] == subject)
                sources["order_id"] = {"type": "object", "ref_id": obj["ref_id"]}
            if action == "query_logistics":
                based = [o["id"] for o in context["observations"] if o["subject"] == subject and o["action_type"] == "query_order"]
        return {"action_type": action, "goal_ids": [goal["goal_id"]], "parameters": params,
                "parameter_sources": sources, "based_on_observation_ids": based, "public_summary": "核查用户所问事实"}

    async def sql(self, question, schema, error=None, *, budget=None):
        kind = self.case["kind"]
        if kind == "aggregate":
            sql = ("SELECT SUM(amount) AS amount_cents, COUNT(*) AS order_count FROM orders "
                   "WHERE create_time >= '2026-10-01 00:00:00' AND create_time < '2026-11-01 00:00:00' "
                   "AND status IN (1,2,3,5,6)")
        elif kind == "product":
            sku = re.search(r"SKU-[0-9]+", question).group()
            sql = f"SELECT sku_id, name, price, stock FROM products WHERE sku_id = '{sku}'"
        elif kind == "self_correction":
            field = "amount" if error else "refund_amount"
            sql = f"SELECT order_id, {field} FROM orders WHERE status = 6 ORDER BY order_id"
        elif kind == "sql_block":
            sql = "SELECT name FROM sqlite_master"
        elif kind == "empty":
            sql = "SELECT order_id, amount FROM orders"
        elif kind in {"product_order_gap", "chart_gap"}:
            sql = "SELECT order_id FROM order_items" if kind == "product_order_gap" else "SELECT strftime('%Y-%m', create_time), SUM(amount) FROM orders GROUP BY 1"
        else:
            subject = _ORDER.search(question).group()
            sql = f"SELECT order_id, amount, status, create_time FROM orders WHERE order_id = '{subject}'"
        return sql
