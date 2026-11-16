"""Render a local, read-only view of captured redacted trace observations."""
import argparse
import html
import json
from pathlib import Path


def _show(row):
    event = row["event"]
    if event == "model_usage":
        usage = row.get("usage") or {}
        return f"{row['operation']} · {row['status']} · {usage.get('total_tokens', '未报告')} tokens · {row['latency_ms']} ms"
    if event == "http_send":
        return f"{row['method']} · {row['tool']} · {row['order_id']}"
    if event == "http_response":
        return f"{row['route']} · HTTP {row['http_status']}" + (f" · {row['ticket_id']}" if row.get("ticket_id") else "")
    if event == "operation":
        op = row.get("operation") or {}
        return f"{op.get('state', '未建立')} · {op.get('attempt_count', 0)} 次发送记录"
    if event == "policy_retrieval":
        return " · ".join([row["status"], *dict.fromkeys(e["file_name"] for e in row.get("evidences", []))])
    if event == "sql_execution":
        return f"{row['outcome']} · 只读 SQL 已执行"
    if event == "delivery":
        return f"{row.get('request_status')} / {row.get('status')} · persisted={row.get('persisted')} · {row['latency_ms']} ms"
    return None


def _detail(obs):
    trace = obs["trace"]
    sql = next((r for r in trace if r["event"] == "sql_execution" and r.get("result")), None)
    if sql:
        provider = next(r for r in trace if r["event"] == "model_usage" and r.get("operation") == "sql")
        messages = {m["role"]: m["content"] for m in provider["request_messages"]}
        inputs = json.loads(messages["user"])
        result = sql["result"]
        return [
            ("system 摘录", messages["system"][:125] + "…"),
            ("user 摘录", inputs["question"]),
            ("候选 SQL", provider["structured_candidate"]["sql"]),
            ("实际执行 SQL", result["executed_sql"]),
            ("执行返回", json.dumps(result["rows"], ensure_ascii=False)),
        ]
    evidence = next(r["evidences"][0] for r in trace if r["event"] == "policy_retrieval" and r.get("evidences"))
    digest = evidence["content_hash"]
    return [
        ("原文", evidence["text"]),
        ("来源", f"{evidence['file_name']} / 第 {evidence['paragraph']} 段 / {evidence['anchor']}"),
        ("生效与适用", f"{evidence['effective_date']} 生效 · {evidence['applied_on']} 适用"),
        ("内容 SHA-256", digest[:32] + " " + digest[32:]),
    ]


def render(inputs, *, detail=False):
    cards = []
    for path, case_id in inputs:
        obs = json.loads(Path(path).read_text(encoding="utf-8"))[case_id]
        events = []
        pairs = _detail(obs) if detail else [(row["event"], _show(row)) for row in obs["trace"]]
        for field, value in pairs:
            if value is not None:
                events.append(f"<li><b>{html.escape(field)}</b><span>{html.escape(value)}</span></li>")
        profile = obs["evidence"]["profile"]
        if profile == "real":
            has_embedding = any(row.get("operation") == "embed" for row in obs["trace"])
            title = "真实 Qwen + embedding" if has_embedding else "真实 Qwen"
        else:
            title = "受控模型 + 真实冻结 Mock"
        cards.append(f"<article><h2>{html.escape(case_id)} · {title}</h2><ul>{''.join(events)}</ul></article>")
    subtitle = ("真实执行摘录：左侧为脱敏 SQL 请求及执行，右侧为实际发票政策片段。"
                if detail else "读取脱敏 Trace。真实模型与受控模型分别标记；未显示请求正文、凭据或隐藏推理。")
    return """<!doctype html><html lang="zh-CN"><meta charset="UTF-8">
<title>售后 Agent · 实际链路记录</title><style>
body{font:16px/1.55 'Microsoft YaHei',sans-serif;background:#f2f5f8;color:#1a2733;padding:30px 42px;margin:0}
h1{margin:0;font-size:36px;letter-spacing:-.5px}p{margin:8px 0 24px;color:#526275}
main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}
article{padding:20px 24px;background:#fff;border:1px solid #d2dae4;border-radius:14px}
h2{font-size:21px;margin:0 0 12px}ul{margin:0;padding:0;list-style:none}
li{display:grid;grid-template-columns:118px 1fr;gap:12px;border-top:1px solid #e2e8f0;padding:9px 0;overflow-wrap:anywhere}
b{font:600 13px/1.8 Consolas,monospace;color:#264c78}span{font-size:14px}
</style><h1>售后 Agent · 实际链路记录</h1>
<p>""" + html.escape(subtitle) + "</p><main>" + "".join(cards) + "</main></html>"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", nargs=2, action="append", metavar=("OBSERVATIONS_JSON", "CASE_ID"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--detail", action="store_true")
    args = parser.parse_args()
    args.output.write_text(render(args.case, detail=args.detail), encoding="utf-8")


if __name__ == "__main__":
    main()
