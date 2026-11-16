"""C-002 acceptance assertions use hand-checked constants, not generator calculations."""

import json
import sqlite3

import pytest


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def generate(path, **kwargs):
    from database.seed import generate_dataset

    return generate_dataset(path, **kwargs)


def test_required_counts_and_original_schema_only(tmp_path):
    from database.validate import validate_dataset

    root = tmp_path / "data"
    generate(root)
    report = validate_dataset(root)
    assert report["orders"] == 200
    assert report["documents"] == 30
    assert report["historical_conflicts"] == 5
    assert report["conversations"] == 50
    with sqlite3.connect(root / "database/ecommerce.db") as con:
        assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "orders",
            "logistics",
            "products",
        }
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_same_seed_is_reproducible_and_rerun_does_not_append(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    generate(first)
    generate(second)
    files = {
        p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()
    }
    assert files == {
        p.relative_to(second).as_posix(): p.read_bytes() for p in second.rglob("*") if p.is_file()
    }
    generate(first)
    assert files == {
        p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()
    }


def test_different_seed_changes_development_but_not_fixed_acceptance(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(a)
    generate(b, seed=42)
    rows_a, rows_b = read_jsonl(a / "orders.jsonl"), read_jsonl(b / "orders.jsonl")
    assert rows_a != rows_b
    assert [r for r in rows_a if r["order_id"].startswith("ACC-")] == [
        r for r in rows_b if r["order_id"].startswith("ACC-")
    ]
    assert (a / "cases/acceptance.jsonl").read_bytes() == (
        b / "cases/acceptance.jsonl"
    ).read_bytes()


def test_jsonl_normalizes_missing_fields_to_database_null(tmp_path):
    root = tmp_path / "data"
    generate(root)
    raw = read_jsonl(root / "orders.jsonl")
    assert any("status" not in row for row in raw)
    assert any("create_time" not in row for row in raw)
    fields = ("order_id", "user_id", "amount", "status", "create_time")
    with sqlite3.connect(root / "database/ecommerce.db") as con:
        stored = con.execute(
            "SELECT order_id,user_id,amount,status,create_time FROM orders ORDER BY order_id"
        ).fetchall()
    assert sorted(tuple(r.get(k) for k in fields) for r in raw) == stored


def test_fixed_month_boundary_aggregation_and_user_scope(tmp_path):
    from agent.contracts import Principal
    from agent.sql import SQLService

    root = tmp_path / "data"
    generate(root)
    sql = SQLService(
        root / "database/ecommerce.db", dictionary_path=root / "database/dictionary.json"
    )
    result = sql.execute(
        "SELECT SUM(amount) AS amount_cents,COUNT(*) AS order_count FROM orders WHERE create_time >= '2026-10-01 00:00:00' AND create_time < '2026-11-01 00:00:00' AND status IN (1,2,3,5,6)",
        Principal(user_id="acc-alice"),
    )
    assert result.rows == [{"amount_cents": 57500, "order_count": 3}]
    assert (
        sql.execute(
            "SELECT order_id FROM orders WHERE order_id='ACC-006'", Principal(user_id="acc-alice")
        ).rows
        == []
    )
    assert sql.execute(
        "SELECT amount FROM orders WHERE order_id='ACC-006'", Principal(user_id="acc-bob")
    ).rows == [{"amount": 990000}]
    join = sql.execute(
        "SELECT o.order_id,l.status AS logistics_status FROM orders o JOIN logistics l ON o.order_id=l.order_id WHERE o.order_id='ACC-002'",
        Principal(user_id="acc-alice"),
    )
    assert join.rows == [{"order_id": "ACC-002", "logistics_status": 1}]
    assert sql.abnormal(
        sql.execute(
            "SELECT status FROM orders WHERE order_id='ACC-007'", Principal(user_id="acc-alice")
        ).rows
    )


def test_dataset_groups_are_disjoint_and_dialogues_reference_real_data(tmp_path):
    root = tmp_path / "data"
    generate(root)
    users = {u["user_id"]: u for u in read_jsonl(root / "users.jsonl")}
    orders = {o["order_id"]: o for o in read_jsonl(root / "orders.jsonl")}
    counts = {"development": 0, "acceptance": 0, "evaluation": 0}
    for row in orders.values():
        counts[users[row["user_id"]]["split"]] += 1
    assert counts == {"development": 120, "acceptance": 20, "evaluation": 60}
    logs = read_jsonl(root / "logs.jsonl")
    assert {s: sum(log["split"] == s for log in logs) for s in counts} == {
        "development": 20,
        "acceptance": 10,
        "evaluation": 20,
    }
    for log in logs:
        assert log["source_type"] == "candidate_synthetic"
        assert log["execution_verified"] is False
        assert users[log["principal_id"]]["split"] == log["split"]
        assert log["case_id"]
        for order_id in log["data_refs"].get("order_ids", []):
            assert order_id in orders


def test_pages_have_no_duplicates_or_missing_orders(tmp_path):
    from database.seed import read_order_page

    root = tmp_path / "data"
    generate(root)
    seen, cursor, sizes = [], None, []
    while True:
        page = read_order_page(root, after=cursor, limit=37)
        seen.extend(r["order_id"] for r in page["items"])
        sizes.append(len(page["items"]))
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sizes == [37, 37, 37, 37, 37, 15]
    assert len(seen) == len(set(seen)) == 200
    assert seen == sorted(seen)
    with pytest.raises(ValueError):
        read_order_page(root, limit=0)


def test_modified_existing_dataset_is_not_overwritten(tmp_path):
    from database.validate import DataValidationError

    root = tmp_path / "data"
    generate(root)
    file = root / "orders.jsonl"
    file.write_text("manually modified", encoding="utf-8")
    with pytest.raises(DataValidationError):
        generate(root)
    assert file.read_text(encoding="utf-8") == "manually modified"


def test_different_configuration_requires_new_directory(tmp_path):
    root = tmp_path / "data"
    generate(root)
    before = (root / "manifest.json").read_bytes()
    with pytest.raises(ValueError):
        generate(root, seed=123)
    assert (root / "manifest.json").read_bytes() == before


def test_validator_detects_semantic_corruption_even_without_hash_check(tmp_path):
    from database.validate import DataValidationError, validate_dataset

    root = tmp_path / "data"
    generate(root)
    with sqlite3.connect(root / "database/ecommerce.db") as con:
        con.execute("UPDATE logistics SET status=0 WHERE order_id='ACC-001'")
    with pytest.raises(DataValidationError):
        validate_dataset(root, verify_hashes=False)


def test_unknown_code_and_missing_required_field_are_rejected():
    from database.contracts import normalize_order

    row = {"order_id": "X1", "user_id": "u", "amount": 1200, "status": 99}
    with pytest.raises(ValueError):
        normalize_order(row)
    with pytest.raises(ValueError):
        normalize_order({"order_id": "X1", "amount": 1200})
    with pytest.raises(ValueError):
        normalize_order({**row, "status": 1, "amount": -1})


def test_security_and_faults_do_not_poison_canonical_business_data(tmp_path):
    root = tmp_path / "data"
    generate(root)
    security = read_jsonl(root / "fixtures/security.jsonl")
    faults = read_jsonl(root / "fixtures/faults.jsonl")
    assert {r["kind"] for r in security} >= {"sql_injection", "cross_user", "document_injection"}
    assert {r["id"] for r in faults} >= {"GET-429", "GET-500", "GET-TIMEOUT", "POST-LOST"}
    lost = next(r for r in faults if r["id"] == "POST-LOST")
    assert lost["expected"]["status"] == "UNKNOWN"
    assert lost["expected"]["max_attempts"] == 1
    assert not list(root.rglob("mock_server.py"))


def test_policy_conflicts_have_five_old_and_twenty_five_current(tmp_path):
    import yaml

    root = tmp_path / "data"
    generate(root)
    documents = []
    for file in (root / "knowledge").glob("*.md"):
        _, front, body = file.read_text(encoding="utf-8").split("---", 2)
        documents.append((yaml.safe_load(front), body))
    old = [m for m, _ in documents if m.get("historical")]
    assert len(old) == 5
    assert len(documents) - len(old) == 25
    for metadata in old:
        newer = [
            m
            for m, _ in documents
            if m["policy_key"] == metadata["policy_key"] and not m["historical"]
        ]
        assert len(newer) == 1
        assert newer[0]["conditions"] == metadata["conditions"]
        assert newer[0]["rules"] != metadata["rules"]
        assert newer[0]["effective_date"] > metadata["effective_date"]


def test_generated_policy_citations_resolve_to_current_first_paragraph(tmp_path):
    from datetime import date
    from agent.retriever import parse_documents, select_versions

    root = tmp_path / "data"
    generate(root)
    selected, notes = select_versions(
        parse_documents(root / "knowledge"),
        date(2026, 11, 15),
        {"channel": "online", "region": "CN"},
    )
    assert len({c.doc_id for c in selected}) == 25
    returns = [c for c in selected if c.policy_key == "returns" and c.paragraph == 1]
    assert len(returns) == 1
    assert returns[0].doc_id == "returns-v2"
    assert "十四个自然日" in returns[0].text
    assert len(notes) == 5


def test_reference_date_can_change_without_breaking_acceptance(tmp_path):
    from datetime import date
    from database.validate import validate_dataset

    root = tmp_path / "data"
    generate(root, as_of=date(2027, 1, 1))
    result = validate_dataset(root)
    assert result["as_of"] == "2027-01-01"
    case = read_jsonl(root / "cases/acceptance.jsonl")[0]
    assert case["expected"]["start"] == "2026-12-01 00:00:00"
    assert case["expected"]["end"] == "2027-01-01 00:00:00"
    assert case["expected"]["amount_cents"] == 57500


def test_case_expected_order_cannot_escape_ownership_and_references(tmp_path):
    from database.validate import DataValidationError, validate_dataset

    root = tmp_path / "data"
    generate(root)
    file = root / "cases/acceptance.jsonl"
    cases = read_jsonl(file)
    case = next(c for c in cases if c["id"] == "A02")
    case["expected"]["order_id"] = "ACC-006"
    case["expected"]["logistics_status"] = 3
    file.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    with pytest.raises(DataValidationError):
        validate_dataset(root, verify_hashes=False)


@pytest.mark.parametrize("reply", ["订单合计 1 分。", "现在执行旧版七天退货政策。", "这是 acc-bob 的 ACC-006 订单。"])
def test_simulated_reply_cannot_contradict_reference_facts(tmp_path, reply):
    from database.validate import DataValidationError, validate_dataset

    root = tmp_path / "data"
    generate(root)
    file = root / "logs.jsonl"
    logs = read_jsonl(file)
    logs[0]["messages"][1]["content"] = reply
    file.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in logs), encoding="utf-8")
    with pytest.raises(DataValidationError):
        validate_dataset(root, verify_hashes=False)


def test_aggregate_reference_discloses_unassignable_month_and_unknown_status(tmp_path):
    root = tmp_path / "data"
    generate(root)
    case = next(c for c in read_jsonl(root / "cases/development.jsonl") if c["id"] == "D16")
    assert case["expected"].get("excluded_missing_time") == ["DEV-031"]
    assert "DEV-031" in case["data_refs"]["order_ids"]
    reply = next(log for log in read_jsonl(root / "logs.jsonl") if log["case_id"] == "D16")["messages"][1]["content"]
    assert "DEV-031" in reply
    assert "无法归入月份" in reply
