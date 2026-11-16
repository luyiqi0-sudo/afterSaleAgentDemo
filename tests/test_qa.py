import json

import pytest

from database.seed import generate_dataset


def test_qa_is_staged_and_missing_agent_does_not_block_data(tmp_path):
    from database.qa import qa_report
    root = tmp_path / "data"
    generate_dataset(root)
    report = qa_report(root)
    assert report["data_ready"] == "PASS"
    assert report["integration_ready"] == "NOT_RUN"
    assert report["evaluation_ready"] == "NOT_RUN"
    items = {i["id"]: i for i in report["items"]}
    assert set(items) == {f"QA-{i:02}" for i in range(1, 16)}
    assert items["QA-07"]["status"] == "DEFER"
    assert items["QA-13"]["stage"] == "Evaluation Ready"
    assert items["QA-14"]["stage"] == "Integration Ready"
    assert items["QA-15"]["status"] == "NOT_RUN"


def test_data_tampering_fails_qa_even_before_agent_exists(tmp_path):
    from database.qa import qa_report
    root = tmp_path / "data"
    generate_dataset(root)
    path = root / "snapshots/evolved/orders.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["user_id"] = "acc-bob"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    report = qa_report(root)
    assert report["data_ready"] == "FAIL"


def test_static_scan_is_only_a_heuristic_and_can_find_actual_ids(tmp_path):
    from database.qa import scan_coupling
    root = tmp_path / "data"
    generate_dataset(root)
    code = tmp_path / "runtime"
    code.mkdir()
    (code / "prompt.py").write_text('ANSWER = "ACC-001"\n', encoding="utf-8")
    result = scan_coupling(root, code)
    assert result["status"] == "FAIL"
    assert result["matches"][0]["token"] == "ACC-001"
    assert result["proves_no_overfitting"] is False


def apparent_full_report(root):
    from eval.eval import evaluate_cases
    report = evaluate_cases(root)
    for row in report["cases"]:
        row.update(status="PASS", scope="agent_e2e", reasons=[])
    report.update(safety_gate="PASS", semantic={"status": "PASS"})
    return report


@pytest.mark.parametrize("failure", ["case", "safety", "semantic"])
def test_qa_preserves_executed_failures(tmp_path, failure):
    from database.qa import qa_report
    root = tmp_path / "data"
    generate_dataset(root)
    report = apparent_full_report(root)
    if failure == "case":
        report["cases"][0]["status"] = "FAIL"
    elif failure == "safety":
        report["safety_gate"] = "FAIL"
    else:
        report["semantic"]["status"] = "FAIL"
    result = qa_report(root, evaluation=report)
    assert result["evaluation_ready"] == "FAIL"
    assert next(r for r in result["items"] if r["id"] == "QA-15")["status"] == "FAIL"


@pytest.mark.parametrize("mutation", ["duplicate_ids", "seed", "variant", "anchor_date", "contract"])
def test_qa_rejects_wrong_dataset_or_case_coverage(tmp_path, mutation):
    from database.qa import qa_report
    root = tmp_path / "data"
    generate_dataset(root)
    report = apparent_full_report(root)
    if mutation == "duplicate_ids":
        report["cases"] = [report["cases"][0]] * 36
    else:
        report[mutation] = "wrong-dataset"
    assert qa_report(root, evaluation=report)["evaluation_ready"] == "FAIL"


def test_qa_full_readiness_requires_bound_dual_seed_evidence(tmp_path):
    from database.qa import qa_report
    from eval.eval import compare_reports
    a, b = tmp_path / "a", tmp_path / "b"
    generate_dataset(a)
    generate_dataset(b, seed=999999, variant="perturbation")
    report_a, report_b = apparent_full_report(a), apparent_full_report(b)
    assert qa_report(a, evaluation=report_a)["evaluation_ready"] == "NOT_RUN"
    comparison = compare_reports(report_a, report_b)
    assert qa_report(a, evaluation=report_a, comparison=comparison)["evaluation_ready"] == "PASS"
    comparison["reports"]["perturbed"]["cases"][0]["status"] = "FAIL"
    assert qa_report(a, evaluation=report_a, comparison=comparison)["evaluation_ready"] == "FAIL"
