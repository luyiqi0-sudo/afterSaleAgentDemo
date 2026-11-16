"""Staged gates: data validity does not require an Agent, eval runner or remote service."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from database.contracts import jsonl_rows, stable_json
from database.validate import DataValidationError, validate_dataset


def scan_coupling(root: Path, source: Path) -> dict:
    tokens = {r["order_id"] for r in jsonl_rows(root / "orders.jsonl")} | {r["user_id"] for r in jsonl_rows(root / "users.jsonl")}
    matches = []
    paths = sorted(source.rglob("*.py")) if source.exists() else []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for token in sorted(tokens):
                if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(token) + r"(?![A-Za-z0-9_-])", line):
                    matches.append({"file": path.relative_to(source).as_posix(), "line": number, "token": token})
            if re.search(r"(?:cases[/\\]evaluation|golden_dataset|from\s+eval\b|import\s+eval\b)", line):
                matches.append({"file": path.relative_to(source).as_posix(), "line": number, "token": "evaluation dependency"})
    return {"status": "FAIL" if matches else "PASS" if paths else "NOT_RUN", "matches": matches,
            "files_scanned": len(paths), "proves_no_overfitting": False,
            "limitations": "exact known IDs/evaluation imports only; does not prove absence of hardcoded numeric answers or indirect leakage"}


def evaluation_gate(root: Path, evaluation: dict | None, comparison: dict | None) -> tuple[str, str]:
    if evaluation is None:
        return "NOT_RUN", "no actual evaluation report"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    expected_ids = {f"GOLD-{n:02}" for n in range(1, 37)}

    def check(report):
        if not isinstance(report, dict) or report.get("contract") != "c004-oracle-v1":
            raise ValueError("evaluation contract mismatch")
        rows = report.get("cases")
        if not isinstance(rows, list) or len(rows) != 36 or any(not isinstance(r, dict) for r in rows) or {r.get("id") for r in rows} != expected_ids:
            raise ValueError("report must cover each Golden ID exactly once")
        if any(r.get("status") not in ("PASS", "FAIL", "NOT_RUN") for r in rows):
            raise ValueError("unknown case result")
        if type(report.get("seed")) is not int or report.get("variant") not in ("baseline", "perturbation") or not re.fullmatch(r"[0-9a-f]{64}", str(report.get("dataset_sha256"))):
            raise ValueError("missing dataset identity")
        semantic = report.get("semantic", {}).get("status")
        safety = report.get("safety_gate")
        if safety not in ("PASS", "FAIL", "NOT_RUN") or semantic not in ("PASS", "FAIL", "ERROR", "NOT_RUN", "PARTIAL"):
            raise ValueError("missing or invalid semantic/safety result")
        if any(r["status"] == "FAIL" for r in rows) or safety == "FAIL" or semantic in ("FAIL", "ERROR"):
            return "FAIL"
        return "PASS" if all(r["status"] == "PASS" and r.get("scope") == "agent_e2e" for r in rows) and safety == "PASS" and semantic == "PASS" else "NOT_RUN"

    try:
        local_status = check(evaluation)
        identity = {"seed": manifest["seed"], "variant": manifest["variant"], "anchor_date": manifest["anchor_date"],
                    "dataset_sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()}
        if any(evaluation.get(key) != value for key, value in identity.items()):
            raise ValueError("evaluation report belongs to another dataset")
        if local_status == "FAIL":
            return "FAIL", "executed case, deterministic safety or semantic evaluation failed"
        if comparison is None:
            return "NOT_RUN", "requires bound baseline/perturbation comparison and complete Agent/semantic executions"
        reports = comparison["reports"]
        left, right = reports["baseline"], reports["perturbed"]
        statuses = [check(left), check(right)]
        if left["variant"] != "baseline" or right["variant"] != "perturbation" or left["seed"] == right["seed"] or left["dataset_sha256"] == right["dataset_sha256"] or left.get("anchor_date") != right.get("anchor_date") or evaluation != reports["baseline" if identity["variant"] == "baseline" else "perturbed"]:
            raise ValueError("comparison identities do not match the current report and distinct paired seeds")
        if "FAIL" in statuses:
            return "FAIL", "paired evaluation contains an executed failure"
        if local_status == "PASS" and all(s == "PASS" for s in statuses):
            return "PASS", "both bound public datasets have complete Agent/semantic executions; no score-delta threshold"
        return "NOT_RUN", "paired reports are bound, but complete Agent/semantic execution is missing"
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return "FAIL", str(exc)


def qa_report(root: Path, source: Path | None = None, evaluation: dict | None = None,
              comparison: dict | None = None) -> dict:
    root = Path(root)
    items = []
    try:
        validation = validate_dataset(root)
        data_error = None
    except DataValidationError as exc:
        validation, data_error = None, str(exc)

    def add(number, stage, status, detail):
        items.append({"id": f"QA-{number:02}", "stage": stage, "status": status, "detail": detail})

    data_checks = {
        1: "30 documents, explicit responsibilities, two headings/anchors; no meaningless >=250-character padding gate",
        2: "30 valid effective_date values and query_date policy basis",
        3: "5 semantic rule differences with overlapping scope and explicit supersedes; no arbitrary 90-day gap gate",
        4: "isolated SEC-DOC injection fixture and GOLD-09 untrusted context; never loaded into default business policies",
        5: "exactly orders/logistics/products; status dictionary is external JSON, not a fourth table",
        6: "nullable integer statuses; order 6 means refund failed, not detailed refund progress",
        8: "200 valid orders, optional missing fields, separate 3 rejected Raw rows; no arbitrary 15..30 missing-field quota",
        9: "at least 45 orders for a user and at least 6 owners; offline pagination is not authenticated API acceptance",
        10: "six calendar months and fixed prior-month boundaries; chart generation remains deferred",
        11: "exact normalized key/field equality; Raw lineage and declared Snapshot changes instead of unconditional raw equality",
        12: "50 synthetic conversations, split 20/10/20, categories and controlled messages; not real customer logs",
    }
    for number, detail in data_checks.items():
        add(number, "Data Ready", "FAIL" if data_error else "PASS", data_error or detail)
    add(7, "Data Ready", "DEFER", "P2 order_items extension; original three-table core does not require product-order FK")
    if not data_error:
        orders = jsonl_rows(root / "orders.jsonl")
        owners = Counter(r["user_id"] for r in orders)
        months = {r["create_time"][:7] for r in orders if r.get("create_time")}
        for number, valid in ((9, max(owners.values()) >= 45 and len(owners) >= 6), (10, len(months) >= 6)):
            if not valid:
                next(i for i in items if i["id"] == f"QA-{number:02}").update(status="FAIL", detail="required data coverage absent")
    try:
        # Deliberately delayed until Evaluation Ready, never a seed generation prerequisite.
        from eval.golden import build_golden
        from eval.oracle import ReferenceOracle
        cases = jsonl_rows(root / "eval/golden_dataset.jsonl")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if len(cases) != 36 or cases != build_golden(root, manifest["variant"]):
            raise ValueError("Golden mapping/provenance mismatch")
        oracle = ReferenceOracle(root)
        for case in cases:
            oracle.expected(case)
        add(13, "Evaluation Ready", "PASS", "36 public holdout cases, independent Oracle selectors resolve; not Agent execution")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        add(13, "Evaluation Ready", "FAIL", str(exc))
    scan = scan_coupling(root, source) if source and not data_error else {"status": "NOT_RUN", "reason": "no source supplied or invalid data"}
    add(14, "Integration Ready", scan["status"], scan)
    gate, detail = evaluation_gate(root, evaluation, comparison) if not data_error else ("NOT_RUN", "invalid data")
    add(15, "Evaluation Ready", gate, detail)
    return {"data_ready": "FAIL" if any(i["stage"] == "Data Ready" and i["status"] == "FAIL" for i in items) else "PASS",
            "integration_ready": "FAIL" if scan["status"] == "FAIL" else "NOT_RUN",
            "evaluation_ready": "FAIL" if any(i["stage"] == "Evaluation Ready" and i["status"] == "FAIL" for i in items) else gate,
            "items": sorted(items, key=lambda i: i["id"]), "data_validation": validation,
            "missing_integration_evidence": ["frozen formal Mock version/hash and actual HTTP acceptance", "Agent graph/auth/confirmation/recovery integration"],
            "formal_mock_integration": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--evaluation", type=Path)
    parser.add_argument("--comparison", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    evaluation = json.loads(args.evaluation.read_text(encoding="utf-8")) if args.evaluation else None
    comparison = json.loads(args.comparison.read_text(encoding="utf-8")) if args.comparison else None
    result = qa_report(args.root, args.source, evaluation, comparison)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(stable_json(result), encoding="utf-8", newline="\n")
    print(stable_json(result))
    if result["data_ready"] == "FAIL" or result["integration_ready"] == "FAIL" or result["evaluation_ready"] == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
