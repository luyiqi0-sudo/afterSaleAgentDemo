import json

import pytest
import yaml

from database.contracts import jsonl_rows
from database.seed import generate_dataset
from database.validate import DataValidationError, validate_dataset


def test_policy_coverage_has_30_resolvable_roles_and_five_replacements(tmp_path):
    root = tmp_path / "data"
    generate_dataset(root)
    coverage = json.loads((root / "knowledge_manifest.json").read_text(encoding="utf-8"))
    assert len(coverage["documents"]) == 30
    assert {r["catalog_id"] for r in coverage["documents"]} == {f"DOC-{i:02}" for i in range(1, 31)}
    doc12 = next(r for r in coverage["documents"] if r["catalog_id"] == "DOC-12")
    assert doc12["policy_key"] == "missing_item"
    assert "缺件" in doc12["responsibility"]
    for entry in coverage["documents"]:
        text = (root / "knowledge" / entry["file"]).read_text(encoding="utf-8")
        meta = yaml.safe_load(text.split("---")[1])
        assert meta["effective_date"] and meta["time_basis"] == "query_date"
        assert meta["anchors"] == {"1": "policy-rule", "2": "simulation-scope"}
        assert 'id="policy-rule"' in text and 'id="simulation-scope"' in text
        assert "不代表真实法律或真实企业政策" in text
    assert len(coverage["conflict_pairs"]) == 5
    for pair in coverage["conflict_pairs"]:
        assert pair["old_rules"] != pair["new_rules"]
        assert pair["conditions"] == {"channel": "online", "region": "CN"}
        assert pair["resolution"] == "explicit_supersedes_on_overlap"


def test_policy_replacement_tampering_is_rejected_without_hashes(tmp_path):
    root = tmp_path / "data"
    generate_dataset(root)
    file = root / "knowledge/returns-v2.md"
    file.write_text(file.read_text(encoding="utf-8").replace("- returns-v1", "- invoice-v1"), encoding="utf-8")
    with pytest.raises(DataValidationError):
        validate_dataset(root, verify_hashes=False)


def test_synthetic_multiturn_and_identity_are_not_execution_or_authentication(tmp_path):
    root = tmp_path / "data"
    generate_dataset(root)
    logs = jsonl_rows(root / "logs.jsonl")
    assert len(logs) == 50 and any(len(log["messages"]) == 4 for log in logs)
    assert all(log["execution_verified"] is False for log in logs)
    dictionary = json.loads((root / "database/dictionary.json").read_text(encoding="utf-8"))
    assert dictionary["identity"]["mode"] == "demo_registry_only"
    assert dictionary["identity"]["trusted_membership_source"] is None
