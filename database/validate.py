"""Independent read-only checks for a generated C-002 dataset."""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path, PurePosixPath

import yaml

from database.catalog import POLICIES, coverage_manifest, policy_documents
from database.contracts import (
    COMPATIBLE_LOGISTICS,
    EFFECTIVE_ORDER_STATUSES,
    LOG_COUNTS,
    LOGISTICS_FIELDS,
    LOGISTICS_STATUS,
    ORDER_FIELDS,
    PRODUCT_FIELDS,
    SOURCE,
    SPLIT_COUNTS,
    VERSION,
    dictionary,
    data_start,
    integer_in_range,
    jsonl_rows,
    month_bounds,
    normalize_order,
    parse_time,
    sha256,
    stable_json,
    valid_id,
)


class DataValidationError(ValueError):
    pass


def require(condition, message: str):
    if not condition:
        raise DataValidationError(message)


def indexed(rows: list[dict], key: str) -> dict:
    require(all(isinstance(r, dict) and valid_id(r.get(key)) for r in rows), f"非法 {key}")
    result = {r[key]: r for r in rows}
    require(len(result) == len(rows), f"重复 {key}")
    return result


def _files(root: Path, manifest: dict, verify_hashes: bool):
    require(
        manifest.get("contract_version") == VERSION and manifest.get("source_type") == SOURCE,
        "数据版本或来源不符",
    )
    require(manifest.get("formal_mock_integration") is False, "不能声明正式 Mock 已集成")
    require(type(manifest.get("seed")) is int and 0 <= manifest["seed"] < 2**63, "非法随机种子")
    require(manifest.get("timezone") == "Asia/Shanghai", "时区不符")
    files = manifest.get("files", {})
    require(isinstance(files, dict) and bool(files), "缺少文件清单")
    all_paths = list(root.rglob("*"))
    require(not any(p.is_symlink() for p in all_paths), "数据集内不允许符号链接")
    actual = {
        p.relative_to(root).as_posix()
        for p in all_paths
        if p.is_file() and p != root / "manifest.json"
    }
    require(actual == set(files), "文件清单不一致（新增、丢失或未登记文件）")
    for name, expected in files.items():
        path = PurePosixPath(name)
        require(
            not path.is_absolute() and ".." not in path.parts and "\\" not in name,
            "文件清单路径越界",
        )
        file = root / name
        require(file.resolve().is_relative_to(root.resolve()), "文件路径越界")
        if verify_hashes:
            require(sha256(file) == expected, f"文件摘要不一致：{name}")


