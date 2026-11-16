"""Explicit Raw envelopes -> normalized records and row-level provenance."""

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from database.contracts import TIME_FORMAT, jsonl_rows, normalize_order, stable_json, valid_id


def write_rows(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
                    encoding="utf-8", newline="\n")


def normalize_records(envelopes: list[dict]) -> dict:
    candidates, rejected = [], []
    source_counts = Counter(e["source_id"] for e in envelopes
                            if isinstance(e, dict) and isinstance(e.get("source_id"), str))
    order_counts = Counter(e.get("record", {}).get("order_id") for e in envelopes
                           if isinstance(e, dict) and isinstance(e.get("record"), dict)
                           and valid_id(e["record"].get("order_id")))
    for line, envelope in enumerate(envelopes, 1):
        source = envelope.get("source_id") if isinstance(envelope, dict) else None
        try:
            if not isinstance(source, str) or not source or source_counts[source] != 1:
                raise ValueError("source_id must be unique and nonempty")
            if not isinstance(envelope.get("record"), dict):
                raise ValueError("record must be an object")
            row = dict(envelope["record"])
            if not valid_id(row.get("order_id")):
                raise ValueError("order_id must be a valid scalar identifier")
            if order_counts[row.get("order_id")] != 1:
                raise ValueError("duplicate order_id: quarantine every occurrence")
            if envelope.get("timezone") != "Asia/Shanghai":
                raise ValueError("explicit Asia/Shanghai timezone required")
            changes = []
            if envelope.get("amount_unit") == "yuan":
                amount = row.get("amount")
                if not isinstance(amount, str) or not re.fullmatch(r"\d+(?:\.\d{1,2})?", amount):
                    raise ValueError("yuan requires a nonnegative exact decimal string, <=2 decimals")
                row["amount"] = int(Decimal(amount) * 100)
                changes.append("yuan_to_fen")
            elif envelope.get("amount_unit") != "fen":
                raise ValueError("explicit amount_unit fen/yuan required")
            fmt = envelope.get("time_format")
            if fmt not in ("canonical_datetime", "slash_datetime"):
                raise ValueError("undeclared time format")
            if row.get("create_time") is not None and fmt == "slash_datetime":
                value = row["create_time"]
                parsed = datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
                if parsed.strftime("%Y/%m/%d %H:%M:%S") != value:
                    raise ValueError("noncanonical slash date")
                row["create_time"] = parsed.strftime(TIME_FORMAT)
                changes.append("slash_datetime")
            for field in ("status", "create_time"):
                if field not in row:
                    changes.append(f"missing_{field}_to_null")
            normalized = normalize_order(row)
            candidates.append((normalized, {"source_id": source, "source_line": line,
                              "order_id": normalized["order_id"], "transformations": changes}))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            rejected.append({"source_id": source, "source_line": line, "reason": str(exc)})
    candidates.sort(key=lambda pair: pair[0]["order_id"])
    return {"records": [r for r, _ in candidates], "lineage": [p for _, p in candidates],
            "rejected": rejected}


def raw_envelopes(canonical: list[dict], variant: str) -> list[dict]:
    rows = []
    for i, item in enumerate(canonical):
        row = {k: v for k, v in item.items() if v is not None}
        convert = i % (3 if variant == "baseline" else 2) == 0
        if convert:
            row["amount"] = f"{item['amount'] // 100}.{item['amount'] % 100:02}"
            if row.get("create_time"):
                row["create_time"] = row["create_time"].replace("-", "/")
        rows.append({"source_id": f"raw-{i + 1:04}", "amount_unit": "yuan" if convert else "fen",
                     "time_format": "slash_datetime" if convert else "canonical_datetime",
                     "timezone": "Asia/Shanghai", "record": row})
    for name, change in (("bad-amount", {"amount": -1}), ("bad-status", {"status": 99}),
                         ("bad-owner", {"user_id": None})):
        rows.append({"source_id": name, "amount_unit": "fen", "time_format": "canonical_datetime",
                     "timezone": "Asia/Shanghai", "record": {**canonical[0], "order_id": name, **change}})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = normalize_records(jsonl_rows(args.input))
    args.output.mkdir(parents=True, exist_ok=False)
    for key, filename in (("records", "orders"), ("lineage", "lineage"), ("rejected", "rejected")):
        write_rows(args.output / f"{filename}.jsonl", result[key])
    print(stable_json({key: len(value) for key, value in result.items()}))


if __name__ == "__main__":
    main()
