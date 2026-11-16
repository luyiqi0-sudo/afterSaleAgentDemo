import inspect
import sqlite3
from datetime import date

import pytest

from database.contracts import jsonl_rows
from database.seed import generate_dataset


def test_oracle_hand_calculated_month_and_owner(tmp_path):
    from eval.oracle import ReferenceOracle
    (tmp_path / "database").mkdir()
    with sqlite3.connect(tmp_path / "database/ecommerce.db") as con:
        con.execute("CREATE TABLE orders(order_id TEXT, user_id TEXT, amount INT, status INT, create_time TEXT)")
        con.executemany("INSERT INTO orders VALUES(?,?,?,?,?)", [
            ("first", "a", 101, 1, "2026-12-01 00:00:00"),
            ("last", "a", 202, 6, "2026-12-31 23:59:59"),
            ("next", "a", 999, 3, "2027-01-01 00:00:00"),
            ("cancel", "a", 999, 4, "2026-12-05 00:00:00"),
            ("unknown", "a", 999, None, "2026-12-05 00:00:00"),
            ("undated", "a", 999, 3, None),
            ("foreign", "b", 90000, 3, "2026-12-15 00:00:00"),
        ])
    oracle = ReferenceOracle(tmp_path, date(2027, 1, 1))
    actual = oracle.expected({"kind": "aggregate", "principal_id": "a", "inputs": {}})
    assert actual["facts"] == {"amount_cents": 303, "order_count": 2,
                              "start": "2026-12-01 00:00:00", "end": "2027-01-01 00:00:00",
                              "excluded_missing_time": ["undated"], "excluded_unknown_status": ["unknown"]}
    assert oracle.expected({"kind": "cross_user", "principal_id": "a",
                            "inputs": {"order_id": "foreign"}})["facts"] == {"rows": []}
    source = inspect.getsource(ReferenceOracle)
    assert "agent." not in source and "database.scenarios" not in source


def test_36_golden_are_traceable_public_and_not_runtime_answers(tmp_path):
    generate_dataset(tmp_path / "data")
    root = tmp_path / "data"
    cases = jsonl_rows(root / "eval/golden_dataset.jsonl")
    assert len(cases) == 36
    assert {c["id"] for c in cases} == {f"GOLD-{i:02}" for i in range(1, 37)}
    logs = {r["conversation_id"] for r in jsonl_rows(root / "logs.jsonl")}
    for case in cases:
        assert case["source_log_id"] in logs
        assert case["visibility"] == "public_holdout_not_blind"
        assert "expected" not in case
        assert case["disposition"] in {"ACCEPT", "MODIFY", "DEFER"}
        assert case["split"] == "evaluation"


def test_missing_observations_are_not_perfect_score_and_unknown_ids_rejected(tmp_path):
    from eval.eval import evaluate_cases
    root = tmp_path / "data"
    generate_dataset(root)
    report = evaluate_cases(root)
    assert report["summary"] == {"passed": 0, "failed": 0, "not_run": 36, "executed": 0,
                                  "pass_rate": None, "coverage": 0.0}
    assert report["semantic"]["status"] == "NOT_RUN"
    assert report["safety_gate"] == "NOT_RUN"
    with pytest.raises(ValueError):
        evaluate_cases(root, {"GOLD-99": {}})
    broken = evaluate_cases(root, {"GOLD-12": {}})
    assert broken["summary"]["failed"] == 1


def test_wrong_amount_and_call_parameters_fail_without_changing_truth(tmp_path):
    from eval.eval import evaluate_cases
    from eval.oracle import ReferenceOracle
    root = tmp_path / "data"
    generate_dataset(root)
    case = next(c for c in jsonl_rows(root / "eval/golden_dataset.jsonl") if c["id"] == "GOLD-20")
    oracle = ReferenceOracle(root)
    truth_before = oracle.expected(case)
    wrong = {"facts": {"amount_cents": -1}, "calls": [{"tool": "query_order", "args": {"order_id": "ACC-006"},
                                                       "turn": 1, "http_attempts": 9}],
             "answer": "测试错误答案", "evidence": {"producer": "test-harness", "scope": "unit"}}
    report = evaluate_cases(root, {case["id"]: wrong})
    assert report["cases"][19]["status"] == "FAIL"
    assert oracle.expected(case) == truth_before


def test_dual_seed_dimensions_and_no_three_percent_gate():
    from eval.eval import compare_reports
    a = {"dimensions": {"sql": {"passed": 1, "executed": 1, "pass_rate": 1.0, "not_run": 0}}, "cases": []}
    b = {"dimensions": {"sql": {"passed": 0, "executed": 1, "pass_rate": 0.0, "not_run": 0}}, "cases": []}
    report = compare_reports(a, b)
    assert report["dimensions"]["sql"]["delta"] == -1.0
    assert report["score_delta_is_gate"] is False


def test_optional_semantics_without_model_are_explicitly_not_run():
    from eval.semantic import semantic_report
    assert semantic_report([], judge_model=None)["status"] == "NOT_RUN"


def test_observation_cannot_pass_with_missing_call_trace(tmp_path):
    from eval.eval import evaluate_cases
    root = tmp_path / "data"
    generate_dataset(root)
    observation = {"facts": {"clarify": ["order_id", "reason"]}, "answer": "请提供订单号和问题。",
                   "evidence": {"producer": "test", "scope": "unit"}}
    result = evaluate_cases(root, {"GOLD-22": observation})
    assert result["cases"][21]["status"] == "FAIL"