def _business(root: Path, as_of: date) -> tuple[dict, dict, dict, dict]:
    users = indexed(jsonl_rows(root / "users.jsonl"), "user_id")
    raw_orders = jsonl_rows(root / "orders.jsonl")
    orders = indexed([normalize_order(r) for r in raw_orders], "order_id")
    logistics = indexed(jsonl_rows(root / "logistics.jsonl"), "logistics_id")
    products = indexed(jsonl_rows(root / "products.jsonl"), "sku_id")
    require(len(orders) == 200, "订单数量必须为 200")
    require(len(products) == 24, "商品数量必须为 24")
    require(
        all(
            u.get("source_type") == SOURCE and u.get("split") in SPLIT_COUNTS
            for u in users.values()
        ),
        "用户来源或分组不符",
    )
    require(
        all(u.get("conditions") == {"channel": "online", "region": "CN"} for u in users.values()),
        "用户适用条件不完整",
    )
    require(all(o["user_id"] in users for o in orders.values()), "订单归属用户不存在")
    require(
        dict(Counter(users[o["user_id"]]["split"] for o in orders.values())) == SPLIT_COUNTS,
        "订单分组数量不符",
    )
    prefixes = {"acceptance": "ACC-", "development": "DEV-", "evaluation": "EVAL-"}
    earliest = data_start(as_of)
    latest = datetime.combine(as_of, time(23, 59, 59))
    for order in orders.values():
        require(
            order["order_id"].startswith(prefixes[users[order["user_id"]]["split"]]),
            "订单 ID 与分组不符",
        )
        if order["create_time"] is not None:
            require(earliest <= parse_time(order["create_time"]) <= latest, "订单日期越界")
    require(any("status" not in r for r in raw_orders), "缺少状态缺失边界场景")
    require(any("create_time" not in r for r in raw_orders), "缺少时间缺失边界场景")
    linked = set()
    for row in logistics.values():
        require(set(row) == set(LOGISTICS_FIELDS), "物流字段不符")
        require(
            row["order_id"] in orders and row["order_id"] not in linked, "物流孤儿或订单多条快照"
        )
        linked.add(row["order_id"])
        order = orders[row["order_id"]]
        require(type(row["status"]) is int and row["status"] in LOGISTICS_STATUS, "未知物流状态")
        require(
            row["status"] in COMPATIBLE_LOGISTICS.get(order["status"], set()),
            "订单与物流状态不一致",
        )
        require(order["create_time"] is not None, "缺少订单时间时不生成物流")
        require(
            parse_time(order["create_time"]) <= parse_time(row["update_time"]) <= latest,
            "物流时间顺序不符",
        )
    expected_linked = {
        o["order_id"]
        for o in orders.values()
        if o["status"] in COMPATIBLE_LOGISTICS and o["create_time"] is not None
    }
    require(linked == expected_linked, "应有物流的订单缺少快照")
    for row in products.values():
        require(set(row) == set(PRODUCT_FIELDS), "商品字段不符")
        require(isinstance(row["name"], str) and bool(row["name"].strip()), "商品名称为空")
        require(
            integer_in_range(row["stock"], 0, 10000) and integer_in_range(row["price"], 0, 1000000),
            "商品库存或价格越界",
        )
    with closing(
        sqlite3.connect((root / "database/ecommerce.db").resolve().as_uri() + "?mode=ro", uri=True)
    ) as con:
        require(con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite 完整性错误")
        require(con.execute("PRAGMA foreign_key_check").fetchall() == [], "SQLite 外键错误")
        require(
            {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            == {"orders", "logistics", "products"},
            "三表边界被修改",
        )
        require(
            not con.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall(),
            "不允许未说明的视图",
        )
        for table, fields, records in (
            ("orders", ORDER_FIELDS, orders),
            ("logistics", LOGISTICS_FIELDS, logistics),
            ("products", PRODUCT_FIELDS, products),
        ):
            require(
                tuple(r[1] for r in con.execute(f'PRAGMA table_info("{table}")')) == fields,
                f"{table} schema 与原题不符",
            )
            stored = con.execute(
                f"SELECT {','.join(fields)} FROM {table} ORDER BY {fields[0]}"
            ).fetchall()
            expected = [tuple(r[k] for k in fields) for _, r in sorted(records.items())]
            require(stored == expected, f"{table} JSONL 与 SQLite 不一致")
    require(
        json.loads((root / "database/dictionary.json").read_text(encoding="utf-8")) == dictionary(),
        "业务字典不符合契约",
    )
    return users, orders, logistics, products


def _policies(root: Path, as_of: date) -> dict:
    expected_documents = policy_documents(as_of)
    expected_metadata = {m["doc_id"]: m for _, m, _ in expected_documents}
    require(json.loads((root / "knowledge_manifest.json").read_text(encoding="utf-8"))
            == coverage_manifest(expected_documents), "政策覆盖清单不完整")
    paths = sorted((root / "knowledge").glob("*.md"))
    require(len(paths) == 30, "政策文档数量必须为 30")
    documents, groups = {}, defaultdict(list)
    catalog = {p[0]: p for p in POLICIES}
    for file in paths:
        text = file.read_text(encoding="utf-8")
        require(text.startswith("---\n"), "文档缺少 front matter")
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front)
        doc_id = meta.get("doc_id")
        require(valid_id(doc_id) and doc_id not in documents, "文档 ID 非法或重复")
        require(file.stem == doc_id, "文件名与 doc_id 不符")
        require(meta == expected_metadata.get(doc_id), "政策版本、替代关系或锚点元数据错误")
        require('id="policy-rule"' in body and 'id="simulation-scope"' in body, "政策锚点缺失")
        require(
            meta.get("source_type") == SOURCE and meta.get("contract_version") == VERSION,
            "文档来源缺失",
        )
        require(meta.get("conditions") == {"channel": "online", "region": "CN"}, "政策适用条件不符")
        effective = date.fromisoformat(meta["effective_date"])
        require(effective <= as_of, "主政策集合出现未来版本")
        require(meta.get("expires_at") is None, "主政策集合失效条件未说明")
        require(meta.get("policy_key") in catalog, "未知政策主题")
        _, _, rule, new, clause, old, old_clause = catalog[meta["policy_key"]]
        historical = meta.get("historical")
        require(type(historical) is bool, "缺少历史标记")
        require(not historical or old is not None, "不应存在的历史版本")
        require(meta.get("version") == (1 if historical or old is None else 2), "政策版本号错误")
        require(meta.get("rules") == {rule: old if historical else new}, "政策结构化规则错误")
        require((old_clause if historical else clause) in body, "政策正文与规则不一致")
        documents[doc_id] = meta
        groups[meta["policy_key"]].append(meta)
    require(len(groups) == 25, "政策主题数量不符")
    conflicts = 0
    for versions in groups.values():
        if len(versions) == 2:
            older, newer = sorted(versions, key=lambda m: m["effective_date"])
            require(older["historical"] and not newer["historical"], "历史版本标记错误")
            require(
                older["effective_date"] < newer["effective_date"]
                and older["rules"] != newer["rules"],
                "缺少实质政策冲突",
            )
            conflicts += 1
        else:
            require(len(versions) == 1 and not versions[0]["historical"], "孤立旧版本")
    require(conflicts == 5, "历史政策冲突必须为 5 对")
    return documents


def _cases(root, as_of, users, orders, logistics, documents) -> dict:
    all_cases = []
    for split in SPLIT_COUNTS:
        cases = jsonl_rows(root / "cases" / f"{split}.jsonl")
        require(all(c["split"] == split for c in cases), "案例跨分组")
        all_cases.extend(cases)
    cases = indexed(all_cases, "id")
    require(
        set(c["category"] for c in cases.values()) == {"normal", "boundary", "security", "fault"},
        "场景分层不完整",
    )
    by_order = {row["order_id"]: row for row in logistics.values()}
    faults = indexed(jsonl_rows(root / "fixtures/faults.jsonl"), "id")
    for case in cases.values():
        require(
            case.get("source_type") == SOURCE and case.get("as_of") == as_of.isoformat(),
            "案例来源或基准日错误",
        )
        principal = case["principal_id"]
        require(
            principal in users and users[principal]["split"] == case["split"], "案例身份与分组不符"
        )
        require(
            isinstance(case.get("question"), str) and bool(case["question"].strip()), "案例缺少问题"
        )
        refs, expected, kind = case["data_refs"], case["expected"], case["kind"]
        require(
            all(
                r in orders and users[orders[r]["user_id"]]["split"] == case["split"]
                for r in refs["order_ids"]
            ),
            "订单引用不存在或跨分组",
        )
        require(all(r in documents for r in refs["doc_ids"]), "文档引用不存在")
        if kind != "cross_user":
            require(all(orders[r]["user_id"] == principal for r in refs["order_ids"]), "案例引用越权")
        if "order_id" in expected:
            require(expected["order_id"] in refs["order_ids"], "期望订单未列入引用")
        if kind == "aggregate":
            start, end = (v.strftime("%Y-%m-%d %H:%M:%S") for v in month_bounds(as_of))
            require((expected["start"], expected["end"]) == (start, end), "统计月份错误")
            missing = [o["order_id"] for o in orders.values() if o["user_id"] == principal
                       and o["create_time"] is None]
            unknown = [o["order_id"] for o in orders.values() if o["user_id"] == principal
                       and o["status"] is None and o["create_time"] is not None
                       and start <= o["create_time"] < end]
            require(expected.get("excluded_missing_time", []) == missing
                    and expected.get("excluded_unknown_status", []) == unknown,
                    "统计排除项未披露")
            values = [
                o
                for o in orders.values()
                if o["user_id"] == principal
                and o["create_time"] is not None
                and expected["start"] <= o["create_time"] < expected["end"]
                and o["status"] in EFFECTIVE_ORDER_STATUSES
            ]
            require({o["order_id"] for o in values} | set(missing + unknown) == set(refs["order_ids"]),
                    "统计来源订单不完整")
            require(
                sum(o["amount"] for o in values) == expected["amount_cents"]
                and len(values) == expected["order_count"],
                "统计期望错误",
            )
        elif kind == "order_lookup":
            order = orders[expected["order_id"]]
            require(
                order["user_id"] == principal and order["amount"] == expected["amount_cents"],
                "查询期望错误",
            )
        elif kind == "join":
            require(
                by_order[expected["order_id"]]["status"] == expected["logistics_status"],
                "Join 期望错误",
            )
        elif kind == "cross_user":
            require(
                all(orders[r]["user_id"] != principal for r in refs["order_ids"])
                and expected["rows"] == [],
                "越权案例错误",
            )
        elif kind == "policy":
            require(expected["selected_doc_id"] in refs["doc_ids"], "政策期望未列入引用")
            selected = documents[expected["selected_doc_id"]]
            require(
                selected["rules"] == expected["rules"] and not selected["historical"],
                "政策期望错误",
            )
        elif kind == "abnormal":
            require(
                all(orders[r]["status"] == expected["status"] for r in refs["order_ids"])
                and expected["status"] in (5, 6),
                "异常案例错误",
            )
        elif kind == "missing_fields":
            require(set(expected["missing_status"] + expected["missing_create_time"])
                    <= set(refs["order_ids"]), "缺字段期望未列入引用")
            require(
                all(orders[r]["status"] is None for r in expected["missing_status"]),
                "缺失状态案例错误",
            )
            require(
                all(orders[r]["create_time"] is None for r in expected["missing_create_time"]),
                "缺失时间案例错误",
            )
        elif kind == "amount_bounds":
            require(set(expected["amounts"]) <= set(refs["order_ids"]), "金额期望未列入引用")
            require(
                all(orders[r]["amount"] == value for r, value in expected["amounts"].items()),
                "金额边界案例错误",
            )
        elif kind == "empty_result":
            require(
                not any(o["user_id"] == principal for o in orders.values()), "空结果用户实际有订单"
            )
        elif kind == "fault_script":
            fixture = faults[expected["fixture_id"]]
            require(
                all(
                    fixture["expected"][key] == expected[key] for key in ("status", "max_attempts")
                ),
                "故障期望不一致",
            )
    for name in ("security", "faults", "invalid_orders"):
        require(
            all(
                f.get("source_type") == SOURCE and f.get("formal_mock_integration") is False
                for f in jsonl_rows(root / "fixtures" / f"{name}.jsonl")
            ),
            "测试替身来源缺失",
        )
    for sample in jsonl_rows(root / "fixtures/invalid_orders.jsonl"):
        try:
            normalize_order(sample["row"])
        except ValueError:
            continue
        raise DataValidationError("非法订单夹具意外通过契约")
    return cases


def _logs(root, cases, users) -> list:
    from database.scenarios import simulated_messages
    logs = jsonl_rows(root / "logs.jsonl")
    indexed(logs, "conversation_id")
    require(len(logs) == 50, "模拟对话数量必须为 50")
    require(dict(Counter(log["split"] for log in logs)) == LOG_COUNTS, "对话分组数量不符")
    for log in logs:
        require(
            log.get("source_type") == SOURCE and log.get("execution_verified") is False,
            "模拟对话不能冒充真实执行",
        )
        require(log["case_id"] in cases, "对话引用不存在")
        case = cases[log["case_id"]]
        for key in ("split", "principal_id", "as_of", "category", "data_refs"):
            require(log[key] == case[key], f"对话与案例 {key} 不一致")
        require(users[log["principal_id"]]["split"] == log["split"], "对话身份跨分组")
        messages = log["messages"]
        require(len(messages) >= 2 and messages[0]["role"] == "user", "对话角色序列错误")
        require(messages[0]["content"] == case["question"], "对话问题与案例不一致")
        require(messages == simulated_messages(case),
                "合成回复不符合受控模板；此检查不是 Agent 语义评测")
        require(
            all(
                m["role"] == ("user" if i % 2 == 0 else "assistant")
                and isinstance(m["content"], str)
                and m["content"].strip()
                for i, m in enumerate(messages)
            ),
            "对话角色或内容无效",
        )
    return logs


def validate_dataset(root: Path, verify_hashes: bool = True) -> dict:
    root = Path(root)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        _files(root, manifest, verify_hashes)
        as_of = date.fromisoformat(manifest["as_of"])
        require(manifest.get("anchor_date") == manifest["as_of"], "基准日不一致")
        require(manifest.get("variant") in ("baseline", "perturbation"), "未知数据变体")
        users, orders, logistics, products = _business(root, as_of)
        from database.normalize import normalize_records
        from database.snapshot import validate_snapshot
        normalized = normalize_records(jsonl_rows(root / "raw/orders.jsonl"))
        expected_orders = [r for _, r in sorted(orders.items())]
        require(normalized["records"] == expected_orders
                == jsonl_rows(root / "canonical/orders.jsonl")
                == jsonl_rows(root / "normalized/orders.jsonl"), "分层数据不一致")
        require(normalized["lineage"] == jsonl_rows(root / "lineage/orders.jsonl")
                and normalized["rejected"] == jsonl_rows(root / "lineage/rejected.jsonl")
                and len(normalized["rejected"]) == 3, "规范化血缘不一致")
        for profile in ("baseline", "evolved"):
            validate_snapshot(root, root / "snapshots" / profile)
        documents = _policies(root, as_of)
        cases = _cases(root, as_of, users, orders, logistics, documents)
        logs = _logs(root, cases, users)
        counts = {
            "orders": len(orders),
            "logistics": len(logistics),
            "products": len(products),
            "documents": len(documents),
            "historical_conflicts": 5,
            "conversations": len(logs),
        }
        require(manifest["counts"] == counts, "manifest 数量错误")
        require(
            manifest["order_splits"] == SPLIT_COUNTS
            and manifest["conversation_splits"] == LOG_COUNTS,
            "manifest 分组错误",
        )
        return {
            "status": "PASS",
            "contract_version": VERSION,
            "seed": manifest["seed"],
            "as_of": manifest["as_of"],
            "source_type": SOURCE,
            **counts,
            "case_count": len(cases),
            "missing_status": sum(o["status"] is None for o in orders.values()),
            "missing_create_time": sum(o["create_time"] is None for o in orders.values()),
            "checks": [
                "counts",
                "schema",
                "ranges",
                "ownership",
                "foreign_keys",
                "status_compatibility",
                "timestamps",
                "jsonl_sqlite_equality",
                "policy_versions",
                "case_expectations",
                "dialogue_references",
                "split_isolation",
                "file_hashes" if verify_hashes else "file_hashes_skipped",
            ],
            "formal_mock_integration": False,
            "agent_execution_verified": False,
        }
    except DataValidationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, yaml.YAMLError) as exc:
        raise DataValidationError(f"数据校验失败：{exc}") from exc


def main():
    parser = argparse.ArgumentParser(description="只读校验 C-002 数据契约及文件摘要")
    parser.add_argument("--root", type=Path, default=Path("data/seed"))
    args = parser.parse_args()
    try:
        result = validate_dataset(args.root)
    except DataValidationError as exc:
        parser.exit(1, f"校验失败：{exc}\n")
    print(stable_json(result))


if __name__ == "__main__":
    main()
