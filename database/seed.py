"""Reproducible candidate-authored dataset. Run: python -m database.seed."""

import argparse
import hashlib
import json
import random
import sqlite3
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

from database.catalog import coverage_manifest, policy_documents, render_document
from database.contracts import (
    COMPATIBLE_LOGISTICS,
    DEFAULT_AS_OF,
    DEFAULT_SEED,
    LOG_COUNTS,
    LOGISTICS_FIELDS,
    ORDER_FIELDS,
    PRODUCT_FIELDS,
    SOURCE,
    SPLIT_COUNTS,
    TIME_FORMAT,
    VERSION,
    dictionary,
    data_start,
    jsonl_rows,
    month_bounds,
    normalize_order,
    sha256,
    stable_json,
)
from database.scenarios import acceptance_cases, fixtures, sample_cases, simulated_messages
from database.normalize import normalize_records, raw_envelopes
from database.snapshot import build_snapshots


def _rng(seed: int, split: str) -> random.Random:
    digest = hashlib.sha256(f"{VERSION}:{seed}:{split}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
        newline="\n",
    )


def _acceptance_orders(as_of: date) -> list[dict]:
    start, end = month_bounds(as_of)
    # All values and ownership are fixed; only dates move with the explicit reference date.
    entries = [
        ("acc-alice", 19900, 3, start),
        ("acc-alice", 30100, 2, end - timedelta(seconds=1)),
        ("acc-alice", 77700, 3, start - timedelta(seconds=1)),
        ("acc-alice", 88800, 1, end),
        ("acc-alice", 99900, 4, start + timedelta(days=14)),
        ("acc-bob", 990000, 3, start + timedelta(days=9)),
        ("acc-alice", 7500, 6, start + timedelta(days=15)),
        ("acc-alice", 5000, 0, start + timedelta(days=17)),
        ("acc-edge", 1200, None, start + timedelta(days=19)),
        ("acc-edge", 3500, 1, None),
        ("acc-edge", 0, 3, start + timedelta(days=6)),
        ("acc-edge", 1000000, 3, start + timedelta(days=7)),
        ("acc-edge", 4200, 5, start + timedelta(days=8)),
        ("acc-edge", 5900, 2, start + timedelta(days=10)),
        ("acc-edge", 8900, 2, start + timedelta(days=11)),
        ("acc-edge", 1300, 1, start + timedelta(days=12)),
        ("acc-edge", 2100, 0, start + timedelta(days=13)),
        ("acc-edge", 3200, 4, start + timedelta(days=16)),
        ("acc-edge", 4700, 3, start + timedelta(days=18)),
        ("acc-edge", 6100, 5, start + timedelta(days=20)),
    ]
    return [
        {
            k: v
            for k, v in {
                "order_id": f"ACC-{i:03}",
                "user_id": user,
                "amount": amount,
                "status": status,
                "create_time": created.strftime(TIME_FORMAT) if created else None,
            }.items()
            if v is not None
        }
        for i, (user, amount, status, created) in enumerate(entries, 1)
    ]


