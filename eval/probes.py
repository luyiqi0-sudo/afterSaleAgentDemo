"""Actual existing SQLService probes, NOT LLM/graph or formal Mock evaluation.

Queries and trace conversion below never consult ReferenceOracle or expected data.
Only this harness imports runtime code; eval.oracle remains independent.
"""

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

from agent.contracts import Principal
from agent.guards import SafetyError
from agent.sql import SQLService
from eval.source import read_cases


def run_probes(root: Path) -> dict:
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    dictionary = json.loads((root / "database/dictionary.json").read_text(encoding="utf-8"))
    cases = read_cases(root)
    database = root / "database/ecommerce.db"
    service = SQLService(database)
    observations = {}
    for case in cases:
        if case["id"] not in {f"GOLD-{n:02}" for n in (11, 12, 13, 14, 16, 17, 32, 33, 34, 35, 36)}:
            continue
        principal = Principal(user_id=case["principal_id"])
        queries = []

        def query(sql, params=None):
            result = service.execute(sql, principal, params)
            queries.append({"sql": sql, "parameters": params or {}, "truncated": result.truncated})
            if result.truncated:
                raise ValueError("probe requires complete query result")
            return result.rows

        inputs, kind = case["inputs"], case["kind"]
        if kind == "aggregate":
            end = date.fromisoformat(manifest["anchor_date"]).replace(day=1)
            start = (end - timedelta(days=1)).replace(day=1)
            params = {"lo": f"{start} 00:00:00", "hi": f"{end} 00:00:00"}
            totals = query("SELECT COALESCE(SUM(amount),0) AS amount_cents,COUNT(*) AS order_count FROM orders WHERE create_time>=:lo AND create_time<:hi AND status IN (1,2,3,5,6)", params)[0]
            undated = query("SELECT order_id FROM orders WHERE create_time IS NULL ORDER BY order_id")
            unknown = query("SELECT order_id FROM orders WHERE status IS NULL AND create_time>=:lo AND create_time<:hi ORDER BY order_id", params)
            facts = {**totals, "start": params["lo"], "end": params["hi"], "excluded_missing_time": [r["order_id"] for r in undated],
                     "excluded_unknown_status": [r["order_id"] for r in unknown]}
        elif kind in ("order", "join"):
            row = query("SELECT order_id,amount,status,create_time FROM orders WHERE order_id=:oid", {"oid": inputs["order_id"]})[0]
            row["status_label"] = dictionary["order_status"].get(str(row["status"]), "未知")
            facts = {"order": row}
            if kind == "join":
                rows = query("SELECT l.logistics_id,l.status,l.update_time FROM orders o LEFT JOIN logistics l ON o.order_id=l.order_id WHERE o.order_id=:oid", {"oid": inputs["order_id"]})
                facts["logistics"] = [{**r, "status_label": dictionary["logistics_status"][str(r["status"]) ]} for r in rows if r["logistics_id"] is not None]
        elif kind == "product":
            facts = {"product": query("SELECT sku_id,name,price,stock FROM products WHERE sku_id=:sku", {"sku": inputs["sku_id"]})[0]}
        elif kind == "empty":
            facts = {"rows": query("SELECT order_id FROM orders"), "amount_cents": query("SELECT COALESCE(SUM(amount),0) AS total FROM orders")[0]["total"]}
        elif kind in ("cross_user", "injection_parameter"):
            facts = {"rows": query("SELECT order_id,amount,status FROM orders WHERE order_id=:oid", {"oid": inputs["order_id"]})}
        elif kind == "cross_user_logistics":
            facts = {"rows": query("SELECT logistics_id,status,update_time FROM logistics WHERE order_id=:oid", {"oid": inputs["order_id"]})}
        elif kind == "sql_block":
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            attempts = ["SELECT order_id FROM orders; DROP TABLE orders"] if case["id"] == "GOLD-32" else ["SELECT name FROM sqlite_master", "PRAGMA table_info(orders)"]
            rejected = 0
            for sql in attempts:
                try:
                    query(sql)
                except SafetyError:
                    rejected += 1
                    queries.append({"sql": sql, "rejected": True})
            facts = {"blocked": rejected == len(attempts), "data_unchanged": before == hashlib.sha256(database.read_bytes()).hexdigest()}
        else:
            raise ValueError("unsupported probe")
        observations[case["id"]] = {"facts": facts, "calls": [], "answer": json.dumps(facts, ensure_ascii=False),
                                      "sql_queries": queries, "evidence": {"producer": "eval.probes.SQLService", "scope": "component",
                                                                          "model_used": False, "formal_mock_integration": False}}
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    observations = run_probes(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(observations, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print(json.dumps({"observations": len(observations), "scope": "component"}))


if __name__ == "__main__":
    main()