def _business_data(seed: int, as_of: date, variant: str) -> tuple[list, list, list, list]:
    earliest = data_start(as_of)
    ceiling = datetime.combine(as_of, time(23, 59, 59))
    users = [
        {
            "user_id": f"acc-{name}",
            "split": "acceptance",
            "conditions": {"channel": "online", "region": "CN"},
            "source_type": SOURCE,
        }
        for name in ("alice", "bob", "edge", "empty")
    ]
    orders = _acceptance_orders(as_of)
    for split, prefix in (("development", "DEV"), ("evaluation", "EVAL")):
        rng = _rng(seed, split)
        if variant == "perturbation":
            prefix += "-" + hashlib.sha256(str(seed).encode()).hexdigest()[:8]
        members = [f"{prefix.lower()}-u{i:02}" for i in range(1, 11)]
        users.extend(
            {
                "user_id": u,
                "split": split,
                "conditions": {"channel": "online", "region": "CN"},
                "source_type": SOURCE,
            }
            for u in members
        )
        if split == "evaluation":
            users.append({"user_id": prefix.lower() + "-empty", "split": split,
                          "conditions": {"channel": "online", "region": "CN"}, "source_type": SOURCE})
        for i in range(1, SPLIT_COUNTS[split] + 1):
            created = earliest + timedelta(
                seconds=rng.randint(0, int((ceiling - earliest).total_seconds()) - 3600)
            )
            row = {
                "order_id": f"{prefix}-{i:03}",
                "user_id": members[0 if i <= 45 else (i - 1) % len(members)],
                "amount": rng.randint(1, 200000),
                "status": rng.choice([0, 1, 2, 2, 3, 3, 4, 5, 6]),
                "create_time": created.strftime(TIME_FORMAT),
            }
            if i == 17:
                del row["status"]
            if i == 31:
                del row["create_time"]
            # Guaranteed selectors for Golden cases; amounts still depend on the seed.
            if i <= 9:
                row["status"] = [1, 2, 6, 0, 3, 4, 5, 2, 3][i - 1]
            if i in range(4, 10):
                month = as_of.year * 12 + as_of.month - 1 - (i - 4)
                row["create_time"] = datetime(month // 12, month % 12 + 1, 1).strftime(TIME_FORMAT)
            if i == 46:
                row["status"] = 3  # Cross-user logistics selector is always present.
            orders.append(row)
    orders.sort(key=lambda r: r["order_id"])
    logistics = []
    for order in orders:
        status = order.get("status")
        if status not in COMPATIBLE_LOGISTICS or not order.get("create_time"):
            continue
        # Stable snapshot semantics, no fabricated event history.
        logistics_status = {2: 1, 3: 3, 5: 4, 6: 3}[status]
        updated = min(
            datetime.strptime(order["create_time"], TIME_FORMAT) + timedelta(days=1), ceiling
        )
        logistics.append(
            {
                "logistics_id": "L-" + order["order_id"],
                "order_id": order["order_id"],
                "status": logistics_status,
                "update_time": updated.strftime(TIME_FORMAT),
            }
        )
    rng = _rng(seed, "products")
    products = [
        {
            "sku_id": f"SKU-{i:03}",
            "name": ("红色运动鞋" if i == 1 else "蓝色运动鞋" if i == 2 else f"测试商品{i:02}"),
            "stock": 0 if i == 3 else 10000 if i == 4 else rng.randint(1, 500),
            "price": rng.randint(100, 100000),
        }
        for i in range(1, 25)
    ]
    return sorted(users, key=lambda r: r["user_id"]), orders, logistics, products


def _build(root: Path, seed: int, as_of: date, variant: str) -> None:
    root.mkdir()
    users, orders, logistics, products = _business_data(seed, as_of, variant)
    for name, rows in (
        ("users", users),
        ("orders", orders),
        ("logistics", logistics),
        ("products", products),
    ):
        _write_jsonl(root / f"{name}.jsonl", rows)
    canonical = [normalize_order(row) for row in orders]
    raw = raw_envelopes(canonical, variant)
    result = normalize_records(raw)
    _write_jsonl(root / "canonical/orders.jsonl", canonical)
    _write_jsonl(root / "raw/orders.jsonl", raw)
    _write_jsonl(root / "normalized/orders.jsonl", result["records"])
    _write_jsonl(root / "lineage/orders.jsonl", result["lineage"])
    _write_jsonl(root / "lineage/rejected.jsonl", result["rejected"])
    dbdir = root / "database"
    dbdir.mkdir()
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    (dbdir / "schema.sql").write_text(schema, encoding="utf-8", newline="\n")
    (dbdir / "dictionary.json").write_text(
        stable_json(dictionary()), encoding="utf-8", newline="\n"
    )
    con = sqlite3.connect(dbdir / "ecommerce.db")
    try:
        con.executescript(schema)
        with con:
            normalized = result["records"]
            for table, fields, rows in (
                ("orders", ORDER_FIELDS, normalized),
                ("logistics", LOGISTICS_FIELDS, logistics),
                ("products", PRODUCT_FIELDS, products),
            ):
                con.executemany(
                    f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                    [tuple(row[k] for k in fields) for row in rows],
                )
    finally:
        con.close()
    (root / "knowledge").mkdir()
    documents = policy_documents(as_of)
    for name, metadata, body in documents:
        (root / "knowledge" / name).write_text(
            render_document(metadata, body), encoding="utf-8", newline="\n"
        )
    (root / "knowledge_manifest.json").write_text(stable_json(coverage_manifest(documents)),
                                                encoding="utf-8", newline="\n")
    all_cases = {
        "acceptance": acceptance_cases(as_of),
        "development": sample_cases("development", orders, users, as_of),
        "evaluation": sample_cases("evaluation", orders, users, as_of),
    }
    logs = []
    for split, cases in sorted(all_cases.items()):
        _write_jsonl(root / "cases" / f"{split}.jsonl", cases)
        dialogue_cases = cases[:LOG_COUNTS[split]] if split != "acceptance" else [
            c for c in cases if c["id"] in ("A01", "A02", "A03", "A04", "A05", "A06", "A07", "A09", "A10", "A15")]
        for i, case in enumerate(dialogue_cases, 1):
            logs.append(
                {
                    "conversation_id": f"sim-{split}-{i:03}",
                    "split": split,
                    "source_type": SOURCE,
                    "execution_verified": False,
                    "principal_id": case["principal_id"],
                    "as_of": as_of.isoformat(),
                    "category": case["category"],
                    "case_id": case["id"],
                    "data_refs": case["data_refs"],
                    "messages": simulated_messages(case),
                }
            )
    _write_jsonl(root / "logs.jsonl", logs)
    from eval.golden import build_golden
    _write_jsonl(root / "eval/golden_dataset.jsonl", build_golden(root, variant))
    build_snapshots(root, as_of)
    for name, rows in fixtures().items():
        _write_jsonl(
            root / "fixtures" / f"{name}.jsonl",
            [{"source_type": SOURCE, "formal_mock_integration": False, **r} for r in rows],
        )
    (root / "SOURCE.md").write_text(
        "# 候选人自建种子数据\n\nC-002 授权的面试模拟数据，不是真实客户数据。logs.jsonl 为模拟对话，不是 Agent 执行日志。\n\nfixtures 中的故障序列仅用于本地替身，不能证明不可修改 mock_server.py 已集成。\n\n运行时仅使用业务数据与政策；不得把 cases/evaluation.jsonl 的参考结果加载到 Agent Prompt。\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "contract_version": VERSION,
        "source_type": SOURCE,
        "seed": seed,
        "as_of": as_of.isoformat(),
        "anchor_date": as_of.isoformat(),
        "variant": variant,
        "timezone": "Asia/Shanghai",
        "counts": {
            "orders": len(orders),
            "logistics": len(logistics),
            "products": len(products),
            "documents": 30,
            "historical_conflicts": 5,
            "conversations": len(logs),
        },
        "order_splits": SPLIT_COUNTS,
        "conversation_splits": LOG_COUNTS,
        "formal_mock_integration": False,
        "files": {
            p.relative_to(root).as_posix(): sha256(p)
            for p in sorted(root.rglob("*"))
            if p.is_file()
        },
    }
    (root / "manifest.json").write_text(stable_json(manifest), encoding="utf-8", newline="\n")


def generate_dataset(output: Path, seed: int = DEFAULT_SEED, as_of: date | None = None,
                     *, anchor_date: date | None = None, variant: str = "baseline") -> dict:
    from database.validate import DataValidationError, validate_dataset

    if as_of is not None and anchor_date is not None and as_of != anchor_date:
        raise ValueError("as_of 与 anchor_date 冲突")
    as_of = anchor_date if anchor_date is not None else as_of if as_of is not None else DEFAULT_AS_OF
    if variant not in ("baseline", "perturbation"):
        raise ValueError("未知 variant")
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("seed 必须为 0..2^63-1 整数")
    if type(as_of) is not date or as_of.year < 2001 or as_of.year > 9998:
        raise ValueError("as_of 必须为 2001..9998 年的日期")
    raw_output = Path(output)
    if raw_output.is_symlink():
        raise ValueError("拒绝以符号链接为输出目录")
    output = raw_output.resolve()
    if output.exists():
        manifest_path = output / "manifest.json"
        if not manifest_path.is_file():
            raise DataValidationError("目标已存在但不是受管数据集，请指定新的输出目录")
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(
            existing.get(k) != v
            for k, v in {
                "seed": seed,
                "as_of": as_of.isoformat(),
                "contract_version": VERSION,
                "variant": variant,
            }.items()
        ):
            raise ValueError("已有数据集配置不同，请指定新的输出目录")
        return validate_dataset(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # TemporaryDirectory owns only the newly created child inside this verified parent.
    with tempfile.TemporaryDirectory(prefix=".seed-build-", dir=output.parent) as temporary:
        staging = Path(temporary) / "dataset"
        _build(staging, seed, as_of, variant)
        report = validate_dataset(staging)
        staging.rename(output)  # Same-volume publication, no partially built target.
    return report


def read_order_page(root: Path, after: str | None = None, limit: int = 50) -> dict:
    """Offline fixture reader only: never an authenticated user-facing endpoint."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("page size 必须为 1..100")
    rows = sorted(jsonl_rows(Path(root) / "orders.jsonl"), key=lambda r: r["order_id"])
    if after is not None:
        rows = [r for r in rows if r["order_id"] > after]
    items = rows[:limit]
    return {"items": items, "next_cursor": items[-1]["order_id"] if len(rows) > limit else None}


def main():
    parser = argparse.ArgumentParser(
        description="初始化 C-004 候选人自建数据；不会覆盖修改过的数据集"
    )
    parser.add_argument("--output", type=Path, default=Path("data/c004"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    dates = parser.add_mutually_exclusive_group()
    dates.add_argument("--as-of", type=date.fromisoformat)
    dates.add_argument("--anchor-date", type=date.fromisoformat)
    parser.add_argument("--variant", choices=("baseline", "perturbation"), default="baseline")
    args = parser.parse_args()
    try:
        result = generate_dataset(args.output, args.seed, args.as_of,
                                  anchor_date=args.anchor_date, variant=args.variant)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"初始化失败：{exc}\n")
    print(stable_json(result))


if __name__ == "__main__":
    main()
